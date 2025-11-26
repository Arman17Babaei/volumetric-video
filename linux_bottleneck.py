# linux_bottleneck.py

from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.link import TCLink
from mininet.log import info

from common import sh

BW_MBIT = 10
DELAY_MS_ONEWAY = 20  # 20 ms at each host => ~40 ms RTT baseline


def build_linux_net():
    """
    Build and start the Mininet topology:
      h1 -- s1 -- h2
    using OVSBridge and TCLink.
    Returns: (net, h1, h2, s1)
    """
    net = Mininet(switch=OVSBridge, controller=None, link=TCLink)

    info("*** Nodes\n")
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    s1 = net.addSwitch('s1')

    info("*** Links\n")
    net.addLink(h1, s1, bw=100, delay='1ms')
    net.addLink(s1, h2, bw=100, delay='1ms')

    info("*** Start\n")
    net.start()
    return net, h1, h2, s1


def clear_all_qdisc(s1, *hosts):
    """Remove any existing qdiscs on switch and host interfaces."""
    for dev in ("s1-eth1", "s1-eth2"):
        sh(s1, f"tc qdisc del dev {dev} root >/dev/null 2>&1 || true")
    for h in hosts:
        sh(h, f"tc qdisc del dev {h.name}-eth0 root >/dev/null 2>&1 || true")


def set_bottleneck_dualpi2(s1):
    """
    Configure s1-eth2 as a 10 Mbit/s HTB-shaped bottleneck
    with DualPI2 as the child qdisc.
    """
    # clean slate
    s1.cmd("tc qdisc del dev s1-eth2 root >/dev/null 2>&1 || true")

    # HTB shaper
    s1.cmd("tc qdisc add dev s1-eth2 root handle 1: htb default 1")
    s1.cmd(
        f"tc class add dev s1-eth2 parent 1: classid 1:1 "
        f"htb rate {BW_MBIT}mbit ceil {BW_MBIT}mbit"
    )

    # DualPI2 child
    s1.cmd("modprobe sch_dualpi2 >/dev/null 2>&1 || true")
    s1.cmd(
        "tc qdisc add dev s1-eth2 parent 1:1 handle 10: "
        "dualpi2 target 5ms limit 10000"
    )


def set_bottleneck_fifo(s1):
    """
    Configure s1-eth2 as a 10 Mbit/s HTB-shaped bottleneck
    with a plain pfifo child.
    """
    # clean slate
    s1.cmd("tc qdisc del dev s1-eth2 root >/dev/null 2>&1 || true")

    # HTB shaper
    s1.cmd("tc qdisc add dev s1-eth2 root handle 1: htb default 1")
    s1.cmd(
        f"tc class add dev s1-eth2 parent 1: classid 1:1 "
        f"htb rate {BW_MBIT}mbit ceil {BW_MBIT}mbit"
    )

    # Classic FIFO child
    s1.cmd("tc qdisc add dev s1-eth2 parent 1:1 handle 10: pfifo limit 1000")
