#!/usr/bin/env python3
"""
Mininet delay-capacity probe.

Version: 1.1.0

Topology:
    client (c) -- s1 -- s2 -- server (srv)

The probe compares two endpoint-delay profiles by default:

    0 ms  on each endpoint egress -> approximately 0 ms added RTT
    20 ms on each endpoint egress -> approximately 40 ms added RTT

This matches common.py's experiment model:

    tc qdisc replace dev <host>-eth0 root netem delay 20ms

No bandwidth shaper, AQM, or explicit queue limit is installed.

Measurements per profile:
  - idle ICMP RTT
  - TCP throughput client -> server
  - TCP throughput server -> client
  - ICMP RTT while TCP saturates client -> server
  - ICMP RTT while TCP saturates server -> client
  - optional qdisc and socket diagnostics

Run:
    sudo python3 mininet_delay_probe_v1.1.0.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from mininet.link import Link
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge


VERSION = "1.1.0"
IPERF_PORT = 5201

PING_TIME_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")
PING_LOSS_RE = re.compile(r"([0-9.]+)% packet loss")


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("This probe must be run with sudo/root.")


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required program not found: {name}")


def parse_csv_ints(value: str) -> List[int]:
    values: List[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parsed = int(item)
        if parsed < 0:
            raise argparse.ArgumentTypeError(
                "Delay values must be non-negative integers."
            )
        values.append(parsed)

    if not values:
        raise argparse.ArgumentTypeError(
            "At least one delay profile is required."
        )
    return values


def parse_parallel(value: str) -> List[int]:
    values: List[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parsed = int(item)
        if parsed < 1:
            raise argparse.ArgumentTypeError(
                "Parallel-stream counts must be positive."
            )
        values.append(parsed)

    if not values:
        raise argparse.ArgumentTypeError(
            "At least one parallel-stream count is required."
        )
    return values


def command_output(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return f"ERROR: {exc}"

    return "\n".join(
        part.strip()
        for part in (result.stdout, result.stderr)
        if part.strip()
    )


def percentile(ordered: Sequence[float], value: float) -> float:
    if not ordered:
        raise ValueError("Cannot calculate percentile of an empty sequence.")
    if len(ordered) == 1:
        return float(ordered[0])

    position = (len(ordered) - 1) * value / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return (
        float(ordered[lower]) * (1.0 - fraction)
        + float(ordered[upper]) * fraction
    )


def parse_ping(output: str) -> Dict[str, Any]:
    samples = [float(value) for value in PING_TIME_RE.findall(output)]
    loss_match = PING_LOSS_RE.search(output)

    result: Dict[str, Any] = {
        "samples_ms": samples,
        "received_samples": len(samples),
        "packet_loss_percent": (
            float(loss_match.group(1)) if loss_match else None
        ),
        "raw_output": output,
    }

    if samples:
        ordered = sorted(samples)
        result.update(
            {
                "minimum_ms": min(samples),
                "mean_ms": statistics.fmean(samples),
                "median_ms": statistics.median(samples),
                "p95_ms": percentile(ordered, 95),
                "p99_ms": percentile(ordered, 99),
                "maximum_ms": max(samples),
            }
        )

    return result


def run_ping(
    source,
    destination_ip: str,
    *,
    count: int,
    interval_seconds: float,
) -> Dict[str, Any]:
    output = source.cmd(
        f"ping -n -c {count} -i {interval_seconds} "
        f"-W 3 {destination_ip} 2>&1"
    )
    return parse_ping(output)


def build_network() -> Tuple[Mininet, Any, Any, Any, Any]:
    net = Mininet(
        switch=OVSBridge,
        controller=None,
        link=Link,
        build=False,
        autoSetMacs=True,
    )

    client = net.addHost("c", ip="10.0.0.1/24")
    server = net.addHost("srv", ip="10.0.0.2/24")
    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")

    net.addLink(client, s1)
    net.addLink(s1, s2)
    net.addLink(s2, server)

    net.build()
    net.start()
    return net, client, server, s1, s2


def clear_endpoint_qdiscs(client, server) -> None:
    for host in (client, server):
        host.cmd(
            f"tc qdisc del dev {host.name}-eth0 root "
            ">/dev/null 2>&1 || true"
        )


def apply_endpoint_delay(client, server, delay_ms: int) -> None:
    """Apply the same netem model used by common.add_access_delays()."""
    clear_endpoint_qdiscs(client, server)

    if delay_ms <= 0:
        return

    for host in (client, server):
        output = host.cmd(
            f"tc qdisc replace dev {host.name}-eth0 root "
            f"netem delay {delay_ms}ms 2>&1"
        ).strip()
        if output:
            raise RuntimeError(
                f"Failed to apply netem on {host.name}: {output}"
            )


def tune_endpoints(client, server) -> Dict[str, Any]:
    """Mirror the experiment's ECN and Prague host configuration."""
    result: Dict[str, Any] = {}

    for host in (client, server):
        host.cmd("sysctl -w net.ipv4.tcp_ecn=1 >/dev/null")
        host.cmd(
            "sysctl -w net.ipv4.tcp_congestion_control=prague "
            ">/dev/null"
        )
        result[host.name] = {
            "tcp_ecn": host.cmd(
                "sysctl -n net.ipv4.tcp_ecn"
            ).strip(),
            "tcp_congestion_control": host.cmd(
                "sysctl -n net.ipv4.tcp_congestion_control"
            ).strip(),
        }

    return result


