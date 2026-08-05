from scapy.all import rdpcap, IP, ICMP
from collections import defaultdict

def is_ping(pkt):
    return IP in pkt and ICMP in pkt and pkt[ICMP].type in (8, 0)  # echo req/rep

def load_packets(path):
    return rdpcap(path)

def build_ingress_index(pkts):
    """
    Index ingress packets by 5-tuple + ICMP id/seq so we can match them at egress.
    """
    index = {}
    for p in pkts:
        if IP not in p:
            continue
        ip = p[IP]
        key = (ip.src, ip.dst, ip.proto)
        if ICMP in p:
            ic = p[ICMP]
            key = key + (ic.id, ic.seq)
        else:
            key = key + (None, None)
        # First occurrence is enough for matching
        index.setdefault(key, []).append(p)
    return index

def analyze_direction(ingress_pcap, egress_pcap, label):
    ing_pkts = load_packets(ingress_pcap)
    eg_pkts  = load_packets(egress_pcap)

    ing_index = build_ingress_index(ing_pkts)

    # Build a time-ordered list of egress packets, labelling ping/non-ping
    egress_order = []
    for idx, p in enumerate(eg_pkts):
        if IP not in p:
            continue
        ip = p[IP]
        if ICMP in p:
            ic = p[ICMP]
            key = (ip.src, ip.dst, ip.proto, ic.id, ic.seq)
        else:
            key = (ip.src, ip.dst, ip.proto, None, None)
        egress_order.append({
            "idx": idx,
            "time": float(p.time),
            "is_ping": is_ping(p),
            "key": key
        })

    # Map key -> first egress index
    first_egress_idx = {}
    for entry in egress_order:
        key = entry["key"]
        first_egress_idx.setdefault(key, entry["idx"])

    ping_delays = []
    ping_overtakes = []

    for p in ing_pkts:
        if not is_ping(p):
            continue
        ip = p[IP]
        ic = p[ICMP]
        key = (ip.src, ip.dst, ip.proto, ic.id, ic.seq)

        ing_t = float(p.time)
        # Find matching egress packet
        if key not in first_egress_idx:
            continue
        eg_idx = first_egress_idx[key]
        eg_p = eg_pkts[eg_idx]
        eg_t = float(eg_p.time)

        delay = eg_t - ing_t
        ping_delays.append(delay)

        # Count non-ping packets that left *after* this ping arrived but *before* it left
        overtakes = 0
        for entry in egress_order:
            if entry["idx"] >= eg_idx:
                break
            if entry["time"] < ing_t:
                continue
            if not entry["is_ping"]:
                overtakes += 1
        ping_overtakes.append(overtakes)

    if not ping_delays:
        print(f"[{label}] No ping packets found.")
        return

    avg_delay = sum(ping_delays) / len(ping_delays)
    max_delay = max(ping_delays)
    avg_overtakes = sum(ping_overtakes) / len(ping_overtakes)
    max_overtakes = max(ping_overtakes)

    print(f"[{label}] pings through switch:")
    print(f"  count        = {len(ping_delays)}")
    print(f"  avg delay    = {avg_delay*1000:.3f} ms")
    print(f"  max delay    = {max_delay*1000:.3f} ms")
    print(f"  avg overtakes= {avg_overtakes:.2f}")
    print(f"  max overtakes= {max_overtakes}")

# Example usage:
analyze_direction("pcap/s1-eth1_in.pcap", "pcap/s1-eth2_out.pcap", "h1->h2")
# analyze_direction("pcap/s1-eth2_in.pcap", "pcap/s1-eth1_out.pcap", "h2->h1")
