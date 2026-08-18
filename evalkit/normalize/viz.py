"""Trajectory presentation: timing charts (text/Mermaid/PNG), per-turn summary, equivalent-script reconstruction.

All functions take a TraceIR and are source-independent (hook / official).
Lanes are divided by run: one lane when there is a single run; one lane per
run when multiple (child agents mixed in old data).
"""

from __future__ import annotations

import collections
import json
import os
from datetime import datetime
from typing import Optional

from .schema import TraceIR


def _setup_cjk_font(plt, fm) -> bool:
    """Do our best to load a CJK font so Chinese labels render properly; if none is
    found, keep the matplotlib default (Chinese may appear as boxes but no error).
    The environment variable KIRO_TRACE_FONT can specify a font file."""
    import glob as _glob
    cands = []
    env = os.environ.get("KIRO_TRACE_FONT")
    if env:
        cands.append(env)
    for pat in ("/usr/share/fonts/**/NotoSansCJK*.tt?",
                "/usr/share/fonts/**/*CJK*.tt?",
                "/usr/share/fonts/**/wqy*.tt?",
                "/System/Library/Fonts/**/PingFang*",
                "/System/Library/Fonts/**/*Hei*"):
        cands += _glob.glob(pat, recursive=True)
    for c in cands:
        if c and os.path.isfile(c):
            try:
                fm.fontManager.addfont(c)
                plt.rcParams["font.family"] = fm.FontProperties(fname=c).get_name()
                return True
            except Exception:
                continue
    return False

# Tool action → single-character symbol (for text lanes)
_SYM = {
    "run_command": "$", "read_file": "r", "list_dir": "l", "create_file": "w",
    "modify_file": "w", "search_files": "g", "search_content": "g",
    "summarize": "S", "spawn_subagent": ">", "aws_call": "A",
}
# Tool action → English label
_LABEL = {
    "run_command": "cmd", "read_file": "read", "list_dir": "list",
    "create_file": "write", "modify_file": "modify", "search_files": "find",
    "search_content": "grep", "summarize": "report", "spawn_subagent": "dispatch",
    "aws_call": "aws",
}
# Colors for PNG
_COLOR = {
    "run_command": "#4C78A8", "read_file": "#54A24B", "list_dir": "#88B04B",
    "create_file": "#E45756", "modify_file": "#E45756", "search_files": "#F58518",
    "search_content": "#F58518", "summarize": "#B279A2", "spawn_subagent": "#000000",
    "aws_call": "#9D755D",
}

_SYM_LEGEND = "  ".join(f"{sym}={_LABEL[act]}" for sym, act in
                        {"$": "run_command", "r": "read_file", "l": "list_dir",
                         "w": "create_file", "g": "search_files", "S": "summarize",
                         ">": "spawn_subagent"}.items())


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Layer 2: per-turn summary
# ---------------------------------------------------------------------------

