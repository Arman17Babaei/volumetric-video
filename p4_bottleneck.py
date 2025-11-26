# p4_bottleneck.py

from p4utils.mininetlib.network_API import NetworkAPI
from mininet.log import info

H1_IP = "10.0.0.1/24"
H2_IP = "10.0.0.2/24"


def build_p4_net(p4src: str = "aqm.p4"):
    """
    Build and start a P4-based topology using p4-utils:
      h1 -- s1(P4/BMv2, aqm.p4) -- h2

    Returns: (net_api, mn, h1, h2, s1)
      - net_api: NetworkAPI instance
      - mn: underlying Mininet instance (net_api.net)
      - h1, h2, s1: Mininet node objects
    """
    net = NetworkAPI()
    net.setLogLevel('info')

    info("*** Adding P4 switch and hosts\n")
    net.addP4Switch('s1')
    net.addHost('h1', ip=H1_IP)
    net.addHost('h2', ip=H2_IP)

    info("*** Adding links (h1-s1, h2-s1)\n")
    # Fix switch-side ports to match our assumptions (1 and 2)
    net.addLink('h1', 's1', port2=1)
    net.addLink('h2', 's1', port2=2)

    info(f"*** Loading P4 program {p4src}\n")
    net.setP4Source('s1', p4src)

    # No CLI file here; we will program the switch ourselves later
    # net.setP4CliInput('s1', ...)

    # Optional debugging
    net.enablePcapDumpAll()
    net.enableLogAll()

    # Do NOT drop into interactive Mininet CLI
    net.disableCli()

    info("*** Starting P4 network\n")
    net.startNetwork()

    mn = net.net
    h1 = mn.get('h1')
    h2 = mn.get('h2')
    s1 = mn.get('s1')

    info(f"*** P4 network started. h1 MAC={h1.MAC()}, h2 MAC={h2.MAC()}\n")

    return net, mn, h1, h2, s1
