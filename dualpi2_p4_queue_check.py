#!/usr/bin/env python3
from scapy.all import PcapReader, IP, TCP, UDP  # type: ignore
from dataclasses import dataclass
from collections import defaultdict
import argparse
from typing import Dict, List, Tuple, Optional

# ---------- Data structures ----------

@dataclass
class PktSummary:
    key: Tuple
    ts: float
    ecn: int

@dataclass
class MatchedPkt:
    idx: int
    key: Tuple
    ingress_ts: float
    egress_ts: float
    ecn_in: int
    ecn_out: int
    priority: int  # 0 = high (L4S), 1 = low (classic)
    src: str
    dst: str
    sport: int
    dport: int
    proto: str

# ---------- Helpers ----------

def ecn_to_str(ecn: int) -> str:
    if ecn == 0: return "Not-ECT"
    if ecn == 1: return "ECT(1)/L4S"
    if ecn == 2: return "ECT(0)/Classic"
    if ecn == 3: return "CE"
    return f"{ecn}"

def summarize_pcap(path: str) -> Dict[Tuple, List[PktSummary]]:
    """
    Build a dict: fingerprint -> list of packets (ordered by time).
    Fingerprint is 5-tuple + TCP seq or UDP payload sig so we can match packets.
    """
    flows: Dict[Tuple, List[PktSummary]] = defaultdict(list)

    with PcapReader(path) as pcap:
        for pkt in pcap:
            try:
                if IP not in pkt:
                    continue
                ip = pkt[IP]
                ecn = ip.tos & 0x3  # ECN bits

                if TCP in pkt:
                    l4 = pkt[TCP]
                    proto = "TCP"
                    sport = int(l4.sport)
                    dport = int(l4.dport)
                    seq = int(l4.seq)
                    payload = bytes(l4.payload)
                    payload_len = len(payload)
                    payload_sig = payload[:16]
                    key = (
                        ip.src, ip.dst,
                        sport, dport,
                        proto,
                        seq,
                        payload_len,
                        payload_sig,
                    )
                elif UDP in pkt:
                    l4 = pkt[UDP]
                    proto = "UDP"
                    sport = int(l4.sport)
                    dport = int(l4.dport)
                    payload = bytes(l4.payload)
                    payload_len = len(payload)
                    payload_sig = payload[:16]
                    key = (
                        ip.src, ip.dst,
                        sport, dport,
                        proto,
                        payload_len,
                        payload_sig,
                    )
                else:
                    continue

                ts = float(pkt.time)
                flows[key].append(PktSummary(key=key, ts=ts, ecn=ecn))
            except Exception:
                # ignore malformed packets
                continue

    # ensure per-flow lists are time-ordered
    for k in flows:
        flows[k].sort(key=lambda p: p.ts)
    return flows

def match_ingress_egress(
    ingress: Dict[Tuple, List[PktSummary]],
    egress: Dict[Tuple, List[PktSummary]],
    ignore_negative: bool = True,
) -> List[MatchedPkt]:
    """
    Greedy matching per fingerprint, preserving order.
    For each ingress packet we find the first egress packet
    with >= timestamp.
    """
    matches: List[MatchedPkt] = []
    idx_counter = 0

    for key, in_list in ingress.items():
        out_list = egress.get(key)
        if not out_list:
            continue

        i = 0
        o = 0
        n_in = len(in_list)
        n_out = len(out_list)

        # extract static fields from the key for reporting
        if len(key) == 8:  # TCP
            src, dst, sport, dport, proto, *_ = key
        else:              # UDP
            src, dst, sport, dport, proto, *_ = key

        while i < n_in and o < n_out:
            p_in = in_list[i]
            p_out = out_list[o]

            if p_out.ts < p_in.ts:
                # this egress packet seems to correspond to an earlier ingress
                # that we already matched, skip it
                o += 1
                continue

            delay = p_out.ts - p_in.ts
            if ignore_negative and delay < 0:
                i += 1
                o += 1
                continue

            ecn_in = p_in.ecn
            ecn_out = p_out.ecn

            # priority uses INGRESS ECN (before DualPI2 marks)
            priority = 0 if (ecn_in & 0x1) == 0x1 else 1  # 0 = high (L4S), 1 = low

            matches.append(
                MatchedPkt(
                    idx=idx_counter,
                    key=key,
                    ingress_ts=p_in.ts,
                    egress_ts=p_out.ts,
                    ecn_in=ecn_in,
                    ecn_out=ecn_out,
                    priority=priority,
                    src=src,
                    dst=dst,
                    sport=sport,
                    dport=dport,
                    proto=proto,
                )
            )
            idx_counter += 1
            i += 1
            o += 1

    return matches