def turn_summary(ir: TraceIR) -> str:
    off = ir.official
    lines = [f"turn summary  session {ir.session_id[:12]}  agent={ir.agent_name or '-'}  "
             f"{ir.runs} spawns / {ir.turns} turns / {len(ir.actions)} actions"]
    if off and off.found:
        lines[0] += f"  {off.total_credits:.3f} credits"
    lines.append(f"{'turn':>4} {'act':>4} {'tools':<26} {'duration':>8} {'credits':>8} {'end':<14}")
    lines.append("-" * 68)
    for turn in range(1, ir.turns + 1):
        acts = ir.by_turn(turn)
        hist = collections.Counter(_LABEL.get(a.action, a.action) for a in acts)
        comp = " ".join(f"{k}×{v}" for k, v in hist.most_common())
        tm = off.turns[turn - 1] if (off and off.found and turn <= len(off.turns)) else None
        dur = f"{tm.duration_s}s" if tm and tm.duration_s else "-"
        cr = f"{tm.credits:.3f}" if tm and tm.credits else "-"
        end = tm.end_reason if tm and tm.end_reason else "-"
        lines.append(f"{turn:>3} {len(acts):>4} {comp:<26} {dur:>6} {cr:>8} {end:<14}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 1: text lanes
# ---------------------------------------------------------------------------

def text_timeline(ir: TraceIR, width: int = 70) -> str:
    runs = sorted({a.run for a in ir.actions}) or [1]
    # x coordinate: prefer timestamps; fall back to idx
    ts_all = [_parse_ts(a.ts) for a in ir.actions if a.ts]
    ts_all = [x for x in ts_all if x]
    use_ts = len(ts_all) >= 2
    if use_ts:
        t0 = min(ts_all)
        span = (max(ts_all) - t0).total_seconds() or 1
        axis = f"timeline {t0.strftime('%H:%M:%S')} → +{span/60:.0f}min  each cell ~{span/width:.0f}s"

        def xpos(a):
            tt = _parse_ts(a.ts)
            return int((tt - t0).total_seconds() / span * (width - 1)) if tt else 0
    else:
        n = max((a.idx for a in ir.actions), default=0) or 1
        axis = f"by action index (total {len(ir.actions)} actions, official source has no per-action time)"

        def xpos(a):
            return int(a.idx / n * (width - 1))

    lines = [axis, f"legend: {_SYM_LEGEND}  x=failed", ""]
    for run in runs:
        row = [" "] * width
        for a in ir.actions:
            if a.run != run:
                continue
            c = "x" if not a.completed else _SYM.get(a.action, "?")
            row[xpos(a)] = c
        label = f"run{run}" if len(runs) > 1 else (ir.agent_name or ir.session_id[:8])
        lines.append(f"{label:<16}|{''.join(row)}|")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mermaid gantt (aggregated by turn)
# ---------------------------------------------------------------------------

def to_mermaid(ir: TraceIR) -> str:
    ts_all = [_parse_ts(a.ts) for a in ir.actions if a.ts]
    ts_all = [x for x in ts_all if x]
    lines = ["```mermaid", "gantt", "    dateFormat X", "    axisFormat %Ss",
             f"    title {ir.agent_name or ir.session_id[:8]} ({len(ir.actions)} actions)"]
    if ts_all:
        t0 = min(ts_all)
        for run in sorted({a.run for a in ir.actions}):
            lines.append(f"    section run{run}")
            byturn = collections.defaultdict(list)
            for a in ir.actions:
                if a.run != run:
                    continue
                tt = _parse_ts(a.ts)
                if tt:
                    byturn[a.turn].append(tt)
            for turn, tss in sorted(byturn.items()):
                s = int((min(tss) - t0).total_seconds())
                e = int((max(tss) - t0).total_seconds())
                if e <= s:
                    e = s + 2
                lines.append(f"    turn{turn} ({len(tss)} acts) :{s}, {e}")
    else:
        # No timestamps: use turn ordinal as the sequence axis
        lines.append("    section turns")
        for turn in range(1, ir.turns + 1):
            n = len(ir.by_turn(turn))
            lines.append(f"    turn{turn} ({n} acts) :{turn-1}, {turn}")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PNG lane chart
# ---------------------------------------------------------------------------

def to_png(ir: TraceIR, out_path: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    from matplotlib.patches import Patch

    # Register the system-bundled Noto CJK to eliminate Chinese boxes
    import glob
    _setup_cjk_font(plt, fm)
    plt.rcParams["axes.unicode_minus"] = False

    runs = sorted({a.run for a in ir.actions}) or [1]
    ts_all = [_parse_ts(a.ts) for a in ir.actions if a.ts]
    ts_all = [x for x in ts_all if x]
    use_ts = len(ts_all) >= 2
    t0 = min(ts_all) if use_ts else None
    span = (max(ts_all) - t0).total_seconds() or 1 if use_ts else 1
    nmax = max((a.idx for a in ir.actions), default=1) or 1

    def xpos(a):
        if use_ts:
            tt = _parse_ts(a.ts)
            return (tt - t0).total_seconds() if tt else 0
        return a.idx

    fig, ax = plt.subplots(figsize=(14, 1.2 + 0.7 * len(runs)))
    for i, run in enumerate(runs):
        y = len(runs) - i
        for a in ir.actions:
            if a.run != run:
                continue
            x = xpos(a)
            if not a.completed:
                ax.scatter(x, y, marker="x", s=90, c="red", zorder=3, linewidths=2)
            else:
                mk = "*" if a.action == "spawn_subagent" else "o"
                sz = 170 if a.action == "spawn_subagent" else 55
                ax.scatter(x, y, marker=mk, s=sz, c=_COLOR.get(a.action, "#888"),
                           zorder=3, edgecolors="white", linewidths=0.5)
        ax.axhline(y, color="#ddd", lw=0.6, zorder=1)

    ax.set_yticks(range(1, len(runs) + 1))
    ax.set_yticklabels([f"run{r}" for r in reversed(runs)] if len(runs) > 1
                       else [ir.agent_name or ir.session_id[:8]], fontsize=9)
    ax.set_xlabel(f"relative time (s), origin {t0.strftime('%H:%M:%S')}" if use_ts
                  else "action index")
    ax.set_title(f"{ir.agent_name or ir.session_id[:8]} timeline  (× = failed/incomplete)", fontsize=11)
    ax.set_ylim(0.3, len(runs) + 0.7)
    seen = {a.action for a in ir.actions}
    legend = [Patch(facecolor=_COLOR.get(k, "#888"), label=_LABEL.get(k, k))
              for k in _LABEL if k in seen]
    if legend:
        ax.legend(handles=legend, ncol=min(7, len(legend)), fontsize=7,
                  loc="upper center", bbox_to_anchor=(0.5, -0.3), frameon=False)
    plt.tight_layout()
    plt.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Equivalent-script reconstruction
# ---------------------------------------------------------------------------

def to_replay(ir: TraceIR) -> str:
    lines = ["#!/bin/bash",
             f"# Restored from action sequence of session {ir.session_id[:12]} ({ir.agent_name or '-'})",
             "# For audit reading only; contains comment lines (non-shell actions) and side-effect commands, do not blindly rerun", ""]
    cur = None
    for a in ir.actions:
        tag = (a.run, a.turn)
        if tag != cur:
            cur = tag
            hdr = f"run{a.run} turn{a.turn}" if ir.runs > 1 else f"turn{a.turn}"
            lines.append(f"\n# ===== {hdr} =====")
        mark = "" if a.completed else "   # ✗ incomplete/failed"
        act = a.action
        if act == "run_command":
            lines.append((a.command or "").rstrip() + mark)
        elif act == "read_file":
            lines.append(f"cat {a.path or '?'}{mark}")
        elif act == "list_dir":
            lines.append(f"ls {a.path or '?'}{mark}")
        elif act in ("create_file", "modify_file"):
            lines.append(f"# [side effect: {act}] {a.path or '?'}{mark}")
        elif act in ("search_files", "search_content"):
            lines.append(f"# [search] {a.pattern or ''} @ {a.root or ''}{mark}")
        elif act == "spawn_subagent":
            lines.append(f"# >>> dispatch sub-agent: {a.pattern or '?'} (stage={a.command or ''}){mark}")
        elif act == "summarize":
            lines.append(f"# [report] {(a.pattern or '')[:70]}{mark}")
        else:
            lines.append(f"# [{act}]{mark}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Two-source comparison
# ---------------------------------------------------------------------------

def compare_sources(session_id: str, hook_trace_path: Optional[str],
                    official_dir: Optional[str] = None) -> str:
    from .core import normalize_file
    from .mapping import canonical_tool
    from .official_loader import load_trace_from_official

    hook = normalize_file(hook_trace_path, enrich=False) if hook_trace_path else None
    off = load_trace_from_official(session_id, official_dir)

    def ms(ir):
        return collections.Counter(canonical_tool(a.tool) for a in ir.actions) if ir else collections.Counter()

    h_ms, o_ms = ms(hook), ms(off)
    L = []
    L.append(f"two-source comparison  session {session_id[:12]}")
    L.append(f"  {'dimension':<16}{'hook':>18}{'official':>18}")
    L.append("  " + "-" * 52)
    L.append(f"  {'actions':<16}{str(len(hook.actions)) if hook else '-':>18}{len(off.actions):>18}")
    L.append(f"  {'turns':<16}{str(hook.turns) if hook else '-':>18}{off.turns:>18}")
    L.append(f"  {'agent name':<15}{str(hook.agent_name) if hook else '-':>18}{str(off.agent_name):>18}")
    L.append(f"  {'runs':<16}{str(hook.runs) if hook else '-':>18}{off.runs:>18}")
    hc = sum(a.completed for a in hook.actions) if hook else 0
    oc = sum(a.completed for a in off.actions)
    L.append(f"  {'done/total':<14}{(str(hc)+'/'+str(len(hook.actions))) if hook else '-':>18}"
             f"{str(oc)+'/'+str(len(off.actions)):>18}")
    L.append(f"  {'credits':<16}{'none':>18}{str(off.credits):>18}")
    only_off = o_ms - h_ms
    only_hook = h_ms - o_ms
    if only_off:
        L.append(f"  only in official: {dict(only_off)}  (hook cannot see: rejected by validation, etc.)")
    if only_hook:
        L.append(f"  only in hook: {dict(only_hook)}")
    if hook and not only_off and not only_hook:
        L.append("  ✅ tool call multisets fully match")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Cross-session panorama: parent + child agents share a single timeline
# (shows orchestration and parallelism)
# ---------------------------------------------------------------------------

def multi_session_text(lanes: list[tuple[str, TraceIR]], width: int = 72) -> str:
    """lanes: [(label, ir), ...]; placed on a common axis by each action's real timestamp."""
    allts = []
    for _, ir in lanes:
        allts += [_parse_ts(a.ts) for a in ir.actions if a.ts]
    allts = [x for x in allts if x]
    if len(allts) < 2:
        return "(insufficient timestamps to draw panorama)"
    t0 = min(allts)
    span = (max(allts) - t0).total_seconds() or 1
    out = [f"orchestration panorama  {len(lanes)} sessions  start {t0.strftime('%H:%M:%S')} → +{span/60:.0f}min",
           f"legend: {_SYM_LEGEND}  x=failed", ""]
    for label, ir in lanes:
        row = [" "] * width
        for a in ir.actions:
            tt = _parse_ts(a.ts)
            if not tt:
                continue
            pos = int((tt - t0).total_seconds() / span * (width - 1))
            row[pos] = "x" if not a.completed else _SYM.get(a.action, "?")
        out.append(f"{label:<26}|{''.join(row)}|")
    return "\n".join(out)


def multi_session_png(lanes: list[tuple[str, TraceIR]], out_path: str,
                      completed_override: Optional[dict] = None) -> str:
    """completed_override: {session_id: {idx: bool}} — use another source's (official)
    success/failure to override completed on actions in the lanes, resolving the
    issue that hook timestamps are accurate but success/failure can be misjudged."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    from matplotlib.patches import Patch
    import glob

    _setup_cjk_font(plt, fm)
    plt.rcParams["axes.unicode_minus"] = False

    allts = []
    for _, ir in lanes:
        allts += [_parse_ts(a.ts) for a in ir.actions if a.ts]
    allts = [x for x in allts if x]
    if len(allts) < 2:
        raise ValueError("insufficient timestamps")
    t0 = min(allts)

    fig, ax = plt.subplots(figsize=(14, 1.2 + 0.7 * len(lanes)))
    for i, (label, ir) in enumerate(lanes):
        y = len(lanes) - i
        ov = (completed_override or {}).get(ir.session_id, {})
        for a in ir.actions:
            tt = _parse_ts(a.ts)
            if not tt:
                continue
            x = (tt - t0).total_seconds()
            done = ov.get(a.idx, a.completed)   # use the override if present (official), else this source
            if not done:
                ax.scatter(x, y, marker="x", s=90, c="red", zorder=3, linewidths=2)
            else:
                mk = "*" if a.action == "spawn_subagent" else "o"
                sz = 180 if a.action == "spawn_subagent" else 55
                ax.scatter(x, y, marker=mk, s=sz, c=_COLOR.get(a.action, "#888"),
                           zorder=3, edgecolors="white", linewidths=0.5)
        ax.axhline(y, color="#ddd", lw=0.6, zorder=1)

    ax.set_yticks(range(1, len(lanes) + 1))
    ax.set_yticklabels([lab for lab, _ in reversed(lanes)], fontsize=9)
    ax.set_xlabel(f"relative time (s)  origin {t0.strftime('%H:%M:%S')}")
    ax.set_title("orchestration panorama: parent agent + child agents share a timeline  (★=dispatch ×=failed)", fontsize=11)
    ax.set_ylim(0.3, len(lanes) + 0.7)
    seen = {a.action for _, ir in lanes for a in ir.actions}
    legend = [Patch(facecolor=_COLOR.get(k, "#888"), label=_LABEL.get(k, k))
              for k in _LABEL if k in seen]
    if legend:
        ax.legend(handles=legend, ncol=min(7, len(legend)), fontsize=7,
                  loc="upper center", bbox_to_anchor=(0.5, -0.3), frameon=False)
    plt.tight_layout()
    plt.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path
