#!/usr/bin/env python3
"""Run the two-switch P4/BMv2 DualPI2 experiment.

Experiment bundle version: 1.3.1

The logical topology is:
    clients -- s1 -- s2 -- server

DualPI2 is enabled on both inter-switch egress ports. Browser initialization
starts at a temporary BMv2 packet rate. The rate is reduced to the experiment
rate after every client has received enough downstream bytes to load its page
and JavaScript.

Stale Chrome/Chromium and chromedriver processes are terminated in the client
network namespaces before and after the trial.
"""

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from mininet.log import info, setLogLevel
from p4utils.utils.helper import load_topo

from common import (
    add_access_delays,
    create_experiment_directory,
    endpoint_tuning,
    run_trial,
)
from linux_bottleneck import (
    BOOTSTRAP_BW_MBIT,
    BW_MBIT,
    DELAY_MS_ONEWAY,
)
from p4_bottleneck import build_p4_net


BUNDLE_VERSION = "1.3.1"
P4_PROGRAM_DEFAULT = "dualpi2_repaired_v1.2.0.p4"
SWITCH_NAMES = ("s1", "s2")

LINUX_DUALPI2_TARGET_US = 5_000
LINUX_DUALPI2_LIMIT_PACKETS = 10_000
DEFAULT_REFERENCE_PACKET_BYTES = 1_500
DEFAULT_BOOTSTRAP_BYTES_PER_CLIENT = 1_000_000
DEFAULT_BOOTSTRAP_MAX_WAIT_SECONDS = 30.0

BROWSER_PROCESS_TERMS = (
    "chromedriver",
    "google-chrome",
    "chrome",
    "chromium",
)

DUALPI2_PARAMETER_ORDER = (
    "alpha_q32_per_us",
    "beta_q32_per_us",
    "target_us",
    "update_interval_log2_us",
    "coupling_factor",
    "overload_base_probability",
    "drop_on_overload",
    "l4s_step_threshold_us",
    "l4s_ramp_range_us",
    "l4s_ramp_slope_q32_per_us",
    "l4s_min_queue_packets",
    "coupled_min_backlog_packets",
    "state_timeout_us",
    "idle_reset_us",
)

LINUX_MATCHED_DUALPI2_PROFILE: Dict[str, int] = {
    "alpha_q32_per_us": 687,
    "beta_q32_per_us": 13_744,
    "target_us": LINUX_DUALPI2_TARGET_US,
    "update_interval_log2_us": 14,
    "coupling_factor": 2,
    "overload_base_probability": 2_147_483_647,
    "drop_on_overload": 1,
    "l4s_step_threshold_us": 1_000,
    "l4s_ramp_range_us": 0,
    "l4s_ramp_slope_q32_per_us": 0,
    "l4s_min_queue_packets": 0,
    "coupled_min_backlog_packets": 2,
    "state_timeout_us": 65_536,
    "idle_reset_us": 1_000_000,
}


def profile_to_tuple(profile: Dict[str, int]) -> Tuple[int, ...]:
    missing = [
        name
        for name in DUALPI2_PARAMETER_ORDER
        if name not in profile
    ]
    if missing:
        raise ValueError(f"DualPI2 profile is missing: {missing}")
    return tuple(profile[name] for name in DUALPI2_PARAMETER_ORDER)


def mbit_to_bmv2_pps(
    bandwidth_mbit: float,
    reference_packet_bytes: int,
) -> int:
    """Approximate a bit rate using BMv2's packet/s queue-rate unit."""
    if bandwidth_mbit <= 0:
        raise ValueError("bandwidth_mbit must be positive")
    if reference_packet_bytes <= 0:
        raise ValueError("reference_packet_bytes must be positive")

    pps = bandwidth_mbit * 1_000_000.0 / (
        8.0 * reference_packet_bytes
    )
    return max(1, round(pps))