def extract_json(output: str) -> Dict[str, Any]:
    first = output.find("{")
    last = output.rfind("}")
    if first < 0 or last < first:
        raise ValueError("No JSON object found in iperf3 output.")
    return json.loads(output[first : last + 1])


def bps_from_iperf(data: Dict[str, Any]) -> Optional[float]:
    end = data.get("end", {})

    for key in ("sum_received", "sum_sent", "sum"):
        candidate = end.get(key)
        if isinstance(candidate, dict):
            value = candidate.get("bits_per_second")
            if isinstance(value, (int, float)):
                return float(value)

    return None


def retransmits_from_iperf(data: Dict[str, Any]) -> Optional[int]:
    value = data.get("end", {}).get("sum_sent", {}).get("retransmits")
    return int(value) if isinstance(value, int) else None


def cpu_from_iperf(data: Dict[str, Any]) -> Dict[str, Any]:
    candidate = data.get("end", {}).get("cpu_utilization_percent", {})
    return dict(candidate) if isinstance(candidate, dict) else {}


def run_iperf(
    client,
    server_ip: str,
    *,
    duration_seconds: int,
    parallel_streams: int,
    reverse: bool,
) -> Dict[str, Any]:
    direction = "server_to_client" if reverse else "client_to_server"

    command = (
        f"iperf3 -c {server_ip} -p {IPERF_PORT} "
        f"-t {duration_seconds} -P {parallel_streams} -J"
    )
    if reverse:
        command += " -R"

    output = client.cmd(f"{command} 2>&1")
    result: Dict[str, Any] = {
        "direction": direction,
        "parallel_streams": parallel_streams,
        "command": command,
        "success": False,
        "raw_output": output,
    }

    try:
        data = extract_json(output)
    except (ValueError, json.JSONDecodeError) as exc:
        result["parse_error"] = str(exc)
        return result

    if "error" in data:
        result["iperf_error"] = data["error"]
        result["json"] = data
        return result

    throughput_bps = bps_from_iperf(data)
    result.update(
        {
            "success": True,
            "json": data,
            "throughput_bps": throughput_bps,
            "throughput_mbit_s": (
                throughput_bps / 1_000_000.0
                if throughput_bps is not None
                else None
            ),
            "throughput_gbit_s": (
                throughput_bps / 1_000_000_000.0
                if throughput_bps is not None
                else None
            ),
            "retransmits": retransmits_from_iperf(data),
            "cpu_utilization_percent": cpu_from_iperf(data),
        }
    )
    return result


def start_iperf_server(server) -> None:
    server.cmd("pkill -x iperf3 >/dev/null 2>&1 || true")
    output = server.cmd(
        f"iperf3 -s -D -p {IPERF_PORT} "
        "--logfile /tmp/mininet_delay_probe_iperf_server.log "
        "2>&1"
    ).strip()
    if output:
        info(f"*** iperf3 server startup output: {output}\n")
    time.sleep(0.5)


