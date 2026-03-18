"""
orchestrator.py — Orchestrator

Responsible for:
  - Inheriting RyuApp (single OpenFlow entry point)
  - Receiving all OpenFlow and topology events from RYU
  - Deciding WHAT to do in response to each event
  - Delegating execution to AutomationEngine
  - Delegating observability to Telemetry

The Orchestrator decides what action is needed (reroute? learn? ignore?) 
but does not know the OpenFlow details.
All FlowMod, PacketOut, and graph operations are in AutomationEngine.

Launch with:
    ryu-manager orchestrator.py --observe-links
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event
from ryu.topology import switches as topo_switches
from ryu.lib import hub

from sot import SourceOfTruth
from telemetry import Telemetry
from automation_engine import AutomationEngine


class Orchestrator(app_manager.RyuApp):
    """
    SDN Ring Controller — Orchestrator layer.

    Wires together the three support modules:
        SoT              → topology knowledge base
        Telemetry        → event logging & CSV export
        AutomationEngine → OpenFlow execution + rerouting logic

    Event flow
    ----------
    EventOFPSwitchFeatures  → register datapath, install table-miss
    EventOFPPacketIn        → delegate to engine.handle_packet_in()
    EventOFPPortStatus      → detect port DOWN, trigger rerouting
    EventLinkDelete         → detect link DOWN (LLDP), trigger rerouting
    EventSwitchLeave        → detect switch DOWN, trigger rerouting
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS    = {'switches': topo_switches.Switches}

    # =========================================================================
    # Initialisation
    # =========================================================================

    def __init__(self, *args, **kwargs):
        super(Orchestrator, self).__init__(*args, **kwargs)

        # == Instantiate support modules ======================================
        self.sot = SourceOfTruth('SourceOfTruth.yaml')
        self.telemetry = Telemetry(events_csv='sdn_events.csv')
        self.engine = AutomationEngine(self.sot, self.telemetry)

        # == Topology readiness guard =========================================
        # Prevents spurious rerouting during the initial LLDP discovery burst.
        # Set to True once all expected switches have completed their handshake.
        self._n_expected = self.sot.get_switch_count()
        self._topology_ready = False

        self.logger.info("=" * 64)
        self.logger.info("  SDN Ring — Orchestrator")
        self.logger.info(f"  {self.sot.summary()}")
        self.logger.info(f"  Rerouting delay : {__import__('automation_engine').REROUTE_DELAY}s")
        self.logger.info("=" * 64)

    # =========================================================================
    # Switch connection
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Triggered when a switch completes its OpenFlow handshake.
        Registers the datapath and installs the table-miss rule.
        Sets _topology_ready once all expected switches are connected.
        """
        datapath = ev.msg.datapath
        dpid = datapath.id
        sw_name = self.sot.resolve_dpid(dpid) or f'dpid={dpid}'

        self.engine.register_datapath(datapath)
        n_connected = len(self.engine.datapaths)

        self.telemetry.record_switch_connect(sw_name, dpid, n_connected, self._n_expected)

        if n_connected >= self._n_expected and not self._topology_ready:
            self._topology_ready = True
            self.telemetry.record_topology_ready(self._n_expected)
            self.telemetry.start_polling(self.engine.datapaths)

    # =========================================================================
    # Packet-In
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Delegate all Packet-In processing to the AutomationEngine.
        The engine handles MAC learning, ARP, and unicast forwarding.
        """
        self.engine.handle_packet_in(ev)

    # =========================================================================
    # Failure detection — three event sources
    # =========================================================================

    @set_ev_cls(topo_event.EventLinkDelete, MAIN_DISPATCHER)
    def link_failure_handler(self, ev):
        """
        Fired ~6s after a link goes down (LLDP timeout).
        RYU fires this event twice (one per direction) — the failed_links set
        in the engine ensures each physical link is processed only once.

        Note: if port_status_handler already caught the same failure,
        the failed_links guard silently skips this event.
        """
        if not self._topology_ready:
            return

        src_name = self.sot.resolve_dpid(ev.link.src.dpid)
        dst_name = self.sot.resolve_dpid(ev.link.dst.dpid)
        if not src_name or not dst_name:
            return

        key = tuple(sorted([src_name, dst_name]))
        if key in self.engine.failed_links:
            return   # already handled by port_status_handler
        self.engine.failed_links.add(key)

        self.telemetry.record_failure_link(src_name, dst_name, method='LLDP')
        self.engine.remove_arc(src_name, dst_name)
        self.engine.remove_arc(dst_name, src_name)
        self.engine.reroute_delayed(f"link failure {src_name}<->{dst_name}")

    @set_ev_cls(topo_event.EventSwitchLeave, MAIN_DISPATCHER)
    def switch_failure_handler(self, ev):
        """
        Fired immediately when the OpenFlow session to a switch is lost.
        Removes all arcs incident to the failed switch from live_graph,
        then schedules delayed rerouting.
        """
        if not self._topology_ready:
            return

        dpid = ev.switch.dp.id
        sw_name = self.sot.resolve_dpid(dpid)
        if not sw_name:
            return

        self.telemetry.record_failure_node(sw_name, dpid)
        self.engine.failed_nodes.add(sw_name)
        self.engine.unregister_datapath(dpid)

        # Remove all arcs incident to the failed switch
        neighbors = set(
            list(self.sot.intended_graph.successors(sw_name)) +
            list(self.sot.intended_graph.predecessors(sw_name))
        )
        for nb in neighbors:
            key = tuple(sorted([sw_name, nb]))
            self.engine.failed_links.add(key)
            self.engine.remove_arc(sw_name, nb)
            self.engine.remove_arc(nb, sw_name)

        self.engine.reroute_delayed(f"node failure {sw_name}")

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """
        Fired immediately when an individual port changes state (UP/DOWN).
        Faster than EventLinkDelete (no LLDP timeout).
        Only ring ports (1 and 2) trigger rerouting — port 3 is host-facing.
        """
        if not self._topology_ready:
            return

        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        sw_name = self.sot.resolve_dpid(dpid)
        if not sw_name:
            return

        port_no = msg.desc.port_no
        state = msg.desc.state
        ofproto = datapath.ofproto

        # Only ring ports, only MODIFY reason, only DOWN state
        if port_no not in (1, 2):
            return
        if msg.reason != ofproto.OFPPR_MODIFY:
            return
        PORT_DOWN = 1   # OFPPS_LINK_DOWN bit
        if not (state & PORT_DOWN):
            return

        self.telemetry.record_failure_port(sw_name, port_no)

        # Find which link uses this port and trigger rerouting
        for src, dst, attrs in self.sot.intended_graph.edges(data=True):
            if src == sw_name and attrs.get('port_out') == port_no:
                key = tuple(sorted([src, dst]))
                if key not in self.engine.failed_links:
                    self.engine.failed_links.add(key)
                    self.engine.remove_arc(src, dst)
                    self.engine.remove_arc(dst, src)
                    self.engine.reroute_delayed(f"port down {sw_name}:port{port_no}")
                break

    # =========================================================================
    # Stats reply handlers — forward to Telemetry
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        """Forward OFPPortStatsReply to the Telemetry module for storage."""
        self.telemetry.port_stats_reply_handler(ev)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """Forward OFPFlowStatsReply to the Telemetry module for storage."""
        self.telemetry.flow_stats_reply_handler(ev)
