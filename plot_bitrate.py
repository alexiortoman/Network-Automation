"""
Visualization of SDN network resilience — UDP throughput vs. time.
Parses an iperf2 UDP server output file and overlays failure/recovery events
extracted from failure_simulation.py scenario definitions.

Usage:
    # Mode 1 — single failure window
    python3 plot_resilience.py --iperf results/iperf_server.txt \
                                --failure-start 15 --failure-end 25 \
                                --label "s1<->s2"

    # Mode 2 is not implemented, doesn't exist... 
    # Mode 2 — multi-failure scenario (mirrors simulate_scenario() format)
    python3 plot_resilience.py --iperf results/iperf_server.txt \
                                --scenario '[{"sw1":"s1","sw2":"s2","wait_before":15,"failure_duration":10}]'

    # Mode 3 — auto-detect failure window from iperf data (longest 0-Mbps run)
    python3 plot_resilience.py --iperf results/iperf_server.txt
"""

import re
import json
import argparse
from dataclasses import dataclass
from typing import List, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IperfInterval:
    t_start : float
    t_end   : float
    bw_mbps : float


@dataclass
class FailureEvent:
    label          : str
    failure_start  : float
    failure_end    : float
    color_down     : str = "#e74c3c"
    color_up       : str = "#2ecc71"


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"\[\s*\d+\]\s+"
    r"([\d.]+)\s*-\s*([\d.]+)\s+sec\s+"
    r"[\d.]+\s+\S+\s+"
    r"([\d.]+)\s+Mbits/sec"
)

_ZERO_RE = re.compile(
    r"\[\s*\d+\]\s+"
    r"([\d.]+)\s*-\s*([\d.]+)\s+sec\s+"
    r"0\.00\s+Bytes\s+"
    r"0\.00\s+bits/sec"
)


def parse_iperf_file(filepath: str) -> List[IperfInterval]:
    """
    Parses an iperf2 UDP server output file.
    Returns a list of IperfInterval sorted by t_start.
    Skips the summary line (0.0-XX sec total).
    Ignores out-of-order datagram lines.
    """
    with open(filepath, 'r', encoding='utf-8') as fh:
        lines = fh.readlines()

    raw: List[IperfInterval] = []

    for line in lines:
        if 'out-of-order' in line:
            continue

        m = _INTERVAL_RE.search(line)
        if m:
            t0, t1, bw = float(m.group(1)), float(m.group(2)), float(m.group(3))
            raw.append(IperfInterval(t_start=t0, t_end=t1, bw_mbps=bw))
            continue

        z = _ZERO_RE.search(line)
        if z:
            t0, t1 = float(z.group(1)), float(z.group(2))
            raw.append(IperfInterval(t_start=t0, t_end=t1, bw_mbps=0.0))

    if not raw:
        raise ValueError(f"No iperf intervals found in '{filepath}'")

    # Drop summary line (t_start=0.0 spanning >= 95% of total duration)
    max_end   = max(iv.t_end for iv in raw)
    intervals = [iv for iv in raw
                 if not (iv.t_start == 0.0 and iv.t_end >= max_end * 0.95)]

    intervals.sort(key=lambda iv: iv.t_start)
    return intervals


# ─────────────────────────────────────────────────────────────────────────────
# Scenario helpers
# ─────────────────────────────────────────────────────────────────────────────

def scenario_to_events(scenario: list) -> List[FailureEvent]:
    """
    Converts a failure_simulation.py scenario list into FailureEvent objects.
    Timeline is cumulative: each failure starts after the previous one ends.
    """
    events      = []
    cursor      = 0.0
    colors_down = ["#e74c3c", "#e67e22", "#9b59b6", "#c0392b"]
    colors_up   = ["#2ecc71", "#1abc9c", "#27ae60", "#16a085"]

    for i, item in enumerate(scenario):
        wait      = float(item.get('wait_before', 5))
        dur       = float(item.get('failure_duration', 10))
        t_fail    = cursor + wait
        t_recover = t_fail + dur
        cursor    = t_recover

        events.append(FailureEvent(
            label         = f"{item['sw1']}<->{item['sw2']}",
            failure_start = t_fail,
            failure_end   = t_recover,
            color_down    = colors_down[i % len(colors_down)],
            color_up      = colors_up[i % len(colors_up)],
        ))

    return events


# ─────────────────────────────────────────────────────────────────────────────
# Plotting engine
# ─────────────────────────────────────────────────────────────────────────────

PALETTE = {
    'bg'         : '#0f1117',
    'ax_bg'      : '#161b22',
    'grid'       : '#30363d',
    'text'       : '#e6edf3',
    'text_dim'   : '#8b949e',
    'bandwidth'  : '#58a6ff',
    'fill'       : '#1f6feb',
    'fill_alpha' : 0.18,
}


