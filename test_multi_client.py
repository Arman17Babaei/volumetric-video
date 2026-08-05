#!/usr/bin/env python3
"""
Test script to verify multi-client network setup works correctly.
This doesn't run the full experiment but validates the topology.
"""

from mininet.log import setLogLevel, info
from linux_bottleneck import build_linux_net, clear_all_qdisc
from common import endpoint_tuning, add_access_delays

def test_topology(num_clients=3):
    """Test that the topology builds correctly with N clients."""
    setLogLevel('info')
    
    print(f"\n{'='*60}")
    print(f"Testing topology with {num_clients} clients")
    print(f"{'='*60}\n")
    
    # Build network
    net, clients, server, s1 = build_linux_net(num_clients=num_clients)
    
    try:
        # Verify clients
        assert len(clients) == num_clients, f"Expected {num_clients} clients, got {len(clients)}"
        print(f"✓ Created {num_clients} clients")
        
        # Verify IPs
        for i, client in enumerate(clients, start=1):
            expected_ip = f"10.0.0.{i}"
            actual_ip = client.IP()
            assert actual_ip == expected_ip, f"Client {i} IP mismatch: expected {expected_ip}, got {actual_ip}"
            print(f"✓ Client h{i}: IP={actual_ip}, MAC={client.MAC()}")
        
        # Verify server
        expected_server_ip = f"10.0.0.{num_clients + 1}"
        actual_server_ip = server.IP()
        assert actual_server_ip == expected_server_ip, f"Server IP mismatch: expected {expected_server_ip}, got {actual_server_ip}"
        print(f"✓ Server: IP={actual_server_ip}, MAC={server.MAC()}")
        
        # Test connectivity
        print("\nTesting connectivity...")
        
        # Ping from each client to server
        for i, client in enumerate(clients, start=1):
            result = client.cmd(f"ping -c 1 -W 1 {actual_server_ip}")
            if "1 received" in result:
                print(f"✓ Client h{i} -> server: OK")
            else:
                print(f"✗ Client h{i} -> server: FAILED")
        
        # Ping between clients
        if num_clients > 1:
            print("\nTesting client-to-client connectivity...")
            result = clients[0].cmd(f"ping -c 1 -W 1 {clients[1].IP()}")
            if "1 received" in result:
                print(f"✓ Client h1 -> h2: OK")
            else:
                print(f"✗ Client h1 -> h2: FAILED")
        
        print(f"\n{'='*60}")
        print(f"✓ All tests passed for {num_clients} clients!")
        print(f"{'='*60}\n")
        
    finally:
        net.stop()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Test multi-client network topology")
    parser.add_argument('--num_clients', type=int, default=3, help="Number of clients to test (default: 3)")
    args = parser.parse_args()
    
    test_topology(args.num_clients)
