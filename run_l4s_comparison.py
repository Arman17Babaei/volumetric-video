#!/usr/bin/env python3
"""Collect a phased Prague-vs-Prague trace on Linux DualPI2 or P4/BMv2.

This is intentionally separate from the DASH/QoE experiment runners. It reuses
existing topology and AQM configuration helpers but runs two iperf3 TCP Prague
flows with deterministic start/stop phases and backend-neutral logging.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
FLOW_PORTS = {"A": 5201, "B": 5202}


def run_local(command: List[str]) -> str:
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def git_identity() -> Dict[str, object]:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    return {"commit": git("rev-parse", "HEAD"), "status": git("status", "--porcelain"),
            "dirty": bool(git("status", "--porcelain"))}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def phase_schedule(phase_seconds: float) -> List[Tuple[float, str, str]]:
    p = phase_seconds
    return [(0,"start","A"),(p,"start","B"),(2*p,"stop","A"),(3*p,"start","A"),(4*p,"stop","B"),(5*p,"stop","A")]


def ensure_prague(host) -> None:
    host.cmd("sysctl -qw net.ipv4.tcp_ecn=1")
    host.cmd("sysctl -qw net.ipv4.tcp_congestion_control=prague")
    active = host.cmd("sysctl -n net.ipv4.tcp_congestion_control").strip()
    if active != "prague":
        raise RuntimeError(f"{host.name}: expected Prague, got {active!r}")


def start_tcpdump(node, device: str, path: Path):
    return node.popen(["tcpdump", "-U", "-s", "128", "-i", device,
                       "tcp port 5201 or tcp port 5202", "-w", str(path)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_ss(text: str, mss: int = 1460) -> Dict[str, float]:
    values: Dict[str, float] = {}
    m = re.search(r"\bcwnd:(\d+)", text)
    if m: values["cwnd_bytes"] = int(m.group(1)) * mss
    m = re.search(r"\brtt:([0-9.]+)/([0-9.]+)", text)
    if m:
        values["rtt_ms"] = float(m.group(1)); values["rttvar_ms"] = float(m.group(2))
    for key in ("bytes_acked", "bytes_sent"):
        m = re.search(rf"\b{key}:(\d+)", text)
        if m: values[key] = int(m.group(1))
    m = re.search(r"\bretrans:(?:\d+/)?(\d+)", text)
    if m: values["retrans"] = int(m.group(1))
    m = re.search(r"\bpacing_rate\s+([0-9.]+)([KMG]?bps)", text)
    if m:
        scale = {"bps":1, "Kbps":1e3, "Mbps":1e6, "Gbps":1e9}[m.group(2)]
        values["pacing_bps"] = float(m.group(1))*scale
    return values


def parse_ss_data_connection(text: str) -> Dict[str, float]:
    """Select iperf's bulk-data socket, not its small control socket."""
    connections: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace():
            if current:
                connections.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        connections.append("\n".join(current))
    parsed = [parse_ss(connection) for connection in connections]
    candidates = [row for row in parsed if row]
    return max(candidates, key=lambda row: row.get("bytes_sent", 0.0), default={})


def configure_experiment_interfaces(nodes, mtu: int = 1500) -> None:
    """Give Linux and P4 runs identical MTUs and packet-level captures.

    P4-Utils otherwise creates 9500-byte interfaces. BMv2 shapes packets/s,
    so that silently turns a queue configured for 1500-byte packets into a
    roughly 63 Mbit/s queue. Disabling host offloads also prevents captures
    from observing synthetic GSO packets that never enter the AQM as such.
    """
    for node in nodes:
        for intf in node.intfList():
            if intf.name == "lo":
                continue
            result = node.cmd(f"ip link set dev {intf.name} mtu {int(mtu)} 2>&1")
            if result.strip():
                raise RuntimeError(f"failed to set MTU on {node.name}/{intf.name}: {result}")
            node.cmd(
                f"ethtool -K {intf.name} tso off gso off gro off lro off "
                "2>/dev/null || true"
            )