def analyze_priority_order(matches: List[MatchedPkt], max_examples: int = 20):
    """
    Egress is treated as the queue head.
    At each departure, if we send a low-prio packet while there exists any
    high-prio packet that has already arrived (ingress_ts <= now) and not yet
    departed, that's a priority violation.
    """
    if not matches:
        print("No matched packets; nothing to analyze.")
        return

    eps = 1e-9

    # Sort by ingress and egress time
    by_arrival = sorted(matches, key=lambda m: m.ingress_ts)
    by_departure = sorted(matches, key=lambda m: m.egress_ts)

    # For fast lookup
    id_to_pkt = {m.idx: m for m in matches}

    pending_high = set()  # idx of packets
    pending_low = set()
    arrival_idx = 0
    n = len(matches)

    violations = []
    total_departures = 0
    total_high = sum(1 for m in matches if m.priority == 0)
    total_low = n - total_high

    for m_dep in by_departure:
        t_dep = m_dep.egress_ts
        total_departures += 1

        # enqueue arrivals up to this time
        while arrival_idx < n and by_arrival[arrival_idx].ingress_ts <= t_dep + eps:
            m_arr = by_arrival[arrival_idx]
            if m_arr.priority == 0:
                pending_high.add(m_arr.idx)
            else:
                pending_low.add(m_arr.idx)
            arrival_idx += 1

        # sanity: departure must be in one of the pending sets
        if m_dep.idx not in pending_high and m_dep.idx not in pending_low:
            # can happen with slight clock skew; ignore
            pass

        # priority rule: if any high is pending, we must not serve low
        if m_dep.priority == 1 and len(pending_high) > 0:
            # record one example high packet that was pending
            sample_high_idx = next(iter(pending_high))
            high_pkt = id_to_pkt[sample_high_idx]
            violations.append((m_dep, high_pkt))

        # now this packet leaves the queue
        if m_dep.idx in pending_high:
            pending_high.remove(m_dep.idx)
        elif m_dep.idx in pending_low:
            pending_low.remove(m_dep.idx)

    print(f"\nTotal matched packets: {n}")
    print(f"  High-priority (L4S, ECN&1==1): {total_high}")
    print(f"  Low-priority  (classic):       {total_low}")
    print(f"Priority violations (low sent while high pending): {len(violations)}")
    if total_departures > 0:
        ratio = 100.0 * len(violations) / total_departures
        print(f"  => {ratio:.3f}% of departures violate strict priority")

    if not violations:
        print("\nNo priority ordering mismatches detected (within this sample).")
        return

    print(f"\nFirst {min(max_examples, len(violations))} violations:")
    for i, (low_pkt, high_pkt) in enumerate(violations[:max_examples], 1):
        print(f"\nViolation #{i}:")
        print(f"  LOW packet: {low_pkt.proto} {low_pkt.src}:{low_pkt.sport} -> {low_pkt.dst}:{low_pkt.dport}")
        print(f"    ingress_ts = {low_pkt.ingress_ts:.9f}")
        print(f"    egress_ts  = {low_pkt.egress_ts:.9f}")
        print(f"    ECN in/out = {ecn_to_str(low_pkt.ecn_in)} -> {ecn_to_str(low_pkt.ecn_out)}")
        print(f"  HIGH pending at that time: {high_pkt.proto} {high_pkt.src}:{high_pkt.sport} -> {high_pkt.dst}:{high_pkt.dport}")
        print(f"    ingress_ts = {high_pkt.ingress_ts:.9f}")
        print(f"    egress_ts  = {high_pkt.egress_ts:.9f}")
        print(f"    ECN in/out = {ecn_to_str(high_pkt.ecn_in)} -> {ecn_to_str(high_pkt.ecn_out)}")

# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(
        description="Check if high-priority (L4S) packets are always served before low-priority ones."
    )
    parser.add_argument("--ingress", required=True, help="pcap before switch (e.g. s1-eth1_in.pcap)")
    parser.add_argument("--egress", required=True, help="pcap after switch (e.g. s1-eth2_out.pcap)")
    parser.add_argument("--max-examples", type=int, default=10,
                        help="max number of violations to print")
    args = parser.parse_args()

    print(f"Reading ingress pcap: {args.ingress}")
    ingress = summarize_pcap(args.ingress)
    print(f"  Unique fingerprints (ingress): {len(ingress)}")

    print(f"Reading egress pcap:  {args.egress}")
    egress = summarize_pcap(args.egress)
    print(f"  Unique fingerprints (egress):  {len(egress)}")

    print("Matching ingress/egress packets...")
    matches = match_ingress_egress(ingress, egress)
    print(f"  Matched packets: {len(matches)}")

    analyze_priority_order(matches, max_examples=args.max_examples)

if __name__ == "__main__":
    main()
