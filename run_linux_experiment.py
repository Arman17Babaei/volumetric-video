# run_linux_experiment.py

#!/usr/bin/env python3

from mininet.log import setLogLevel, info

from common import (
    endpoint_tuning,
    add_access_delays,
    run_trial,
)
from linux_bottleneck import (
    build_linux_net,
    clear_all_qdisc,
    set_bottleneck_dualpi2,
    set_bottleneck_fifo,
    DELAY_MS_ONEWAY,
)


def main():
    setLogLevel('info')

    # Build topology
    net, h1, h2, s1 = build_linux_net()

    try:
        # Endpoint tuning + clean qdiscs
        endpoint_tuning(h1)
        endpoint_tuning(h2)
        clear_all_qdisc(s1, h1, h2)

        # Baseline RTT via access netem, bottleneck only on s1-eth2
        add_access_delays(h1, h2, DELAY_MS_ONEWAY)

        # --- L4S (DualPI2 bottleneck) ---
        info("*** L4S: HTB + DualPI2 at bottleneck\n")
        set_bottleneck_dualpi2(s1)
        info(s1.cmd("tc qdisc show dev s1-eth2 | sed -n '1,3p'"))

        # Optional: sanity ping
        info(net.pingFull()[0])

        run_trial(h1, h2, bottleneck_node=s1,
                  bottleneck_dev="s1-eth2", mode_name="l4s")

        # --- Classic (FIFO bottleneck) ---
        info("*** Classic: HTB + pfifo at bottleneck\n")
        set_bottleneck_fifo(s1)
        info(s1.cmd("tc qdisc show dev s1-eth2 | sed -n '1,3p'"))

        run_trial(h1, h2, bottleneck_node=s1,
                  bottleneck_dev="s1-eth2", mode_name="classic")

        info("*** Artifacts: /tmp/ping_l4s.log "
             "/tmp/ping_classic.log /tmp/iperf_server.log\n")

    finally:
        net.stop()


if __name__ == "__main__":
    main()
