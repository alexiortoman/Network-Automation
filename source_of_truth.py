"""
This file generates intended network ring topology in a YAML file.  
"""

import yaml
import os

def generate_sdn_source_of_truth(n_switches: int):
    topology = {
        'network_name': f'Ring_Topology_{n_switches}',
        'switches': [],                 # type( switches[i] ) = dict
        'hosts': [],
        'links': []
    }

    # Switches
    for i in range(1, n_switches + 1):
        dpid = format(i, '016x')
        topology['switches'].append({
            'name': f's{i}',
            'id': f'SW{i}',
            'ip': f'10.0.0.{i}',        # 10.0.0.1 (SW 1), 10.0.0.2 (SW 2), ...
            'dpid': dpid                # used by the controller to distinguish between the switches (used for flowtables)
        })
        
        topology['hosts'].append({
            'name': f'h{i}',
            'id': f'H{i}',
            'ip': f'10.0.0.{i}0'        # 10.0.0.10 (h1), 10.0.0.20 (h2), ... 
        })
   
    # Links : SW - SW links + SW - Hosts links 
    for i in range(1, n_switches + 1):
        dest = 1 if i == n_switches else i + 1
        topology['links'].append({
            'source': f's{i}',
            'destination': f's{dest}',
            'port_src': 1,   # previous switch clockwise
            'port_dst': 2,    # next switch clockwise
            'type': 'core'
        }) 
        topology['links'].append({
                'source': f's{i}', 
                'destination': f'h{i}',   
                'port_src': 3,  
                'type': 'access'
        })

    return topology

# --- Execution ---
n_switches = 8
data = generate_sdn_source_of_truth(n_switches)

# Export in YAML 
with open('SourceOfTruth.yaml', 'w', encoding='utf-8') as f:
    yaml.dump(data, f, sort_keys=False, default_flow_style=False)  
    # sort_keys=False : not sorting the dict keys by alphabetical order.  
    # default_flow_style=False : to preserve YAML displaying format 

print("File 'SourceOfTruth.yaml' generated.")