def stop_iperf_server(server) -> None:
    server.cmd("pkill -x iperf3 >/dev/null 2>&1 || true")


def best_parallel(tests: Iterable[Dict[str, Any]]) -> int:
    valid = [
        item
        for item in tests
        if item.get("success")
        and isinstance(item.get("throughput_bps"), (int, float))
    ]
    if not valid:
        return 1
    return int(
        max(valid, key=lambda item: float(item["throughput_bps"]))[
            "parallel_streams"
        ]
    )


def run_latency_under_load(
    client,
    server_ip: str,
    *,
    parallel_streams: int,
    load_duration_seconds: int,
    ping_count: int,
    reverse: bool,
) -> Dict[str, Any]:
    direction = "server_to_client" if reverse else "client_to_server"
    log_path = f"/tmp/mininet_delay_load_{direction}.json"

    command = (
        f"iperf3 -c {server_ip} -p {IPERF_PORT} "
        f"-t {load_duration_seconds} -P {parallel_streams} -J"
    )
    if reverse:
        command += " -R"

    pid_output = client.cmd(
        f"{command} > {log_path} 2>&1 & echo $!"
    ).strip()

    try:
        pid = int(pid_output.splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "success": False,
            "direction": direction,
            "error": f"Could not start load: {pid_output!r}",
        }

    time.sleep(1.0)

    ping_duration = max(1.0, load_duration_seconds - 2.0)
    max_samples = max(2, int(ping_duration / 0.2))
    actual_ping_count = min(ping_count, max_samples)

    ping_result = run_ping(
        client,
        server_ip,
        count=actual_ping_count,
        interval_seconds=0.2,
    )

    client.cmd(
        f"while kill -0 {pid} >/dev/null 2>&1; "
        "do sleep 0.1; done"
    )
    load_output = client.cmd(f"cat {log_path} 2>/dev/null")

    result: Dict[str, Any] = {
        "success": True,
        "direction": direction,
        "parallel_streams": parallel_streams,
        "load_command": command,
        "ping": ping_result,
        "load_raw_output": load_output,
    }

    try:
        data = extract_json(load_output)
        throughput_bps = bps_from_iperf(data)
        result["load_json"] = data
        result["load_throughput_mbit_s"] = (
            throughput_bps / 1_000_000.0
            if throughput_bps is not None
            else None
        )
        result["load_cpu_utilization_percent"] = cpu_from_iperf(data)
    except (ValueError, json.JSONDecodeError) as exc:
        result["load_parse_error"] = str(exc)

    return result


