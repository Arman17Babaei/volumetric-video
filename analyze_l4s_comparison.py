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
    ip_id: int = 0

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
        "ip.id",
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
                ip_id=int(parts[10], 0) if parts[10] else 0,
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
            "ecn_in": inc.ecn, "ecn_out": out.ecn,
            "ce_marked": int(inc.ecn == 1 and out.ecn == 3),
            "p4_queue_delay_us": out.ip_id if backend == "p4" else "",
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


def _load_numeric_csv(path: Path) -> List[Dict[str, float]]:
    if not path.exists():
        return []
    with path.open(newline="") as stream:
        return [{key: float(value) for key, value in row.items() if value not in (None, "")}
                for row in csv.DictReader(stream)]


def analyze_root(root: Path, bin_ms: float = 250.0) -> Path:
    analysis_dir = root / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    packet_rows: List[Dict[str, float]] = []
    transport_rows: List[Dict[str, float | str]] = []
    queue_rows: List[Dict[str, float | str]] = []
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
            for row in _load_numeric_csv(rep_dir / "queue.csv"):
                row["backend"] = backend; row["rep"] = float(rep)
                queue_rows.append(row)

    if not packet_rows:
        raise SystemExit("no packet matches found")

    packet_path = analysis_dir / "packet_matches.csv"
    with packet_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(packet_rows[0].keys()))
        writer.writeheader(); writer.writerows(packet_rows)

    for row in packet_rows:
        if row["backend"] == "p4" and row.get("p4_queue_delay_us") not in (None, ""):
            queue_rows.append({
                "backend": "p4", "rep": row["rep"], "time_s": row["time_s"],
                "queue_delay_us": row["p4_queue_delay_us"],
            })
    if not queue_rows:
        raise SystemExit("no backend-native queue-delay samples found; old pcap residence times are not queue delay")
    with (analysis_dir / "queue_samples.csv").open("w", newline="") as stream:
        fields = ["backend", "rep", "time_s", "queue_delay_us", "delay_c_us",
                  "delay_l_us", "base_probability"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(queue_rows)

    summary = {}
    for backend in sorted(set(row["backend"] for row in packet_rows)):
        subset = [r for r in packet_rows if r["backend"] == backend]
        delays = [float(r["queue_delay_us"]) / 1000.0 for r in queue_rows
                  if r["backend"] == backend]
        lengths = [float(r["ip_len"]) for r in subset]
        summary[backend] = {
            "matched_packets": len(subset),
            "packet_size_median_bytes": statistics.median(lengths),
            "packet_size_max_bytes": max(lengths),
            "queue_delay_median_ms": statistics.median(delays),
            "queue_delay_p99_ms": quantile(delays, 0.99),
            "new_ce_marks": sum(int(r["ce_marked"]) for r in subset),
        }
    (analysis_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    phase_seconds = 10.0
    target_mbit = 10.0
    manifest = root / "manifest.json"
    if manifest.exists():
        manifest_data = json.loads(manifest.read_text())
        phase_seconds = float(manifest_data.get("phase_seconds", phase_seconds))
        target_mbit = float(manifest_data.get("rate_mbit", target_mbit))
        # Older manifests only record the schedule.
        schedule = manifest_data.get("schedule", [])
        if schedule and len(schedule) > 1:
            phase_seconds = float(schedule[1][0])

    validation = validation_metrics(packet_rows, queue_rows, transport_rows,
                                    phase_seconds, target_mbit)
    (analysis_dir / "validation.json").write_text(
        json.dumps(validation, indent=2) + "\n")

    try:
        render_figure(packet_rows, transport_rows, queue_rows, analysis_dir,
                      bin_ms / 1000.0, phase_seconds, target_mbit)
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


def validation_metrics(packet_rows, queue_rows, transport_rows,
                       phase_seconds: float, target_mbit: float) -> Dict[str, object]:
    """Compute explicit gates before claiming Linux/P4 behavioral parity."""
    margin = min(1.0, phase_seconds * .15)
    phases = [(i*phase_seconds+margin, (i+1)*phase_seconds-margin) for i in range(5)]
    result: Dict[str, object] = {"target_mbit_s": target_mbit, "backends": {}}
    for backend in ("linux", "p4"):
        rows = [r for r in packet_rows if r["backend"] == backend]
        reps = sorted({int(r["rep"]) for r in rows})
        phase_goodput = []
        solo_ce, dual_ce = [], []
        for rep in reps:
            for phase, (lo, hi) in enumerate(phases):
                samples = [r for r in rows if int(r["rep"]) == rep
                           and lo <= float(r["time_s"]) < hi]
                phase_goodput.append(
                    sum(float(r["ip_len"]) * 8 / 1e6 for r in samples) / (hi-lo))
                ect1 = [r for r in samples if int(float(r["ecn_in"])) == 1]
                if ect1:
                    fraction = sum(int(float(r["ce_marked"])) for r in ect1) / len(ect1)
                    (dual_ce if phase in (1, 3) else solo_ce).append(fraction)
        qdelay = [float(r["queue_delay_us"])/1000 for r in queue_rows
                  if r["backend"] == backend]
        cwnd_solo, cwnd_dual = [], []
        for row in transport_rows:
            if row.get("backend") != backend or "cwnd_bytes" not in row:
                continue
            t = float(row["time_s"])
            for phase, (lo, hi) in enumerate(phases):
                if lo <= t < hi:
                    (cwnd_dual if phase in (1, 3) else cwnd_solo).append(
                        float(row["cwnd_bytes"])/1024)
                    break
        result["backends"][backend] = {
            "repetitions": len(reps),
            "max_packet_bytes": max(float(r["ip_len"]) for r in rows),
            "phase_goodput_median_mbit_s": statistics.median(phase_goodput),
            "queue_delay_median_ms": statistics.median(qdelay),
            "queue_delay_p99_ms": quantile(qdelay, .99),
            "solo_ce_fraction": statistics.median(solo_ce),
            "dual_flow_ce_fraction": statistics.median(dual_ce),
            "solo_cwnd_median_kib": statistics.median(cwnd_solo),
            "dual_flow_cwnd_median_kib": statistics.median(cwnd_dual),
        }
    linux = result["backends"]["linux"]
    p4 = result["backends"]["p4"]
    relative_gap = lambda a, b: abs(a-b) / max(abs(a), abs(b), 1e-12)
    gates = {
        "five_repetitions_each": linux["repetitions"] >= 5 and p4["repetitions"] >= 5,
        "mtu_1500_or_less": linux["max_packet_bytes"] <= 1500 and p4["max_packet_bytes"] <= 1500,
        "linux_goodput_within_10pct_of_target": abs(linux["phase_goodput_median_mbit_s"]-target_mbit)/target_mbit <= .10,
        "p4_goodput_within_10pct_of_target": abs(p4["phase_goodput_median_mbit_s"]-target_mbit)/target_mbit <= .10,
        "backend_goodput_gap_at_most_10pct": relative_gap(linux["phase_goodput_median_mbit_s"], p4["phase_goodput_median_mbit_s"]) <= .10,
        "median_queue_delay_gap_at_most_25pct": relative_gap(linux["queue_delay_median_ms"], p4["queue_delay_median_ms"]) <= .25,
        "solo_ce_gap_at_most_0_10": abs(linux["solo_ce_fraction"]-p4["solo_ce_fraction"]) <= .10,
        "dual_ce_gap_at_most_0_10": abs(linux["dual_flow_ce_fraction"]-p4["dual_flow_ce_fraction"]) <= .10,
        "cwnd_falls_with_competition_linux": linux["dual_flow_cwnd_median_kib"] < linux["solo_cwnd_median_kib"],
        "cwnd_falls_with_competition_p4": p4["dual_flow_cwnd_median_kib"] < p4["solo_cwnd_median_kib"],
    }
    result["gates"] = gates
    result["overall_pass"] = all(gates.values())
    return result


def _packet_metric_by_rep(packet_rows, backend: str, width_s: float, metric: str,
                          flow: str | None = None):
    """Return a per-repetition binned behavioral metric."""
    result: Dict[int, Dict[int, float]] = {}
    reps = sorted({int(r["rep"]) for r in packet_rows if r["backend"] == backend})
    for rep in reps:
        rows = [r for r in packet_rows
                if r["backend"] == backend and int(r["rep"]) == rep
                and (flow is None or r["flow"] == flow)]
        bins: Dict[int, List[Mapping[str, float]]] = defaultdict(list)
        for row in rows:
            index = int(math.floor(float(row["time_s"]) / width_s))
            if index >= 0:
                bins[index].append(row)
        values: Dict[int, float] = {}
        for index, samples in bins.items():
            if metric == "goodput_mbit_s":
                values[index] = sum(float(r["ip_len"]) * 8 / 1e6 for r in samples) / width_s
            elif metric == "flow_a_share":
                total = sum(float(r["ip_len"]) for r in samples)
                values[index] = (sum(float(r["ip_len"]) for r in samples if r["flow"] == "A") / total
                                 if total else math.nan)
            elif metric == "ce_fraction":
                ect1 = [r for r in samples if int(float(r["ecn_in"])) == 1]
                if ect1:
                    values[index] = sum(int(float(r["ce_marked"])) for r in ect1) / len(ect1)
            else:
                raise ValueError(f"unknown metric: {metric}")
        result[rep] = values
    return result


def _sample_metric_by_rep(rows, backend: str, width_s: float, key: str,
                          scale: float = 1.0, flow: str | None = None):
    result = {}
    reps = sorted({int(float(r["rep"])) for r in rows if r.get("backend") == backend})
    for rep in reps:
        values = [(float(r["time_s"]), float(r[key]) * scale) for r in rows
                  if r.get("backend") == backend and int(float(r["rep"])) == rep
                  and key in r and (flow is None or r.get("flow") == flow)]
        bins = time_bin(values, width_s)
        result[rep] = {index: statistics.median(samples) for index, samples in bins.items()}
    return result


def render_figure(packet_rows, transport_rows, queue_rows, analysis_dir: Path,
                  width_s: float, phase_seconds: float = 10.0,
                  target_mbit: float = 10.0) -> None:
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(11.0, 7.3), sharex=True)
    backend_colors = {"linux": "#1f77b4", "p4": "#d95f02"}
    flow_colors = {"A": "#3366a6", "B": "#c44e52"}
    backend_labels = {"linux": "Linux DualPI2", "p4": "P4/BMv2"}
    linestyles = {"linux": "-", "p4": "--"}

    # (a) Absolute per-flow goodput. Matching the configured-rate envelopes is
    # a gate, not something to normalize away.
    ax = axes[0, 0]
    for flow in ("A", "B"):
        for backend in ("linux", "p4"):
            series = _packet_metric_by_rep(packet_rows, backend, width_s,
                                           "goodput_mbit_s", flow)
            xs, med, q1, q3 = _aggregate_series(series, width_s)
            label = f"Flow {flow}, {backend_labels[backend]}"
            ax.plot(xs, med, color=flow_colors[flow], linestyle=linestyles[backend],
                    linewidth=1.6, label=label)
            ax.fill_between(xs, q1, q3, color=flow_colors[flow], alpha=.08, linewidth=0)
    ax.set_title("(a) Per-flow goodput", loc="left", fontsize=11)
    ax.set_ylabel("Mbit/s")
    ax.set_ylim(bottom=0)

    # (b) Backend-native AQM queue-delay estimate: tc DualPI2 delay_l/c for
    # Linux and BMv2 deq_timedelta registers for P4.
    ax = axes[0, 1]
    for backend in ("linux", "p4"):
        series = _sample_metric_by_rep(queue_rows, backend, width_s,
                                       "queue_delay_us", 1/1000)
        xs, med, q1, q3 = _aggregate_series(series, width_s)
        ax.plot(xs, med, color=backend_colors[backend], linewidth=1.7,
                label=backend_labels[backend])
        ax.fill_between(xs, q1, q3, color=backend_colors[backend], alpha=.14, linewidth=0)
    ax.set_title("(b) AQM queue delay", loc="left", fontsize=11)
    ax.set_ylabel("ms")
    ax.set_ylim(bottom=0)

    # (c) CE behavior over time; no residence-time-derived x-axis.
    ax = axes[1, 0]
    for backend in ("linux", "p4"):
        series = _packet_metric_by_rep(packet_rows, backend, width_s, "ce_fraction")
        xs, med, q1, q3 = _aggregate_series(series, width_s)
        ax.plot(xs, med, color=backend_colors[backend], linewidth=1.7,
                label=backend_labels[backend])
        ax.fill_between(xs, q1, q3, color=backend_colors[backend], alpha=.14, linewidth=0)
    ax.set_title("(c) New CE-mark fraction", loc="left", fontsize=11)
    ax.set_ylabel("fraction of entering ECT(1)")
    ax.set_ylim(0, 1)

    # (d) Data-socket congestion windows only.
    ax = axes[1, 1]
    for flow in ("A", "B"):
        for backend in ("linux", "p4"):
            series = _sample_metric_by_rep(transport_rows, backend, width_s,
                                            "cwnd_bytes", 1/1024, flow)
            xs, med, q1, q3 = _aggregate_series(series, width_s)
            ax.plot(xs, med, color=flow_colors[flow], linestyle=linestyles[backend],
                    linewidth=1.6, label=f"Flow {flow}, {backend_labels[backend]}")
            ax.fill_between(xs, q1, q3, color=flow_colors[flow], alpha=.08, linewidth=0)
    ax.set_title("(d) Prague data-socket congestion window", loc="left", fontsize=11)
    ax.set_ylabel("KiB")
    ax.set_ylim(bottom=0)

    total_seconds = 5 * phase_seconds
    phase_names = ("A only", "A + B", "B only", "A + B", "A only")
    for ax in axes.flat:
        for phase in range(5):
            lo, hi = phase*phase_seconds, (phase+1)*phase_seconds
            if phase % 2:
                ax.axvspan(lo, hi, color="0.5", alpha=.06, linewidth=0)
            if phase:
                ax.axvline(lo, color="0.4", linewidth=.7, alpha=.45)
        ax.set_xlim(0, total_seconds)
        ax.set_xlabel("Time (s)")
        ax.grid(axis="y", alpha=.22)
    for phase, name in enumerate(phase_names):
        axes[0, 0].text((phase+.5)*phase_seconds, .965, name,
                        transform=axes[0, 0].get_xaxis_transform(), ha="center",
                        va="top", fontsize=7.5, color="0.3",
                        bbox={"facecolor": "white", "edgecolor": "none", "alpha": .7, "pad": 1})

    axes[0, 0].legend(fontsize=7, ncol=2)
    axes[0, 1].legend(fontsize=8)
    axes[1, 0].legend(fontsize=8)
    axes[1, 1].legend(fontsize=7, ncol=2)
    reps = {backend: len({int(r["rep"]) for r in packet_rows if r["backend"] == backend})
            for backend in ("linux", "p4")}
    fig.suptitle("L4S implementation validation: Linux DualPI2 vs P4/BMv2\n"
                 f"{target_mbit:g} Mbit/s bottleneck; median and IQR over "
                 f"{reps['linux']} Linux and {reps['p4']} P4 runs",
                 fontsize=13, y=1.055)
    fig.tight_layout(rect=(0, 0, 1, .965))
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
