#!/usr/bin/env python3
"""Run two-switch Linux DualPI2 and FIFO experiments.

Experiment bundle version: 1.3.1

The application bootstrap and media use the same inter-switch path. At
0.3 Mbit/s, loading two copies of dash.js can consume most of a short trial.
Each mode therefore starts at a temporary bootstrap rate. The runner changes
both inter-switch directions to the measurement rate only after every client
has received a configurable number of bytes.

The runner also terminates Chrome/Chromium and chromedriver processes in each
client network namespace before and after every mode. This prevents a browser
from the L4S trial from continuing to request media during the Classic trial.
"""

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Tuple

from mininet.cli import CLI
from mininet.log import info, setLogLevel

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
    DUALPI2_LIMIT_PACKETS,
    DUALPI2_TARGET,
    FIFO_LIMIT_PACKETS,
    build_linux_net,
    change_bottleneck_rate,
    clear_all_qdisc,
    get_interswitch_devices,
    set_bottleneck_dualpi2,
    set_bottleneck_fifo,
)


BUNDLE_VERSION = "1.3.1"
DEFAULT_BOOTSTRAP_BYTES_PER_CLIENT = 1_000_000
DEFAULT_BOOTSTRAP_MAX_WAIT_SECONDS = 30.0
BROWSER_PROCESS_TERMS = (
    "chromedriver",
    "google-chrome",
    "chrome",
    "chromium",
)


def write_config(directory: str, config: dict) -> str:
    path = os.path.join(directory, "experiment_config.json")
    with open(path, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2)
    info(f"*** Config saved to: {path}\n")
    return path


