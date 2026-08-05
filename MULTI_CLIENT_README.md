# Multi-Client Network Experiment Setup

## Overview

The network topology has been updated to support **multiple clients** connecting to a single server, with each client getting its own subdirectory in the experiment results.

## Network Topology

### Previous (Single Client):
```
h1 -- s1 -- h2
```

### New (N Clients):
```
h1 ----+
h2 ----+
h3 ----+-- s1 -- server
...    |
hN ----+
```

- **Clients**: h1, h2, h3, ..., hN (IP: 10.0.0.1, 10.0.0.2, ..., 10.0.0.N)
- **Server**: server (IP: 10.0.0.{N+1})
- **Switch**: s1 (applies bottleneck on link to server)

## Usage

### Linux Bottleneck Experiments

Run with default 2 clients:
```bash
sudo python3 run_linux_experiment.py
```

Run with custom number of clients (e.g., 5 clients):
```bash
sudo python3 run_linux_experiment.py --num_clients 5
```

### P4 Bottleneck Experiments

Run with default 2 clients:
```bash
sudo python3 run_p4_experiment.py
```

Run with custom number of clients (e.g., 5 clients):
```bash
sudo python3 run_p4_experiment.py --num_clients 5
```

## Experiment Directory Structure

Each experiment creates a timestamped directory with subdirectories for each client:

```
experiments/
├── 20251231_120000_l4s/
│   ├── experiment_config.json
│   ├── client1/
│   │   ├── player_log.txt
│   │   └── qoe_metrics.json
│   ├── client2/
│   │   ├── player_log.txt
│   │   └── qoe_metrics.json
│   └── client3/
│       ├── player_log.txt
│       └── qoe_metrics.json
└── 20251231_120100_classic/
    ├── experiment_config.json
    ├── client1/
    │   ├── player_log.txt
    │   └── qoe_metrics.json
    └── client2/
        ├── player_log.txt
        └── qoe_metrics.json
```

## Key Changes

### 1. **linux_bottleneck.py**
- `build_linux_net(num_clients=1)`: Now creates N clients + 1 server + 1 switch
- Bottleneck is applied to the server link (not a specific client link)
- Returns: `(net, clients_list, server, s1)`

### 2. **p4_bottleneck.py**
- `build_p4_net(num_clients=1, p4src="aqm.p4")`: Creates N clients + 1 server + P4 switch
- Dynamically assigns switch ports (clients on ports 1..N, server on port N+1)
- Returns: `(net_api, mn, clients_list, server, s1)`

### 3. **common.py**
- `get_server_ip(num_clients)`: Helper to calculate server IP
- `add_access_delays(delay_ms, *hosts)`: Now accepts variable number of hosts
- `run_trial(clients, server, ...)`: Updated to handle multiple clients
- Each client gets a subdirectory: `{exp_dir}/client1/`, `{exp_dir}/client2/`, etc.

### 4. **run_linux_experiment.py**
- Added `--num_clients` command-line argument
- Dynamically determines bottleneck port based on number of clients
- Config files now include `num_clients` field

### 5. **run_p4_experiment.py**
- Added `--num_clients` command-line argument
- `program_p4_switch()` now programs forwarding rules for all clients
- AQM configured on all ports dynamically

## Configuration Files

The `experiment_config.json` now includes:
```json
{
  "experiment_type": "linux_bottleneck",
  "mode_name": "l4s",
  "num_clients": 3,
  "qdisc": "dualpi2",
  "bottleneck_device": "s1-eth4",
  ...
}
```

## Benefits

1. **Scalability**: Easily test with 1, 2, 5, 10+ clients
2. **Organization**: Each client's logs are isolated in subdirectories
3. **Realism**: Better simulates real-world scenarios with multiple competing flows
4. **Analysis**: Can compare per-client performance metrics

## Example Scenarios

### Test fairness with 3 clients:
```bash
sudo python3 run_linux_experiment.py --num_clients 3
```

### Stress test with 10 clients:
```bash
sudo python3 run_p4_experiment.py --num_clients 10
```

### Single client (backward compatible):
```bash
sudo python3 run_linux_experiment.py --num_clients 1
```

## Notes

- All clients stream simultaneously from the same server
- Bottleneck is shared among all clients (on the server link)
- Each client runs the DASH video player independently
- Experiment duration: ~60 seconds per trial (configurable in `run_trial()`)
