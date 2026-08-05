# run_linux_experiment.py

#!/usr/bin/env python3

from mininet.log import setLogLevel, info
from mininet.cli import CLI
import json
import os
import argparse

from common import (
    endpoint_tuning,
    add_access_delays,
    run_trial,
    create_experiment_directory,
    get_server_ip,
)
from linux_bottleneck import (
    build_linux_net,
    clear_all_qdisc,
    set_bottleneck_dualpi2,
    set_bottleneck_fifo,
    DELAY_MS_ONEWAY,
    BW_MBIT,
)


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Run Linux bottleneck experiment with multiple clients")
    parser.add_argument('--num_clients', type=int, default=2, help="Number of clients (default: 2)")
    args = parser.parse_args()
    
    num_clients = args.num_clients
    setLogLevel('info')

    # Build topology with n clients and 1 server
    net, clients, server, s1 = build_linux_net(num_clients=num_clients)

    try:
        # Endpoint tuning + clean qdiscs
        for client in clients:
            endpoint_tuning(client)
        endpoint_tuning(server)
        clear_all_qdisc(s1, server, *clients)

        # Baseline RTT via access netem
        # Note: bottleneck is on the link to the server
        add_access_delays(DELAY_MS_ONEWAY, server, *clients)

        # Optional: sanity ping
        info(net.pingFull()[0])

        # Determine bottleneck port (last port connected to server)
        # In our topology: clients on eth1..eth{num_clients}, server on eth{num_clients+1}
        bottleneck_port = f"s1-eth{num_clients + 1}"

        # --- L4S (DualPI2 bottleneck) ---
        info("*** L4S: HTB + DualPI2 at bottleneck\n")
        set_bottleneck_dualpi2(s1, bottleneck_port)
        info(s1.cmd(f"tc qdisc show dev {bottleneck_port} | sed -n '1,3p'"))
        
        # Create experiment directory and config for L4S
        l4s_dir = create_experiment_directory("l4s")
        l4s_config = {
            "experiment_type": "linux_bottleneck",
            "mode_name": "l4s",
            "num_clients": num_clients,
            "qdisc": "dualpi2",
            "bottleneck_device": bottleneck_port,
            "bottleneck_bandwidth_mbit": BW_MBIT,
            "delay_ms_oneway": DELAY_MS_ONEWAY,
            "rtt_baseline_ms": DELAY_MS_ONEWAY * 2,
            "tcp_congestion_control": "prague",
            "tcp_ecn": "enabled",
            "dualpi2_params": {
                "target": "5ms",
                "limit": 10000
            }
        }
        
        with open(os.path.join(l4s_dir, "experiment_config.json"), "w") as f:
            json.dump(l4s_config, f, indent=2)
        
        info(f"*** L4S config saved to: {l4s_dir}/experiment_config.json\n")

        run_trial(clients, server, bottleneck_node=s1,
                  bottleneck_dev=bottleneck_port, exp_dir=l4s_dir)

        # --- Classic (FIFO bottleneck) ---
        info("*** Classic: HTB + pfifo at bottleneck\n")
        set_bottleneck_fifo(s1, bottleneck_port)
        info(s1.cmd(f"tc qdisc show dev {bottleneck_port} | sed -n '1,3p'"))
        
        # Create experiment directory and config for Classic
        classic_dir = create_experiment_directory("classic")
        classic_config = {
            "experiment_type": "linux_bottleneck",
            "mode_name": "classic",
            "num_clients": num_clients,
            "qdisc": "pfifo",
            "bottleneck_device": bottleneck_port,
            "bottleneck_bandwidth_mbit": BW_MBIT,
            "delay_ms_oneway": DELAY_MS_ONEWAY,
            "rtt_baseline_ms": DELAY_MS_ONEWAY * 2,
            "tcp_congestion_control": "prague",
            "tcp_ecn": "enabled",
            "pfifo_params": {
                "limit": 1000
            }
        }
        
        with open(os.path.join(classic_dir, "experiment_config.json"), "w") as f:
            json.dump(classic_config, f, indent=2)
        
        info(f"*** Classic config saved to: {classic_dir}/experiment_config.json\n")

        run_trial(clients, server, bottleneck_node=s1,
                  bottleneck_dev=bottleneck_port, exp_dir=classic_dir)

        info("*** Experiment complete! Client-specific logs in subdirectories.\n")
        CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
