#!/usr/bin/env python3
"""P4-Utils two-switch topology for DualPI2 experiments.

Experiment bundle version: 1.3.1

Topology:
    h1 --\
    h2 ---- s1 ===== s2 ---- server
    ... --/

Both s1 and s2 run the same P4 program. The egress queues on the inter-switch
ports are configured as the symmetric shared bottleneck by the runner.

Version 1.3.1 keeps the topology unchanged; browser bootstrap and dynamic
queue-rate changes are implemented in run_p4_experiment.py.
"""

from mininet.log import info
from p4utils.mininetlib.network_API import NetworkAPI


def build_p4_net(
    num_clients: int = 1,
    p4src: str = "dualpi2_repaired_v1.2.0.p4",
):
    """Build and start the deterministic two-switch P4 topology.

    Port allocation:
      s1 ports 1..N       -> clients h1..hN
      s1 port N+1         -> s2
      s2 port 1           -> s1
      s2 port 2           -> server

    Returns:
      net_api, mininet, clients, server, s1, s2
    """
    if num_clients < 1:
        raise ValueError("num_clients must be at least 1")

    net = NetworkAPI()
    net.setLogLevel("info")

    info("*** Adding P4 switches and hosts\n")
    net.addP4Switch("s1", priority_queues_num=2)
    net.addP4Switch("s2", priority_queues_num=2)

    client_names = []
    for index in range(1, num_clients + 1):
        host_name = f"h{index}"
        net.addHost(host_name, ip=f"10.0.0.{index}/24")
        client_names.append(host_name)

    net.addHost(
        "server",
        ip=f"10.0.0.{num_clients + 1}/24",
    )

    info("*** Adding client access links\n")
    for index, client_name in enumerate(client_names, start=1):
        net.addLink(client_name, "s1", port2=index)

    info("*** Adding inter-switch bottleneck link\n")
    net.addLink(
        "s1",
        "s2",
        port1=num_clients + 1,
        port2=1,
    )

    info("*** Adding server access link\n")
    net.addLink("s2", "server", port1=2)

    info(f"*** Loading P4 program {p4src} on both switches\n")
    net.setP4Source("s1", p4src)
    net.setP4Source("s2", p4src)

    net.enablePcapDumpAll()
    net.enableLogAll()
    net.disableCli()

    info("*** Starting P4 network\n")
    net.startNetwork()

    mn = net.net
    clients = [mn.get(name) for name in client_names]
    server = mn.get("server")
    s1 = mn.get("s1")
    s2 = mn.get("s2")

    info(f"*** P4 network started with {num_clients} clients\n")
    for client in clients:
        info(
            f"    {client.name} MAC={client.MAC()}, "
            f"IP={client.IP()}\n"
        )
    info(f"    server MAC={server.MAC()}, IP={server.IP()}\n")

    return net, mn, clients, server, s1, s2