def run_profile(
    client,
    server,
    *,
    delay_ms_oneway_endpoint: int,
    tcp_duration_seconds: int,
    parallel_values: Sequence[int],
    ping_count: int,
    load_duration_seconds: int,
) -> Dict[str, Any]:
    apply_endpoint_delay(
        client,
        server,
        delay_ms_oneway_endpoint,
    )
    time.sleep(0.5)

    server_ip = server.IP()
    profile: Dict[str, Any] = {
        "delay_ms_oneway_endpoint": delay_ms_oneway_endpoint,
        "expected_added_rtt_ms": delay_ms_oneway_endpoint * 2,
        "qdisc": {
            "client": client.cmd(
                "tc qdisc show dev c-eth0"
            ).strip(),
            "server": server.cmd(
                "tc qdisc show dev srv-eth0"
            ).strip(),
        },
    }

    info(
        f"\n*** Profile: {delay_ms_oneway_endpoint} ms "
        "on each endpoint egress "
        f"(approximately {delay_ms_oneway_endpoint * 2} ms added RTT)\n"
    )

    info("*** Idle latency\n")
    profile["idle_latency"] = run_ping(
        client,
        server_ip,
        count=ping_count,
        interval_seconds=0.2,
    )

    forward_tests: List[Dict[str, Any]] = []
    reverse_tests: List[Dict[str, Any]] = []

    for parallel_streams in parallel_values:
        info(
            f"*** TCP c->srv: P={parallel_streams}, "
            f"t={tcp_duration_seconds}s\n"
        )
        forward_tests.append(
            run_iperf(
                client,
                server_ip,
                duration_seconds=tcp_duration_seconds,
                parallel_streams=parallel_streams,
                reverse=False,
            )
        )
        time.sleep(0.5)

        info(
            f"*** TCP srv->c: P={parallel_streams}, "
            f"t={tcp_duration_seconds}s\n"
        )
        reverse_tests.append(
            run_iperf(
                client,
                server_ip,
                duration_seconds=tcp_duration_seconds,
                parallel_streams=parallel_streams,
                reverse=True,
            )
        )
        time.sleep(0.5)

    profile["tcp"] = {
        "client_to_server": forward_tests,
        "server_to_client": reverse_tests,
    }

    selected_forward = best_parallel(forward_tests)
    selected_reverse = best_parallel(reverse_tests)
    profile["selected_parallel_streams"] = {
        "client_to_server": selected_forward,
        "server_to_client": selected_reverse,
    }

    info("*** RTT under c->srv saturation\n")
    loaded_forward = run_latency_under_load(
        client,
        server_ip,
        parallel_streams=selected_forward,
        load_duration_seconds=load_duration_seconds,
        ping_count=ping_count,
        reverse=False,
    )

    info("*** RTT under srv->c saturation\n")
    loaded_reverse = run_latency_under_load(
        client,
        server_ip,
        parallel_streams=selected_reverse,
        load_duration_seconds=load_duration_seconds,
        ping_count=ping_count,
        reverse=True,
    )

    profile["latency_under_tcp_load"] = {
        "client_to_server_load": loaded_forward,
        "server_to_client_load": loaded_reverse,
    }

    return profile


