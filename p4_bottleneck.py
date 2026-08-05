# p4_bottleneck.py

from p4utils.mininetlib.network_API import NetworkAPI
from mininet.log import info


def build_p4_net(num_clients=1, p4src: str = "aqm.p4"):
    """
    Build and start a P4-based topology using p4-utils:
      h1, h2, ..., h{num_clients} -- s1(P4/BMv2, aqm.p4) -- server

    Returns: (net_api, mn, clients_list, server, s1)
      - net_api: NetworkAPI instance
      - mn: underlying Mininet instance (net_api.net)
      - clients_list: list of client hosts [h1, h2, ...]
      - server: the server host
      - s1: P4 switch
    """
    net = NetworkAPI()
    net.setLogLevel('info')

    info("*** Adding P4 switch and hosts\n")
    net.addP4Switch('s1', priority_queues_num=2)
    
    clients = []
    for i in range(1, num_clients + 1):
        host_name = f'h{i}'
        host_ip = f'10.0.0.{i}/24'
        net.addHost(host_name, ip=host_ip)
        clients.append(host_name)
    
    server_ip = f'10.0.0.{num_clients + 1}/24'
    net.addHost('server', ip=server_ip)

    info("*** Adding links\n")
    # Connect each client to the switch
    for i, client in enumerate(clients, start=1):
        net.addLink(client, 's1', port2=i)
    
    # Connect server to switch (on port num_clients + 1)
    net.addLink('server', 's1', port2=num_clients + 1)

    info(f"*** Loading P4 program {p4src}\n")
    net.setP4Source('s1', p4src)

    # Optional debugging
    net.enablePcapDumpAll()
    net.enableLogAll()

    # Do NOT drop into interactive Mininet CLI
    net.disableCli()

    info("*** Starting P4 network\n")
    net.startNetwork()

    mn = net.net
    client_objs = [mn.get(name) for name in clients]
    server_obj = mn.get('server')
    s1 = mn.get('s1')

    info(f"*** P4 network started with {num_clients} clients\n")
    for i, client in enumerate(client_objs, start=1):
        info(f"    h{i} MAC={client.MAC()}, IP={client.IP()}\n")
    info(f"    server MAC={server_obj.MAC()}, IP={server_obj.IP()}\n")

    return net, mn, client_objs, server_obj, s1
