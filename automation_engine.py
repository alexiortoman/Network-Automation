"""
automation_engine.py — Automation Engine

Responsible for:
  - All low-level OpenFlow operations (FlowMod ADD/DELETE, PacketOut)
  - MAC learning (port 3 only) and ARP 
  - Path computation on live_graph (_get_out_port)
  - live_graph mutations (_remove_arc)
  - Proactive two-steps rerouting logic (_reroute_all)
  - Delayed rerouting via hub.spawn + hub.sleep

The Automation Engine is the "HOW" layer: it knows HOW to push rules,
HOW to compute a path, and HOW to repair the forwarding state.
It does not make policy decisions — the Orchestrator calls it.
"""

import networkx as nx

from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.lib import hub

from sot import SourceOfTruth
from telemetry import Telemetry

PRIORITY_TABLE_MISS = 0
PRIORITY_NORMAL     = 1
PRIORITY_REROUTE    = 10

REROUTE_DELAY = 10   # seconds — deliberate outage window for demo


class AutomationEngine:
    """
    Executes all OpenFlow operations on behalf of the Orchestrator.

    Parameters
    ----------
    sot      : SourceOfTruth   topology reference (intended_graph + lookups)
    telemetry: Telemetry       event recorder
    """

    def __init__(self, sot: SourceOfTruth, telemetry: Telemetry):
        self.sot = sot
        self.telemetry = telemetry

        # Runtime forwarding state
        self.datapaths = {}   # {dpid: Datapath}
        self.mac_to_port = {}   # {dpid: {mac: port}}
        self.mac_to_dpid = {}   # {mac: dpid}
        self.ip_to_mac = {}   # {ip: mac} — ARP cache

        # Live graph — mutable working copy of intended_graph
        self.live_graph  = sot.get_live_graph()

        # Failure dedup sets
        self.failed_links = set()   # {tuple(sorted([sw_a, sw_b]))}
        self.failed_nodes = set()   # {sw_name}

    # =========================================================================
    # Datapath registration
    # =========================================================================

    def register_datapath(self, datapath):
        """Called by Orchestrator when a switch completes its OF handshake."""
        dpid = datapath.id
        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})
        self._install_table_miss(datapath)

    def unregister_datapath(self, dpid: int):
        """Called by Orchestrator when a switch session is lost."""
        self.datapaths.pop(dpid, None)

    # =========================================================================
    # Table-miss
    # =========================================================================

    def _install_table_miss(self, datapath):
        """Install the catch-all table-miss rule (PRIORITY 0)."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, PRIORITY_TABLE_MISS, match, actions)

    # =========================================================================
    # OpenFlow helpers — ADD / DELETE / PACKET-OUT
    # =========================================================================

    def _add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        """Install a flow rule via OFPFC_ADD."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        instrs = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=instrs, idle_timeout=idle_timeout, hard_timeout=hard_timeout))

    def _delete_flow_by_dst_and_port(self, datapath, dst_mac: str, out_port: int):
        """
        Step 1 — delete the PRIORITY_NORMAL rule for
        dst_mac that forwards through out_port (now dead).
        OFPFC_DELETE with out_port ensures only that specific rule is removed.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE, priority=PRIORITY_NORMAL, match=parser.OFPMatch(eth_dst=dst_mac), out_port=out_port, out_group=ofproto.OFPG_ANY))

    def _send_packet_out(self, datapath, buffer_id, in_port, out_port, data):
        """Send a PacketOut message to the switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPPacketOut(datapath=datapath, buffer_id=buffer_id, in_port=in_port, actions=[parser.OFPActionOutput(out_port)], data=data if buffer_id == ofproto.OFP_NO_BUFFER else None))

    # =========================================================================
    # Packet-In processing — MAC learning + ARP + forwarding
    # =========================================================================

    def handle_packet_in(self, ev):
        """
        Main Packet-In handler. Called by the Orchestrator for every
        unmatched packet. Performs:
          1. Ethernet frame parsing (LLDP filtering)
          2. MAC learning on port 3 only
          3. ARP (reply if known, flood if unknown)
          4. Unicast forwarding via live_graph shortest path
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_mac = eth.src
        dst_mac = eth.dst
        sw_name = self.sot.resolve_dpid(dpid) or f'dpid={dpid}'

        # == MAC learning (port 3 = host-facing only) ======================
        if in_port == 3 and src_mac not in self.mac_to_dpid:
            self.mac_to_port[dpid][src_mac] = 3
            self.mac_to_dpid[src_mac] = dpid
            self.telemetry.record_mac_learnt(src_mac, sw_name, 3)

        # == ARP ===========================================================
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt is not None:
            self.ip_to_mac[arp_pkt.src_ip] = arp_pkt.src_mac
            if arp_pkt.opcode == arp.ARP_REQUEST:
                self._handle_arp_request(datapath, in_port, pkt, arp_pkt)
                return

        # == Unicast forwarding =============================================
        out_port = self._get_out_port(dpid, dst_mac)
        if out_port is None:
            self._send_packet_out(datapath, msg.buffer_id, in_port, ofproto.OFPP_FLOOD, msg.data)
            return

        self._add_flow(datapath, PRIORITY_NORMAL, parser.OFPMatch(eth_dst=dst_mac), [parser.OFPActionOutput(out_port)])
        self._send_packet_out(datapath, msg.buffer_id, in_port, out_port, msg.data)

    # =========================================================================
    # Path computation
    # =========================================================================

    def _get_out_port(self, dpid: int, dst_mac: str):
        """
        Return the egress port on switch dpid toward dst_mac, using live_graph.

        Strategy:
          1. MAC locally attached to this switch → return access port directly
          2. MAC on another switch → compute shortest path on live_graph, return first-hop port_out
          3. Unknown MAC or no path → return None (caller floods)
        """
        # Local host
        if dst_mac in self.mac_to_port.get(dpid, {}):
            return self.mac_to_port[dpid][dst_mac]

        dst_dpid = self.mac_to_dpid.get(dst_mac)
        if dst_dpid is None:
            return None

        # Guard: destination switch is down
        dst_name = self.sot.resolve_dpid(dst_dpid)
        if dst_name in self.failed_nodes:
            return None

        src_name = self.sot.resolve_dpid(dpid)
        if not src_name or not dst_name:
            return None

        try:
            path = nx.shortest_path(self.live_graph, src_name, dst_name)
            return self.live_graph[src_name][path[1]]['port_out']
        except (nx.NetworkXNoPath, nx.NodeNotFound, IndexError):
            return None

    # =========================================================================
    # ARP 
    # =========================================================================

    def _handle_arp_request(self, datapath, in_port, pkt, arp_pkt):
        """Reply directly if ip_to_mac known, otherwise FLOOD."""
        target_mac = self.ip_to_mac.get(arp_pkt.dst_ip)
        if target_mac is not None:
            self._send_arp_reply(datapath, in_port, arp_pkt, target_mac)
            self.telemetry.record_arp_reply(arp_pkt.dst_ip, target_mac)
            return
        ofproto = datapath.ofproto
        self._send_packet_out(datapath, ofproto.OFP_NO_BUFFER, in_port, ofproto.OFPP_FLOOD, pkt.data)

    def _send_arp_reply(self, datapath, in_port, arp_pkt, target_mac: str):
        """Craft and send an ARP reply from the controller."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, src=target_mac, dst=arp_pkt.src_mac))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=target_mac, src_ip=arp_pkt.dst_ip, dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
        reply.serialize()
        datapath.send_msg(parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER, in_port=ofproto.OFPP_CONTROLLER, actions=[parser.OFPActionOutput(in_port)], data=reply.data))

    # =========================================================================
    # live_graph mutations
    # =========================================================================

    def remove_arc(self, src_name: str, dst_name: str):
        """Remove a directed arc from live_graph if it exists."""
        if self.live_graph.has_edge(src_name, dst_name):
            self.live_graph.remove_edge(src_name, dst_name)

    # =========================================================================
    # Proactive rerouting — two-steps
    # =========================================================================

    def reroute_all(self, reason: str = '') -> int:
        """
        Proactive two-steps rerouting triggered after any failure event.

        Step 1 — DELETE stale rules
          For each (switch, known MAC) pair, determine the egress port the
          current PRIORITY_NORMAL rule is using (inferred from intended_graph).
          If that port's arc is gone from live_graph, send OFPFC_DELETE
          targeting (eth_dst=mac, out_port=dead_port).

        Step 2 — PUSH new rules at PRIORITY_REROUTE=10
          For each (switch, MAC) pair, compute the new shortest path on
          live_graph and install a rule at PRIORITY_REROUTE.
          Higher priority guarantees immediate effect.

        Returns the number of flow rules pushed (Step 2 only).
        """
        n_rules = 0

        for dpid, datapath in list(self.datapaths.items()):
            sw_name = self.sot.resolve_dpid(dpid) or f'dpid={dpid}'
            parser = datapath.ofproto_parser

            for mac, dst_dpid in self.mac_to_dpid.items():
                src_name = self.sot.resolve_dpid(dpid)
                dst_name = self.sot.resolve_dpid(dst_dpid)

                # == Step 1: delete stale rule ==============================
                if src_name and dst_name and src_name != dst_name:
                    try:
                        old_path = nx.shortest_path(self.sot.intended_graph, src_name, dst_name)
                        old_next = old_path[1]
                        old_port = self.sot.intended_graph[src_name][old_next]['port_out']

                        if not self.live_graph.has_edge(src_name, old_next):
                            self._delete_flow_by_dst_and_port(datapath, mac, old_port)
                    except (nx.NetworkXNoPath, nx.NodeNotFound, IndexError):
                        pass

                # == Step 2: push new rule =================================
                new_port = self._get_out_port(dpid, mac)
                if new_port is None:
                    continue

                self._add_flow(datapath, PRIORITY_REROUTE, parser.OFPMatch(eth_dst=mac), [parser.OFPActionOutput(new_port)])
                n_rules += 1

        self.telemetry.record_reroute(reason, n_rules)
        return n_rules

    def reroute_delayed(self, reason: str):
        """
        Waits REROUTE_DELAY seconds before calling reroute_all(). Non-blocking 
        — RYU event loop is never blocked. 

        The deliberate delay creates a visible 0-Mbps outage window in
        the iperf output, proving the failure was real before rerouting
        restores traffic on the alternative path.
        """
        self.telemetry.record_reroute_delay(reason, REROUTE_DELAY)

        def _delayed():
            hub.sleep(REROUTE_DELAY)
            self.reroute_all(reason)

        hub.spawn(_delayed)
