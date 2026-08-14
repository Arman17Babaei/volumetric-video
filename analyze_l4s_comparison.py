#!/usr/bin/env python3
"""Analyze Linux DualPI2 vs P4/BMv2 phased Prague experiments.

The analyzer deliberately consumes a backend-neutral raw schema produced by
run_l4s_comparison.py. Packet parsing is done with tshark so the analysis does
not depend on Scapy being installed on the experiment host.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, MutableMapping, Sequence, Tuple


@dataclass(frozen=True)
class Packet:
    ts: float
    src: str
    dst: str
    sport: int
    dport: int
    seq: int
    ack: int
    flags: str
    ip_len: int
    ecn: int

    @property
    def flow(self) -> str:
        if self.sport == 5201 or self.dport == 5201:
            return "A"
        if self.sport == 5202 or self.dport == 5202:
            return "B"
        return "unknown"

    @property
    def fingerprint(self) -> Tuple[object, ...]:
        return (self.src, self.dst, self.sport, self.dport,
                self.seq, self.ack, self.flags, self.ip_len)


def planned_events(phase_seconds: float = 10.0) -> List[Tuple[float, str, str]]:
    if phase_seconds <= 0:
        raise ValueError("phase_seconds must be positive")
    p = float(phase_seconds)
    return [
        (0.0, "start", "A"),
        (p, "start", "B"),
        (2*p, "stop", "A"),
        (3*p, "start", "A"),
        (4*p, "stop", "B"),
        (5*p, "stop", "A"),
    ]


def parse_ss_line(text: str) -> Dict[str, float]:
    """Parse the most useful fields from one ``ss -tin`` detail line."""
    result: Dict[str, float] = {}
    tokens = text.replace("\n", " ").split()
    for token in tokens:
        if token.startswith("cwnd:"):
            result["cwnd_packets"] = float(token.split(":", 1)[1])
        elif token.startswith("rtt:"):
            value = token.split(":", 1)[1].split("/", 1)
            result["rtt_ms"] = float(value[0])
            if len(value) > 1:
                result["rttvar_ms"] = float(value[1])
        elif token.startswith("bytes_acked:"):
            result["bytes_acked"] = float(token.split(":", 1)[1])
        elif token.startswith("bytes_sent:"):
            result["bytes_sent"] = float(token.split(":", 1)[1])
        elif token.startswith("pacing_rate"):
            # ss may print "pacing_rate 123bps" instead of key:value.
            continue
        elif token.startswith("retrans:"):
            value = token.split(":", 1)[1].split("/", 1)[-1]
            try:
                result["retrans"] = float(value)
            except ValueError:
                pass
    return result


def _tshark_rows(path: Path) -> Iterator[Packet]:
    fields = [
        "frame.time_epoch", "ip.src", "ip.dst", "tcp.srcport", "tcp.dstport",
        "tcp.seq_raw", "tcp.ack_raw", "tcp.flags.str", "ip.len", "ip.dsfield.ecn",
    ]

    cmd = [
        "tshark", "-r", str(path),
        "-Y", "tcp.port == 5201 || tcp.port == 5202",
        "-T", "fields",
        "-E", "separator=/t",
        "-E", "occurrence=f",
    ]

    for field in fields:
        cmd += ["-e", field]

    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"tshark failed for {path} with exit code {proc.returncode}:\n"
            f"{proc.stderr.strip()}"
        )

    for line in proc.stdout.splitlines():
        parts = line.split("\t")

        if len(parts) != len(fields) or not all(parts[:7]):
            continue

        try:
            yield Packet(
                ts=float(parts[0]),
                src=parts[1],
                dst=parts[2],
                sport=int(parts[3]),
                dport=int(parts[4]),
                seq=int(parts[5]),
                ack=int(parts[6]),
                flags=parts[7],
                ip_len=int(parts[8]),
                ecn=int(parts[9], 0) if parts[9] else 0,
            )
        except ValueError:
            continue

def match_packets(ingress: Sequence[Packet], egress: Sequence[Packet]) -> List[Tuple[Packet, Packet]]:
    """Order-preserving exact-fingerprint matcher, robust to retransmissions."""
    out: MutableMapping[Tuple[object, ...], deque[Packet]] = defaultdict(deque)
    for packet in sorted(egress, key=lambda p: p.ts):
        out[packet.fingerprint].append(packet)
    matches: List[Tuple[Packet, Packet]] = []
    for packet in sorted(ingress, key=lambda p: p.ts):
        queue = out.get(packet.fingerprint)
        if not queue:
            continue
        while queue and queue[0].ts < packet.ts:
            queue.popleft()
        if queue:
            matches.append((packet, queue.popleft()))
    return matches


def estimate_baseline_us(residences_us: Sequence[float], quantile: float = 0.05) -> float:
    if not residences_us:
        raise ValueError("no residence samples")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile outside [0,1]")
    values = sorted(residences_us)
    index = min(len(values)-1, max(0, int((len(values)-1) * quantile)))
    return values[index]


def corrected_queue_delay_us(residence_us: float, baseline_us: float) -> float:
    return max(0.0, residence_us - baseline_us)


def time_bin(values: Iterable[Tuple[float, float]], width_s: float, origin: float = 0.0) -> Dict[int, List[float]]:
    if width_s <= 0:
        raise ValueError("width_s must be positive")
    bins: Dict[int, List[float]] = defaultdict(list)
    for ts, value in values:
        index = int(math.floor((ts - origin) / width_s))
        if index >= 0:
            bins[index].append(value)
    return bins


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = q * (len(values)-1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] * (hi-pos) + values[hi] * (pos-lo)


def marking_curve(rows: Sequence[Mapping[str, float]], bin_ms: float = 0.25,
                  min_samples: int = 100) -> List[Dict[str, float]]:
    if bin_ms <= 0:
        raise ValueError("bin_ms must be positive")
    bins: Dict[int, List[int]] = defaultdict(list)
    for row in rows:
        delay_ms = float(row["queue_delay_us"]) / 1000.0
        index = int(math.floor(delay_ms / bin_ms))
        bins[index].append(int(row["ce_marked"]))
    result = []
    for index in sorted(bins):
        marks = bins[index]
        if len(marks) < min_samples:
            continue
        result.append({
            "queue_delay_ms": (index + 0.5) * bin_ms,
            "ce_probability": sum(marks) / len(marks),
            "samples": len(marks),
        })
    return result


def write_packet_matches(rep_dir: Path, backend: str, rep: int, analysis_dir: Path) -> List[Dict[str, float]]:
    ingress = list(_tshark_rows(rep_dir / "ingress-A.pcap")) + list(_tshark_rows(rep_dir / "ingress-B.pcap"))
    egress = list(_tshark_rows(rep_dir / "egress.pcap"))
    matches = match_packets(ingress, egress)
    residences = [(out.ts - inc.ts) * 1e6 for inc, out in matches if out.ts >= inc.ts]
    baseline = estimate_baseline_us(residences)
    rows: List[Dict[str, float]] = []
    t0 = min((inc.ts for inc, _ in matches), default=0.0)
    for inc, out in matches:
        residence = max(0.0, (out.ts - inc.ts) * 1e6)
        rows.append({
            "backend": backend, "rep": rep, "time_s": inc.ts - t0,
            "flow": inc.flow, "ip_len": inc.ip_len, "residence_us": residence,
            "queue_delay_us": corrected_queue_delay_us(residence, baseline),
            "ecn_in": inc.ecn, "ecn_out": out.ecn,
            "ce_marked": int(inc.ecn == 1 and out.ecn == 3),
        })
    return rows


def _load_transport(path: Path) -> List[Dict[str, float | str]]:
    if not path.exists():
        return []
    with path.open(newline="") as stream:
        rows = []
        for row in csv.DictReader(stream):
            parsed: Dict[str, float | str] = {"flow": row["flow"]}
            for key, value in row.items():
                if key == "flow" or value in (None, ""):
                    continue
                try:
                    parsed[key] = float(value)
                except ValueError:
                    parsed[key] = value
            rows.append(parsed)
        return rows


def analyze_root(root: Path, bin_ms: float = 250.0) -> Path:
    analysis_dir = root / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    packet_rows: List[Dict[str, float]] = []
    transport_rows: List[Dict[str, float | str]] = []
    for backend in ("linux", "p4"):
        parent = root / backend
        if not parent.exists():
            continue
        for rep_dir in sorted(parent.glob("rep_*")):
            rep = int(rep_dir.name.split("_", 1)[1])
            packet_rows.extend(write_packet_matches(rep_dir, backend, rep, analysis_dir))
            for row in _load_transport(rep_dir / "transport.csv"):
                row["backend"] = backend; row["rep"] = float(rep)
                transport_rows.append(row)

    if not packet_rows:
        raise SystemExit("no packet matches found")

    packet_path = analysis_dir / "packet_matches.csv"
    with packet_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(packet_rows[0].keys()))
        writer.writeheader(); writer.writerows(packet_rows)

    curve_rows = []
    for backend in sorted(set(row["backend"] for row in packet_rows)):
        curve = marking_curve([r for r in packet_rows if r["backend"] == backend], min_samples=20)
        for row in curve:
            curve_rows.append({"backend": backend, **row})
    with (analysis_dir / "marking_curve.csv").open("w", newline="") as stream:
        fields = ["backend", "queue_delay_ms", "ce_probability", "samples"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(curve_rows)

    summary = {}
    for backend in sorted(set(row["backend"] for row in packet_rows)):
        subset = [r for r in packet_rows if r["backend"] == backend]
        delays = [r["queue_delay_us"] / 1000.0 for r in subset]
        summary[backend] = {
            "matched_packets": len(subset),
            "queue_delay_median_ms": statistics.median(delays),
            "queue_delay_p99_ms": quantile(delays, 0.99),
            "new_ce_marks": sum(int(r["ce_marked"]) for r in subset),
        }
    (analysis_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    try:
        render_figure(packet_rows, transport_rows, curve_rows, analysis_dir, bin_ms / 1000.0)
    except ImportError:
        pass
    return analysis_dir


def _aggregate_series(series_by_rep: Dict[int, Dict[int, float]], width_s: float):
    indices = sorted({i for bins in series_by_rep.values() for i in bins})
    xs=[]; med=[]; q1=[]; q3=[]
    for i in indices:
        values=[bins[i] for bins in series_by_rep.values() if i in bins]
        if not values: continue
        xs.append((i+.5)*width_s); med.append(statistics.median(values))
        q1.append(quantile(values,.25)); q3.append(quantile(values,.75))
    return xs,med,q1,q3


def render_figure(packet_rows, transport_rows, curve_rows, analysis_dir: Path, width_s: float) -> None:
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(10.5, 7.0))
    phase_lines = [10, 20, 30, 40]

    # (a) IP wire goodput from matched packets, aggregated per repetition.
    ax = axes[0, 0]
    for backend in ("linux", "p4"):
        for flow in ("A", "B"):
            by_rep: Dict[int, Dict[int,float]]={}
            reps=sorted({int(r["rep"]) for r in packet_rows if r["backend"]==backend and r["flow"]==flow})
            for rep in reps:
                bins=time_bin([(float(r["time_s"]), float(r["ip_len"])*8/1e6)
                               for r in packet_rows if r["backend"]==backend and r["flow"]==flow and int(r["rep"])==rep], width_s)
                by_rep[rep]={i:sum(v)/width_s for i,v in bins.items()}
            xs,med,q1,q3=_aggregate_series(by_rep,width_s)
            if xs:
                line="-" if backend=="linux" else "--"
                ax.plot(xs,med,linestyle=line,label=f"{flow} {backend}")
                ax.fill_between(xs,q1,q3,alpha=.10)
    ax.set_title("(a) Per-flow wire goodput"); ax.set_ylabel("Mbit/s"); ax.set_xlabel("Time (s)")

    # (b) packet residence minus backend/repetition low-delay baseline.
    ax = axes[0, 1]
    for backend in ("linux", "p4"):
        by_rep_med: Dict[int,Dict[int,float]]={}; by_rep_p99: Dict[int,Dict[int,float]]={}
        reps=sorted({int(r["rep"]) for r in packet_rows if r["backend"]==backend})
        for rep in reps:
            vals=[(float(r["time_s"]),float(r["queue_delay_us"])/1000.0)
                  for r in packet_rows if r["backend"]==backend and int(r["rep"])==rep]
            bins=time_bin(vals,width_s)
            by_rep_med[rep]={i:statistics.median(v) for i,v in bins.items()}
            by_rep_p99[rep]={i:quantile(v,.99) for i,v in bins.items()}
        xs,med,_,_=_aggregate_series(by_rep_med,width_s)
        xp,p99,_,_=_aggregate_series(by_rep_p99,width_s)
        ax.plot(xs,med,linestyle="-" if backend=="linux" else "--",label=f"{backend} median")
        ax.plot(xp,p99,linestyle=":" if backend=="linux" else "-.",label=f"{backend} p99")
    ax.set_title("(b) Queue delay"); ax.set_ylabel("ms"); ax.set_xlabel("Time (s)")

    # (c) empirical probability that an entering ECT(1) packet exits CE.
    ax = axes[1, 0]
    for backend in ("linux", "p4"):
        rows=[r for r in curve_rows if r["backend"]==backend]
        ax.plot([r["queue_delay_ms"] for r in rows],[r["ce_probability"] for r in rows],
                linestyle="-" if backend=="linux" else "--",label=backend)
    ax.set_title("(c) Empirical CE marking"); ax.set_xlabel("Queue delay (ms)"); ax.set_ylabel("P(new CE)"); ax.set_ylim(-.02,1.02)

    # (d) sender cwnd, median and IQR over repetitions in 250 ms bins.
    ax = axes[1, 1]
    for backend in ("linux","p4"):
        for flow in ("A","B"):
            reps=sorted({int(float(r["rep"])) for r in transport_rows if r.get("backend")==backend and r.get("flow")==flow and "cwnd_bytes" in r})
            by_rep={}
            for rep in reps:
                vals=[(float(r["time_s"]),float(r["cwnd_bytes"])/1024.0)
                      for r in transport_rows if r.get("backend")==backend and r.get("flow")==flow and int(float(r["rep"]))==rep and "cwnd_bytes" in r]
                bins=time_bin(vals,width_s)
                by_rep[rep]={i:statistics.median(v) for i,v in bins.items()}
            xs,med,q1,q3=_aggregate_series(by_rep,width_s)
            if xs:
                ax.plot(xs,med,linestyle="-" if backend=="linux" else "--",label=f"{flow} {backend}")
                ax.fill_between(xs,q1,q3,alpha=.10)
    ax.set_title("(d) Prague congestion window"); ax.set_xlabel("Time (s)"); ax.set_ylabel("KiB")

    for ax in (axes[0,0],axes[0,1],axes[1,1]):
        for x in phase_lines: ax.axvline(x,linewidth=.7,alpha=.35)
    for ax in axes.flat:
        ax.grid(alpha=.2); ax.legend(fontsize=7)
    fig.tight_layout()
    for ext in ("pdf","svg","png"):
        fig.savefig(analysis_dir/f"comparison.{ext}",dpi=300,bbox_inches="tight")
    plt.close(fig)


def self_test() -> None:
    events = planned_events(10)
    assert events[3] == (30.0, "start", "A")
    parsed = parse_ss_line("cwnd:10 rtt:1.2/0.3 bytes_acked:100 bytes_sent:120 retrans:0/2")
    assert parsed["cwnd_packets"] == 10 and parsed["rtt_ms"] == 1.2
    a = Packet(1,"a","b",5201,1,1,2,"S",60,1); b = Packet(2,"a","b",5201,1,1,2,"S",60,3)
    assert match_packets([a],[b]) == [(a,b)]
    assert corrected_queue_delay_us(120, 100) == 20
    curve = marking_curve([{"queue_delay_us":100,"ce_marked":1}]*20, min_samples=10)
    assert curve and curve[0]["ce_probability"] == 1.0
    print("analyze_l4s_comparison self-test: PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, nargs="?")
    parser.add_argument("--bin-ms", type=float, default=250.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test(); return
    if args.root is None:
        parser.error("root is required unless --self-test is used")
    print(analyze_root(args.root, args.bin_ms))


if __name__ == "__main__":
    main()