def _apply_dark_theme(fig, ax):
    fig.patch.set_facecolor(PALETTE['bg'])
    ax.set_facecolor(PALETTE['ax_bg'])
    for spine in ax.spines.values():
        spine.set_color(PALETTE['grid'])
    ax.tick_params(colors=PALETTE['text'], labelsize=10)
    ax.xaxis.label.set_color(PALETTE['text'])
    ax.yaxis.label.set_color(PALETTE['text'])
    ax.title.set_color(PALETTE['text'])
    ax.grid(True, color=PALETTE['grid'], linestyle='--', linewidth=0.6, alpha=0.7)


def plot_resilience(
    intervals            : List[IperfInterval],
    events               : List[FailureEvent],
    output_path          : str  = "resilience_plot.png",
    title                : str  = "Resilience of the SDN Network (Ring Topology)",
    dark_mode            : bool = True,
    annotate_recovery_bw : bool = True,
):
    # ── Arrays ───────────────────────────────────────────────────────────────
    xs  = np.array([(iv.t_start + iv.t_end) / 2 for iv in intervals])
    bws = np.array([iv.bw_mbps for iv in intervals])

    # Light rolling average (window=3) on non-zero segments only
    bws_smooth = bws.copy()
    for i in range(1, len(bws) - 1):
        if bws[i] > 0 and bws[i-1] > 0 and bws[i+1] > 0:
            bws_smooth[i] = (bws[i-1] + bws[i] + bws[i+1]) / 3

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 6))
    if dark_mode:
        _apply_dark_theme(fig, ax)
    else:
        ax.grid(True, linestyle='--', linewidth=0.6, alpha=0.5)

    # ── Bandwidth curve ───────────────────────────────────────────────────────
    ax.plot(xs, bws_smooth, color=PALETTE['bandwidth'], linewidth=2.0,
            zorder=4, label='Measured UDP bitrate')
    ax.fill_between(xs, bws_smooth, alpha=PALETTE['fill_alpha'],
                    color=PALETTE['fill'], zorder=3)

    # ── Failure markers ───────────────────────────────────────────────────────
    legend_handles = [
        plt.Line2D([0], [0], color=PALETTE['bandwidth'], linewidth=2,
                   label='Measured UDP bitrate'),
    ]

    for ev in events:
        ax.axvspan(ev.failure_start, ev.failure_end,
                   alpha=0.08, color=ev.color_down, zorder=1)
        ax.axvline(ev.failure_start, color=ev.color_down,
                   linestyle='--', linewidth=1.8, zorder=5)
        ax.axvline(ev.failure_end, color=ev.color_up,
                   linestyle='--', linewidth=1.8, zorder=5)

        ax.text(ev.failure_start + 0.3, 0.45,
                f'▼ Outage  {ev.label}',
                transform=ax.get_xaxis_transform(),
                color=ev.color_down, fontsize=8.5, fontweight='bold',
                rotation=90, va='center', zorder=6)
        ax.text(ev.failure_end + 0.3, 0.45,
                f'▲ Rerouting  {ev.label}',
                transform=ax.get_xaxis_transform(),
                color=ev.color_up, fontsize=8.5, fontweight='bold',
                rotation=90, va='center', zorder=6)

        legend_handles.append(mpatches.Patch(
            color=ev.color_down, alpha=0.6,
            label=f'Outage {ev.label} '
                  f'(t={ev.failure_start:.0f}s → t={ev.failure_end:.0f}s)'
        ))

        # Rerouting convergence annotation
        post = [(iv.t_start, iv.bw_mbps)
                for iv in intervals
                if iv.t_start >= ev.failure_end and iv.bw_mbps > 0.5]
        if post and annotate_recovery_bw:
            t_restored, bw_restored = post[0]
            delta = t_restored - ev.failure_end
            ax.annotate(
                f'Re-routing\nDt = {delta:.1f}s\nBitrate = {bw_restored:.1f} Mbps',
                xy=(t_restored, bw_restored),
                xytext=(t_restored + 1.5, bw_restored * 1.15),
                arrowprops=dict(arrowstyle='->', color=ev.color_up, lw=1.5),
                color=ev.color_up, fontsize=8,
                bbox=dict(boxstyle='round,pad=0.3',
                          facecolor=PALETTE['ax_bg'], edgecolor=ev.color_up,
                          alpha=0.85),
                zorder=7
            )

    # ── Axes ─────────────────────────────────────────────────────────────────
    ax.set_xlabel('Time (s)', fontsize=12, labelpad=8)
    ax.set_ylabel('Bitrate (Mbps)', fontsize=12, labelpad=8)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=14)

    x_max = max(xs[-1] + 2,
                (events[-1].failure_end + 5) if events else xs[-1] + 2)
    ax.set_xlim(left=0, right=x_max)
    ax.set_ylim(bottom=0, top=ax.get_ylim()[1] * 1.25)

    # ── Stats box ─────────────────────────────────────────────────────────────
    stable_bws = [iv.bw_mbps for iv in intervals
                  if iv.bw_mbps > 1.0
                  and all(not (ev.failure_start <= iv.t_start <= ev.failure_end)
                          for ev in events)]
    if stable_bws:
        stats_txt = (
            f"Average stable bitrate : {np.mean(stable_bws):.1f} Mbps\n"
            f"Peak bitrate           : {np.max(stable_bws):.1f} Mbps\n"
            f"Simulated outages      : {len(events)}"
        )
        ax.text(0.015, 0.975, stats_txt,
                transform=ax.transAxes, fontsize=8.5, verticalalignment='top',
                color=PALETTE['text_dim'] if dark_mode else '#555',
                bbox=dict(boxstyle='round,pad=0.4',
                          facecolor=PALETTE['bg'] if dark_mode else '#f8f8f8',
                          edgecolor=PALETTE['grid'], alpha=0.85))

    # ── Legend ────────────────────────────────────────────────────────────────
    leg = ax.legend(handles=legend_handles, loc='upper right',
                    fontsize=9, framealpha=0.85,
                    facecolor=PALETTE['bg'] if dark_mode else 'white',
                    edgecolor=PALETTE['grid'])
    for text in leg.get_texts():
        text.set_color(PALETTE['text'] if dark_mode else 'black')

    plt.tight_layout()
    plt.savefig(output_path, dpi=180, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[OK] Plot saved → {output_path}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Plot SDN resilience from an iperf2 UDP server output file"
    )
    parser.add_argument('--iperf', type=str, required=True,
                        help="Path to iperf server results file")
    parser.add_argument('--failure-start', type=float, default=None,
                        help="Single failure: start time in seconds")
    parser.add_argument('--failure-end', type=float, default=None,
                        help="Single failure: end time in seconds")
    parser.add_argument('--label', type=str, default='s1<->s2',
                        help="Link label for single-failure mode (default: s1<->s2)")
    parser.add_argument('--scenario', type=str, default=None,
                        help='JSON array of failure dicts '
                             '(same format as simulate_scenario)')
    parser.add_argument('--output', type=str, default='resilience_plot.png',
                        help="Output PNG path (default: resilience_plot.png)")
    parser.add_argument('--title', type=str,
                        default='SDN Network Resilience (Ring Topology)',
                        help="Plot title")
    parser.add_argument('--light', action='store_true',
                        help="Use light theme instead of dark")

    args = parser.parse_args()

    # ── Parse iperf data ──────────────────────────────────────────────────────
    intervals = parse_iperf_file(args.iperf)
    print(f"[INFO] Parsed {len(intervals)} intervals from '{args.iperf}'")

    # ── Build failure events ──────────────────────────────────────────────────
    events: List[FailureEvent] = []

    if args.scenario:
        events = scenario_to_events(json.loads(args.scenario))
        print(f"[INFO] {len(events)} failure event(s) from --scenario")

    elif args.failure_start is not None and args.failure_end is not None:
        events = [FailureEvent(
            label         = args.label,
            failure_start = args.failure_start,
            failure_end   = args.failure_end,
        )]
        print(f"[INFO] Single failure: {args.label} "
              f"t={args.failure_start}s → t={args.failure_end}s")

    else:
        # Auto-detect: find the longest consecutive 0-Mbps run
        zero_runs: List[Tuple[float, float]] = []
        run_start = None
        for iv in intervals:
            if iv.bw_mbps == 0.0:
                if run_start is None:
                    run_start = iv.t_start
            else:
                if run_start is not None:
                    zero_runs.append((run_start, iv.t_start))
                    run_start = None
        if run_start is not None:
            zero_runs.append((run_start, intervals[-1].t_end))

        if zero_runs:
            t0, t1 = max(zero_runs, key=lambda r: r[1] - r[0])
            events = [FailureEvent(label='auto-detected',
                                   failure_start=t0, failure_end=t1)]
            print(f"[INFO] Auto-detected failure window: t={t0:.1f}s → t={t1:.1f}s")
        else:
            print("[WARN] No failure window found — plotting without markers")

    # ── Render ────────────────────────────────────────────────────────────────
    plot_resilience(
        intervals   = intervals,
        events      = events,
        output_path = args.output,
        title       = args.title,
        dark_mode   = not args.light,
    )


if __name__ == '__main__':
    main()