def _network_namespace_id(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/ns/net")


def _matching_browser_pids(hosts: Iterable) -> List[int]:
    """Find browser processes that belong to the supplied Mininet hosts."""
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
    """Stop stale browser processes without touching the desktop namespace."""
    hosts = list(hosts)
    if not hosts:
        return

    pids = _matching_browser_pids(hosts)
    if not pids:
        return

    info(f"*** Cleaning stale browser processes: {pids}\n")
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
    label: str,
) -> Tuple[threading.Thread, Dict[str, object], threading.Event]:
    """Switch rates after every client receives enough downstream bytes."""
    devices = tuple(client_devices)
    if not devices:
        raise ValueError("client_devices must not be empty")
    if bytes_per_client <= 0:
        raise ValueError("bytes_per_client must be positive")
    if max_wait_seconds <= 0:
        raise ValueError("max_wait_seconds must be positive")

    baselines = {
        device: read_tx_bytes(s1, device)
        for device in devices
    }
    completed = threading.Event()
    transition_lock = threading.Lock()

    state: Dict[str, object] = {
        "label": label,
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
            info(
                f"*** {label}: switched to measurement rate "
                f"after {state['elapsed_seconds']} s "
                f"({state['trigger']})\n"
            )
        except Exception as exc:  # noqa: BLE001
            state["error"] = repr(exc)
            state["trigger"] = "watcher_error"
            try:
                transition_once()
            finally:
                info(f"*** {label}: bootstrap watcher failed: {exc}\n")

    thread = threading.Thread(
        target=worker,
        name=f"{label}-bootstrap",
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
    """Guarantee the measurement rate is installed when a trial exits."""
    if not completed.is_set():
        transition()
        completed.set()
    thread.join(timeout=1.0)


def run_mode(
    *,
    mode_name: str,
    qdisc_name: str,
    clients,
    server,
    s1,
    s2,
    s1_to_s2_device: str,
    s2_to_s1_device: str,
    monitored_node,
    monitored_device: str,
    bootstrap_rate_mbit: float,
    bootstrap_bytes_per_client: int,
    bootstrap_max_wait_seconds: float,
    configure_qdisc: Callable[..., None],
    qdisc_params: Mapping[str, object],
) -> str:
    """Configure one mode, clean players, bootstrap, and run the trial."""
    cleanup_browser_processes(clients)

    configure_qdisc(
        s1,
        s1_to_s2_device,
        s2,
        s2_to_s1_device,
        rate_mbit=bootstrap_rate_mbit,
    )

    info(s1.cmd(
        f"tc qdisc show dev {s1_to_s2_device} | sed -n '1,4p'"
    ))
    info(s2.cmd(
        f"tc qdisc show dev {s2_to_s1_device} | sed -n '1,4p'"
    ))

    experiment_dir = create_experiment_directory(mode_name)
    config: Dict[str, object] = {
        "bundle_version": BUNDLE_VERSION,
        "experiment_type": "linux_bottleneck",
        "mode_name": mode_name,
        "topology": "clients--s1--s2--server",
        "num_clients": len(clients),
        "qdisc": qdisc_name,
        "bottleneck_link": "s1<->s2",
        "bottleneck_egress_devices": {
            "client_to_server": f"s1:{s1_to_s2_device}",
            "server_to_client": f"s2:{s2_to_s1_device}",
        },
        "monitored_downstream_device": (
            f"{monitored_node.name}:{monitored_device}"
        ),
        "measurement_bandwidth_mbit_each_direction": BW_MBIT,
        "delay_ms_oneway": DELAY_MS_ONEWAY,
        "rtt_baseline_ms": DELAY_MS_ONEWAY * 2,
        "tcp_congestion_control": "prague",
        "tcp_ecn": "enabled",
        "browser_process_cleanup_between_modes": True,
        "bootstrap": {
            "rate_mbit_each_direction": bootstrap_rate_mbit,
            "bytes_per_client": bootstrap_bytes_per_client,
            "max_wait_seconds": bootstrap_max_wait_seconds,
            "client_delivery_devices": [
                f"s1:s1-eth{index}"
                for index in range(1, len(clients) + 1)
            ],
            "result": None,
        },
        f"{qdisc_name}_params": dict(qdisc_params),
    }
    config_path = write_config(experiment_dir, config)

    def transition() -> None:
        change_bottleneck_rate(
            s1,
            s1_to_s2_device,
            s2,
            s2_to_s1_device,
            rate_mbit=BW_MBIT,
        )

    watcher, bootstrap_state, completed = (
        start_per_client_bootstrap_transition(
            s1=s1,
            client_devices=(
                f"s1-eth{index}"
                for index in range(1, len(clients) + 1)
            ),
            bytes_per_client=bootstrap_bytes_per_client,
            max_wait_seconds=bootstrap_max_wait_seconds,
            transition=transition,
            label=mode_name,
        )
    )

    try:
        run_trial(
            clients,
            server,
            bottleneck_node=monitored_node,
            bottleneck_dev=monitored_device,
            exp_dir=experiment_dir,
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

    return experiment_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run Linux experiments with a shared two-switch bottleneck "
            "and a per-client browser bootstrap phase"
        )
    )
    parser.add_argument("--num_clients", type=int, default=2)
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
    if args.bootstrap_bw_mbit <= BW_MBIT:
        parser.error("--bootstrap_bw_mbit must be greater than BW_MBIT")
    if args.bootstrap_bytes_per_client <= 0:
        parser.error("--bootstrap_bytes_per_client must be positive")
    if args.bootstrap_max_wait_seconds <= 0:
        parser.error("--bootstrap_max_wait_seconds must be positive")

    setLogLevel("info")

    net, clients, server, s1, s2 = build_linux_net(
        num_clients=args.num_clients
    )
    s1_to_s2_device, s2_to_s1_device = get_interswitch_devices(
        args.num_clients
    )

    try:
        clear_all_qdisc(s1, s2, server, *clients)

        for client in clients:
            endpoint_tuning(client)
        endpoint_tuning(server)

        add_access_delays(DELAY_MS_ONEWAY, server, *clients)
        info(net.pingFull()[0])

        info("*** L4S: symmetric HTB + DualPI2 bottleneck\n")
        run_mode(
            mode_name="l4s",
            qdisc_name="dualpi2",
            clients=clients,
            server=server,
            s1=s1,
            s2=s2,
            s1_to_s2_device=s1_to_s2_device,
            s2_to_s1_device=s2_to_s1_device,
            monitored_node=s2,
            monitored_device=s2_to_s1_device,
            bootstrap_rate_mbit=args.bootstrap_bw_mbit,
            bootstrap_bytes_per_client=args.bootstrap_bytes_per_client,
            bootstrap_max_wait_seconds=args.bootstrap_max_wait_seconds,
            configure_qdisc=set_bottleneck_dualpi2,
            qdisc_params={
                "target": DUALPI2_TARGET,
                "limit_packets": DUALPI2_LIMIT_PACKETS,
            },
        )

        info("*** Classic: symmetric HTB + pfifo bottleneck\n")
        run_mode(
            mode_name="classic",
            qdisc_name="pfifo",
            clients=clients,
            server=server,
            s1=s1,
            s2=s2,
            s1_to_s2_device=s1_to_s2_device,
            s2_to_s1_device=s2_to_s1_device,
            monitored_node=s2,
            monitored_device=s2_to_s1_device,
            bootstrap_rate_mbit=args.bootstrap_bw_mbit,
            bootstrap_bytes_per_client=args.bootstrap_bytes_per_client,
            bootstrap_max_wait_seconds=args.bootstrap_max_wait_seconds,
            configure_qdisc=set_bottleneck_fifo,
            qdisc_params={
                "limit_packets": FIFO_LIMIT_PACKETS,
            },
        )

        info("*** Experiment complete\n")
        CLI(net)
    finally:
        cleanup_browser_processes(clients)
        net.stop()


if __name__ == "__main__":
    main()
