/* -*- P4_16 -*- */
#include <core.p4>
#include <v1model.p4>

#define WRITE_REG(r, v) r.write((bit<32>)standard_metadata.egress_port, v);
#define READ_REG(r, v)  r.read(v, (bit<32>)standard_metadata.egress_port);
#define CAP(c, v, a, t) { if (v > c) a = c; else a = (t)v; }

const bit<16> TYPE_IPV4 = 0x800;
const bit<32> MAX_PROB  = 0xFFFFFFFF;

// crude constants for test purposes
const bit<32> MTU_BYTES = 1500;

typedef bit<9>  egressSpec_t;
typedef bit<48> macAddr_t;
typedef bit<32> ip4Addr_t;

// PI2 parameters
typedef int<32> alpha_t;
typedef int<32> beta_t;
typedef int<32> delay_t;     // same unit as your PI2 code expects
typedef bit<5>  interval_t;  // update interval exponent (2^interval time units)

// DualPI2 parameters (test adaptation)
typedef bit<8>  coupling_t;  // k
typedef bit<2>  ecnmask_t;   // e.g. 2w1 for ECT(1)
typedef bit<1>  flag_t;

register<bit<48>>(256) r_update_time;
register<int<32>>(256) r_last_qdelay;
register<bit<32>>(256) r_probability;

// per-queue last-seen (per egress port)
register<int<32>>(256)  r_qdelay_c;
register<int<32>>(256)  r_qdelay_l;
register<bit<32>>(256)  r_qdepth_c;
register<bit<32>>(256)  r_qdepth_l;

// drop-reporting (your original style)
register<bit<32>>(256) r_dropped;

header ethernet_t {
    macAddr_t dstAddr;
    macAddr_t srcAddr;
    bit<16>   etherType;
}

header ipv4_t {
    bit<4>    version;
    bit<4>    ihl;
    bit<6>    diffserv;
    bit<2>    ecn;
    bit<16>   totalLen;

    bit<5>    drops;
    bit<11>   qdelay_ms;

    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr;
    ip4Addr_t dstAddr;
}

struct metadata {
    bit<1> mark_drop;
}

struct headers {
    ethernet_t ethernet;
    ipv4_t     ipv4;
}

parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t standard_metadata) {
    state start { transition parse_ethernet; }

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            default: accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply { }
}

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t standard_metadata) {

    action drop() { mark_to_drop(standard_metadata); }

    action ipv4_forward(macAddr_t dstAddr, egressSpec_t port) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = hdr.ethernet.dstAddr;
        hdr.ethernet.dstAddr = dstAddr;
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }

    table ipv4_lpm {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { ipv4_forward; drop; }
        size = 1024;
        default_action = drop;
    }

    apply {
        if (hdr.ipv4.isValid()) {
            ipv4_lpm.apply();

            // DualQ split using L4S identifier:
            // ECT(1) or CE => LSB mask 2w1 matches
            // priority: 0 is highest in simple_switch
            if ((hdr.ipv4.ecn & 2w1) == 2w1) {
                standard_metadata.priority = 3w0; // L queue = 0
            } else {
                standard_metadata.priority = 3w1; // C queue = 1
            }
        }
    }
}

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t standard_metadata) {

    action drop_and_count() {
        bit<32> dropped_pks = 0;
        mark_to_drop(standard_metadata);
        READ_REG(r_dropped, dropped_pks);
        dropped_pks = dropped_pks + 1;
        WRITE_REG(r_dropped, dropped_pks);
    }

    // IMPORTANT: cap_prob MUST be provided by control plane as:
    //    cap_prob = floor(MAX_PROB / coupling_factor)
    // This avoids runtime division (unsupported in BMv2/p4c).
    action dualpi2(alpha_t alpha,
                  beta_t beta,
                  delay_t target,
                  interval_t interval,
                  coupling_t coupling_factor,
                  bit<32> cap_prob,
                  ecnmask_t ecn_mask,
                  flag_t drop_overload,
                  delay_t step_thresh,
                  flag_t step_in_packets) {

        /***************
         * 0) Init locals (fix uninitialized warnings)
         ***************/
        meta.mark_drop = 1w0;

        bit<48> now = standard_metadata.egress_global_timestamp;

        bit<48> last_update_time = 0;
        int<32> last_qdelay_max  = 0;
        bit<32> prob             = 0;

        int<32> qdelay_c = 0;
        int<32> qdelay_l = 0;
        bit<32> qdepth_c = 0;
        bit<32> qdepth_l = 0;

        int<32> qdelay_now = 0;
        bit<32> qdepth_now = 0;

        bit<32> r1 = 0;
        bit<32> r2 = 0;
        bit<32> rL = 0;

        /***************
         * 1) EXTERN CALLS FIRST (must be unconditional in actions)
         ***************/
        READ_REG(r_update_time, last_update_time);
        READ_REG(r_last_qdelay, last_qdelay_max);
        READ_REG(r_probability, prob);

        READ_REG(r_qdelay_c, qdelay_c);
        READ_REG(r_qdelay_l, qdelay_l);
        READ_REG(r_qdepth_c, qdepth_c);
        READ_REG(r_qdepth_l, qdepth_l);

        CAP(1000000, standard_metadata.deq_timedelta, qdelay_now, int<32>);
        qdepth_now = (bit<32>) standard_metadata.deq_qdepth;

        random(r1, 0, MAX_PROB);
        random(r2, 0, MAX_PROB);
        random(rL, 0, MAX_PROB);

        /***************
         * 2) Pure logic (no externs)
         ***************/
        bit<1> is_l4s = (((hdr.ipv4.ecn & ecn_mask) != 2w0) ? 1w1 : 1w0);

        // update last-seen per-queue delay/depth
        if (is_l4s == 1w1) {
            qdelay_l = qdelay_now;
            qdepth_l = qdepth_now;
        } else {
            qdelay_c = qdelay_now;
            qdepth_c = qdepth_now;
        }

        bit<32> backlog_bytes = qdepth_c + qdepth_l;
        bit<1> allow_aqm = (backlog_bytes >= (2 * MTU_BYTES)) ? 1w1 : 1w0;

        int<32> qdelay_max = (qdelay_l > qdelay_c) ? qdelay_l : qdelay_c;

        if (last_update_time == 0) {
            last_update_time = now;
        }

        bit<32> update_laps =
            (bit<32>) ((now - last_update_time) >> interval);

        if (update_laps >= 1) {
            if (update_laps >= 2000) update_laps = 2000;

            int<32> prev_qdelay_max = last_qdelay_max;
            int<32> laps_target = (int<32>)update_laps * target;

            int<32> delta = (qdelay_max - laps_target) * alpha
                          + (qdelay_max - prev_qdelay_max) * beta;

            bit<33> new_prob = (bit<33>) prob;
            new_prob = (bit<33>) ((int<33>)new_prob + (int<33>)delta);

            if (new_prob > (bit<33>)MAX_PROB) {
                if (delta > 0) prob = MAX_PROB;
                else           prob = 0;
            } else {
                prob = (bit<32>) new_prob;
            }

            last_update_time = now;
            last_qdelay_max  = qdelay_max;

            // kernel behavior when !drop_overload:
            // cap so that k * p' does not exceed 100%.
            // (division done in control plane -> cap_prob)
            if (drop_overload == 1w0 && coupling_factor != 0) {
                if (prob > cap_prob) prob = cap_prob;
            }
        }

        // overload check using 64b multiply (no division)
        bit<64> local_l_prob64 = (bit<64>)prob * (bit<64>)coupling_factor;
        bit<1> overload = (local_l_prob64 > (bit<64>)MAX_PROB) ? 1w1 : 1w0;
        bit<32> local_l_prob = (overload == 1w1) ? MAX_PROB : (bit<32>)local_l_prob64;

        // ECT?
        bit<2> ect = hdr.ipv4.ecn;
        bit<1> not_ect = (ect == 2w0) ? 1w1 : 1w0;
        bit<1> ecn_capable = (ect != 2w0) ? 1w1 : 1w0;

        bit<1> classic_trigger = ((r1 <= prob) && (r2 <= prob)) ? 1w1 : 1w0;

        /***************
         * 3) Mark/drop logic (no externs)
         ***************/
        if (allow_aqm == 1w1) {
            if (is_l4s == 1w0) {
                // Classic queue: pC ~= (p')^2
                if (classic_trigger == 1w1) {
                    if (overload == 1w1 || not_ect == 1w1) {
                        meta.mark_drop = 1w1;
                    } else {
                        hdr.ipv4.ecn = 2w3; // CE
                    }
                }
            } else {
                // L4S queue: pL = k*p'
                if (overload == 1w1) {
                    // trade losses to preserve latency when configured
                    if (drop_overload == 1w1 && classic_trigger == 1w1) {
                        meta.mark_drop = 1w1;
                    } else {
                        if (not_ect == 1w1) meta.mark_drop = 1w1;
                        else hdr.ipv4.ecn = 2w3;
                    }
                } else {
                    if (rL <= local_l_prob) {
                        if (not_ect == 1w1) meta.mark_drop = 1w1;
                        else hdr.ipv4.ecn = 2w3;
                    }
                }

                // Step AQM (approx) for L queue only
                if (meta.mark_drop == 1w0 && step_thresh > 0) {
                    bit<1> step_hit = 1w0;

                    if (step_in_packets == 1w1) {
                        // treat step_thresh as packets (convert to bytes roughly)
                        bit<32> thresh_bytes = (bit<32>)step_thresh * MTU_BYTES;
                        if ((bit<32>)standard_metadata.enq_qdepth > thresh_bytes)
                            step_hit = 1w1;
                    } else {
                        // treat step_thresh in same time unit as qdelay_now
                        if (qdelay_now > step_thresh) step_hit = 1w1;
                    }

                    if (step_hit == 1w1) {
                        if (ecn_capable == 1w0) meta.mark_drop = 1w1;
                        else hdr.ipv4.ecn = 2w3;
                    }
                }
            }
        }

        /***************
         * 4) EXTERN WRITES LAST (unconditional)
         ***************/
        WRITE_REG(r_qdelay_c, qdelay_c);
        WRITE_REG(r_qdelay_l, qdelay_l);
        WRITE_REG(r_qdepth_c, qdepth_c);
        WRITE_REG(r_qdepth_l, qdepth_l);

        WRITE_REG(r_probability, prob);
        WRITE_REG(r_last_qdelay, last_qdelay_max);
        WRITE_REG(r_update_time, last_update_time);
    }

    table aqm {
        key = { standard_metadata.egress_port: exact; }
        actions = { dualpi2(); NoAction; }
        default_action = NoAction;
    }

    apply {
        if (hdr.ipv4.isValid()) {
            hdr.ipv4.qdelay_ms = (bit<11>)(standard_metadata.deq_timedelta >> 10);

            aqm.apply();

            if (meta.mark_drop == 1w1) {
                drop_and_count();
            } else {
                bit<32> dropped_pks = 0;
                bit<5> drops = 0;
                READ_REG(r_dropped, dropped_pks);
                CAP(31, dropped_pks, drops, bit<5>);
                dropped_pks = dropped_pks - (bit<32>)drops;
                WRITE_REG(r_dropped, dropped_pks);
                hdr.ipv4.drops = drops;
            }
        }
    }
}

control MyComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version,
              hdr.ipv4.ihl,
              hdr.ipv4.diffserv,
              hdr.ipv4.ecn,
              hdr.ipv4.totalLen,
              hdr.ipv4.drops,
              hdr.ipv4.qdelay_ms,
              hdr.ipv4.flags,
              hdr.ipv4.fragOffset,
              hdr.ipv4.ttl,
              hdr.ipv4.protocol,
              hdr.ipv4.srcAddr,
              hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

control MyDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
    }
}

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
