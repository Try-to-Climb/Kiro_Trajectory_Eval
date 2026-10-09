"""Task 6: render a provenance graph so a human can read it.

Structured as one selection pass plus per-format emitters. Selection is where
the judgement lives -- which turns, which confidence levels, what to do with an
entity whose producer falls outside the window -- and it is shared. Emitting is
string templating, ~40 lines per format, so adding DOT or PROV-JSON later means
adding a function rather than reworking the traversal.

On formats: mermaid is not a graph interchange standard, it is a rendering
syntax. The W3C PROV model this module implements does have official
serializations (PROV-N, PROV-JSON, PROV-O), and Graphviz DOT lays out large
DAGs far better. Mermaid is chosen for Task 6 because Task 6's purpose is
narrow -- look at the graph and judge whether the approach holds -- and mermaid
renders inline in GitLab and this repo's own markdown with no toolchain at all.
See DESIGN.md §8.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .schema import (CERTAIN, GENERATED_BY, INFORMED_BY, USED_BY, WEAK,
                     Activity, Edge, Entity, ProvGraph)

__all__ = ["View", "select", "to_mermaid", "to_dot", "to_text",
           "EDGE_COLOURS"]


@dataclass
class View:
    """The subgraph chosen for rendering.

    `external entities` are kept deliberately: with `--turns 6`, a file the window
    reads may have been produced in turn 3, and dropping it would hide the fact
    that this turn depends on earlier work -- which is one of the more useful
    things the graph shows.
    """
    activities: list[Activity] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    outside: set[str] = field(default_factory=set)     # entity ids from other turns
    hidden_isolated: int = 0
    turns: Optional[tuple[int, ...]] = None
    min_confidence: str = WEAK


def select(g: ProvGraph, turns: Optional[Iterable[int]] = None,
           min_confidence: str = WEAK, hide_isolated: bool = False) -> View:
    """Pick the nodes and edges to render.

    An edge survives only if it clears `min_confidence` *and* touches an
    activity in the window. Entities are then whatever those edges reach, so a
    version chain is never left with a dangling half.
    """
    wanted = tuple(sorted(set(turns))) if turns is not None else None
    in_window = {a.node_id: a for a in g.activities.values()
                 if wanted is None or a.turn in wanted}

    edges = [e for e in g.filter_edges(min_confidence)
             if e.src in in_window or e.dst in in_window]

    reached: set[str] = set()
    for e in edges:
        for node in (e.src, e.dst):
            if node in g.entities:
                reached.add(node)

    linked = {e.src for e in edges} | {e.dst for e in edges}
    acts, hidden = [], 0
    for a in sorted(in_window.values(), key=lambda x: x.idx):
        if hide_isolated and a.node_id not in linked:
            hidden += 1
            continue
        acts.append(a)

    # An entity is "outside" when the activity that produced it is not shown.
    shown = {a.node_id for a in acts}
    outside = {n for n in reached
               if g.entities[n].producer_ref
               and f"a:{g.entities[n].producer_ref}" not in shown}

    return View(
        activities=acts,
        entities=sorted((g.entities[n] for n in reached),
                        key=lambda e: (e.born_idx, e.key, e.version)),
        edges=edges,
        outside=outside,
        hidden_isolated=hidden,
        turns=wanted,
        min_confidence=min_confidence,
    )


# ---- mermaid -------------------------------------------------------------
# `purpose` is free text and routinely contains characters that terminate a
# mermaid node label. Unescaped, one quote in one purpose breaks the whole
# diagram, so this is not cosmetic.
#
# Mermaid's escape is `#<entity>;`, which means the replacements cannot simply
# be applied in sequence: substituting `;` after `"` would turn the `#quot;`
# just produced into `#quot#59;`. Each character is mapped in a single pass.
_MERMAID_ESCAPES = {
    "#": "#35;",           # the escape introducer itself
    "&": "#amp;",
    '"': "#quot;",
    "'": "#39;",
    "[": "#91;",
    "]": "#93;",
    "(": "#40;",
    ")": "#41;",
    "{": "#123;",
    "}": "#125;",
    "<": "#lt;",
    ">": "#gt;",
    "|": "#124;",
    ";": "#59;",
}


def _esc(text: str) -> str:
    text = " ".join(text.split())          # newlines would end the statement
    return "".join(_MERMAID_ESCAPES.get(ch, ch) for ch in text)


def _mid(node_id: str) -> str:
    """A short mermaid-safe node id.

    Real ids hold `/`, `#`, `@` and `.`, none of which mermaid accepts, and a
    full path makes the source unreadable -- one entity produced a 76-character
    identifier. A hash of the id keeps it stable and unique; the label carries
    the meaning.
    """
    tag = "a" if node_id.startswith("a:") else "e"
    return tag + hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:8]


def to_mermaid(g: ProvGraph, turns: Optional[Iterable[int]] = None,
               min_confidence: str = WEAK, hide_isolated: bool = False,
               direction: str = "LR") -> str:
    v = select(g, turns, min_confidence, hide_isolated)
    lines = [f"graph {direction}"]

    for a in v.activities:
        lines.append(f'  {_mid(a.node_id)}["a[{a.idx}] {_esc(a.label(48))}"]')

    for e in v.entities:
        label = _esc(e.label())
        if e.node_id in v.outside:
            label += " .ext"
        # Shape carries the kind, so it survives a black-and-white render.
        if e.kind == "pid":
            lines.append(f"  {_mid(e.node_id)}([{label}])")
        else:
            lines.append(f"  {_mid(e.node_id)}({label})")

    for e in v.edges:
        arrow = "-->" if e.confidence == CERTAIN else "-.->"
        lines.append(f"  {_mid(e.src)} {arrow} {_mid(e.dst)}")

    if v.hidden_isolated:
        lines.append(f"  %% {v.hidden_isolated} isolated activities hidden")
    return "\n".join(lines)


# ---- graphviz dot --------------------------------------------------------
# Edge colour says *direction*, dashes say *confidence*. Two channels, so
# neither has to be given up: a reader following the flow of a file cares which
# way it goes, and a reader auditing the graph cares how solid the claim is.
# Blue and orange rather than green and red, which a colour-blind reader cannot
# separate, and both stay legible on the light node fills `dot` produces as well
# as on the dark page `html.py` embeds them in.
# Chosen for a light canvas (ref/web-example3.html): far enough apart in hue to
# tell at a glance, dark enough to read as a hairline on #f8fafc. This is the one
# thing on the page that colour alone has to carry, so it is defined once and the
# html, the dot output and the legend all read it from here.
EDGE_COLOURS = {
    GENERATED_BY: "#c2410c",          # activity produced this -- outward
    USED_BY: "#2563eb",               # activity consumed this -- inward
    INFORMED_BY: "#8250df",           # weak association between activities
}


def to_dot(g: ProvGraph, turns: Optional[Iterable[int]] = None,
           min_confidence: str = WEAK, hide_isolated: bool = False,
           direction: str = "LR", timeline: bool = False,
           drop_pids: bool = False, ranksep: float = 0.7,
           nodesep: float = 0.5,
           sizes: Optional[Mapping[str, tuple[float, float]]] = None) -> str:
    """Graphviz DOT.

    Here to keep the formatter boundary honest -- it reuses `select` untouched,
    which was the claim -- and because `dot`'s layered DAG layout stays legible
    at sizes where mermaid does not.

    `timeline` is for reading a single turn. Two things change:

    * activities are pinned in `idx` order down the page with invisible edges.
      Without them the vertical order is whatever minimised edge crossings, so
      a[26] could sit above a[9] -- fine for seeing structure, useless for
      following what the agent did.
    * every arrow starts at the activity, so an action reads as "I consumed
      this, I produced that". A `usedBy` edge keeps its `entity -> activity`
      form, which is what ranks the file above the action that reads it, and
      gets `dir=back` so only the arrowhead moves. Writing it the other way
      round would tell `dot` the file came into being after the read.

    One rank per activity is why this is not the default: over 277 actions the
    page becomes unreadably tall, which is what the per-turn view exists for.
    """
    def q(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    # Sizes and gaps are stated rather than left to graphviz's defaults, which
    # were tuned for the label-inside-a-box drawing this no longer is. `sizes`
    # carries the size the renderer is going to draw each card at -- a card
    # showing a full command is as tall as that command wraps -- because `dot`
    # has to reserve that room or the cards overlap the row beneath them.
    sizes = sizes or {}

    lines = [f"digraph prov {{",
             f'  rankdir={"TB" if timeline else direction};',
             f'  ranksep={ranksep};',
             f'  nodesep={nodesep};',
             '  node [fontname="sans-serif", fontsize=10, fixedsize=true, '
             'width=2.9, height=0.86];',
             '  edge [fontname="sans-serif", fontsize=8];']
    v = select(g, turns, min_confidence, hide_isolated)

    for a in v.activities:
        wh = sizes.get(a.node_id)
        size = f'width={wh[0]:.3f}, height={wh[1]:.3f}, ' if wh else ""
        lines.append(f'  "{a.node_id}" [shape=box, style="filled,rounded", '
                     f'fillcolor="#dae8fc", {size}'
                     f'label="a[{a.idx}] {q(a.label(60))}"];')
    skip: set[str] = set()
    if drop_pids:
        # Excluded before layout, not hidden after. `dot` reserves a column for
        # every node it is given, so hiding them left ~200px of empty gutter on
        # both sides of every action and the eye crossed it to reach each label.
        skip = {e.node_id for e in v.entities if e.kind == "pid"}
    for e in v.entities:
        if e.node_id in skip:
            continue
        shape = "ellipse" if e.kind == "pid" else "note"
        colour = "#ffe6cc" if e.node_id in v.outside else "#d5e8d4"
        lines.append(f'  "{e.node_id}" [shape={shape}, style=filled, '
                     f'fillcolor="{colour}", width=0.95, height=0.95, '
                     f'label="{q(e.label())}"];')
    for e in v.edges:
        if e.src in skip or e.dst in skip:
            continue
        style = "solid" if e.confidence == CERTAIN else "dashed"
        colour = EDGE_COLOURS.get(e.kind, "#57606a")
        attrs = [f"style={style}", f'color="{colour}"', "penwidth=1.3"]
        if timeline and e.kind == USED_BY:
            attrs.append("dir=back")
        lines.append(f'  "{e.src}" -> "{e.dst}" [{", ".join(attrs)}];')

    if timeline and len(v.activities) > 1:
        # Invisible spine: fixes the order without adding anything to read.
        lines.append("  // idx order, drawn as nothing")
        for prev, nxt in zip(v.activities, v.activities[1:]):
            lines.append(f'  "{prev.node_id}" -> "{nxt.node_id}" '
                         f'[style=invis, weight=100];')
    lines.append("}")
    return "\n".join(lines)


# ---- text ----------------------------------------------------------------
def to_text(g: ProvGraph, turns: Optional[Iterable[int]] = None,
            min_confidence: str = WEAK, hide_isolated: bool = False) -> str:
    """A terminal-readable listing.

    Mermaid needs somewhere to render; this does not, which makes it the format
    for a quick look. Reads are listed before writes so each activity reads
    like what it did: consumed these, produced those.
    """
    v = select(g, turns, min_confidence, hide_isolated)
    by_src: dict[str, list[Edge]] = {}
    by_dst: dict[str, list[Edge]] = {}
    for e in v.edges:
        by_src.setdefault(e.src, []).append(e)
        by_dst.setdefault(e.dst, []).append(e)

    scope = ("all turns" if v.turns is None
             else "turn " + ",".join(str(t) for t in v.turns))
    out = [f"{g.session_id or '(no session)'}  [{scope}, "
           f"≥{v.min_confidence}]  "
           f"{len(v.activities)} activities / {len(v.entities)} entities / "
           f"{len(v.edges)} edges"]

    for a in v.activities:
        ins = by_dst.get(a.node_id, [])
        outs = by_src.get(a.node_id, [])
        out.append(f"  a[{a.idx:>3}]  {a.label(64)}")
        if not ins and not outs:
            out.append("           (isolated)")
        for e in ins:
            ent = g.entities[e.src]
            mark = " ·outside" if e.src in v.outside else ""
            out.append(f"           ← {ent.label()}{mark}   [{e.confidence}]")
        for e in outs:
            ent = g.entities[e.dst]
            out.append(f"           → {ent.label()}   [{e.confidence}]")

    if v.hidden_isolated:
        out.append(f"  ({v.hidden_isolated} isolated activities hidden)")
    return "\n".join(out)
