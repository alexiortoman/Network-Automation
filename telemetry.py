"""
telemetry.py — Telemetry

Responsible for:
  - Active OpenFlow stats collection (OFPPortStatsRequest / OFPFlowStatsRequest)
  - Receiving and parsing OFPPortStatsReply / OFPFlowStatsReply
  - Timestamped logging of network events (failures, rerouting, MAC learning)
  - Writing metrics to CSV files for post-processing (plot_resilience.py)
  - Tracking failure/recovery timeline statistics

All other modules call telemetry methods to record events — they do not
log directly to file. This centralises all observability in one place.

Author : Alex IORTOMAN
"""

import csv
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional

from ryu.lib import hub
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER

STATS_INTERVAL = 5   # seconds between each polling cycle


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NetworkEvent:
    """A single recorded network event."""
    timestamp  : float
    event_type : str
    source     : str
    detail     : str  = ''
    t_relative : float = 0.0


@dataclass
class PortStat:
    """Snapshot of one port's counters at one polling instant."""
    timestamp   : float
    t_relative  : float
    dpid        : int
    port_no     : int
    tx_bytes    : int
    rx_bytes    : int
    tx_packets  : int
    rx_packets  : int
    tx_errors   : int
    rx_errors   : int


@dataclass
class FlowStat:
    """Snapshot of one flow entry at one polling instant."""
    timestamp  : float
    t_relative : float
    dpid       : int
    priority   : int
    match      : str
    packet_cnt : int
    byte_cnt   : int
    duration_s : int


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry class
# ─────────────────────────────────────────────────────────────────────────────