def run_simple_switch_cli(
    thrift_port: int,
    commands: Sequence[str],
) -> str:
    cli_input = "\n".join(commands) + "\n"
    result = subprocess.run(
        ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=cli_input,
        text=True,
        capture_output=True,
        check=False,
    )

    output = "\n".join(
        part
        for part in (result.stdout.strip(), result.stderr.strip())
        if part
    )
    failed = (
        result.returncode != 0
        or "RuntimeCmd: Error:" in output
        or "\nError:" in output
        or output.startswith("Error:")
    )
    if failed:
        print(f">>> [P4] CLI failure on Thrift port {thrift_port}")
        print(output)
        print(">>> [P4] Commands sent")
        print(cli_input)
        raise RuntimeError("Failed to program P4 switch")
    return output


def _route_next_hop(
    switch_name: str,
    destination_name: str,
    client_names: set,
) -> str:
    if switch_name == "s1":
        return destination_name if destination_name in client_names else "s2"
    if switch_name == "s2":
        return destination_name if destination_name == "server" else "s1"
    raise ValueError(f"Unknown switch: {switch_name}")


def _switch_neighbors(switch_name: str, clients) -> List[str]:
    if switch_name == "s1":
        return [client.name for client in clients] + ["s2"]
    if switch_name == "s2":
        return ["s1", "server"]
    raise ValueError(f"Unknown switch: {switch_name}")


def program_p4_switches(
    clients,
    server,
    *,
    initial_rate_mbit: float,
    reference_packet_bytes: int = DEFAULT_REFERENCE_PACKET_BYTES,
    dualpi2_profile: Dict[str, int] = LINUX_MATCHED_DUALPI2_PROFILE,
) -> Dict[str, object]:
    """Program forwarding, AQM placement, limits, and initial queue rates."""
    topo = load_topo("topology.json")
    endpoints = [*clients, server]
    client_names = {client.name for client in clients}
    initial_rate_pps = mbit_to_bmv2_pps(
        initial_rate_mbit,
        reference_packet_bytes,
    )
    measurement_rate_pps = mbit_to_bmv2_pps(
        BW_MBIT,
        reference_packet_bytes,
    )
    dualpi2_args = " ".join(
        str(value)
        for value in profile_to_tuple(dualpi2_profile)
    )

    switch_results: Dict[str, object] = {}

    for switch_name in SWITCH_NAMES:
        thrift_port = topo.get_thrift_port(switch_name)
        interswitch_neighbor = "s2" if switch_name == "s1" else "s1"
        bottleneck_port = topo.node_to_node_port_num(
            switch_name,
            interswitch_neighbor,
        )

        commands: List[str] = ["table_clear MyIngress.ipv4_lpm"]
        route_records = []

        print(
            f">>> [P4] Programming {switch_name} "
            f"on Thrift port {thrift_port}"
        )

        for endpoint in endpoints:
            next_hop = _route_next_hop(
                switch_name,
                endpoint.name,
                client_names,
            )
            egress_port = topo.node_to_node_port_num(
                switch_name,
                next_hop,
            )
            source_mac = topo.node_to_node_mac(
                switch_name,
                next_hop,
            )
            if not source_mac:
                raise RuntimeError(
                    f"No MAC for {switch_name} interface toward {next_hop}"
                )

            destination_mac = endpoint.MAC()

            commands.append(
                "table_add MyIngress.ipv4_lpm "
                "MyIngress.ipv4_forward "
                f"{endpoint.IP()}/32 => "
                f"{source_mac} {destination_mac} {egress_port}"
            )
            route_records.append(
                {
                    "destination": endpoint.name,
                    "destination_ip": endpoint.IP(),
                    "next_hop": next_hop,
                    "egress_port": egress_port,
                    "source_mac": source_mac,
                    "destination_mac": destination_mac,
                }
            )

        commands.append("table_clear MyEgress.aqm")
        for neighbor in _switch_neighbors(switch_name, clients):
            port = topo.node_to_node_port_num(switch_name, neighbor)
            if port == bottleneck_port:
                commands.append(
                    "table_add MyEgress.aqm "
                    "MyEgress.dualpi2 "
                    f"{port} => {dualpi2_args}"
                )
            else:
                commands.append(
                    f"table_add MyEgress.aqm NoAction {port} =>"
                )

        commands.append(
            f"set_queue_depth "
            f"{LINUX_DUALPI2_LIMIT_PACKETS} {bottleneck_port}"
        )
        commands.append(
            f"set_queue_rate {initial_rate_pps} {bottleneck_port}"
        )

        output = run_simple_switch_cli(thrift_port, commands)
        if output:
            print(f">>> [P4] {switch_name} CLI output")
            print(output)

        switch_results[switch_name] = {
            "thrift_port": thrift_port,
            "interswitch_neighbor": interswitch_neighbor,
            "bottleneck_egress_port": bottleneck_port,
            "routes": route_records,
        }

    return {
        "switches": switch_results,
        "bottleneck_link": "s1<->s2",
        "client_to_server_bottleneck": "s1 egress toward s2",
        "server_to_client_bottleneck": "s2 egress toward s1",
        "measurement_bandwidth_mbit_each_direction": BW_MBIT,
        "initial_bandwidth_mbit_each_direction": initial_rate_mbit,
        "measurement_queue_rate_pps": measurement_rate_pps,
        "initial_queue_rate_pps": initial_rate_pps,
        "reference_packet_bytes": reference_packet_bytes,
        "queue_limit_per_priority_packets": (
            LINUX_DUALPI2_LIMIT_PACKETS
        ),
        "dualpi2_parameter_order": list(DUALPI2_PARAMETER_ORDER),
        "dualpi2_profile": dict(dualpi2_profile),
    }