def summarize_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    idle = profile.get("idle_latency", {})
    tcp = profile.get("tcp", {})

    summary: Dict[str, Any] = {
        "delay_ms_oneway_endpoint": profile.get(
            "delay_ms_oneway_endpoint"
        ),
        "expected_added_rtt_ms": profile.get("expected_added_rtt_ms"),
        "idle_rtt_mean_ms": idle.get("mean_ms"),
        "idle_rtt_p95_ms": idle.get("p95_ms"),
        "idle_rtt_p99_ms": idle.get("p99_ms"),
    }

    for direction in ("client_to_server", "server_to_client"):
        tests = tcp.get(direction, [])
        valid = [
            test
            for test in tests
            if isinstance(test.get("throughput_mbit_s"), (int, float))
        ]
        if valid:
            best = max(
                valid,
                key=lambda item: float(item["throughput_mbit_s"]),
            )
            summary[f"best_tcp_{direction}_mbit_s"] = best[
                "throughput_mbit_s"
            ]
            summary[f"best_tcp_{direction}_parallel"] = best[
                "parallel_streams"
            ]

    loaded = profile.get("latency_under_tcp_load", {})
    for load_name, result in loaded.items():
        ping = result.get("ping", {}) if isinstance(result, dict) else {}
        summary[f"{load_name}_rtt_mean_ms"] = ping.get("mean_ms")
        summary[f"{load_name}_rtt_p95_ms"] = ping.get("p95_ms")
        summary[f"{load_name}_rtt_p99_ms"] = ping.get("p99_ms")
        summary[f"{load_name}_throughput_mbit_s"] = result.get(
            "load_throughput_mbit_s"
        )

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare unshaped Mininet throughput and latency with "
            "zero delay and experiment-style endpoint delay."
        )
    )
    parser.add_argument(
        "--delay-profiles-ms",
        type=parse_csv_ints,
        default=[0, 20],
        help=(
            "Comma-separated netem delay values applied to both endpoint "
            "egresses (default: 0,20)."
        ),
    )
    parser.add_argument(
        "--tcp-duration",
        type=int,
        default=8,
    )
    parser.add_argument(
        "--parallel",
        type=parse_parallel,
        default=[1, 2, 4, 8],
    )
    parser.add_argument(
        "--ping-count",
        type=int,
        default=50,
    )
    parser.add_argument(
        "--load-duration",
        type=int,
        default=15,
    )
    parser.add_argument(
        "--output",
        help="Output JSON path; timestamped by default.",
    )
    args = parser.parse_args()

    require_root()
    for program in ("iperf3", "ping", "tc", "ovs-vsctl"):
        require_program(program)

    if args.tcp_duration < 1:
        parser.error("--tcp-duration must be positive")
    if args.ping_count < 2:
        parser.error("--ping-count must be at least 2")
    if args.load_duration < 4:
        parser.error("--load-duration must be at least 4")

    setLogLevel("info")

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(
        args.output
        or f"mininet_delay_probe_{timestamp}.json"
    )

    results: Dict[str, Any] = {
        "probe_version": VERSION,
        "timestamp": timestamp,
        "topology": "c--s1--s2--srv",
        "restrictions": {
            "bandwidth_shaping": False,
            "aqm": False,
            "explicit_queue_limit": False,
        },
        "delay_model": (
            "The configured delay is applied to the root netem qdisc on "
            "both c-eth0 and srv-eth0, matching common.add_access_delays()."
        ),
        "system": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "python": sys.version,
            "logical_cpu_count": os.cpu_count(),
            "iperf3_version": command_output(["iperf3", "--version"]),
            "ovs_version": command_output(["ovs-vsctl", "--version"]),
            "mininet_version": command_output(["mn", "--version"]),
            "load_average_before": (
                os.getloadavg() if hasattr(os, "getloadavg") else None
            ),
        },
        "arguments": vars(args),
        "profiles": [],
    }

    net: Optional[Mininet] = None
    server = None

    try:
        info("*** Building c--s1--s2--srv topology\n")
        net, client, server, s1, s2 = build_network()
        results["endpoint_tuning"] = tune_endpoints(client, server)

        info("*** Connectivity check\n")
        results["connectivity_ping_loss_percent"] = net.ping(
            hosts=[client, server],
            timeout="3",
        )

        start_iperf_server(server)

        for delay_ms in args.delay_profiles_ms:
            results["profiles"].append(
                run_profile(
                    client,
                    server,
                    delay_ms_oneway_endpoint=delay_ms,
                    tcp_duration_seconds=args.tcp_duration,
                    parallel_values=args.parallel,
                    ping_count=args.ping_count,
                    load_duration_seconds=args.load_duration,
                )
            )
            time.sleep(1.0)

        results["summary"] = [
            summarize_profile(profile)
            for profile in results["profiles"]
        ]
        results["system"]["load_average_after"] = (
            os.getloadavg() if hasattr(os, "getloadavg") else None
        )

    finally:
        if server is not None:
            stop_iperf_server(server)
        if net is not None:
            clear_endpoint_qdiscs(client, server)
            info("*** Stopping Mininet\n")
            net.stop()

        output_path.write_text(
            json.dumps(results, indent=2),
            encoding="utf-8",
        )

    print(f"\nResults written to: {output_path}")
    print("\nSummary:")
    for item in results.get("summary", []):
        print(
            f"  Delay {item.get('delay_ms_oneway_endpoint')} ms/endpoint: "
            f"idle mean RTT={item.get('idle_rtt_mean_ms'):.3f} ms, "
            f"idle p95={item.get('idle_rtt_p95_ms'):.3f} ms"
        )

        for direction in ("client_to_server", "server_to_client"):
            rate = item.get(f"best_tcp_{direction}_mbit_s")
            parallel = item.get(f"best_tcp_{direction}_parallel")
            if isinstance(rate, (int, float)):
                print(
                    f"    best TCP {direction}: "
                    f"{rate:.2f} Mbit/s at P={parallel}"
                )

        for load_name in (
            "client_to_server_load",
            "server_to_client_load",
        ):
            mean_rtt = item.get(f"{load_name}_rtt_mean_ms")
            p95_rtt = item.get(f"{load_name}_rtt_p95_ms")
            rate = item.get(f"{load_name}_throughput_mbit_s")
            if isinstance(mean_rtt, (int, float)):
                print(
                    f"    {load_name}: mean RTT={mean_rtt:.3f} ms, "
                    f"p95={p95_rtt:.3f} ms, "
                    f"TCP={rate:.2f} Mbit/s"
                )


if __name__ == "__main__":
    main()
