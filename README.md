## SDN Ring Topology with Proactive Rerouting

This SDN project is implementing an 8-switch ring topology with a RYU controller (the number of switches and hosts can be changed). The controller performs link-failure "rerouting" via OpenFlow 1.3 and continuously collects network telemetry. Developed and tested on Mininet with OpenvSwitch. 

## Network Topology

- 8 OVS switches in a ring (s1 -> s2 -> ... -> s8 -> s1)
- Each switch connects to one host: h1 (10.0.0.10) through h8 (10.0.0.80)
- Port 1 - clockwise ring link | Port 2 - counter-clockwise ring link | Port 3 - host access
- Controller on 127.0.0.1:6653, OpenFlow 1.3

<img width="2403" height="1656" alt="na" src="https://github.com/user-attachments/assets/310c620c-71ad-494b-8d39-7150a10d0467" />

## Project Structure

| Fichier | Description |
| :--- | :--- |
| `SourceofTruth.yaml` | Auto-generated topology definition |
| `source_of_truth.py` | YAML generator |
| `sot.py` | Loading and validation the .yaml file + building the graphs from it |
| `telemetry.py` | Telemetry module (polling + event logging) |
| `automation_engine.py` | Automation Engine (OpenFlow + rerouting) |
| `orchestrator.py` | Orchestrator / RyuApp entry point |
| `project_ring_topology.py` | Mininet data plane builder |
| `plot_resilience.py` | Bitrate plot |

## Requirements 

- Python 3.8+ | Mininet 2.3+ | OpenvSwitch 2.13+
- pip install ryu networkx pyyaml matplotlib numpy

## Quick started

## Step 1 - Generate topology YAML

<pre>
python3 source_of_truth.py
</pre>

## Step 2 - Start the RYU controller (separate terminal)
<pre>
ryu-manager orchestrator.py --observe-links
</pre>
Wait for: [READY] All switches connected - failure detection is now ACTIVE

# Step 3 - Start Mininet (separate terminal)
<pre>
sudo python3 project_ring_topology.py
</pre>

# Step 4 - Verify connectivity 
<pre>
mininet > pingall 
</pre>
Expected: 0% dropped (56/56 received)

# Step 5 - Measure throughput and inject a failure
<pre>
mininet> h3 iperf -s -u -i 0.5 > iperf_server.txt &
mininet> h1 iperf -c 10.0.0.30 -u -b 105M -t 60 &
mininet> link s1 s2 down
</pre>
The controller detects the port DOWN event immediately, waits REROUTE_DELAY seconds, then pushes PRIORITY_REROUTE=10 rules routing traffic via the alternative ring path.

# Step 6 - Plot the Bitrate curve
<pre>
python3 plot_bitrate.py --iperf iperf_server.txt
</pre>

# Demo Project 2
Here is the link of the video of the demo https://youtu.be/GSHuNgtuNGk
