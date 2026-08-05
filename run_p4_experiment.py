#!/usr/bin/env python3

# run_p4_experiment.py

import subprocess
import json
import os
import argparse

from mininet.log import setLogLevel, info

from common import (
    endpoint_tuning,
    add_access_delays,
    run_trial,
    create_experiment_directory,
    get_server_ip,
)
from linux_bottleneck import DELAY_MS_ONEWAY
from p4_bottleneck import build_p4_net
from p4utils.utils.helper import load_topo

DEFAULT_DUALPI2_PARAMS = (
    687,          # alpha = approximately 0.16 Hz in Q0.32/us
    13744,        # beta  = approximately 3.2 Hz in Q0.32/us
    15000,        # Classic target: 15 ms
    14,           # update interval: 2^14 us = 16.384 ms
    2,            # coupling factor k
    2147483647,   # overload threshold: approximately 0.5 in Q0.32
    1,            # drop during overload
    800,          # Native L4S ramp begins at 0.8 ms
    400,          # Native L4S ramp width: 0.4 ms
    10737418,     # floor(0xffffffff / 400)
    1,            # Native L4S minimum queue: 1 packet
    2,            # Coupled AQM minimum backlog: 2 packets
    65536,        # Per-queue observation timeout
    1000000,      # Reset controller after 1 second idle
)


def program_p4_switch(
    net_api,
    clients,
    server,
    num_clients,
    table_only=False,
    dualpi2_params=DEFAULT_DUALPI2_PARAMS,
):
    """
    Program s1 using the action signatures in dualpi2_repaired_v1.1.0.p4.

    MyIngress.ipv4_forward parameters:
        switch_source_mac, destination_host_mac, egress_port

    MyEgress.dualpi2 parameters:
        the 14 values documented in DEFAULT_DUALPI2_PARAMS
    """
    del net_api, num_clients, table_only  # Kept for caller compatibility.

    if len(dualpi2_params) != 14:
        raise ValueError(
            f"dualpi2_params must contain exactly 14 values; "
            f"received {len(dualpi2_params)}"
        )

    topo = load_topo("topology.json")
    switch_name = "s1"
    thrift_port = topo.get_thrift_port(switch_name)

    print(f">>> [P4] Programming {switch_name} on Thrift port {thrift_port}")

    commands = [
        "table_clear MyIngress.ipv4_lpm",
    ]

    endpoints = [*clients, server]

    for endpoint in endpoints:
        endpoint_name = endpoint.name
        endpoint_ip = endpoint.IP()
        endpoint_mac = endpoint.MAC()

        # Source MAC required by the repaired three-parameter forwarding action.
        switch_mac = topo.node_to_node_mac(switch_name, endpoint_name)
        egress_port = topo.node_to_node_port_num(switch_name, endpoint_name)

        if not switch_mac:
            raise RuntimeError(
                f"No MAC is assigned to the {switch_name} interface facing "
                f"{endpoint_name}. Configure switch-interface MAC addresses "
                f"in the P4-Utils topology."
            )

        print(
            f"    {endpoint_name}: IP={endpoint_ip}, "
            f"host MAC={endpoint_mac}, switch MAC={switch_mac}, "
            f"port={egress_port}"
        )

        commands.append(
            "table_add MyIngress.ipv4_lpm "
            "MyIngress.ipv4_forward "
            f"{endpoint_ip}/32 => "
            f"{switch_mac} {endpoint_mac} {egress_port}"
        )

    # Clear stale per-port overrides. The P4 program already has the same
    # parameters as its default action, but installing explicit entries makes
    # the experiment configuration visible and reproducible.
    commands.append("table_clear MyEgress.aqm")

    aqm_arg_string = " ".join(str(value) for value in dualpi2_params)

    for endpoint in endpoints:
        egress_port = topo.node_to_node_port_num(switch_name, endpoint.name)
        commands.append(
            "table_add MyEgress.aqm "
            "MyEgress.dualpi2 "
            f"{egress_port} => {aqm_arg_string}"
        )

    cli_input = "\n".join(commands) + "\n"

    result = subprocess.run(
        ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=cli_input,
        text=True,
        capture_output=True,
        check=False,
    )

    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )

    # simple_switch_CLI may report RuntimeCmd errors while exiting with code 0.
    cli_failed = (
        result.returncode != 0
        or "RuntimeCmd: Error:" in output
        or "\nError:" in output
    )

    if cli_failed:
        print(">>> [P4] ERROR programming switch:")
        print(output)
        print(">>> [P4] Commands sent to simple_switch_CLI:")
        print(cli_input)
        raise RuntimeError("Failed to program P4 switch")

    if output:
        print(">>> [P4] simple_switch_CLI output:")
        print(output)