class Telemetry:
    """
    Central telemetry collector for the SDN ring controller.

    Active stats collection
    -----------------------
    Call start_polling(datapaths_ref) once the topology is ready.
    A background greenlet sends OFPPortStatsRequest + OFPFlowStatsRequest
    to every switch every STATS_INTERVAL seconds.

    The reply handlers must be wired in the Orchestrator:
        @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
        def _port_stats_reply(self, ev):
            self.telemetry.port_stats_reply_handler(ev)

        @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
        def _flow_stats_reply(self, ev):
            self.telemetry.flow_stats_reply_handler(ev)

    Event recording
    ---------------
    Other modules call record_*() methods. Every event is appended
    to sdn_events.csv immediately (auto_flush=True by default).
    Port and flow stats go to their own CSV files.
    """

    def __init__(self,
                 events_csv     : str  = 'sdn_events.csv',
                 port_stats_csv : str  = 'sdn_port_stats.csv',
                 flow_stats_csv : str  = 'sdn_flow_stats.csv',
                 logger_name    : str  = 'Telemetry',
                 auto_flush     : bool = True):
        self.events_csv     = events_csv
        self.port_stats_csv = port_stats_csv
        self.flow_stats_csv = flow_stats_csv
        self.auto_flush     = auto_flush

        self.events     : List[NetworkEvent] = []
        self.port_stats : List[PortStat]     = []
        self.flow_stats : List[FlowStat]     = []

        self.t_start = time.time()

        self.n_link_failures = 0
        self.n_node_failures = 0
        self.n_reroutes      = 0
        self.n_macs_learnt   = 0

        self._datapaths_ref  : Optional[Dict] = None
        self._polling_active : bool = False

        self.logger = logging.getLogger(logger_name)
        self._init_csvs()

    # ─────────────────────────────────────────────────────────────────────────
    # CSV initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def _init_csvs(self):
        headers = {
            self.events_csv: [
                'timestamp', 't_relative_s', 'event_type', 'source', 'detail'
            ],
            self.port_stats_csv: [
                'timestamp', 't_relative_s', 'dpid', 'port_no',
                'tx_bytes', 'rx_bytes', 'tx_packets', 'rx_packets',
                'tx_errors', 'rx_errors'
            ],
            self.flow_stats_csv: [
                'timestamp', 't_relative_s', 'dpid', 'priority',
                'match', 'packet_cnt', 'byte_cnt', 'duration_s'
            ],
        }
        for path, header in headers.items():
            try:
                with open(path, 'w', newline='', encoding='utf-8') as f:
                    csv.writer(f).writerow(header)
            except OSError as e:
                self.logger.warning(
                    f"[TELEMETRY] Cannot create '{path}': {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Internal event recorder
    # ─────────────────────────────────────────────────────────────────────────

    def _record(self, event_type: str, source: str,
                detail: str = '') -> NetworkEvent:
        now = time.time()
        ev  = NetworkEvent(
            timestamp  = now,
            event_type = event_type,
            source     = source,
            detail     = detail,
            t_relative = round(now - self.t_start, 3),
        )
        self.events.append(ev)
        if self.auto_flush:
            try:
                with open(self.events_csv, 'a', newline='',
                          encoding='utf-8') as f:
                    csv.writer(f).writerow([
                        ev.timestamp, ev.t_relative,
                        ev.event_type, ev.source, ev.detail,
                    ])
            except OSError as e:
                self.logger.warning(
                    f"[TELEMETRY] Event CSV write error: {e}")
        return ev

    # ─────────────────────────────────────────────────────────────────────────
    # Active stats collection — polling
    # ─────────────────────────────────────────────────────────────────────────

    def start_polling(self, datapaths_ref: dict):
        """
        Start the background greenlet that sends OFPPortStatsRequest
        and OFPFlowStatsRequest to every switch every STATS_INTERVAL s.

        Parameters
        ----------
        datapaths_ref : dict   Live reference to engine.datapaths.
                               Newly connected switches are included
                               automatically since the dict is shared.
        """
        if self._polling_active:
            return
        self._datapaths_ref  = datapaths_ref
        self._polling_active = True
        hub.spawn(self._polling_loop)
        self.logger.info(
            f"[TELEMETRY] Stats polling started (interval={STATS_INTERVAL}s)"
        )

    def _polling_loop(self):
        """Background greenlet — request stats from all switches."""
        while self._polling_active:
            hub.sleep(STATS_INTERVAL)
            if not self._datapaths_ref:
                continue
            for dpid, datapath in list(self._datapaths_ref.items()):
                try:
                    self._request_port_stats(datapath)
                    self._request_flow_stats(datapath)
                except Exception as e:
                    self.logger.debug(
                        f"[TELEMETRY] Stats request failed "
                        f"dpid=0x{dpid:016x}: {e}"
                    )

    def _request_port_stats(self, datapath):
        """Send OFPPortStatsRequest for all ports."""
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        datapath.send_msg(
            parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY))

    def _request_flow_stats(self, datapath):
        """Send OFPFlowStatsRequest for all flow entries."""
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPFlowStatsRequest(datapath))

    def stop_polling(self):
        """Stop the background polling greenlet."""
        self._polling_active = False

    # ─────────────────────────────────────────────────────────────────────────
    # Stats reply handlers — wired from Orchestrator @set_ev_cls
    # ─────────────────────────────────────────────────────────────────────────

    def port_stats_reply_handler(self, ev):
        """
        Handle OFPPortStatsReply. Store one PortStat per port
        and append to sdn_port_stats.csv.
        """
        dpid  = ev.msg.datapath.id
        now   = time.time()
        t_rel = round(now - self.t_start, 3)
        rows  = []

        for stat in ev.msg.body:
            ps = PortStat(
                timestamp   = now,
                t_relative  = t_rel,
                dpid        = dpid,
                port_no     = stat.port_no,
                tx_bytes    = stat.tx_bytes,
                rx_bytes    = stat.rx_bytes,
                tx_packets  = stat.tx_packets,
                rx_packets  = stat.rx_packets,
                tx_errors   = stat.tx_errors,
                rx_errors   = stat.rx_errors,
            )
            self.port_stats.append(ps)
            rows.append([
                ps.timestamp, ps.t_relative, ps.dpid, ps.port_no,
                ps.tx_bytes, ps.rx_bytes,
                ps.tx_packets, ps.rx_packets,
                ps.tx_errors, ps.rx_errors,
            ])

        try:
            with open(self.port_stats_csv, 'a', newline='',
                      encoding='utf-8') as f:
                csv.writer(f).writerows(rows)
        except OSError as e:
            self.logger.warning(
                f"[TELEMETRY] Port stats CSV write error: {e}")

    def flow_stats_reply_handler(self, ev):
        """
        Handle OFPFlowStatsReply. Store one FlowStat per flow entry
        and append to sdn_flow_stats.csv.
        """
        dpid  = ev.msg.datapath.id
        now   = time.time()
        t_rel = round(now - self.t_start, 3)
        rows  = []

        for stat in ev.msg.body:
            fs = FlowStat(
                timestamp  = now,
                t_relative = t_rel,
                dpid       = dpid,
                priority   = stat.priority,
                match      = str(stat.match),
                packet_cnt = stat.packet_count,
                byte_cnt   = stat.byte_count,
                duration_s = stat.duration_sec,
            )
            self.flow_stats.append(fs)
            rows.append([
                fs.timestamp, fs.t_relative, fs.dpid, fs.priority,
                fs.match, fs.packet_cnt, fs.byte_cnt, fs.duration_s,
            ])

        try:
            with open(self.flow_stats_csv, 'a', newline='',
                      encoding='utf-8') as f:
                csv.writer(f).writerows(rows)
        except OSError as e:
            self.logger.warning(
                f"[TELEMETRY] Flow stats CSV write error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Public recording methods (called by Orchestrator / AutomationEngine)
    # ─────────────────────────────────────────────────────────────────────────

    def record_switch_connect(self, sw_name: str, dpid: int,
                              connected: int, expected: int):
        detail = f"dpid=0x{dpid:016x} [{connected}/{expected} connected]"
        self._record('CONNECT', sw_name, detail)
        self.logger.info(f"[CONNECT] {sw_name} {detail}")

    def record_topology_ready(self, n_switches: int):
        self._record('READY', 'controller',
                     f"all {n_switches} switches connected")
        self.logger.info(
            "[READY  ] All switches connected — "
            "failure detection is now ACTIVE"
        )

    def record_failure_link(self, sw_a: str, sw_b: str,
                            method: str = 'LLDP'):
        self.n_link_failures += 1
        source = f"{sw_a}<->{sw_b}"
        self._record('FAILURE_LINK', source, f"detected via {method}")
        self.logger.warning(f"[FAILURE] Link DOWN ({method}) : {source}")

    def record_failure_node(self, sw_name: str, dpid: int):
        self.n_node_failures += 1
        self._record('FAILURE_NODE', sw_name, f"dpid=0x{dpid:016x}")
        self.logger.warning(
            f"[FAILURE] Switch DOWN : {sw_name} (dpid=0x{dpid:016x})")

    def record_failure_port(self, sw_name: str, port_no: int):
        self._record('FAILURE_PORT', f"{sw_name}:port{port_no}",
                     'port went DOWN')
        self.logger.warning(
            f"[FAILURE] Port DOWN : {sw_name}:port{port_no}")

    def record_reroute_delay(self, reason: str, delay_s: int):
        self._record('REROUTE_DELAY', reason,
                     f"delay={delay_s}s — traffic DOWN")
        self.logger.info(
            f"[DELAY  ] Rerouting in {delay_s}s — "
            f"traffic is DOWN during this window ({reason})"
        )

    def record_reroute(self, reason: str, n_rules: int = 0):
        self.n_reroutes += 1
        self._record('REROUTE', reason, f"rules_pushed={n_rules}")
        self.logger.info(f"[REROUTE] {reason} | rules pushed: {n_rules}")

    def record_mac_learnt(self, mac: str, sw_name: str, port: int):
        self.n_macs_learnt += 1
        self._record('LEARN', mac, f"{sw_name}:port{port}")
        self.logger.info(f"[LEARN  ] {mac} @ {sw_name}:port{port}")

    def record_arp_reply(self, ip: str, mac: str):
        self._record('ARP', ip, f"resolved to {mac}")
        self.logger.info(f"[ARP    ] proxy: {ip} -> {mac}")

    # ─────────────────────────────────────────────────────────────────────────
    # Summary / helpers
    # ─────────────────────────────────────────────────────────────────────────

    def summary(self) -> str:
        elapsed = round(time.time() - self.t_start, 1)
        return (
            f"Telemetry — uptime: {elapsed}s | "
            f"link failures: {self.n_link_failures} | "
            f"node failures: {self.n_node_failures} | "
            f"reroutes: {self.n_reroutes} | "
            f"MACs learnt: {self.n_macs_learnt} | "
            f"port snapshots: {len(self.port_stats)} | "
            f"flow snapshots: {len(self.flow_stats)}"
        )

    def get_failure_timeline(self) -> list:
        """Return failure events as dicts, usable with plot_resilience.py."""
        return [
            {'t_relative': ev.t_relative,
             'event_type': ev.event_type,
             'source'    : ev.source,
             'detail'    : ev.detail}
            for ev in self.events
            if ev.event_type in (
                'FAILURE_LINK', 'FAILURE_NODE', 'FAILURE_PORT')
        ]