def change_p4_bottleneck_rate(
    switch_config: Dict[str, object],
    *,
    bandwidth_mbit: float,
    reference_packet_bytes: int,
) -> int:
    """Change queue rates on both inter-switch egress ports."""
    rate_pps = mbit_to_bmv2_pps(
        bandwidth_mbit,
        reference_packet_bytes,
    )

    switches = switch_config["switches"]
    for switch_name in SWITCH_NAMES:
        data = switches[switch_name]
        output = run_simple_switch_cli(
            int(data["thrift_port"]),
            [
                (
                    f"set_queue_rate {rate_pps} "
                    f"{int(data['bottleneck_egress_port'])}"
                )
            ],
        )
        if output:
            print(
                f">>> [P4] {switch_name} rate update output\n"
                f"{output}"
            )

    return rate_pps


def install_static_arp(clients, server) -> None:
    server_ip = server.IP()
    server_mac = server.MAC()
    for client in clients:
        client.cmd(f"arp -s {server_ip} {server_mac} || true")
        server.cmd(f"arp -s {client.IP()} {client.MAC()} || true")


def _network_namespace_id(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/ns/net")


def _matching_browser_pids(hosts: Iterable) -> List[int]:
    namespace_ids = {
        _network_namespace_id(int(host.pid))
        for host in hosts
    }
    matches: List[int] = []

    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue

        pid = int(proc_entry.name)
        try:
            namespace_id = os.readlink(proc_entry / "ns/net")
            if namespace_id not in namespace_ids:
                continue

            cmdline = (proc_entry / "cmdline").read_bytes()
            command = cmdline.replace(b"\x00", b" ").decode(
                "utf-8",
                errors="ignore",
            ).lower()

            if any(term in command for term in BROWSER_PROCESS_TERMS):
                matches.append(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue

    return matches


def cleanup_browser_processes(hosts: Iterable, *, settle_seconds: float = 0.5) -> None:
    hosts = list(hosts)
    if not hosts:
        return

    pids = _matching_browser_pids(hosts)
    if not pids:
        return

    print(f">>> [P4] Cleaning stale browser processes: {pids}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    time.sleep(settle_seconds)

    for pid in _matching_browser_pids(hosts):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def read_tx_bytes(node, device: str) -> int:
    output = node.cmd(
        f"cat /sys/class/net/{device}/statistics/tx_bytes 2>/dev/null"
    ).strip()
    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Could not read tx_bytes for {node.name}:{device}: "
            f"{output!r}"
        ) from exc


def start_per_client_bootstrap_transition(
    *,
    s1,
    client_devices: Iterable[str],
    bytes_per_client: int,
    max_wait_seconds: float,
    transition: Callable[[], None],
) -> Tuple[threading.Thread, Dict[str, object], threading.Event]:
    devices = tuple(client_devices)
    if not devices:
        raise ValueError("client_devices must not be empty")

    baselines = {
        device: read_tx_bytes(s1, device)
        for device in devices
    }
    completed = threading.Event()
    transition_lock = threading.Lock()

    state: Dict[str, object] = {
        "trigger": "pending",
        "bytes_per_client_threshold": bytes_per_client,
        "max_wait_seconds": max_wait_seconds,
        "delivered_bytes_per_client": {
            device: 0 for device in devices
        },
        "elapsed_seconds": 0.0,
        "rate_transition_completed": False,
        "error": None,
    }

    def transition_once() -> None:
        with transition_lock:
            if completed.is_set():
                return
            transition()
            state["rate_transition_completed"] = True
            completed.set()

    def worker() -> None:
        started = time.monotonic()
        try:
            while True:
                delivered = {
                    device: max(
                        0,
                        read_tx_bytes(s1, device) - baselines[device],
                    )
                    for device in devices
                }
                elapsed = time.monotonic() - started

                state["delivered_bytes_per_client"] = delivered
                state["elapsed_seconds"] = round(elapsed, 3)

                if all(
                    value >= bytes_per_client
                    for value in delivered.values()
                ):
                    state["trigger"] = "all_clients_reached_byte_threshold"
                    break

                if elapsed >= max_wait_seconds:
                    state["trigger"] = "timeout"
                    break

                time.sleep(0.1)

            transition_once()
            print(
                ">>> [P4] Switched to measurement rate after "
                f"{state['elapsed_seconds']} s ({state['trigger']})"
            )
        except Exception as exc:  # noqa: BLE001
            state["error"] = repr(exc)
            state["trigger"] = "watcher_error"
            try:
                transition_once()
            finally:
                print(f">>> [P4] Bootstrap watcher failed: {exc}")

    thread = threading.Thread(
        target=worker,
        name="p4-bootstrap",
        daemon=True,
    )
    thread.start()
    return thread, state, completed


def ensure_transition(
    *,
    thread: threading.Thread,
    completed: threading.Event,
    transition: Callable[[], None],
) -> None:
    if not completed.is_set():
        transition()
        completed.set()
    thread.join(timeout=1.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a two-switch P4/BMv2 DualPI2 experiment with "
            "per-client browser bootstrap"
        )
    )
    parser.add_argument("--num_clients", type=int, default=2)
    parser.add_argument(
        "--p4src",
        default=P4_PROGRAM_DEFAULT,
    )
    parser.add_argument(
        "--reference_packet_bytes",
        type=int,
        default=DEFAULT_REFERENCE_PACKET_BYTES,
    )
    parser.add_argument(
        "--bootstrap_bw_mbit",
        type=float,
        default=BOOTSTRAP_BW_MBIT,
    )
    parser.add_argument(
        "--bootstrap_bytes_per_client",
        type=int,
        default=DEFAULT_BOOTSTRAP_BYTES_PER_CLIENT,
    )
    parser.add_argument(
        "--bootstrap_max_wait_seconds",
        type=float,
        default=DEFAULT_BOOTSTRAP_MAX_WAIT_SECONDS,
    )
    args = parser.parse_args()

    if args.num_clients < 1:
        parser.error("--num_clients must be at least 1")
    if args.reference_packet_bytes < 1:
        parser.error("--reference_packet_bytes must be positive")
    if args.bootstrap_bw_mbit <= BW_MBIT:
        parser.error("--bootstrap_bw_mbit must be greater than BW_MBIT")
    if args.bootstrap_bytes_per_client <= 0:
        parser.error("--bootstrap_bytes_per_client must be positive")
    if args.bootstrap_max_wait_seconds <= 0:
        parser.error("--bootstrap_max_wait_seconds must be positive")

    setLogLevel("info")

    print(
        f">>> [P4] Building two-switch network with {args.p4src} "
        f"and {args.num_clients} clients"
    )
    net_api, mn, clients, server, s1, s2 = build_p4_net(
        num_clients=args.num_clients,
        p4src=args.p4src,
    )

    try:
        switch_config = program_p4_switches(
            clients,
            server,
            initial_rate_mbit=args.bootstrap_bw_mbit,
            reference_packet_bytes=args.reference_packet_bytes,
        )

        print(
            ">>> [P4] Endpoint tuning on clients and server "
            "(ECN + Prague + fq)"
        )
        for client in clients:
            endpoint_tuning(client)
        endpoint_tuning(server)

        print(
            f">>> [P4] Adding access delays: "
            f"{DELAY_MS_ONEWAY} ms one-way at hosts"
        )
        add_access_delays(DELAY_MS_ONEWAY, server, *clients)

        print(">>> [P4] Installing static ARP entries")
        install_static_arp(clients, server)

        print(">>> [P4] Sanity ping over two-switch dataplane")
        info(clients[0].cmd(f"ping -c 3 {server.IP()}"))

        cleanup_browser_processes(clients)

        p4_dir = create_experiment_directory("p4_l4s")
        config: Dict[str, object] = {
            "bundle_version": BUNDLE_VERSION,
            "experiment_type": "p4_bottleneck",
            "mode_name": "p4_l4s",
            "num_clients": args.num_clients,
            "p4_program": args.p4src,
            "topology": "clients--s1--s2--server",
            "bottleneck_link": "s1<->s2",
            "delay_ms_oneway": DELAY_MS_ONEWAY,
            "rtt_baseline_ms": DELAY_MS_ONEWAY * 2,
            "tcp_congestion_control": "prague",
            "tcp_ecn": "enabled",
            "browser_process_cleanup": True,
            "bootstrap": {
                "rate_mbit_each_direction": args.bootstrap_bw_mbit,
                "bytes_per_client": args.bootstrap_bytes_per_client,
                "max_wait_seconds": args.bootstrap_max_wait_seconds,
                "client_delivery_devices": [
                    f"s1:s1-eth{index}"
                    for index in range(1, args.num_clients + 1)
                ],
                "result": None,
            },
            "linux_reference": {
                "qdisc": "dualpi2",
                "target": "5ms",
                "limit_packets_shared": (
                    LINUX_DUALPI2_LIMIT_PACKETS
                ),
                "measurement_rate_mbit_each_direction": BW_MBIT,
            },
            "p4_bmv2_configuration": switch_config,
            "known_non_equivalences": [
                (
                    "BMv2 queue rate is packets/s; the bit rate is exact "
                    "only for the configured reference packet size."
                ),
                (
                    "Linux uses one shared DualPI2 limit; stock BMv2 "
                    "applies the limit separately to each priority queue."
                ),
                (
                    "BMv2 strict priority does not implement Linux "
                    "DualPI2 classic-protection scheduling."
                ),
                (
                    "The P4 PI update is packet-triggered rather than "
                    "driven by a true periodic timer."
                ),
            ],
        }
        config_path = os.path.join(
            p4_dir,
            "experiment_config.json",
        )
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, indent=2)
        print(f">>> [P4] Config saved to: {config_path}")

        def transition() -> None:
            change_p4_bottleneck_rate(
                switch_config,
                bandwidth_mbit=BW_MBIT,
                reference_packet_bytes=args.reference_packet_bytes,
            )

        watcher, bootstrap_state, completed = (
            start_per_client_bootstrap_transition(
                s1=s1,
                client_devices=(
                    f"s1-eth{index}"
                    for index in range(1, args.num_clients + 1)
                ),
                bytes_per_client=args.bootstrap_bytes_per_client,
                max_wait_seconds=args.bootstrap_max_wait_seconds,
                transition=transition,
            )
        )

        print(">>> [P4] Starting DASH trial")
        try:
            run_trial(
                clients,
                server,
                bottleneck_node=None,
                bottleneck_dev="",
                exp_dir=p4_dir,
            )
        finally:
            ensure_transition(
                thread=watcher,
                completed=completed,
                transition=transition,
            )
            cleanup_browser_processes(clients)

            config["bootstrap"]["result"] = bootstrap_state
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file, indent=2)

        print(">>> [P4] Trial complete")
        for index in range(1, args.num_clients + 1):
            print(f"      {p4_dir}/client{index}/")

        input(">>> [P4] Press Enter to stop the P4 network...")
    finally:
        cleanup_browser_processes(clients)
        print(">>> [P4] Stopping P4 network")
        mn.stop()


if __name__ == "__main__":
    main()
