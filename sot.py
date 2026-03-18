"""
sot.py — Source of Truth

Responsible for:
  - Loading and validating SourceOfTruth.yaml
  - Building the intended_graph (immutable reference topology)
  - Exposing lookup helpers (dpid <-> name, host IPs)
  - Providing a fresh live_graph copy for runtime use

The SoT is the single source of truth shared by all other modules.
"""

import yaml
import networkx as nx


class SourceOfTruth:
    """
    Loads SourceOfTruth.yaml and exposes the intended network topology
    as NetworkX graphs and lookup dictionaries.

    Attributes
    ----------
    data            : dict            Raw YAML content
    intended_graph  : nx.DiGraph      Immutable reference — never modified
    dpid_to_name    : dict[int,str]   {1: 's1', 2: 's2', ...}
    name_to_dpid    : dict[str,int]   {'s1': 1, 's2': 2, ...}
    """

    def __init__(self, filepath: str = 'SourceOfTruth.yaml'):
        self.filepath = filepath
        self.data = self._load(filepath)

        self.intended_graph = self._build_intended_graph()

        # DPID lookup tables
        self.dpid_to_name = {
            int(str(sw['dpid']), 16): sw['name']
            for sw in self.data.get('switches', [])
        }
        self.name_to_dpid = {v: k for k, v in self.dpid_to_name.items()}

    # =========================================================================
    # Loading
    # =========================================================================

    def _load(self, filepath: str) -> dict:
        """Load and return raw YAML content. Returns empty dict on error."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return data if data else {}
        except FileNotFoundError:
            raise FileNotFoundError(
                f"[SoT] File not found: '{filepath}'. "
                f"Run source_of_truth.py first."
            )
        except yaml.YAMLError as e:
            raise ValueError(f"[SoT] YAML parse error in '{filepath}': {e}")

    # =========================================================================
    # Graph construction
    # =========================================================================

    def _build_intended_graph(self) -> nx.DiGraph:
        """
        Build the immutable reference directed graph from SoT data.

        Nodes : switches (type='switch', dpid, ip) + hosts (type='host', ip)
        Arcs  :
          core links   — both directions, with port_out and port_in attributes
          access links — switch→host and host→switch
        """
        G = nx.DiGraph()

        # Switch nodes
        for sw in self.data.get('switches', []):
            dpid_int = int(str(sw['dpid']), 16)
            G.add_node(sw['name'], type='switch', dpid=dpid_int, ip=sw['ip'])

        # Host nodes
        for h in self.data.get('hosts', []):
            G.add_node(h['name'], type='host', ip=h['ip'])

        # Arcs
        for link in self.data.get('links', []):
            src = link['source']
            dst = link['destination']
            port_src = link['port_src']
            link_type = link['type']

            if link_type == 'core':
                port_dst = link['port_dst']
                G.add_edge(src, dst, port_out=port_src, port_in=port_dst, link_type='core')
                G.add_edge(dst, src, port_out=port_dst, port_in=port_src, link_type='core')

            elif link_type == 'access':
                G.add_edge(src, dst, port_out=port_src, port_in=None, link_type='access')
                G.add_edge(dst, src, port_out=None, port_in=port_src, link_type='access')
        return G

    # =========================================================================
    # Public helpers
    # =========================================================================

    def get_live_graph(self) -> nx.DiGraph:
        """Return a fresh mutable copy of intended_graph for runtime use."""
        return self.intended_graph.copy()

    def get_switch_count(self) -> int:
        """Return the number of switches defined in the SoT."""
        return len(self.data.get('switches', []))

    def get_switch_names(self) -> list:
        """Return the list of switch names defined in the SoT."""
        return [sw['name'] for sw in self.data.get('switches', [])]

    def resolve_dpid(self, dpid: int) -> str:
        """Return switch name for a given DPID integer, or None."""
        return self.dpid_to_name.get(dpid)

    def resolve_name(self, name: str) -> int:
        """Return DPID integer for a given switch name, or None."""
        return self.name_to_dpid.get(name)

    def summary(self) -> str:
        """One-line summary for logging."""
        n_sw = self.get_switch_count()
        n_hosts = len(self.data.get('hosts', []))
        n_arcs = self.intended_graph.number_of_edges()
        return (
            f"SoT '{self.filepath}' — "
            f"{n_sw} switches, {n_hosts} hosts, {n_arcs} directed arcs"
        )
