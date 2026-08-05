# common.py

from mininet.log import info
from time import sleep
from datetime import datetime
import os

def get_server_ip(num_clients):
    """Get server IP based on number of clients."""
    return f"10.0.0.{num_clients + 1}"


def create_experiment_directory(mode_name: str, base_path: str = "experiments") -> str:
    """
    Create a directory for the experiment based on timestamp and mode name.
    
    Args:
        mode_name: Name of the experiment mode (e.g., 'dualpi2', 'linux')
        base_path: Base directory path for experiments
    
    Returns:
        str: Full path to the created directory
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_name = f"{timestamp}_{mode_name}"
    full_path = os.path.join(base_path, dir_name)
    os.makedirs(full_path, exist_ok=True)
    info(f"*** Created experiment directory: {full_path}\n")
    return full_path


def sh(node, cmd: str) -> str:
    """Run a shell command on a Mininet node and return stdout."""
    return node.cmd(cmd)


def endpoint_tuning(h):
    """Enable ECN + Prague + fq at the host."""
    sh(h, "sysctl -w net.ipv4.tcp_ecn=1 >/dev/null")
    sh(h, "sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null")
    sh(h, f"tc qdisc replace dev {h.name}-eth0 root fq >/dev/null 2>&1 || true")


def add_access_delays(delay_ms: int, *hosts):
    """Add symmetric one-way delay at the endpoints using netem."""
    for h in hosts:
        sh(h, f"tc qdisc replace dev {h.name}-eth0 root netem delay {delay_ms}ms")


def kill_flows(*hosts):
    """Kill any leftover iperf3 / ping flows on the given hosts."""
    for h in hosts:
        sh(h, "pkill -f iperf3 >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'python3 server.py' >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'python3 host-applications/run_client.py' >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'python3 run_client.py' >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'ping -i' >/dev/null 2>&1 || true")


def run_iperf_trial(clients, server, bottleneck_node, bottleneck_dev: str,
              mode_name: str, parallel: int = 4, seconds: int = 5):
    """
    Run one experiment trial:
      - iperf3 from each client -> server
      - ping from each client -> server
      - optionally dump qdisc stats on the bottleneck_dev
    """
    server_ip = server.IP()
    kill_flows(server, *clients)

    # Start iperf3 server on server host
    sh(server, "iperf3 -s > /tmp/iperf_server.log 2>&1 &")
    sleep(0.2)

    # Start ping and iperf3 on each client
    for i, client in enumerate(clients, start=1):
        ping_log = f"/tmp/ping_{mode_name}_client{i}.log"
        sh(client, f"ping -i 0.2 {server_ip} > {ping_log} 2>&1 &")
    
    sleep(0.2)

    # Run iperf3 clients
    for i, client in enumerate(clients, start=1):
        info(f"*** iperf3 {mode_name} client{i}: -P {parallel} -t {seconds}\n")
        out = sh(client, f"iperf3 -c {server_ip} -P {parallel} -t {seconds}")
        info(out)

    # Stop all pings
    for client in clients:
        sh(client, f"pkill -f 'ping -i 0.2 {server_ip}'")

    # Show ping tail for each client
    for i in range(1, len(clients) + 1):
        ping_log = f"/tmp/ping_{mode_name}_client{i}.log"
        info(f"*** {mode_name} client{i} ping tail (last 50):\n")
        info(sh(clients[i-1], f"tail -n 50 {ping_log}"))

    # Optional qdisc stats at bottleneck
    if bottleneck_node is not None and bottleneck_dev:
        info(f"*** {mode_name} qdisc {bottleneck_dev}:\n")
        qdisc_cmd = f"tc -s qdisc show dev {bottleneck_dev} | sed -n '1,80p'"
        info(sh(bottleneck_node, qdisc_cmd))

    # Socket state on clients
    for i, client in enumerate(clients, start=1):
        info(f"*** {mode_name} sockets (client{i}):\n")
        ss_cmd = "ss -ti '( dport = :5201 )' | sed -n '1,8p'"
        info(sh(client, ss_cmd))


def run_trial(clients, server, bottleneck_node, bottleneck_dev: str,
              exp_dir: str, parallel: int = 4, seconds: int = 5):
    """
    Run DASH video streaming experiment with multiple clients.
    Each client gets its own subdirectory in exp_dir.
    """
    info("*** Starting multi-client DASH experiment\n")
    server_ip = server.IP()
    kill_flows(server, *clients)
    
    # Start server
    sh(server, "cd host-applications && python3 server.py 8123 2>&1 > /tmp/server_log &")
    sleep(1)

    # Start each client with its own log directory
    for i, client in enumerate(clients, start=1):
        client_dir = os.path.join(exp_dir, f"client{i}")
        os.makedirs(client_dir, exist_ok=True)
        info(f"*** Starting client{i} logging to {client_dir}\n")
        sh(client, f"cd host-applications && python3 run_client.py --runs 1 --srv_addr {server_ip} --log_dir ../{client_dir} 2>&1 > /tmp/client{i}_log &")
    
    sleep_duration = 100
    info(f"*** Waiting for clients to complete ({sleep_duration}s)\n")
    sleep(sleep_duration)
    
    # Show logs
    for i in range(1, len(clients) + 1):
        info(f"*** Client{i} log:\n")
        info(sh(clients[i-1], f'cat /tmp/client{i}_log'))
    
    info("*** Server log:\n")
    info(sh(server, 'cat /tmp/server_log'))
    info(f"*** Experiment logs saved to: {exp_dir}\n")