def parse_linux_queue_state(text: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for name in ("delay_c", "delay_l"):
        match = re.search(rf"\b{name}\s+(\d+)us\b", text)
        if match:
            result[f"{name}_us"] = float(match.group(1))
    match = re.search(r"\bprob\s+([0-9.]+)", text)
    if match:
        result["base_probability"] = float(match.group(1))
    if "delay_c_us" in result or "delay_l_us" in result:
        result["queue_delay_us"] = max(result.get("delay_c_us", 0), result.get("delay_l_us", 0))
    return result


def parse_p4_queue_state(text: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    names = {"r_qdelay_c": "delay_c_us", "r_qdelay_l": "delay_l_us"}
    for register, name in names.items():
        match = re.search(rf"\b{register}\[\d+\]\s*=\s*(-?\d+)", text)
        if match:
            result[name] = max(0.0, float(match.group(1)))
    match = re.search(r"\br_probability\[\d+\]\s*=\s*(\d+)", text)
    if match:
        result["base_probability"] = float(match.group(1)) / 0xFFFFFFFF
    if "delay_c_us" in result or "delay_l_us" in result:
        result["queue_delay_us"] = max(result.get("delay_c_us", 0), result.get("delay_l_us", 0))
    return result


def transport_sampler(hosts: Dict[str, object], started: float, stop_event: threading.Event,
                      output: Path, sample_ms: float, generation: Dict[str, int]) -> None:
    with output.open("w", newline="") as stream:
        fields = ["time_s","flow","connection_generation","cwnd_bytes","rtt_ms","rttvar_ms",
                  "bytes_acked","bytes_sent","pacing_bps","retrans"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        while not stop_event.is_set():
            now = time.monotonic()
            for flow, host in hosts.items():
                port = FLOW_PORTS[flow]
                text = host.cmd(f"ss -tinH state established '( dport = :{port} )' 2>/dev/null")
                data = parse_ss_data_connection(text)
                if data:
                    row = {"time_s": now-started, "flow": flow,
                           "connection_generation": generation[flow], **data}
                    writer.writerow(row); stream.flush()
            stop_event.wait(sample_ms/1000.0)


def queue_sampler(sample, started: float, stop_event: threading.Event,
                  output: Path, sample_ms: float) -> None:
    fields = ["time_s", "queue_delay_us", "delay_c_us", "delay_l_us", "base_probability"]
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        while not stop_event.is_set():
            now = time.monotonic()
            data = sample()
            if data:
                writer.writerow({"time_s": now-started, **data})
                stream.flush()
            stop_event.wait(sample_ms/1000.0)


def record_environment(path: Path, backend: str, args, p4src: Optional[Path] = None) -> None:
    meta = {
        "backend": backend, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git": git_identity(), "kernel": platform.release(), "python": platform.python_version(),
        "tc_version": run_local(["tc", "-V"]), "iperf3_version": run_local(["iperf3", "--version"]),
        "tshark_version": run_local(["tshark", "--version"]).splitlines()[:1],
        "phase_seconds": args.phase_seconds, "sample_ms": args.sample_ms,
        "configured_rate_mbit": args.rate_mbit, "configured_delay_ms": args.delay_ms,
        "l4s_step_us": args.l4s_step_us, "mtu_bytes": 1500,
    }
    if p4src:
        meta["p4"] = {"source": str(p4src), "sha256": sha256_file(p4src),
                      "simple_switch_version": run_local(["simple_switch", "--version"]),
                      "p4c_version": run_local(["p4c", "--version"]),
                      "queue_rate_scale": args.p4_rate_scale}
    path.write_text(json.dumps(meta, indent=2)+"\n")


def run_phases(clients, server, rep_dir: Path, phase_seconds: float, sample_ms: float,
               ingress_devices: Tuple[str, str], egress_node, egress_device: str,
               sample_queue) -> None:
    rep_dir.mkdir(parents=True, exist_ok=False)
    for host in clients + [server]: ensure_prague(host)
    for port in FLOW_PORTS.values():
        server.cmd(f"pkill -f 'iperf3 -s.*{port}' >/dev/null 2>&1 || true")
        server.cmd(f"iperf3 -s -p {port} >/tmp/l4s-iperf-{port}.log 2>&1 &")
    captures = [
        start_tcpdump(clients[0], ingress_devices[0], rep_dir/"ingress-A.pcap"),
        start_tcpdump(clients[1], ingress_devices[1], rep_dir/"ingress-B.pcap"),
        start_tcpdump(egress_node, egress_device, rep_dir/"egress.pcap"),
    ]
    started = time.monotonic(); stop_sample = threading.Event()
    procs: Dict[str, object] = {}
    generations = {"A":0,"B":0}
    sampler = threading.Thread(target=transport_sampler,
        args=({"A":clients[0],"B":clients[1]}, started, stop_sample, rep_dir/"transport.csv", sample_ms, generations), daemon=True)
    queue_thread = threading.Thread(target=queue_sampler,
        args=(sample_queue, started, stop_sample, rep_dir/"queue.csv", sample_ms), daemon=True)
    sampler.start(); queue_thread.start()
    events = []
    try:
        for planned, action, flow in phase_schedule(phase_seconds):
            delay = started + planned - time.monotonic()
            if delay > 0: time.sleep(delay)
            actual = time.monotonic()-started
            host = clients[0] if flow == "A" else clients[1]
            port = FLOW_PORTS[flow]
            if action == "start":
                generations[flow] += 1
                procs[flow] = host.popen(["iperf3", "-c", server.IP(), "-p", str(port), "-t", str(int(phase_seconds*5+5))],
                                         stdout=(rep_dir/f"iperf-{flow}-gen{generations[flow]}.log").open("w"),
                                         stderr=subprocess.STDOUT)
            else:
                proc = procs.pop(flow, None)
                if proc is not None and proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                    try: proc.wait(timeout=2)
                    except subprocess.TimeoutExpired: proc.kill()
            events.append({"planned_time_s":planned,"actual_time_s":actual,"event":action,"flow":flow})
        time.sleep(.2)
    finally:
        for proc in procs.values():
            if proc.poll() is None: proc.kill()
        stop_sample.set(); sampler.join(timeout=2); queue_thread.join(timeout=2)
        for proc in captures:
            if proc.poll() is None: proc.send_signal(signal.SIGINT)
        for proc in captures:
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()
        for port in FLOW_PORTS.values(): server.cmd(f"pkill -f 'iperf3 -s.*{port}' >/dev/null 2>&1 || true")
    with (rep_dir/"events.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["planned_time_s","actual_time_s","event","flow"])
        writer.writeheader(); writer.writerows(events)


def run_linux(args, root: Path) -> None:
    from common import add_access_delays
    from linux_bottleneck import (build_linux_net, clear_all_qdisc, get_interswitch_devices,
                                  set_bottleneck_dualpi2)
    net, clients, server, s1, s2 = build_linux_net(num_clients=2)
    s1s2, s2s1 = get_interswitch_devices(2)
    try:
        configure_experiment_interfaces([*clients, server, s1, s2])
        clear_all_qdisc(s1, s2, server, *clients)
        add_access_delays(args.delay_ms, server, *clients)
        set_bottleneck_dualpi2(s1, s1s2, s2, s2s1,
                               rate_mbit=args.rate_mbit,
                               l4s_step_us=args.l4s_step_us)
        parent = root/"linux"; parent.mkdir(parents=True)
        record_environment(parent/"metadata.json", "linux", args)
        for rep in range(1, args.repetitions+1):
            rep_dir = parent/f"rep_{rep:02d}"
            qdisc_before = s1.cmd(f"tc -details -statistics qdisc show dev {s1s2}")
            sample_queue = lambda: parse_linux_queue_state(
                s1.cmd(f"tc -details -statistics qdisc show dev {s1s2}"))
            run_phases(clients, server, rep_dir, args.phase_seconds, args.sample_ms,
                       ("h1-eth0","h2-eth0"), s1, s1s2, sample_queue)
            (rep_dir/"qdisc-before.txt").write_text(qdisc_before)
            (rep_dir/"qdisc-after.txt").write_text(s1.cmd(f"tc -details -statistics qdisc show dev {s1s2}"))
    finally:
        net.stop()


def run_p4(args, root: Path) -> None:
    from common import add_access_delays
    from p4_bottleneck import build_p4_net
    from run_p4_experiment import (LINUX_MATCHED_DUALPI2_PROFILE,
                                   install_static_arp, program_p4_switches)
    p4src = ROOT/args.p4src
    net_api, mn, clients, server, s1, s2 = build_p4_net(num_clients=2, p4src=args.p4src)
    try:
        configure_experiment_interfaces([*clients, server, s1, s2])
        validation_profile = dict(LINUX_MATCHED_DUALPI2_PROFILE)
        validation_profile["export_qdelay_in_ipv4_id"] = 1
        validation_profile["l4s_step_threshold_us"] = args.l4s_step_us
        config = program_p4_switches(clients, server,
                                     initial_rate_mbit=args.rate_mbit * args.p4_rate_scale,
                                     reference_packet_bytes=args.reference_packet_bytes,
                                     dualpi2_profile=validation_profile)
        config["comparison_run"] = {
            "target_bandwidth_mbit_each_direction": args.rate_mbit,
            "active_queue_rate_pps": config["initial_queue_rate_pps"],
            "queue_rate_scale": args.p4_rate_scale,
            "dynamic_rate_change": False,
        }
        add_access_delays(args.delay_ms, server, *clients); install_static_arp(clients, server)
        parent=root/"p4"; parent.mkdir(parents=True)
        record_environment(parent/"metadata.json", "p4", args, p4src)
        (parent/"p4-config.json").write_text(json.dumps(config, indent=2)+"\n")
        egress_port = int(config["switches"]["s1"]["bottleneck_egress_port"])
        egress_device = f"s1-eth{egress_port}"
        # Per-packet deq_timedelta is exported in IPv4 ID for validation;
        # avoid intrusive control-plane polling while BMv2 forwards traffic.
        sample_queue = lambda: {}
        for rep in range(1,args.repetitions+1):
            run_phases(clients, server, parent/f"rep_{rep:02d}", args.phase_seconds, args.sample_ms,
                       ("h1-eth0","h2-eth0"), s1, egress_device, sample_queue)
    finally:
        mn.stop()


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["linux","p4"], required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--phase-seconds", type=float, default=10.0)
    parser.add_argument("--sample-ms", type=float, default=50.0)
    parser.add_argument("--rate-mbit", type=float, default=10.0)
    parser.add_argument("--delay-ms", type=float, default=20.0)
    parser.add_argument("--reference-packet-bytes", type=int, default=1500)
    parser.add_argument("--l4s-step-us", type=int, default=5000,
                        help="native L4S step threshold used by both backends")
    parser.add_argument("--p4-rate-scale", type=float, default=1.0,
                        help="calibration factor for BMv2's packet-rate limiter")
    parser.add_argument("--p4src", default="dualpi2_repaired_v1.2.0.p4")
    parser.add_argument("--output", type=Path)
    args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit("run_l4s_comparison.py must run as root")
    if (args.repetitions<1 or args.phase_seconds<=0 or args.sample_ms<=0
            or args.l4s_step_us < 0 or args.p4_rate_scale <= 0):
        parser.error("invalid repetition/timing/AQM values")
    root=args.output or (ROOT/"experiments"/time.strftime("%Y%m%d_%H%M%S_linux_p4_validation"))
    root.mkdir(parents=True, exist_ok=True)
    manifest=root/"manifest.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"experiment":"linux-p4-l4s-behavioral-comparison",
            "schedule":phase_schedule(args.phase_seconds), "rate_mbit":args.rate_mbit,
            "delay_ms":args.delay_ms, "phase_seconds": args.phase_seconds,
            "l4s_step_us": args.l4s_step_us, "mtu_bytes": 1500,
            "p4_rate_scale": args.p4_rate_scale,
            "repetitions":args.repetitions}, indent=2)+"\n")
    if args.backend=="linux": run_linux(args,root)
    else: run_p4(args,root)
    print(root)

if __name__=="__main__": main()
