# linux_bottleneck.py

from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.link import TCLink
from mininet.log import info

from common import sh

BW_MBIT = .3
DELAY_MS_ONEWAY = 20  # 20 ms at each host => ~40 ms RTT baseline


def build_linux_net(num_clients=1):
    """
    Build and start the Mininet topology:
      h1, h2, ..., h{num_clients} -- s1 -- server
    using OVSBridge and TCLink.
    Returns: (net, clients_list, server, s1)
      - clients_list: list of client hosts [h1, h2, ...]
      - server: the server host
      - s1: the switch
    """
    net = Mininet(switch=OVSBridge, controller=None, link=TCLink)

    info("*** Nodes\n")
    clients = []
    for i in range(1, num_clients + 1):
        client = net.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
        clients.append(client)
    
    server = net.addHost('server', ip=f'10.0.0.{num_clients + 1}/24')
    s1 = net.addSwitch('s1')

    info("*** Links\n")
    for client in clients:
        net.addLink(client, s1, bw=100, delay='1ms')
    net.addLink(s1, server, bw=100, delay='1ms')

    info("*** Start\n")
    net.start()
    return net, clients, server, s1


def clear_all_qdisc(s1, *hosts):
    """Remove any existing qdiscs on switch and host interfaces."""
    # Clear all switch interfaces
    for i in range(1, 20):  # Support up to 20 interfaces
        sh(s1, f"tc qdisc del dev s1-eth{i} root >/dev/null 2>&1 || true")
    for h in hosts:
        sh(h, f"tc qdisc del dev {h.name}-eth0 root >/dev/null 2>&1 || true")


def set_bottleneck_dualpi2(s1, bottleneck_port="s1-eth1"):
    """
    Configure the specified bottleneck port as a HTB-shaped bottleneck
    with DualPI2 as the child qdisc.
    Default bottleneck_port is s1-eth1 (link to server in multi-client setup).
    """
    # clean slate
    s1.cmd(f"tc qdisc del dev {bottleneck_port} root >/dev/null 2>&1 || true")

    # HTB shaper
    s1.cmd(f"tc qdisc add dev {bottleneck_port} root handle 1: htb default 1")
    s1.cmd(
        f"tc class add dev {bottleneck_port} parent 1: classid 1:1 "
        f"htb rate {BW_MBIT}mbit ceil {BW_MBIT}mbit"
    )

    # DualPI2 child
    s1.cmd("modprobe sch_dualpi2 >/dev/null 2>&1 || true")
    s1.cmd(
        f"tc qdisc add dev {bottleneck_port} parent 1:1 handle 10: "
        "dualpi2 target 5ms limit 10000"
    )


def set_bottleneck_fifo(s1, bottleneck_port="s1-eth1"):
    """
    Configure the specified bottleneck port as a HTB-shaped bottleneck
    with a plain pfifo child.
    Default bottleneck_port is s1-eth1 (link to server in multi-client setup).
    """
    # clean slate
    s1.cmd(f"tc qdisc del dev {bottleneck_port} root >/dev/null 2>&1 || true")

    # HTB shaper
    s1.cmd(f"tc qdisc add dev {bottleneck_port} root handle 1: htb default 1")
    s1.cmd(
        f"tc class add dev {bottleneck_port} parent 1: classid 1:1 "
        f"htb rate {BW_MBIT}mbit ceil {BW_MBIT}mbit"
    )

    # Classic FIFO child
    s1.cmd(f"tc qdisc add dev {bottleneck_port} parent 1:1 handle 10: pfifo limit 1000")
