# common.py

from mininet.log import info
from time import sleep

IP_H2 = "10.0.0.2"


def sh(node, cmd: str) -> str:
    """Run a shell command on a Mininet node and return stdout."""
    return node.cmd(cmd)


def endpoint_tuning(h):
    """Enable ECN + Prague + fq at the host."""
    sh(h, "sysctl -w net.ipv4.tcp_ecn=1 >/dev/null")
    sh(h, "sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null")
    sh(h, f"tc qdisc replace dev {h.name}-eth0 root fq >/dev/null 2>&1 || true")


def add_access_delays(h1, h2, delay_ms: int):
    """Add symmetric one-way delay at the endpoints using netem."""
    sh(h1, f"tc qdisc replace dev {h1.name}-eth0 root netem delay {delay_ms}ms")
    sh(h2, f"tc qdisc replace dev {h2.name}-eth0 root netem delay {delay_ms}ms")


def kill_flows(*hosts):
    """Kill any leftover iperf3 / ping flows on the given hosts."""
    for h in hosts:
        sh(h, "pkill -f iperf3 >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'python3 server.py' >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'python3 host-applications/client.py' >/dev/null 2>&1 || true")
        sh(h, "pkill -f 'ping -i' >/dev/null 2>&1 || true")


def run_iperf_trial(h1, h2, bottleneck_node, bottleneck_dev: str,
              mode_name: str, parallel: int = 4, seconds: int = 5):
    """
    Run one experiment trial:
      - iperf3 from h1 -> h2
      - ping from h1 -> h2
      - optionally dump qdisc stats on the bottleneck_dev
    """
    kill_flows(h1, h2)

    # Start iperf3 server on h2
    sh(h2, "iperf3 -s > /tmp/iperf_server.log 2>&1 &")
    sleep(0.2)

    # Start ping on h1
    ping_log = f"/tmp/ping_{mode_name}.log"
    sh(h1, f"ping -i 0.2 {IP_H2} > {ping_log} 2>&1 &")
    sleep(0.2)

    # Run iperf3 client
    info(f"*** iperf3 {mode_name}: -P {parallel} -t {seconds}\n")
    out = sh(h1, f"iperf3 -c {IP_H2} -P {parallel} -t {seconds}")
    info(out)

    # Stop ping
    sh(h1, f"pkill -f 'ping -i 0.2 {IP_H2}'")

    # Show ping tail
    info(f"*** {mode_name} ping tail (last 50):\n")
    info(sh(h1, f"tail -n 50 {ping_log}"))

    # Optional qdisc stats at bottleneck
    if bottleneck_node is not None and bottleneck_dev:
        info(f"*** {mode_name} qdisc {bottleneck_dev}:\n")
        qdisc_cmd = f"tc -s qdisc show dev {bottleneck_dev} | sed -n '1,80p'"
        info(sh(bottleneck_node, qdisc_cmd))

    # Socket state on h1
    info(f"*** {mode_name} sockets (h1):\n")
    ss_cmd = "ss -ti '( dport = :5201 )' | sed -n '1,8p'"
    info(sh(h1, ss_cmd))

def run_trial(h1, h2, bottleneck_node, bottleneck_dev: str,
              mode_name: str, parallel: int = 4, seconds: int = 5):
    info("running\n")
    kill_flows(h1, h2)

    sh(h2, "cd host-applications && python3 server.py 8123 2>&1 > /tmp/server_log &")

    sh(h1, f"python3 host-applications/run_client.py --runs 1 --srv_addr {IP_H2} 2>&1 > /tmp/client_log &")
    info("waiting\n")
    sleep(130)
    info("info client\n")
    info(sh(h1, 'cat /tmp/client_log'))
    info("info server\n")
    info(sh(h2, 'cat /tmp/server_log'))