def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Run P4 bottleneck experiment with multiple clients")
    parser.add_argument('--num_clients', type=int, default=2, help="Number of clients (default: 2)")
    args = parser.parse_args()
    
    num_clients = args.num_clients
    setLogLevel('info')

    print(f">>> [P4] Building P4/BMv2 network with aqm.p4 and {num_clients} clients")
    net_api, mn, clients, server, s1 = build_p4_net(num_clients=num_clients, p4src="aqm.p4")

    print(f">>> [P4] Building P4/BMv2 network with aqm.p4 and {num_clients} clients")
    net_api, mn, clients, server, s1 = build_p4_net(num_clients=num_clients, p4src="aqm.p4")

    try:
        # 1) Program the P4 switch tables based on real IP/MACs
        program_p4_switch(net_api, clients, server, num_clients)

        # 2) Host-side tuning and delays (same as Linux experiment)
        print(">>> [P4] Endpoint tuning on clients and server (ECN + Prague + fq)")
        for client in clients:
            endpoint_tuning(client)
        endpoint_tuning(server)

        print(f">>> [P4] Adding access delays: {DELAY_MS_ONEWAY} ms one-way at hosts")
        add_access_delays(DELAY_MS_ONEWAY, server, *clients)

        # 3) Static ARP so hosts know each other's MAC
        print(">>> [P4] Installing static ARP entries")
        server_ip = server.IP()
        server_mac = server.MAC()
        
        for client in clients:
            client.cmd(f"arp -s {server_ip} {server_mac} || true")
        
        for i, client in enumerate(clients, start=1):
            client_ip = client.IP()
            client_mac = client.MAC()
            server.cmd(f"arp -s {client_ip} {client_mac} || true")

        # 4) Sanity ping
        print(">>> [P4] Sanity ping over P4/PI2 dataplane")
        ping_out = clients[0].cmd(f"ping -c 3 {server_ip}")
        info(ping_out)

        # 5) Create experiment directory and config
        p4_dir = create_experiment_directory("p4_pi2")
        p4_config = {
            "experiment_type": "p4_bottleneck",
            "mode_name": "p4_pi2",
            "num_clients": num_clients,
            "p4_program": "aqm.p4",
            "qdisc": "dualpi2_p4",
            "bottleneck_device": "s1 (P4 switch)",
            "delay_ms_oneway": DELAY_MS_ONEWAY,
            "rtt_baseline_ms": DELAY_MS_ONEWAY * 2,
            "tcp_congestion_control": "prague",
            "tcp_ecn": "enabled",
            "p4_dualpi2_params": {
                "target_queue_depth": 100,
                "k_coef": 1000,
                "max_queue": 20000,
                "target_delay": 15,
                "priority_queues": 2,
                "note": "Parameters set via table_add MyEgress.aqm dualpi2"
            }
        }
        
        with open(os.path.join(p4_dir, "experiment_config.json"), "w") as f:
            json.dump(p4_config, f, indent=2)
        
        print(f">>> [P4] Config saved to: {p4_dir}/experiment_config.json")

        # 6) Full DASH trial with multiple clients
        print(">>> [P4] Starting DASH streaming trial under P4 PI2 bottleneck")
        run_trial(
            clients,
            server,
            bottleneck_node=None,
            bottleneck_dev="",
            exp_dir=p4_dir,
            parallel=4,
            seconds=5,
        )

        print(">>> [P4] Trial complete. Logs in client subdirectories:")
        for i in range(1, num_clients + 1):
            print(f"      {p4_dir}/client{i}/")

        input(">>> [P4] Press Enter to stop the P4 network...")

    finally:
        print(">>> [P4] Stopping P4 network")
        mn.stop()


if __name__ == "__main__":
    main()
