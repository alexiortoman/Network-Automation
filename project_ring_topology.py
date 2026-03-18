"""
This file is describing the dataplane. The switches and hosts are physically created and linked together, but no switch table is filled and no forwarding rules exist yet. 
They need to be defined in the control plane (by the RYU controller) using OpenFlow. 
"""

import yaml
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

class RingTopologyDeployer:
    """
    Class responsible of transforming the intended network state (YAML) in a virtual working networking infrastructure. 
    """

    def __init__(self, sot_file):
        self.sot_file = sot_file
        self.config = self._load_source_of_truth()
        self.net = None

    def _load_source_of_truth(self):
        try:
            with open(self.sot_file, 'r') as f:
                info(f"*** Loading of Source of Truth (network topology file) : {self.sot_file}\n")
                return yaml.safe_load(f)
        except Exception as e:
            info(f"*** Error while loading the SoT : {e}\n")
            return None

    def build_infrastructure(self):
        """
        Initialize and build the Mininet network.
        """
        if not self.config:
            return

        # Initialization of Mininet
        self.net = Mininet(topo=None, build=False, ipBase='10.0.0.0/8')

        # Adding the RYU controller 
        info('*** Adding the SDN controller\n')
        self.net.addController(name='c0', controller=RemoteController, ip='127.0.0.1', port=6653)   # ip='127.0.0.1', port=6653

        # Creation of Switches 
        switches = {}
        for sw_cfg in self.config['switches']:
            s = self.net.addSwitch(
                sw_cfg['name'], 
                cls=OVSSwitch, 
                protocols='OpenFlow13', 
                dpid=sw_cfg['dpid']                 # Unique ID for RYU 
            )
            switches[sw_cfg['name']] = s

        # Hosts creation
        hosts = {}
        for h_cfg in self.config['hosts']:
            h = self.net.addHost(h_cfg['name'], ip=h_cfg['ip'])
            hosts[h_cfg['name']] = h

        # Link creation (Ring)
        for link in self.config['links']:
            node1 = switches.get(link['source']) or hosts.get(link['source'])
            node2 = switches.get(link['destination']) or hosts.get(link['destination'])
            
            if node1 and node2:
                self.net.addLink(node1, node2, port1=link.get('port_src'), port2=link.get('port_dst'))

    def start_network(self):
        """
        Starting of the infrastructure and opening of the test terminal
        """
        if self.net:
            info('*** Starting the network...\n')
            self.net.build()
            self.net.start()
            
            # CLI to simulate manually link failure 
            CLI(self.net)
            self.net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    
    # Instance of the deployement engine
    deployer = RingTopologyDeployer('SourceOfTruth.yaml')
    
    # Execution of automatisation pipeline 
    deployer.build_infrastructure()
    deployer.start_network()
