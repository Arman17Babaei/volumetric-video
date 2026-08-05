#!/usr/bin/env python3
"""Linux/Mininet bottleneck topology for DualPI2 experiments.

Experiment bundle version: 1.3.1

Topology:
    h1 --\
    h2 ---- s1 ===== s2 ---- server
    ... --/

The inter-switch link is the shared bottleneck. A qdisc is installed on both
inter-switch egress interfaces, so the link is rate-limited in both directions.

Version 1.3.1 adds a temporary bootstrap rate. The runners start browser
initialization at BOOTSTRAP_BW_MBIT and then change the existing HTB classes to
BW_MBIT without replacing or resetting the child AQM.
"""

from typing import Tuple

from mininet.link import TCLink
from mininet.log import info
from mininet.net import Mininet
from mininet.node import OVSBridge

from common import sh


BUNDLE_VERSION = "1.3.1"

BW_MBIT = 10
BOOTSTRAP_BW_MBIT = 50
DELAY_MS_ONEWAY = 20

ACCESS_LINK_BW_MBIT = 100
ACCESS_LINK_DELAY = "1ms"
INTERSWITCH_LINK_BW_MBIT = 100
INTERSWITCH_LINK_DELAY = "1ms"

DUALPI2_TARGET = "5ms"
DUALPI2_LIMIT_PACKETS = 10_000
FIFO_LIMIT_PACKETS = 1_000


def build_linux_net(num_clients: int = 1):
    """Build and start the deterministic two-switch Linux topology.

    Port allocation:
      s1 ports 1..N       -> clients h1..hN
      s1 port N+1         -> s2
      s2 port 1           -> s1
      s2 port 2           -> server

    Returns:
      net, clients, server, s1, s2
    """
    if num_clients < 1:
        raise ValueError("num_clients must be at least 1")

    net = Mininet(switch=OVSBridge, controller=None, link=TCLink)

    info("*** Nodes\n")
    clients = []
    for index in range(1, num_clients + 1):
        client = net.addHost(f"h{index}", ip=f"10.0.0.{index}/24")
        clients.append(client)

    server = net.addHost(
        "server",
        ip=f"10.0.0.{num_clients + 1}/24",
    )
    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")

    info("*** Client access links\n")
    for index, client in enumerate(clients, start=1):
        net.addLink(
            client,
            s1,
            port2=index,
            bw=ACCESS_LINK_BW_MBIT,
            delay=ACCESS_LINK_DELAY,
        )

    info("*** Inter-switch bottleneck link\n")
    net.addLink(
        s1,
        s2,
        port1=num_clients + 1,
        port2=1,
        bw=INTERSWITCH_LINK_BW_MBIT,
        delay=INTERSWITCH_LINK_DELAY,
    )

    info("*** Server access link\n")
    net.addLink(
        s2,
        server,
        port1=2,
        bw=ACCESS_LINK_BW_MBIT,
        delay=ACCESS_LINK_DELAY,
    )

    info("*** Start\n")
    net.start()
    return net, clients, server, s1, s2


def get_interswitch_devices(num_clients: int) -> Tuple[str, str]:
    """Return `(s1_to_s2_device, s2_to_s1_device)`."""
    if num_clients < 1:
        raise ValueError("num_clients must be at least 1")
    return f"s1-eth{num_clients + 1}", "s2-eth1"


def clear_all_qdisc(*nodes) -> None:
    """Remove root qdiscs from every non-loopback interface on each node."""
    for node in nodes:
        for interface in node.intfList():
            device = str(interface)
            if device == "lo":
                continue
            sh(
                node,
                f"tc qdisc del dev {device} root "
                ">/dev/null 2>&1 || true",
            )


def _validate_rate(rate_mbit: float) -> None:
    if rate_mbit <= 0:
        raise ValueError("rate_mbit must be positive")


def _replace_root_qdisc(
    node,
    device: str,
    child_qdisc: str,
    *,
    rate_mbit: float,
) -> None:
    """Install HTB shaping and one child qdisc on an egress interface."""
    _validate_rate(rate_mbit)

    node.cmd(
        f"tc qdisc del dev {device} root "
        ">/dev/null 2>&1 || true"
    )

    commands = (
        f"tc qdisc add dev {device} root handle 1: htb default 1",
        (
            f"tc class add dev {device} parent 1: classid 1:1 "
            f"htb rate {rate_mbit}mbit ceil {rate_mbit}mbit"
        ),
        (
            f"tc qdisc add dev {device} parent 1:1 handle 10: "
            f"{child_qdisc}"
        ),
    )

    for command in commands:
        result = node.cmd(f"{command} 2>&1")
        if result.strip():
            raise RuntimeError(
                f"Failed on {node.name}:{device}: "
                f"{command}: {result.strip()}"
            )


def change_bottleneck_rate(
    s1,
    s1_to_s2_device: str,
    s2,
    s2_to_s1_device: str,
    *,
    rate_mbit: float,
) -> None:
    """Change both HTB rates while retaining the existing child qdiscs."""
    _validate_rate(rate_mbit)

    for node, device in (
        (s1, s1_to_s2_device),
        (s2, s2_to_s1_device),
    ):
        command = (
            f"tc class change dev {device} parent 1: classid 1:1 "
            f"htb rate {rate_mbit}mbit ceil {rate_mbit}mbit"
        )
        result = node.cmd(f"{command} 2>&1")
        if result.strip():
            raise RuntimeError(
                f"Failed to change rate on {node.name}:{device}: "
                f"{result.strip()}"
            )


def set_bottleneck_dualpi2(
    s1,
    s1_to_s2_device: str,
    s2,
    s2_to_s1_device: str,
    *,
    rate_mbit: float = BW_MBIT,
) -> None:
    """Install symmetric HTB + DualPI2 on the inter-switch link."""
    s1.cmd("modprobe sch_dualpi2 >/dev/null 2>&1 || true")
    s2.cmd("modprobe sch_dualpi2 >/dev/null 2>&1 || true")

    child = (
        f"dualpi2 target {DUALPI2_TARGET} "
        f"limit {DUALPI2_LIMIT_PACKETS}"
    )
    _replace_root_qdisc(
        s1,
        s1_to_s2_device,
        child,
        rate_mbit=rate_mbit,
    )
    _replace_root_qdisc(
        s2,
        s2_to_s1_device,
        child,
        rate_mbit=rate_mbit,
    )


def set_bottleneck_fifo(
    s1,
    s1_to_s2_device: str,
    s2,
    s2_to_s1_device: str,
    *,
    rate_mbit: float = BW_MBIT,
) -> None:
    """Install symmetric HTB + pfifo on the inter-switch link."""
    child = f"pfifo limit {FIFO_LIMIT_PACKETS}"
    _replace_root_qdisc(
        s1,
        s1_to_s2_device,
        child,
        rate_mbit=rate_mbit,
    )
    _replace_root_qdisc(
        s2,
        s2_to_s1_device,
        child,
        rate_mbit=rate_mbit,
    )
