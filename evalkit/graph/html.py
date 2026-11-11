"""Task 6b: an interactive single-file HTML view.

A PNG shows the shape but nothing else -- `a[190] Read the engine lifecycle…`
is truncated and there is no way to reach the command, the arguments, or the
evidence behind an edge. That detail is the point of a provenance graph, so it
has to be reachable.

Layout is delegated to graphviz rather than a JavaScript graph library. `dot`
already produces the layered layout the PNGs proved readable, and it emits SVG
with the full `node_id` in each `<title>`, which is enough to bind the picture
back to the data. The alternative -- cytoscape or d3 -- means either a CDN
reference (breaks offline, and this repo's session-viz deliberately avoids it)
or a megabyte of bundled JavaScript.

Conventions follow `session-viz/`: self-contained, vanilla JS, dark theme, data
embedded as JSON in a script block.
"""
from __future__ import annotations

import html
import json
import shutil
import subprocess
from typing import Any, Iterable, Mapping, Optional, Sequence

import collections

from . import artifacts
from . import layout as layout_mod
from . import overview as overview_mod
from .annotate_schema import TagSet, tagset_to_dict
from .export import EDGE_COLOURS, select, to_dot
from .schema import (CERTAIN, CONNECTED_BY, GENERATED_BY, USED_BY, ProvGraph,
                     WEAK)

__all__ = ["to_html", "HtmlError"]

# `HtmlError` is kept as the name callers catch; layout failures are the only
# way this module can fail, so it aliases the layout error rather than wrapping.


# The only way this module fails is graphviz being missing or refusing the graph,
# which `layout` already reports. Aliasing keeps `except HtmlError` working in
# cli.py rather than wrapping one error in another.
HtmlError = layout_mod.LayoutError


def _node_payload(g: ProvGraph, v) -> dict[str, Any]:
    """Everything the panel can show, keyed by node_id.

    Deliberately includes the fields a PNG cannot: the full purpose, the
    command, the reasoning when present, and for every edge the evidence string
    that justifies it. An edge whose provenance cannot be inspected is not
    auditable, which is why `Edge.evidence` is required in the first place.

    Two things the graph already knows are carried in as well, because they
    answer the questions a reader asks first. An entity gets its fate from
    `artifacts` -- left behind, read downstream, or replaced -- so a deliverable
    and an abandoned scratch file stop looking identical. An isolated activity
    gets the warnings recorded for it, which is usually the reason it is
    isolated: `unresolved path 'LOGFILE=$(…)'` says the value was never knowable,
    not that the action touched nothing.
    """
    shown = {a.node_id for a in v.activities} | {e.node_id for e in v.entities}
    out: dict[str, Any] = {}
    refused_refs = _failed_refs(g)

    fates = {f.node_id: f for f in artifacts.classify(g, include_pids=True)}
    linked = {n for e in g.edges for n in (e.src, e.dst)}
    by_ref: dict[str, list[str]] = {}
    for w in g.warnings:
        ref, _, rest = w.partition(":")
        by_ref.setdefault(ref.strip(), []).append(rest.strip())

    for a in v.activities:
        out[a.node_id] = {
            "type": "activity",
            "ref": a.ref, "idx": a.idx, "turn": a.turn, "action": a.action,
            "purpose": a.purpose or "", "reasoning": a.reasoning or "",
            "command": a.command or "", "path": a.path or "",
            # What a probe action looked for. Carried as a dict so the renderer
            # can show the fields it has without inventing the ones it lacks
            # (`list_dir` has `path` only, `code_search_symbols` has `pattern`
            # only). Empty for every non-probe action, where the panel reads
            # `command` / `path` directly.
            "probe": a.probe or None,
            "isolated": a.node_id not in linked,
            "warnings": by_ref.get(a.ref, []),
            # A call the tool refused is the one case where "no edges" is the
            # correct answer rather than a gap in extraction, and the drawing has
            # to say which -- read from the picture alone, an action with a path
            # and no line looks like a bug in this program. Two of 277 in the
            # reference session; both were a path that did not exist.
            "failed": a.ref in refused_refs,
        }
    for e in v.entities:
        f = fates.get(e.node_id)
        out[e.node_id] = {
            "type": "entity",
            "key": e.key, "version": e.version, "kind": e.kind,
            "producer": e.producer_ref or "", "born_idx": e.born_idx,
            "outside": e.node_id in v.outside,
            "fate": f.fate if f else "",
            "wasted": bool(f and f.written_and_never_read),
            "superseded_by": (f.superseded_by or "") if f else "",
        }

    for node in out.values():
        node["in"], node["out"] = [], []
    for edge in v.edges:
        rec = {"src": edge.src, "dst": edge.dst, "kind": edge.kind,
               "confidence": edge.confidence, "evidence": edge.evidence}
        if edge.src in out:
            out[edge.src]["out"].append(rec)
        if edge.dst in out:
            out[edge.dst]["in"].append(rec)
    return out


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>provenance · {session}</title>
<style>
  /* Palette follows ref/web-example3.html: a light slate canvas, white cards with
     a hairline border and a small shadow, pastel badges. That file pulls Tailwind,
     Lucide and two Google fonts from three CDNs; this page has to open from a
     filesystem, offline, inside a restricted preview, so the palette is written
     out by hand and the fonts fall back to whatever the machine has.

     The earlier pass followed /example/project -- a dark gradient with
     glass panels. It was legible, but read as a visualisation rather than as a
     tool; this one reads like a trace inspector, which is what it is.

     The drawing itself is still ours. `dot -Tsvg` emits boxes with the label
     inside, and no amount of CSS turns that into a card with a badge and a rule.
     `graph/layout.py` takes positions from `dot -Tjson` instead, so the layered
     DAG -- the reason the picture is readable, and what guarantees idx order in
     timeline mode -- is kept while the look is not dictated by graphviz. */
  :root {{
    /* A shade darker than the reference's slate-50: every card on this page is
       white, and on #f8fafc a card's own border was doing all the work of
       separating it from the canvas. */
    --bg:#e9eef4;
    --chrome:#ffffff;        /* header, side panel, cards */
    --line:#e2e8f0;          /* slate-200, hairline borders */
    --line2:#cbd5e1;         /* slate-300, connectors and rules */
    --line3:#94a3b8;         /* slate-400, hover borders */
    --fg:#1e293b;            /* slate-800 */
    --fg2:#475569;           /* slate-600 */
    --dim:#6b7280;           /* gray-500 */
    --faint:#9ca3af;         /* gray-400 */
    --sel:#3b82f6;           /* blue-500, the selected ring */
    --code:#f9fafb;          /* gray-50, code blocks */

    /* Reads and writes. Two hues far enough apart to tell at a glance on white,
       and the only thing on the page that colour alone has to carry. */
    --read:#2563eb;
    --write:#c2410c;

    /* Badges, as pastel triples. Actions are all one colour on purpose: the card
       says `read file` in words on its first line, and a hue per kind was the
       same fact told twice. Only refusal gets its own. */
    --act-bg:#f1f5f9;   --act-fg:#475569;   --act-bd:#e2e8f0;
    --turn-bg:#dcfce7;  --turn-fg:#15803d;  --turn-bd:#bbf7d0;
    --fail-bg:#ffe4e6;  --fail-fg:#be123c;  --fail-bd:#fecdd3;
    --warn-bg:#fef3c7;  --warn-fg:#b45309;  --warn-bd:#fde68a;

    --ent:#64748b;           /* a file: neutral, the shape carries the meaning */
    --pid:#7c3aed;           /* a background process */
    --host:#0e7490;          /* a remote endpoint: ssh / docker / k8s */
    --outside:#b45309;       /* an artifact from before the window */
    --ok:#16a34a;
  }}
  * {{ box-sizing:border-box; }}
  html, body {{
    margin:0; height:100%; color:var(--fg); overflow:hidden;
    background:var(--bg);
    font:14px/1.55 Inter,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",
         Arial,sans-serif;
    -webkit-font-smoothing:antialiased; user-select:none;
  }}
  .mono {{ font-family:"Fira Code",ui-monospace,"SF Mono",Menlo,monospace; }}

  /* ---- header ---------------------------------------------------------- */
  /* Counts in the header, as the reference has them: knowing there are 18 actions
     and 32 links before reading any of them is what makes the picture finite. */
  #top {{ position:fixed; top:0; left:0; right:0; height:78px; z-index:20;
          display:flex; align-items:center; gap:20px; padding:0 26px;
          background:var(--chrome); border-bottom:1px solid var(--line); }}
  .brand {{ font-size:25.5px; font-weight:700; letter-spacing:-.01em;
            color:#0f172a; }}
  .brand .sub {{ font-weight:400; font-size:18.8px; color:var(--faint);
                 margin-left:9px;
                 font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  .stats {{ display:flex; gap:8px; }}
  .stat {{ background:var(--bg); border:1px solid var(--line);
           padding:4px 11px; border-radius:6px; font-size:18.8px;
           color:var(--dim); }}
  .stat b {{ color:var(--fg); font-weight:600; margin-left:3px; }}
  #crumbs {{ display:flex; align-items:center; gap:4px; font-size:20.2px;
             color:var(--dim); }}
  #crumbs .c {{ cursor:pointer; padding:4px 9px; border-radius:6px;
                border:1px solid transparent; }}
  #crumbs .c:hover {{ background:var(--bg); color:var(--fg); }}
  #crumbs .c.here {{ color:var(--fg); font-weight:600; cursor:default; }}
  #crumbs .sep {{ color:var(--line2); }}
  #vsel {{ background:var(--chrome); color:var(--fg2); font-family:inherit;
           font-size:20.2px; font-weight:500; border:1px solid var(--line);
           border-radius:6px; padding:5px 9px; outline:none; cursor:pointer; }}
  #vsel:hover {{ border-color:var(--line3); }}
  #find {{ margin-left:auto; width:290px; padding:7px 13px; font-size:20.2px;
           color:var(--fg); background:var(--chrome); font-family:inherit;
           border:1px solid var(--line); border-radius:6px; outline:none; }}
  #find:focus {{ border-color:var(--sel);
                 box-shadow:0 0 0 3px rgba(59,130,246,.12); }}
  #find::placeholder {{ color:var(--faint); }}

  /* The user's turn prompt, shown only inside a turn view. Fixed under the
     header so it does not scroll with the graph; `#app` padding is bumped from
     JS when the banner is visible so the picture is not covered. */
  #prompt-banner {{ position:fixed; top:78px; left:0; right:0; z-index:19;
                    background:#eef2ff; color:#1e293b;
                    border-bottom:1px solid #c7d2fe;
                    padding:10px 26px; font-size:16px; line-height:1.4;
                    display:none; cursor:pointer; }}
  #prompt-banner.on {{ display:block; }}
  #prompt-banner .pb-label {{ color:#4338ca; font-weight:700;
                              font-size:12px; letter-spacing:.04em;
                              text-transform:uppercase; margin-right:10px; }}
  #prompt-banner .pb-toggle {{ color:#4338ca; font-weight:700;
                               font-size:12px; margin-right:8px;
                               user-select:none; }}
  #prompt-banner .pb-text {{ white-space:pre-wrap; word-break:break-word; }}
  /* Collapsed by default: clamp to one line with ellipsis so a long prompt
     does not cover the graph below. Click the banner to toggle. */
  #prompt-banner.collapsed .pb-text {{ display:-webkit-box;
                                       -webkit-line-clamp:1;
                                       -webkit-box-orient:vertical;
                                       overflow:hidden;
                                       max-height:1.4em; }}

  /* The model's final reply for the turn, as a sticky chip at the bottom
     of the graph pane. The reply is text, not a graph edge -- putting it in
     the picture would need a layout slot it does not have -- but it still
     belongs to the turn, so the chip stays visible while the reader scans
     the actions above. Click opens the full text in the side panel, where
     a long reply (turn 9 of fff7656b is 20 messages worth) has room. */
  #reply-chip {{ position:absolute; left:50%; bottom:16px;
                 transform:translateX(-50%); z-index:10;
                 display:none; max-width:min(680px, calc(100% - 72px));
                 padding:9px 16px; border-radius:999px;
                 background:#ecfdf5; color:#14532d;
                 border:1px solid #86efac; cursor:pointer;
                 box-shadow:0 2px 8px rgba(15,118,110,.12);
                 font: 500 14px/1.3 inherit;
                 display:none; align-items:center; gap:10px;
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  #reply-chip.on {{ display:inline-flex; }}
  #reply-chip:hover {{ background:#d1fae5; border-color:#4ade80; }}
  #reply-chip .rc-label {{ color:#15803d; font-weight:700;
                           font-size:12px; letter-spacing:.04em;
                           text-transform:uppercase; flex:none; }}
  #reply-chip .rc-preview {{ color:#166534; font-weight:400;
                             overflow:hidden; text-overflow:ellipsis;
                             font-family: "Fira Code", ui-monospace, Menlo, monospace;
                             font-size:12.5px; }}

  #app {{ display:flex; height:100%; padding-top:78px; overflow:hidden;
          position:relative; z-index:1; }}
  #graph {{ flex:1; overflow:hidden; position:relative;
            touch-action:none; cursor:grab; background:var(--bg); }}
  #graph.panning {{ cursor:grabbing; }}
  #graph svg {{ display:block; }}
  #side {{ width:545px; flex:none; overflow-y:auto; background:var(--chrome);
           border-left:1px solid var(--line); }}
  #inner {{ padding:20px 24px 34px; }}

  /* ---- the drawing ----------------------------------------------------- */
  .lnk-line {{ fill:none; stroke-linecap:round;
               transition:stroke-opacity .18s ease; }}
  .nd {{ cursor:pointer; }}
  /* A turn card has two jobs: the box focuses the turn's artifacts, the OPEN
     button goes into it. Two cursors, because they are two things. */
  .nd .open {{ cursor:zoom-in; }}
  .nd .open rect {{ transition:fill .16s ease, stroke-opacity .16s ease; }}
  .nd .open:hover rect {{ fill:#bbf7d0; stroke-opacity:.95; }}
  /* The lift on hover is the reference's, and it is the whole of the hover
     feedback: a card that also changed colour looked like it had been selected. */
  .nd .card {{ transition:filter .16s ease, stroke .16s ease; }}
  .nd:hover .card {{ stroke:var(--line3); filter:drop-shadow(0 3px 7px
                     rgba(15,23,42,.1)); }}
  .nd text {{ pointer-events:none; }}
  .nd .t1 {{ font-size:12.5px; font-weight:600; fill:var(--fg); }}
  .nd .t2 {{ font-size:12px; fill:var(--fg2); }}
  .nd .t3 {{ font-size:11px; fill:var(--faint); }}
  /* A command quoted on a card is set in mono, so it reads as a quotation of
     something that ran rather than as more prose. */
  .nd .cmd {{ font-size:11px; fill:var(--dim);
              font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  .nd .bdg {{ font-size:10px; font-weight:700; letter-spacing:.06em; }}
  .nd.sel .card {{ stroke:var(--sel); stroke-width:2;
                   filter:drop-shadow(0 0 0 3px rgba(59,130,246,.2)); }}
  .nd.sel .ring {{ opacity:1; }}
  /* Three levels. Two was not enough contrast to answer "which versions of this
     file are there" at a glance: at .22 the rest of the drawing was still legible
     enough to compete, so the unrelated nodes are blurred as well as faded --
     out of focus reads as "not this" faster than pale does. The actions that
     touched the file stay sharp, because "who edited it" is the question. */
  .dimmed {{ opacity:.13; filter:blur(1.6px); }}
  /* The suggestion list. Typing no longer greys the drawing: it greyed most of a
     view after one letter, which said "almost nothing matches" while the reader
     was still halfway through the word. The list answers the question actually
     being asked -- which of these did I mean -- and picking one goes there. */
  #sug {{ position:fixed; top:70px; right:26px; width:760px; max-height:56vh;
           overflow-y:auto; z-index:30; display:none; background:var(--chrome);
           border:1px solid var(--line2); border-radius:9px;
           box-shadow:0 12px 34px rgba(15,23,42,.16); padding:5px; }}
  .sg {{ display:flex; align-items:baseline; gap:11px; padding:7px 11px;
          border-radius:6px; cursor:pointer; font-size:18.8px;
          white-space:nowrap; }}
  .sg.on, .sg:hover {{ background:#eef2ff; }}
  .sgk {{ flex:none; font-size:13.5px; font-weight:700; text-transform:uppercase;
           letter-spacing:.05em; padding:1px 7px; border-radius:4px;
           background:var(--act-bg); color:var(--act-fg);
           border:1px solid var(--act-bd); }}
  .sgk.k-turn {{ background:var(--turn-bg); color:var(--turn-fg);
                  border-color:var(--turn-bd); }}
  .sgk.k-refused {{ background:var(--fail-bg); color:var(--fail-fg);
                     border-color:var(--fail-bd); }}
  .sgk.k-file, .sgk.k-pid {{ background:#fff; color:var(--ent);
                              border-color:var(--line2); }}
  .sgl {{ flex:none; font-weight:600;
           font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  .sgn {{ flex:1; color:var(--dim); overflow:hidden; text-overflow:ellipsis;
           font-size:17.2px; }}
  .sgv {{ flex:none; color:var(--faint); font-size:15.8px; }}
  .sgnone {{ padding:9px 12px; color:var(--fail-fg); font-weight:600;
              font-size:18.8px; }}
  .sgmore {{ padding:7px 12px; color:var(--faint); font-size:15.8px;
              border-top:1px solid var(--line); }}
  .faded {{ opacity:.62; }}
  .lit {{ opacity:1; }}
  .nd.lit .card {{ stroke-width:2;
                    filter:drop-shadow(0 2px 8px rgba(15,23,42,.16)); }}
  .lnk-line.dimmed {{ opacity:.1; filter:none; }}
  /* The transition is on opacity only: animating a blur repaints the whole
     subtree each frame and 437 nodes could not keep up. */
  .nd, .lnk-line {{ transition:opacity .16s ease; }}
  .lnk-line.hi {{ stroke-opacity:1; stroke-width:2; }}

  /* ---- legend ---------------------------------------------------------- */
  #legend {{ position:fixed; bottom:18px; left:18px; z-index:6;
             pointer-events:auto; font-size:12.5px; padding:10px 13px;
             min-width:200px; background:var(--chrome);
             border:1px solid var(--line); border-radius:8px;
             box-shadow:0 2px 10px rgba(15,23,42,.06); }}
  .lghdr {{ display:flex; align-items:center; justify-content:space-between;
            font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
            color:var(--faint); font-weight:700; margin-bottom:6px; }}
  .lghdr button {{ margin:0; padding:0 7px; font-size:14px; line-height:14px;
                   border-radius:5px; }}
  /* Collapsed it stays a labelled pill, not a stub: the header and its button are
     outside #lgbody, so there is always something to click. */
  #legend.off #lgbody {{ display:none; }}
  #legend.off {{ min-width:0; padding:6px 9px; }}
  #legend.off .lghdr {{ margin-bottom:0; }}
  .lgrow {{ display:flex; align-items:center; gap:7px; margin:3px 0;
            white-space:nowrap; color:var(--fg2); }}
  .lghead {{ color:var(--faint); text-transform:uppercase; letter-spacing:.08em;
             font-size:10.5px; margin-top:8px; font-weight:700; }}
  .lghead:first-child {{ margin-top:0; }}
  .lgdim {{ color:var(--faint); font-weight:400; }}
  .lgdot {{ width:9px; height:9px; border-radius:2.5px; flex:none; }}
  .lgn {{ color:var(--faint); margin-left:auto; padding-left:10px;
          font-variant-numeric:tabular-nums; }}
  .sw {{ width:17px; height:2px; border-radius:2px; flex:none; }}
  .sw.seq {{ background:repeating-linear-gradient(90deg,var(--line2) 0 2px,
             transparent 2px 7px); }}
  .shape {{ width:12px; height:15px; flex:none; border:1.2px solid;
            border-radius:2px; }}
  .shape.ent {{ border-color:var(--ent); background:#fff; }}
  .shape.pid {{ width:11px; height:11px; border-color:var(--pid);
                background:#f5f3ff; transform:rotate(45deg); }}
  .shape.host {{ width:17px; height:11px; border-color:var(--host);
                 background:#ecfeff; border-radius:4px; }}

  /* ---- side panel ------------------------------------------------------ */
  #bar {{ position:sticky; top:0; z-index:2; padding:18px 24px 15px;
          background:var(--chrome); border-bottom:1px solid var(--line); }}
  h1 {{ font-size:21.8px; margin:0 0 7px; font-weight:700; word-break:break-all;
        font-family:"Fira Code",ui-monospace,Menlo,monospace; color:#0f172a; }}
  .meta {{ color:var(--dim); font-size:18.8px; margin-bottom:12px;
           font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  .hint {{ color:var(--dim); font-size:20.2px; line-height:1.7; }}
  /* Label and value on one row, as the reference's inspector has them: the eye
     runs down the left edge for the field it wants. */
  .row {{ display:flex; justify-content:space-between; gap:14px; padding:6px 0;
          border-bottom:1px solid #f1f5f9; font-size:20.2px;
          word-break:break-word; }}
  .k {{ color:var(--faint); font-weight:500; flex:none; }}
  .v {{ text-align:right; color:var(--fg); }}
  .sect {{ margin-top:18px; }}
  .sect h2 {{ font-size:15.8px; text-transform:uppercase; letter-spacing:.1em;
              color:var(--fg2); margin:0 0 8px; font-weight:700; }}
  .edge {{ padding:9px 11px; margin:6px 0; background:var(--code);
           border:1px solid var(--line); border-left:3px solid var(--line2);
           border-radius:6px; cursor:pointer; font-size:19.5px;
           transition:background .16s ease, border-color .16s ease; }}
  .edge:hover {{ background:#f1f5f9; border-color:var(--line3); }}
  .edge.certain {{ border-left-color:var(--ok); }}
  .edge.likely {{ border-left-color:var(--warn-fg); }}
  .edge .ev {{ color:var(--dim); font-size:17.2px; margin-top:4px;
               font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  pre {{ white-space:pre-wrap; word-break:break-word; margin:6px 0 0;
         font-family:"Fira Code",ui-monospace,Menlo,monospace; font-size:17.2px;
         line-height:1.65; color:var(--fg2); background:var(--code);
         padding:11px; border-radius:6px; border:1px solid var(--line);
         max-height:320px; overflow:auto; user-select:text; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:4px;
            font-size:15.8px; font-weight:700; letter-spacing:.05em;
            text-transform:uppercase; background:var(--act-bg);
            color:var(--act-fg); border:1px solid var(--act-bd); }}
  .badge.iso {{ background:var(--warn-bg); color:var(--warn-fg);
                border-color:var(--warn-bd); }}
  .badge.wasted, .badge.fail {{ background:var(--fail-bg); color:var(--fail-fg);
                                border-color:var(--fail-bd); }}
  .badge.fate-unconsumed {{ background:var(--warn-bg); color:var(--warn-fg);
                            border-color:var(--warn-bd); }}
  .badge.fate-consumed {{ background:var(--turn-bg); color:var(--turn-fg);
                          border-color:var(--turn-bd); }}
  .badge.fate-superseded, .badge.fate-input {{ background:var(--act-bg);
    color:var(--act-fg); border-color:var(--act-bd); }}
  .dimtext {{ color:var(--faint); font-size:18.8px; }}
  .lnk {{ color:var(--sel); cursor:pointer; font-weight:500; }}
  .lnk:hover {{ text-decoration:underline; }}
  button {{ background:var(--chrome); color:var(--fg2); border:1px solid var(--line);
            border-radius:6px; padding:5px 12px; font-size:18.8px; font-weight:500;
            cursor:pointer; font-family:inherit; margin-right:6px;
            transition:background .16s ease, border-color .16s ease; }}
  button:hover {{ background:var(--bg); border-color:var(--line3);
                  color:var(--fg); }}

  /* ---- the trail ------------------------------------------------------- */
  .trailv {{ color:var(--faint); font-size:16.5px; font-weight:700;
             letter-spacing:.1em; margin:12px 0 5px;
             font-family:"Fira Code",ui-monospace,Menlo,monospace; }}
  .trailv.here {{ color:var(--sel); }}
  .trailstep {{ display:flex; align-items:baseline; gap:9px; padding:5px 10px;
                margin:3px 0 3px 11px; border-left:3px solid var(--line);
                border-radius:0 5px 5px 0; cursor:pointer; font-size:18.8px;
                transition:background .16s ease; }}
  .trailstep:hover {{ background:var(--code); }}
  .trailstep.wrote {{ border-left-color:var(--write); }}
  .trailstep.read {{ border-left-color:var(--read); }}
  .trailstep.existed {{ border-left-color:var(--line2); cursor:default; }}
  .tverb {{ color:var(--dim); min-width:78px; font-weight:600; }}
  .tturn {{ color:var(--fg); min-width:84px; font-weight:500; }}
  .tref {{ color:var(--faint); min-width:72px;
           font-family:"Fira Code",ui-monospace,Menlo,monospace; font-size:16.5px; }}
  .tp {{ color:var(--fg2); flex:1; }}

  ::-webkit-scrollbar {{ width:9px; height:9px; }}
  ::-webkit-scrollbar-track {{ background:transparent; }}
  ::-webkit-scrollbar-thumb {{ background:var(--line2); border-radius:5px;
                               border:2px solid var(--chrome); }}
  ::-webkit-scrollbar-thumb:hover {{ background:var(--line3); }}
</style>
</head>
<body>
<header id="top">
  <div class="brand">provenance <span class="sub">{session}</span></div>
  <nav id="crumbs"></nav>
  <div class="stats" id="stats"></div>
  <select id="vsel" title="jump to a view"></select>
  <input id="find" type="search" autocomplete="off"
         placeholder="find a file, an action, a turn…" />
  <div id="sug"></div>
</header>
<div id="prompt-banner" aria-live="polite"></div>
<div id="app">
<div id="graph">
  <!-- The svg fills the pane and a camera transform on #view does the moving,
       so the whole graph can be zoomed to fit. Sizing the svg to the drawing
       instead left a 1312x1476 picture reachable only by scrollbar, with no way
       to see it whole. -->
  <svg id="svg" width="100%" height="100%"
       xmlns="http://www.w3.org/2000/svg">
    <defs>
      <marker id="ar-read" viewBox="0 0 8 8" refX="7" refY="4"
              markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="{c_used}"/>
      </marker>
      <marker id="ar-write" viewBox="0 0 8 8" refX="7" refY="4"
              markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="{c_gen}"/>
      </marker>
    </defs>
    <g id="view">
      <g id="seq"></g>
      <g id="links"></g>
      <g id="nodes"></g>
    </g>
  </svg>
  <button id="reply-chip" type="button" aria-label="show the model's final reply for this turn">
    <span class="rc-label">final reply ›</span>
    <span class="rc-preview"></span>
  </button>
  <div id="legend">
    <div class="lghdr">legend <button id="lbtn" type="button">–</button></div>
    <div id="lgbody">
    <div id="lgtypes"></div>
    <div class="lgrow lghead">edges</div>
    <div class="lgrow"><span class="sw" style="background:{c_used}"></span>
      read &nbsp;<span class="lgdim">file → action</span></div>
    <div class="lgrow"><span class="sw" style="background:{c_gen}"></span>
      write &nbsp;<span class="lgdim">action → file</span></div>
    <div class="lgrow"><span class="sw" style="background:{c_connect}"></span>
      connect &nbsp;<span class="lgdim">action → host</span></div>
    <div class="lgrow" id="confrow"></div>
    <div class="lgrow lghead">actions</div>
    <div class="lgrow lghead">nodes</div>
    <div class="lgrow"><span class="shape ent"></span> file version</div>
    <div class="lgrow"><span class="shape pid"></span> background process</div>
    <div class="lgrow"><span class="shape host"></span> remote endpoint
      &nbsp;<span class="lgdim">ssh / docker / k8s</span></div>
    <div class="lgrow lghead">order</div>
    <div class="lgrow"><span class="sw seq"></span> ran next
      &nbsp;<span class="lgdim">execution order, not data</span></div>
    </div>
  </div>
</div>
<div id="side">
  <div id="bar">
    <h1>{session}</h1>
    <div class="meta" id="meta"></div>
    <button id="fbtn" type="button">dim others: on</button>
    <button id="mbtn" type="button">layout: layered</button>
    <button id="tbtn" type="button">dark</button>
  </div>
  <div id="inner"><div id="body"><p class="hint" id="hint">Click a node. Hover
    shows a summary; clicking an edge in the panel jumps to the node at its
    other end.<br><br>
    <b>If this text never changes when you click a node</b>, scripts are being
    blocked — some embedded previews (VS Code's Live Preview among them) apply a
    content security policy that stops inline scripts. Open the file directly in
    a browser.</p></div></div>
</div>
</div>
<script id="data" type="application/json">{payload}</script>
<script id="views" type="application/json">{views}</script>
<script id="trails" type="application/json">{trails}</script>
<script>
// Nothing here can be exercised before shipping -- there is no javascript engine
// on the machine that builds this file -- so a thrown error used to show up as an
// empty canvas and nothing else. It is reported on the page instead.
window.addEventListener("error", ev => {{
  const box = document.getElementById("body");
  if (box) box.innerHTML = '<p class="hint" style="color:#be123c">' +
      "<b>script error</b><br>" + String(ev.message) +
      "<br><br>" + String((ev.filename || "") + ":" + (ev.lineno || "")) +
      "<br><br>" + String(ev.error && ev.error.stack || "").slice(0, 1200)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</p>";
}});
const DATA = JSON.parse(document.getElementById("data").textContent);
const VIEWS = JSON.parse(document.getElementById("views").textContent);
const TRAILS = JSON.parse(document.getElementById("trails").textContent);
const body = document.getElementById("body");
const NS = "http://www.w3.org/2000/svg";
let THEME_TINT = .18;
let dimming = true, current = null;
let GEO = null, GEO_W = 0, GEO_H = 0, COLUMN = 0, view = null;

const el = (name, attrs) => {{
  const n = document.createElementNS(NS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}};
// `closest` is on Element in every current browser, but not on SVG elements in
// every DOM: jsdom 9, which the test harness uses, has it on HTML nodes only, and
// an embedded webview old enough to matter may be the same. Walking the parent
// chain costs nothing at these depths and works everywhere.
function within(node, name) {{
  const isId = name.charAt(0) === "#";
  const key = name.slice(1);
  for (let el = node; el; el = el.parentNode) {{
    if (isId) {{ if (el.id === key) return el; }}
    else if (el.classList && el.classList.contains(key)) return el;
  }}
  return null;
}}

const esc = s => {{ const d = document.createElement("div");
                    d.textContent = s == null ? "" : String(s);
                    return d.innerHTML; }};

const KINDS = {{}};
for (const id in DATA) {{
  const n = DATA[id];
  KINDS[id] = n.type === "activity" ? (n.action === "turn" ? "turn" : "act")
            : n.kind === "pid" ? "pid"
            : n.kind === "host" ? "host"
            : n.outside ? "outside" : "ent";
}}
// One colour for actions. The card names its own kind on the first line, so a
// hue per kind was a second encoding of the same fact, and it clashed with the
// edge colours, which do carry something the text does not: direction.
const ACTION_COLOURS = {json_palette};
const isAct = id => KINDS[id] === "act" || KINDS[id] === "turn";
const ACTION_COLOUR = "#475569";
const FAILED_COLOUR = "#be123c";
// From graph/export.py, so the drawing, the legend and the dot output
// cannot disagree about which colour means which direction.
const EDGE_READ = "{c_used}", EDGE_WRITE = "{c_gen}";
const actionColour = (a, failed) => failed ? FAILED_COLOUR
                                  : (ACTION_COLOURS[a] || ACTION_COLOUR);

const degree = id => {{
  const n = DATA[id];
  return n ? (n.in || []).length + (n.out || []).length : 0;
}};
const radius = id => 15 + Math.sqrt(degree(id)) * 4;

function shortLabel(id, raw) {{
  const n = DATA[id];
  if (!n) return raw || id;
  if (n.action === "turn") return "turn " + n.turn;
  if (n.type === "activity") return "a[" + n.idx + "]";
  if (n.kind === "pid") return "PID " + n.key;
  if (n.kind === "host") {{
    const i = n.key.indexOf("://");
    return i < 0 ? n.key : n.key.slice(0, i) + ": " + n.key.slice(i + 3);
  }}
  const base = n.key.split("/").pop() || n.key;
  return base + (n.version ? "@v" + n.version : "");
}}

// ---- drawing ------------------------------------------------------------
const svgEl = document.getElementById("svg");
const graphEl = document.getElementById("graph");
const viewG = document.getElementById("view");
const seqG = document.getElementById("seq");
const linkG = document.getElementById("links");
const nodeG = document.getElementById("nodes");
const CARD_W = 208, CARD_H = 62;
let SIZE = new Map(), byId = new Map(), lines = [], seqLines = [];
let P = new Map(), HOME = new Map(), LINKS = [];
let cam = {{k: 1, x: 0, y: 0}};

function clear(node) {{ while (node.firstChild) node.removeChild(node.firstChild); }}

function render(name) {{
  view = VIEWS.find(v => v.name === name) || VIEWS[0];
  GEO = view.geometry;
  GEO_W = view.width; GEO_H = view.height;
  const xs = GEO.nodes.filter(n => isAct(n.id)).map(n => n.x)
                      .sort((a, b) => a - b);
  COLUMN = xs.length ? xs[Math.floor(xs.length / 2)] : GEO_W / 2;

  clear(seqG); clear(linkG); clear(nodeG);
  SIZE = new Map(); byId = new Map(); lines = []; seqLines = [];
  P = new Map(); HOME = new Map();
  GEO.nodes.forEach(n => P.set(n.id, {{x: n.x, y: n.y, vx: 0, vy: 0}}));
  P.forEach((v, k) => HOME.set(k, {{x: v.x, y: v.y}}));
  LINKS = GEO.edges.filter(e => P.has(e.src) && P.has(e.dst));

  drawLinks();
  drawOrder();
  drawNodes();
  place();
  fitStart();
  chrome();
  clearSel();
  // The suggestion list is closed on a switch: its rows name views, and half of
  // them would now point at the view already on screen.
  closeSug();
}}

function drawLinks() {{
  for (const e of GEO.edges) {{
    // Whether this is a write is asked of KINDS, not of the node id. The overview
    // namespaces its ids as `ov:a:turn10`, so a prefix test for "a:" said no to
    // every edge on that view: all 124 were drawn as reads and the 24 writes had
    // their arrows pointing back at the turn that produced the file.
    const write = isAct(e.src);
    // Every arrow leaves the action, read or write: the direction of the data is
    // carried by colour, and letting the arrowhead carry it too put arrows both
    // into and out of one card without adding anything.
    const from = write ? e.src : e.dst;
    const to = write ? e.dst : e.src;
    const path = el("path", {{
      "class": "lnk-line", fill: "none",
      stroke: write ? "{c_gen}" : "{c_used}",
      "stroke-width": 1.4, "stroke-opacity": .72,
      "marker-end": write ? "url(#ar-write)" : "url(#ar-read)",
    }});
    linkG.appendChild(path);
    lines.push({{src: from, dst: to, path: path}});
  }}
}}

function drawOrder() {{
  // Execution order, which the graph holds only as idx -- no edge says a[10]
  // follows a[9]. Drawn as the quietest layer for that reason.
  const ordered = GEO.nodes.filter(n => KINDS[n.id] === "act")
      .sort((a, b) => (DATA[a.id].idx || 0) - (DATA[b.id].idx || 0));
  for (let i = 0; i + 1 < ordered.length; i++) {{
    const path = el("path", {{
      "class": "seq-line", fill: "none", stroke: "var(--line2)",
      "stroke-width": 1.4, "stroke-dasharray": "2 5", "stroke-linecap": "round",
    }});
    seqG.appendChild(path);
    seqLines.push({{src: ordered[i].id, dst: ordered[i + 1].id, path: path}});
  }}
}}

function drawNodes() {{
  for (const n of GEO.nodes) {{
    const kind = KINDS[n.id] || "ent";
    const d = DATA[n.id] || {{}};
    const g = el("g", {{"class": "nd", "data-kind": kind}});

    if (kind === "act" || kind === "turn") {{
      // Size and text both come from the layout, which sized the card from the
      // same wrap: the space `dot` reserved is the space the card fills. Wrapping
      // again here would drift and cards would overlap the row below.
      const cw = n.cw || CARD_W, ch = n.ch || CARD_H;
      SIZE.set(n.id, {{w: cw, h: ch, shape: "card"}});
      const bd = kind === "turn" ? "var(--turn-bd)"
               : d.failed ? "var(--fail-bd)" : "var(--line)";
      // A white card with a hairline border and a small shadow, as the reference
      // has. The colour is in the badge, not in the card body: a tinted card at
      // this size reads as selected.
      g.appendChild(el("rect", {{
        "class": "card", x: -cw / 2, y: -ch / 2, width: cw, height: ch,
        rx: 8, ry: 8, fill: "var(--chrome)", stroke: bd, "stroke-width": 1.2,
        filter: "drop-shadow(0 1px 2px rgba(15,23,42,.07))",
      }}));

      // Badge: the kind, in words. Colour is uniform across action kinds -- the
      // word already says which -- and is spent on the one distinction the drawing
      // has to make by itself, a call the tool refused.
      const tag = kind === "turn" ? "turn " + d.turn
                : d.failed ? "refused" : (d.action || "").replace(/_/g, " ");
      const bg = kind === "turn" ? "var(--turn-bg)"
               : d.failed ? "var(--fail-bg)" : "var(--act-bg)";
      const fg = kind === "turn" ? "var(--turn-fg)"
               : d.failed ? "var(--fail-fg)" : "var(--act-fg)";
      const bw = 15 + tag.length * 5.9;
      g.appendChild(el("rect", {{
        x: -cw / 2 + 11, y: -ch / 2 + 10, width: bw, height: 17,
        rx: 4, ry: 4, fill: bg, stroke: fg, "stroke-width": .7,
        "stroke-opacity": .35,
      }}));
      const badge = el("text", {{
        "class": "bdg", x: -cw / 2 + 11 + bw / 2, y: -ch / 2 + 22,
        "text-anchor": "middle", fill: fg,
      }});
      badge.textContent = tag.toUpperCase();
      g.appendChild(badge);

      // The index on the right of the badge row: it is how the panel and the trail
      // refer to this action, so it has to be findable in the picture.
      if (kind === "turn") {{
        // `open` is its own hit area, because the card has two things to do:
        // clicking the box asks "where did this turn's files go", clicking through
        // asks "what did it do". One target for both meant the reader could not
        // look at a turn without leaving the overview.
        const ow = 54, ox = cw / 2 - 11 - ow, oy = -ch / 2 + 9;
        const btn = el("g", {{"class": "open"}});
        btn.appendChild(el("rect", {{
          x: ox, y: oy, width: ow, height: 19, rx: 5, ry: 5,
          fill: "var(--turn-bg)", stroke: "var(--turn-fg)",
          "stroke-width": .8, "stroke-opacity": .4,
        }}));
        const ot = el("text", {{
          "class": "t3", x: ox + ow / 2, y: oy + 13.5, "text-anchor": "middle",
          fill: "var(--turn-fg)", style: "font-weight:700;font-size:10.5px",
        }});
        ot.textContent = "OPEN \u203a";
        btn.appendChild(ot);
        g.appendChild(btn);
      }} else {{
        const rt = el("text", {{
          "class": "t3", x: cw / 2 - 11, y: -ch / 2 + 22, "text-anchor": "end",
          style: 'font-family:"Fira Code",ui-monospace,Menlo,monospace',
        }});
        rt.textContent = "a[" + d.idx + "]";
        g.appendChild(rt);
      }}

      // One <text> per line: SVG has no text flow, and `foreignObject`, which
      // would flow it, is what the restricted preview refuses to render.
      (n.lines || []).forEach((row, i) => {{
        const t = el("text", {{
          "class": "t2", x: -cw / 2 + 11,
          y: -ch / 2 + 45 + i * 15, "text-anchor": "start",
        }});
        t.textContent = row;
        g.appendChild(t);
      }});
      // A single line of the command, in mono, so it reads as a quotation of
      // something that ran. The whole of it is in the panel.
      if (n.hint) {{
        const t = el("text", {{
          "class": "cmd", x: -cw / 2 + 11, y: ch / 2 - 11, "text-anchor": "start",
        }});
        t.textContent = n.hint;
        g.appendChild(t);
      }}
    }} else if (kind === "pid") {{
      const r = 13;
      SIZE.set(n.id, {{w: r * 2, h: r * 2, r: r, shape: "diamond"}});
      g.appendChild(el("polygon", {{
        "class": "card", points: `0,${{-r}} ${{r}},0 0,${{r}} ${{-r}},0`,
        fill: "#f5f3ff", stroke: "var(--pid)", "stroke-width": 1.4,
      }}));
      const t = el("text", {{
        "class": "bdg", x: 0, y: 3.5, "text-anchor": "middle",
        fill: "var(--pid)", style: "font-size:8.5px",
      }});
      t.textContent = "PID";
      g.appendChild(t);
    }} else if (kind === "host") {{
      // A short rounded rectangle wide enough to hold the scheme label,
      // tagged on the first line and the endpoint underneath -- a host is
      // not a file and does not pretend to be one.
      const w = 86, h = 32, c = 10;
      SIZE.set(n.id, {{w: w, h: h, shape: "host"}});
      const key = DATA[n.id].key || "";
      const i = key.indexOf("://");
      const scheme = i < 0 ? "host" : key.slice(0, i);
      const ep = i < 0 ? key : key.slice(i + 3);
      g.appendChild(el("rect", {{
        "class": "card", x: -w / 2, y: -h / 2, width: w, height: h,
        rx: c, ry: c,
        fill: "#ecfeff", stroke: "var(--host)", "stroke-width": 1.4,
      }}));
      // A small desktop-monitor icon on the left tells the eye "this is a
      // machine on the other side", so the card reads as something external
      // before the reader parses the scheme label. The three parts (screen,
      // neck, base) are drawn as plain paths so the icon survives previewers
      // that strip embedded SVG symbols or filters.
      const ix = -w / 2 + 9;             // horizontal centre of icon
      const iy = 0;                      // vertical centre of card
      const sW = 12, sH = 8;             // screen size
      // screen outline
      g.appendChild(el("rect", {{
        x: ix - sW / 2, y: iy - sH / 2 - 2,
        width: sW, height: sH, rx: 1.3, ry: 1.3,
        fill: "none", stroke: "var(--host)", "stroke-width": 1.2,
      }}));
      // short neck + wider base, drawn as one path so one stroke serves both
      g.appendChild(el("path", {{
        d: `M${{ix}},${{iy + sH / 2 - 2}} L${{ix}},${{iy + sH / 2 + 1}} ` +
           `M${{ix - 4}},${{iy + sH / 2 + 1.5}} L${{ix + 4}},${{iy + sH / 2 + 1.5}}`,
        fill: "none", stroke: "var(--host)", "stroke-width": 1.2,
        "stroke-linecap": "round",
      }}));
      const tag = el("text", {{
        "class": "bdg", x: 5, y: -3, "text-anchor": "middle",
        fill: "var(--host)",
        style: "font-size:8.5px;font-weight:700;letter-spacing:.04em",
      }});
      tag.textContent = scheme.toUpperCase();
      g.appendChild(tag);
      const et = el("text", {{
        "class": "t3", x: 5, y: 9, "text-anchor": "middle",
        fill: "var(--host)",
        style: "font-family:\\"Fira Code\\",ui-monospace,Menlo,monospace;" +
               "font-size:9.5px",
      }});
      const show = ep.length > 18 ? "…" + ep.slice(-17) : ep;
      et.textContent = show;
      g.appendChild(et);
    }} else {{
      const w = 21, h = 26, c = 7;
      SIZE.set(n.id, {{w: w, h: h, shape: "doc"}});
      const stroke = kind === "outside" ? "var(--outside)" : "var(--ent)";
      g.appendChild(el("path", {{
        "class": "card",
        d: `M${{-w / 2}},${{-h / 2}} L${{w / 2 - c}},${{-h / 2}} ` +
           `L${{w / 2}},${{-h / 2 + c}} L${{w / 2}},${{h / 2}} ` +
           `L${{-w / 2}},${{h / 2}} Z`,
        fill: kind === "outside" ? "#fffbeb" : "var(--chrome)",
        stroke: stroke, "stroke-width": 1.3, "stroke-linejoin": "round",
      }}));
      g.appendChild(el("path", {{
        d: `M${{w / 2 - c}},${{-h / 2}} L${{w / 2 - c}},${{-h / 2 + c}} ` +
           `L${{w / 2}},${{-h / 2 + c}}`,
        fill: "none", stroke: stroke, "stroke-width": 1.1,
        "stroke-linejoin": "round",
      }}));
    }}

    if (kind !== "act" && kind !== "turn") {{
      const off = SIZE.get(n.id).w / 2 + 6;
      const left = n.x < COLUMN && n.x > 130 || n.x > GEO_W - 130;
      const label = el("text", {{
        "class": "t1", x: left ? -off : off, y: 4,
        "text-anchor": left ? "end" : "start",
      }});
      label.textContent = shortLabel(n.id, n.label);
      g.appendChild(label);
    }}

    g.setAttribute("transform", `translate(${{n.x.toFixed(1)}},${{n.y.toFixed(1)}})`);
    nodeG.appendChild(g);
    byId.set(n.id, g);
    g.addEventListener("click", ev => {{
      ev.stopPropagation();
      if (KINDS[n.id] === "turn" && within(ev.target, ".open")) {{
        go("turn" + DATA[n.id].turn);
        return;
      }}
      show(n.id);
    }});
    g.addEventListener("mouseenter", () => {{ if (!current) show(n.id, true); }});
    // `continue`, not `return`: this is a for-of over every node, and returning
    // here stopped drawNodes at the first action -- nothing after it was drawn.
    if (KINDS[n.id] === "act" || KINDS[n.id] === "turn") continue;
    g.addEventListener("pointerdown", ev => {{
      dragging = n.id;
      P.get(n.id).pinned = true;
      ev.stopPropagation();
      ev.preventDefault();
    }});
  }}
}}

function edgeOffset(id, ux, uy) {{
  const sz = SIZE.get(id);
  if (!sz) return 10;
  const hw = sz.w / 2, hh = sz.h / 2;
  if (sz.shape === "diamond") {{
    // |x|/a + |y|/b = 1. Treating a diamond as a circle and padding it left the
    // arrowhead clear of the shape, reading as an edge joined to nothing.
    const denom = Math.abs(ux) / hw + Math.abs(uy) / hh;
    return denom > 0 ? 1 / denom : hw;
  }}
  const tx = Math.abs(ux) < 1e-6 ? Infinity : hw / Math.abs(ux);
  const ty = Math.abs(uy) < 1e-6 ? Infinity : hh / Math.abs(uy);
  return Math.min(tx, ty);
}}

function place() {{
  byId.forEach((g, id) => {{
    const p = P.get(id);
    if (!p) return;
    g.setAttribute("transform",
                   `translate(${{p.x.toFixed(1)}},${{p.y.toFixed(1)}})`);
    const sz = SIZE.get(id);
    if (sz && sz.shape !== "card") {{
      const t = g.children[g.children.length - 1], off = sz.w / 2 + 6;
      const left = p.x < COLUMN && p.x > 130 || p.x > GEO_W - 130;
      t.setAttribute("x", (left ? -off : off).toFixed(1));
      t.setAttribute("text-anchor", left ? "end" : "start");
    }}
  }});
  seqLines.forEach(l => {{
    const a = P.get(l.src), b = P.get(l.dst);
    if (!a || !b) return;
    const sa = SIZE.get(l.src), sb = SIZE.get(l.dst);
    l.path.setAttribute("d",
        `M${{a.x.toFixed(1)}},${{(a.y + sa.h / 2).toFixed(1)}} ` +
        `L${{b.x.toFixed(1)}},${{(b.y - sb.h / 2).toFixed(1)}}`);
  }});
  lines.forEach(l => {{
    const a = P.get(l.src), b = P.get(l.dst);
    if (!a || !b) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const ea = edgeOffset(l.src, ux, uy) + 2;
    const eb = edgeOffset(l.dst, ux, uy) + 3;
    const x1 = a.x + ux * ea, y1 = a.y + uy * ea;
    const x2 = b.x - ux * eb, y2 = b.y - uy * eb;
    const mx = (x1 + x2) / 2 - dy * .06, my = (y1 + y2) / 2 + dx * .06;
    l.path.setAttribute("d", `M${{x1.toFixed(1)}},${{y1.toFixed(1)}} ` +
                             `Q${{mx.toFixed(1)}},${{my.toFixed(1)}} ` +
                             `${{x2.toFixed(1)}},${{y2.toFixed(1)}}`);
  }});
}}

// ---- camera -------------------------------------------------------------
function applyCam() {{
  viewG.setAttribute("transform",
      `translate(${{cam.x.toFixed(2)}},${{cam.y.toFixed(2)}}) scale(${{cam.k.toFixed(4)}})`);
}}

function boundsOf(ids) {{
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  ids.forEach(id => {{
    const p = P.get(id);
    if (!p) return;
    const sz = SIZE.get(id) || {{w: 20, h: 20}};
    x0 = Math.min(x0, p.x - sz.w / 2); x1 = Math.max(x1, p.x + sz.w / 2);
    y0 = Math.min(y0, p.y - sz.h / 2); y1 = Math.max(y1, p.y + sz.h / 2);
  }});
  if (!isFinite(x0)) return null;
  return {{x0: x0 - 40, y0: y0 - 30, x1: x1 + 200, y1: y1 + 40}};
}}

function bounds() {{
  const all = boundsOf(new Set(P.keys()));
  return all || {{x0: 0, y0: 0, x1: 100, y1: 100}};
}}

function scaleFor(b, r) {{
  return Math.min(r.width / (b.x1 - b.x0), r.height / (b.y1 - b.y0), 1.6);
}}

function moveTo(b, k) {{
  const r = graphEl.getBoundingClientRect();
  cam.k = k;
  cam.x = (r.width - (b.x1 - b.x0) * k) / 2 - b.x0 * k;
  cam.y = (r.height - (b.y1 - b.y0) * k) / 2 - b.y0 * k;
  applyCam();
}}

function fitView() {{
  const r = graphEl.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const b = bounds();
  moveTo(b, scaleFor(b, r));
}}

// The opening shot: the first few steps at a size you can read, not the whole run
// shrunk to fit. Thirty-one turn cards scaled into one pane is an accurate picture
// of nothing -- no label is legible and the shape it shows is "a lot happened".
// Double-click the canvas for the whole thing.
const START_STEPS = 4;

function fitStart() {{
  const r = graphEl.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const whole = bounds();
  // A view that is already readable whole is left alone: zooming into the first
  // four of six actions would hide half of a picture that fitted anyway.
  if (scaleFor(whole, r) >= 0.7) {{
    moveTo(whole, scaleFor(whole, r));
    return;
  }}
  const first = GEO.nodes.filter(n => isAct(n.id))
      .sort((a, b) => (DATA[a.id].idx || 0) - (DATA[b.id].idx || 0))
      .slice(0, START_STEPS).map(n => n.id);
  if (!first.length) {{
    moveTo(whole, scaleFor(whole, r));
    return;
  }}
  // The cards only, plus a fixed margin. Including the files they touched was the
  // first attempt and it did nothing: on the overview those files are spread the
  // full width of the drawing, so the box came out the same size as the whole
  // graph and the scale with it -- 0.167 either way. A margin shows the nearest
  // of them without letting the furthest set the zoom.
  const b = boundsOf(new Set(first)) || whole;
  b.x0 -= 300; b.x1 += 300; b.y0 -= 20; b.y1 += 60;
  // Anchored at the top: the run starts there, and centring on four cards inside a
  // tall drawing left the reader unsure which way was forward.
  const k = Math.min(scaleFor(b, r), 1.15);
  cam.k = k;
  cam.x = (r.width - (b.x1 - b.x0) * k) / 2 - b.x0 * k;
  cam.y = 24 - b.y0 * k;
  applyCam();
}}

function toGraph(ev) {{
  const r = svgEl.getBoundingClientRect();
  return {{x: (ev.clientX - r.left - cam.x) / cam.k,
          y: (ev.clientY - r.top - cam.y) / cam.k}};
}}

graphEl.addEventListener("wheel", ev => {{
  ev.preventDefault();
  const r = svgEl.getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const before = {{x: (mx - cam.x) / cam.k, y: (my - cam.y) / cam.k}};
  cam.k = Math.max(.1, Math.min(4, cam.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)));
  cam.x = mx - before.x * cam.k;
  cam.y = my - before.y * cam.k;
  applyCam();
}}, {{passive: false}});

graphEl.addEventListener("dblclick", ev => {{
  if (!within(ev.target, ".nd") && !within(ev.target, "#legend")) fitView();
}});

// ---- panel --------------------------------------------------------------
const label = id => esc(shortLabel(id, id));

function edgeList(title, list, side) {{
  if (!list || !list.length) return "";
  let h = `<div class="sect"><h2>${{esc(title)}}</h2>`;
  for (const e of list) {{
    const other = side === "in" ? e.src : e.dst;
    h += `<div class="edge ${{e.confidence}}" data-goto="${{esc(other)}}">` +
         `<div>${{side === "in" ? "←" : "→"}} ${{label(other)}} ` +
         `<span class="badge">${{e.kind}} · ${{e.confidence}}</span></div>` +
         `<div class="ev">${{esc(e.evidence)}}</div></div>`;
  }}
  return h + "</div>";
}}

// Label left, value right, as the reference's inspector has it: the eye runs down
// the left edge for the field it wants. The value has to be an element for that --
// a bare text node becomes an anonymous flex item and cannot be aligned.
const row = (k, v) => `<div class="row"><span class="k">${{esc(k)}}</span>` +
                      `<span class="v">${{v}}</span></div>`;

function show(id, hovering) {{
  const n = DATA[id];
  if (!n) return;
  if (!hovering) current = id;
  let h = "";
  if (n.type === "reply") {{
    // The model's final text for a turn. Not a graph node -- no edges, no
    // version, no provenance -- but the panel is the right place to show
    // the long form once the chip at the bottom of the graph is clicked.
    h += row("", `<span class="lnk" data-open="turn${{n.turn}}">\u2039 back to turn ${{n.turn}}</span>`);
    h += row("turn", n.turn);
    h += row("role", "assistant · final reply");
    h += `<div class="sect"><h2>reply</h2><pre>${{esc(n.text)}}</pre></div>`;
    body.innerHTML = h;
    byId.forEach(g => g.classList.remove("sel"));
    return;
  }}
  if (n.type === "activity") {{
    if (n.action === "turn") {{
      h += row("turn", n.turn);
      h += row("contains", esc(n.purpose));
      h += row("", `<span class="lnk" data-open="turn${{n.turn}}">open this turn ›</span>`);
      if (n.reasoning) h += `<div class="sect"><h2>what it did</h2><pre>${{
                             esc(n.reasoning)}}</pre></div>`;
    }} else {{
      h += row("action", esc(n.action));
      h += row("ref", `${{esc(n.ref)}} &nbsp;<span class="badge">turn ${{n.turn}}</span>`);
      if (n.isolated) h += row("note", `<span class="badge iso">isolated — no edges</span>`);
      if (n.warnings && n.warnings.length)
        // The separator is written as an escape for the *browser*, so it has to
        // survive being written by python: a bare backslash-n here becomes a real
        // newline in the file, which splits the template literal and makes the
        // whole script a syntax error -- one blank canvas, no message.
        h += `<div class="sect"><h2>why nothing linked</h2><pre>${{
              esc(n.warnings.join(String.fromCharCode(10)))}}</pre></div>`;
      if (n.path) h += row("path", esc(n.path));
      if (n.purpose) h += `<div class="sect"><h2>purpose</h2>${{esc(n.purpose)}}</div>`;
      if (n.probe) {{
        // What the probe was aimed at. Not an edge: a probe that misses
        // touches nothing, so the picture does not join it to any file. The
        // panel still names the file so a reader can see what was searched,
        // and -- when a version of that path is in the picture -- turns the
        // name into a jump to the latest version so the reader can see where
        // the file sits in the rest of the run.
        let probeRows = "";
        if (n.probe.pattern)
          probeRows += row("looked for", `<code>${{esc(n.probe.pattern)}}</code>`);
        if (n.probe.path) {{
          // The version that was in the tree *when the probe ran*, which is
          // the latest one born strictly before this action's idx. A
          // `scenario.py@v2` born at idx 79 is not what a probe at idx 1 saw,
          // and linking to it would reverse causality -- the panel would
          // show the final content as the probe's target.
          let targetId = null, latestV = -1;
          for (const did in DATA) {{
            const dn = DATA[did];
            if (dn && dn.type === "entity" && dn.kind === "file"
                && dn.key === n.probe.path
                && dn.born_idx < n.idx) {{
              if (dn.version > latestV) {{ latestV = dn.version; targetId = did; }}
            }}
          }}
          probeRows += targetId
            ? row("in file", `<span class="lnk" data-goto="${{esc(targetId)}}">` +
                             `${{label(targetId)}}</span>` +
                             ` <span class="dimtext">(not linked — probe only)</span>`)
            : row("in file", `<code>${{esc(n.probe.path)}}</code>` +
                             ` <span class="dimtext">(not in the picture)</span>`);
        }}
        if (n.probe.root) probeRows += row("under", `<code>${{esc(n.probe.root)}}</code>`);
        if (probeRows) h += `<div class="sect"><h2>probe</h2>${{probeRows}}</div>`;
      }}
      if (n.command) h += `<div class="sect"><h2>command</h2><pre>${{
                           esc(n.command)}}</pre></div>`;
      if (n.reasoning) h += `<div class="sect"><h2>reasoning</h2><pre>${{
                             esc(n.reasoning)}}</pre></div>`;
    }}
  }} else {{
    h += row("kind", esc(n.kind) + (n.outside ? ' <span class="badge">outside window</span>' : ""));
    h += row("version", "v" + n.version);
    h += row("produced by", n.producer ? esc(n.producer) : "— pre-existing");
    if (n.fate) {{
      const note = {{
        input: "pre-existing; only read here",
        consumed: "read by a later action",
        unconsumed: "nothing read it — left behind by the run",
        superseded: "replaced by a later version"
      }}[n.fate] || "";
      h += row("fate", `<span class="badge fate-${{esc(n.fate)}}">${{esc(n.fate)}}</span>` +
           (note ? ` <span class="dimtext">${{esc(note)}}</span>` : ""));
    }}
    if (n.wasted) h += row("", `<span class="badge wasted">written, overwritten, never read</span>`);
    if (n.superseded_by) h += row("replaced by",
         `<span class="lnk" data-goto="${{esc(n.superseded_by)}}">${{label(n.superseded_by)}}</span>`);
    h += `<div class="sect"><h2>key</h2><pre>${{esc(n.key)}}</pre></div>`;
    h += trailOf(n.key, n.version);
  }}
  h += edgeList("consumed (usedBy in)", n.in, "in");
  h += edgeList("produced / consumed by", n.out, "out");
  body.innerHTML = h;

  byId.forEach(g => g.classList.remove("sel"));
  const g = byId.get(id);
  if (g) {{
    g.classList.add("sel");
    // Guarded: scrolling the node into view is a nicety, and an embedded webview
    // without it should not cost the reader the whole panel.
    if (!hovering && g.scrollIntoView)
      g.scrollIntoView({{block: "center", inline: "center",
                         behavior: "smooth"}});
  }}
  // Dimming on click, never on hover. Hovering used to dim the whole drawing,
  // which meant the picture rearranged itself as the pointer crossed it and the
  // reader could not look at anything on the way to what they wanted.
  if (dimming && !hovering) applyFocus(id); else highlightTouching(id);
}}

function trailOf(key, here) {{
  // The history of one file, which is the question a `@v3` label provokes and the
  // one thing no single view can answer: the overview drops the versions only one
  // turn touched, and a turn view drops the other turns.
  const steps = TRAILS[key];
  if (!steps || !steps.length) return "";
  let h = `<div class="sect"><h2>trail \u2014 every turn that touched this ` +
          `file</h2>`;
  let seen = -1;
  for (const st of steps) {{
    if (st.v !== seen) {{
      seen = st.v;
      h += `<div class="trailv${{st.v === here ? " here" : ""}}">v${{st.v}}${{
             st.v === here ? "  \u2190 shown here" : ""}}</div>`;
    }}
    const jump = st.view && st.node
        ? ` data-jump="${{esc(st.view)}}|${{esc(st.node)}}"` : "";
    h += `<div class="trailstep ${{esc(st.verb)}}"${{jump}}>` +
         `<span class="tverb">${{esc(st.verb)}}</span>` +
         (st.turn ? `<span class="tturn">turn ${{st.turn}}</span>` : "") +
         (st.ref ? `<span class="tref">${{esc(st.ref.split("#").pop()
                     ? "a[" + st.ref.split("#").pop() + "]" : st.ref)}}</span>` : "") +
         `<span class="tp">${{esc(st.purpose)}}</span></div>`;
  }}
  return h + `</div>`;
}}

function chainOf(id) {{
  // Every version of the same artifact that this view happens to contain. Matched
  // on the payload key rather than on node id, because the overview namespaces its
  // nodes and a version filtered out of one view still belongs to the history.
  const n = DATA[id];
  if (!n || n.type !== "entity") return null;
  const out = new Set();
  byId.forEach((g, k) => {{
    const d = DATA[k];
    if (d && d.type === "entity" && d.key === n.key) out.add(k);
  }});
  return out;
}}

function applyChainFocus(id) {{
  // The file and its history lit; the actions that touched any of its versions
  // left legible, because "who edited it" is the point and a lit file with every
  // neighbour blacked out cannot answer that; everything else dimmed.
  const chain = chainOf(id);
  if (!chain) return false;
  const touching = new Set();
  lines.forEach(l => {{
    if (chain.has(l.src)) touching.add(l.dst);
    if (chain.has(l.dst)) touching.add(l.src);
  }});
  byId.forEach((g, k) => {{
    g.classList.toggle("dimmed", !chain.has(k) && !touching.has(k));
    g.classList.toggle("faded", !chain.has(k) && touching.has(k));
    g.classList.toggle("lit", chain.has(k));
  }});
  lines.forEach(l => {{
    const on = chain.has(l.src) || chain.has(l.dst);
    l.path.classList.toggle("dimmed", !on);
    l.path.classList.toggle("hi", on);
  }});
  return true;
}}

function neighbours(id) {{
  const n = DATA[id], s = new Set([id]);
  if (n) {{ (n.in || []).forEach(e => s.add(e.src));
            (n.out || []).forEach(e => s.add(e.dst)); }}
  return s;
}}
function highlightTouching(id) {{
  lines.forEach(l => l.path.classList.toggle("hi", l.src === id || l.dst === id));
}}
function applyFocus(id) {{
  if (applyChainFocus(id)) return;      // an entity focuses its whole history
  const keep = neighbours(id);
  byId.forEach((g, k) => g.classList.toggle("dimmed", !keep.has(k)));
  lines.forEach(l => {{
    const on = keep.has(l.src) && keep.has(l.dst);
    l.path.classList.toggle("dimmed", !on);
    l.path.classList.toggle("hi", on);
  }});
}}
function clearFocus() {{
  byId.forEach(g => g.classList.remove("dimmed", "faded", "lit"));
  lines.forEach(l => l.path.classList.remove("dimmed", "hi"));
}}
function clearSel() {{
  current = null;
  byId.forEach(g => g.classList.remove("sel"));
  clearFocus();
  body.innerHTML = `<p class="hint">Click a node. ` +
      (view && view.name === "overview"
        ? `Each card is one turn — click it to open that turn in full.`
        : `Use the breadcrumb to go back to all turns.`) + `</p>`;
}}

// ---- force --------------------------------------------------------------
let mode = "layered", frames = 0;
function step() {{
  const nodes = [...P.entries()];
  for (const [, p] of nodes) {{ p.fx = 0; p.fy = 0; }}
  for (let i = 0; i < nodes.length; i++)
    for (let j = i + 1; j < nodes.length; j++) {{
      const a = nodes[i][1], b = nodes[j][1];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) {{ dx = Math.random(); dy = Math.random(); d2 = 1; }}
      const f = 26000 / d2, d = Math.sqrt(d2);
      a.fx -= f * dx / d; a.fy -= f * dy / d;
      b.fx += f * dx / d; b.fy += f * dy / d;
    }}
  for (const l of LINKS) {{
    const a = P.get(l.src), b = P.get(l.dst);
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - 190) * .02;
    a.fx += f * dx / d; a.fy += f * dy / d;
    b.fx -= f * dx / d; b.fy -= f * dy / d;
  }}
  for (let i = 0; i < nodes.length; i++)
    for (let j = i + 1; j < nodes.length; j++) {{
      const [ai, a] = nodes[i], [bi, b] = nodes[j];
      const ra = (SIZE.get(ai) || {{w: 20}}).w / 2 + 14;
      const rb = (SIZE.get(bi) || {{w: 20}}).w / 2 + 14;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;
      const overlap = ra + rb - d;
      if (overlap > 0) {{
        const push = overlap * .5;
        a.fx -= push * dx / d; a.fy -= push * dy / d;
        b.fx += push * dx / d; b.fy += push * dy / d;
      }}
    }}
  for (const [, p] of nodes) {{
    if (p.pinned) {{ p.vx = p.vy = 0; continue; }}
    p.vx = (p.vx + p.fx) * .84;
    p.vy = (p.vy + p.fy) * .84;
    p.x += Math.max(-18, Math.min(18, p.vx));
    p.y += Math.max(-18, Math.min(18, p.vy));
  }}
  place();
}}
function tick() {{
  if (mode !== "force") return;
  step();
  if (++frames < 400) requestAnimationFrame(tick);
}}

// ---- chrome -------------------------------------------------------------
function chrome() {{
  document.getElementById("stats").innerHTML = view.stats
      .map(([k, n]) => `<div class="stat">${{esc(k)}} <b>${{n}}</b></div>`).join("");
  document.getElementById("lgtypes").innerHTML = view.legend.length
      ? `<div class="lgrow lghead">${{view.name === "overview" ? "turns" : "actions"}}</div>` +
        view.legend.map(([k, n, c]) =>
          `<div class="lgrow"><span class="lgdot" style="background:${{
            c || ACTION_COLOUR}}"></span>` +
          `${{esc(k.replace(/_/g, " "))}}<span class="lgn">${{n}}</span></div>`).join("")
      : "";
  document.getElementById("confrow").innerHTML = view.confidences.length > 1
      ? `<span class="sw" style="background:var(--dim);opacity:.4"></span> ` +
        `faint = parsed from a command <span class="lgdim">solid = a tool's own ` +
        `path field</span>`
      : "";
  const ov = VIEWS.find(v => v.name === "overview");
  const crumbs = [];
  if (ov) crumbs.push(view.name === "overview"
      ? `<span class="c here">all turns</span>`
      : `<span class="c" data-open="overview">all turns</span>`);
  if (view.name !== "overview")
    crumbs.push(`<span class="sep">›</span><span class="c here">${{
                 esc(view.title)}}</span>`);
  document.getElementById("crumbs").innerHTML = crumbs.join("");
  document.getElementById("meta").textContent = view.title + " · " +
      view.stats.map(([k, n]) => n + " " + k).join(" · ");
  const sel = document.getElementById("vsel");
  sel.innerHTML = VIEWS.map(v =>
      `<option value="${{esc(v.name)}}"${{v.name === view.name ? " selected" : ""}}>${{
       esc(v.title)}}</option>`).join("");

  // The user's prompt frames the turn and sits above the graph; the
  // model's final reply sits as a chip at the bottom of the graph pane and
  // opens the full text in the side panel on click. On the overview both
  // are hidden -- turn cards already carry the prompt, and `all turns` has
  // no single "final" reply.
  const pb = document.getElementById("prompt-banner");
  const rc = document.getElementById("reply-chip");
  const rcPrev = rc.querySelector(".rc-preview");
  const app = document.getElementById("app");
  if (view.prompt) {{
    pb.innerHTML = `<span class="pb-label">user asked</span>` +
                   `<span class="pb-toggle">▸</span>` +
                   `<span class="pb-text">${{esc(view.prompt)}}</span>`;
    pb.classList.add("on");
    pb.classList.add("collapsed");
    pb.querySelector(".pb-toggle").textContent = "▸";
  }} else {{
    pb.classList.remove("on"); pb.classList.remove("collapsed");
    pb.innerHTML = "";
  }}
  if (view.reply) {{
    // One-line preview, trimmed and single-line so the chip stays short.
    const flat = view.reply.replace(/\\s+/g, " ").trim();
    rcPrev.textContent = flat.length > 90 ? flat.slice(0, 89) + "\u2026"
                                          : flat;
    rc.classList.add("on");
    rc.dataset.replyId = "reply:" + view.name;
  }} else {{
    rc.classList.remove("on");
    rcPrev.textContent = "";
    delete rc.dataset.replyId;
  }}
  const pbH = view.prompt ? pb.getBoundingClientRect().height : 0;
  app.style.paddingTop = (78 + pbH) + "px";
}}

function go(name) {{
  if (!VIEWS.some(v => v.name === name)) return;
  mode = "layered";
  document.getElementById("mbtn").textContent = "layout: layered";
  render(name);
}}

document.getElementById("vsel").addEventListener("change",
    ev => go(ev.target.value));
document.getElementById("crumbs").addEventListener("click", ev => {{
  const t = ev.target.closest("[data-open]");
  if (t) go(t.getAttribute("data-open"));
}});
document.getElementById("lbtn").addEventListener("click", ev => {{
  ev.stopPropagation();
  const box = document.getElementById("legend");
  ev.target.textContent = box.classList.toggle("off") ? "+" : "–";
}});
document.getElementById("prompt-banner").addEventListener("click", ev => {{
  // Click toggles the user prompt between a single clamped line (default,
  // so a long prompt does not cover the graph) and the full text. The
  // app's padding-top tracks the banner's height so the picture stays
  // visible under either state.
  ev.stopPropagation();
  const pb = ev.currentTarget;
  if (!pb.classList.contains("on")) return;
  const nowCollapsed = pb.classList.toggle("collapsed");
  const toggle = pb.querySelector(".pb-toggle");
  if (toggle) toggle.textContent = nowCollapsed ? "▸" : "▾";
  const app = document.getElementById("app");
  app.style.paddingTop = (78 + pb.getBoundingClientRect().height) + "px";
}});
document.getElementById("reply-chip").addEventListener("click", ev => {{
  // The chip opens the pseudo-node written by to_html for this turn's
  // reply. Stopping propagation keeps the pane's background click (which
  // calls `fitView`) from firing underneath.
  ev.stopPropagation();
  const rid = ev.currentTarget.dataset.replyId;
  if (rid) show(rid);
}});
document.getElementById("fbtn").addEventListener("click", () => {{
  dimming = !dimming;
  document.getElementById("fbtn").textContent =
      "dim others: " + (dimming ? "on" : "off");
  if (dimming && current) applyFocus(current);
  else {{ clearFocus(); if (current) highlightTouching(current); }}
}});
document.getElementById("mbtn").addEventListener("click", () => {{
  mode = mode === "layered" ? "force" : "layered";
  document.getElementById("mbtn").textContent = "layout: " + mode;
  if (mode === "force") {{ frames = 0; requestAnimationFrame(tick); }}
  else {{
    P.forEach((p, id) => {{
      const h = HOME.get(id);
      p.x = h.x; p.y = h.y; p.vx = p.vy = 0;
    }});
    place();
    fitView();
  }}
}});
document.getElementById("tbtn").addEventListener("click", () => {{
  const dark = !document.body.classList.toggle("plain");
  document.getElementById("tbtn").textContent = dark ? "plain" : "dark";
}});
// ---- search -------------------------------------------------------------
// A dropdown rather than a live filter of the drawing. Filtering greyed most of a
// view as soon as a letter was typed, which told the reader that almost nothing
// matched while they were still halfway through the word. A list answers the
// question being asked -- which of these did I mean -- and picking one takes the
// reader to it and highlights it there.
let INDEX = null, sugItems = [], sugAt = -1;

function viewOf(id) {{
  // Where a node can be looked at. An action belongs to its turn; an entity is
  // shown in the turn of the first action that touched it, since it appears in as
  // many turn views as there are turns that touched it and any of them will do.
  const n = DATA[id];
  if (!n) return "";
  if (id.indexOf("ov:") === 0) return "overview";
  if (n.type === "activity") return "turn" + n.turn;
  const edge = (n.out || [])[0] || (n.in || [])[0];
  if (!edge) return "";
  const other = DATA[edge.dst === id ? edge.src : edge.dst];
  return other && other.turn ? "turn" + other.turn : "";
}}

function buildIndex() {{
  // Built once, over the whole run rather than the current view: looking for a
  // file whose name you remember is exactly the case where you do not know which
  // turn to open first.
  const out = [];
  const names = {{}};
  VIEWS.forEach(v => {{ names[v.name] = v.title; }});
  for (const id in DATA) {{
    const n = DATA[id];
    const view = viewOf(id);
    if (!view || !names[view]) continue;
    let label, kind, note;
    if (n.action === "turn") {{
      label = "turn " + n.turn;
      kind = "turn";
      note = n.command || n.purpose || "";
    }} else if (n.type === "activity") {{
      label = "a[" + n.idx + "] " + (n.action || "").replace(/_/g, " ");
      kind = n.failed ? "refused" : "action";
      note = n.purpose || "";
    }} else {{
      label = n.key.split("/").pop() + (n.version ? " @v" + n.version : "");
      kind = n.kind === "pid" ? "pid" : "file";
      note = n.key;
    }}
    out.push({{
      id: id, view: view, viewTitle: names[view], label: label, kind: kind,
      note: note,
      hay: [label, note, n.command, n.action, n.ref, n.key,
            n.turn ? "turn " + n.turn : ""].filter(Boolean).join(" ")
          .toLowerCase(),
    }});
  }}
  return out;
}}

function closeSug() {{
  document.getElementById("sug").style.display = "none";
  sugItems = [];
  sugAt = -1;
}}

function renderSug(q) {{
  const box = document.getElementById("sug");
  if (!q) {{ closeSug(); return; }}
  if (!INDEX) INDEX = buildIndex();
  const here = view ? view.name : "";
  const hit = INDEX.filter(r => r.hay.indexOf(q) >= 0);
  // What is already on screen first: picking it costs no navigation.
  hit.sort((a, b) => (a.view === here ? 0 : 1) - (b.view === here ? 0 : 1)
                     || a.label.localeCompare(b.label));
  sugItems = hit.slice(0, 12);
  sugAt = sugItems.length ? 0 : -1;
  if (!hit.length) {{
    box.innerHTML = '<div class="sgnone">no match</div>';
    box.style.display = "block";
    return;
  }}
  box.innerHTML = sugItems.map((r, i) =>
      '<div class="sg' + (i === sugAt ? " on" : "") + '" data-i="' + i + '">' +
      '<span class="sgk k-' + r.kind + '">' + esc(r.kind) + "</span>" +
      '<span class="sgl">' + esc(r.label) + "</span>" +
      '<span class="sgn">' + esc(r.note.slice(0, 70)) + "</span>" +
      '<span class="sgv">' + esc(r.viewTitle) + "</span></div>").join("")
      + (hit.length > sugItems.length
          ? '<div class="sgmore">' + (hit.length - sugItems.length)
            + " more \u2014 keep typing</div>"
          : "");
  box.style.display = "block";
}}

function moveSug(step) {{
  if (!sugItems.length) return;
  sugAt = (sugAt + step + sugItems.length) % sugItems.length;
  const rows = document.getElementById("sug").querySelectorAll(".sg");
  for (let i = 0; i < rows.length; i++) {{
    if (i === sugAt) rows[i].className = "sg on";
    else rows[i].className = "sg";
  }}
  if (rows[sugAt] && rows[sugAt].scrollIntoView)
    rows[sugAt].scrollIntoView({{block: "nearest"}});
}}

function takeSug(i) {{
  const r = sugItems[i == null ? sugAt : i];
  if (!r) return;
  closeSug();
  document.getElementById("find").value = r.label;
  // Navigate, then select: `show` is what dims the rest, so the reader lands on
  // the thing they picked already highlighted.
  jump(r.view, r.id);
}}

const findEl = document.getElementById("find");
findEl.addEventListener("input", ev => renderSug(
    ev.target.value.trim().toLowerCase()));
findEl.addEventListener("keydown", ev => {{
  if (ev.key === "ArrowDown") {{ moveSug(1); ev.preventDefault(); }}
  else if (ev.key === "ArrowUp") {{ moveSug(-1); ev.preventDefault(); }}
  else if (ev.key === "Enter") {{ takeSug(); ev.preventDefault(); }}
  else if (ev.key === "Escape") {{ closeSug(); findEl.blur(); }}
}});
document.getElementById("sug").addEventListener("mousedown", ev => {{
  // mousedown, not click: the input's blur would close the list first.
  const row = within(ev.target, ".sg");
  if (row) {{ takeSug(Number(row.getAttribute("data-i"))); ev.preventDefault(); }}
}});
findEl.addEventListener("blur", () => setTimeout(closeSug, 140));

function jump(view, node) {{
  if (view && view !== (current_view())) go(view);
  show(node);
}}
const current_view = () => (view ? view.name : "");

body.addEventListener("click", ev => {{
  const j = ev.target.closest("[data-jump]");
  if (j) {{
    const [v, node] = j.getAttribute("data-jump").split("|");
    jump(v, node);
    return;
  }}
  const open = ev.target.closest("[data-open]");
  if (open) {{ go(open.getAttribute("data-open")); return; }}
  const t = ev.target.closest("[data-goto]");
  if (t) show(t.getAttribute("data-goto"));
}});

let dragging = null, panning = null;
// A drag of the canvas ends with a `click` on the svg, indistinguishable from a
// click on empty space unless the movement is remembered. Without this, panning
// cleared whatever the reader had selected -- so any attempt to look around the
// highlighted file undid the highlight.
let moved = false;
const MOVED_PX = 4;                    // below this, a hand is merely unsteady

graphEl.addEventListener("pointerdown", ev => {{
  if (dragging) return;
  // The legend floats inside #graph; without this its fold button started a pan
  // and the click never landed.
  if (within(ev.target, "#legend")) return;
  moved = false;
  panning = {{mx: ev.clientX, my: ev.clientY, x: cam.x, y: cam.y}};
  graphEl.classList.add("panning");
}});
window.addEventListener("pointermove", ev => {{
  if (dragging) {{
    const p = toGraph(ev), n = P.get(dragging);
    n.x = p.x; n.y = p.y;
    HOME.set(dragging, {{x: n.x, y: n.y}});
    place();
    return;
  }}
  if (panning) {{
    const dx = ev.clientX - panning.mx, dy = ev.clientY - panning.my;
    if (Math.abs(dx) > MOVED_PX || Math.abs(dy) > MOVED_PX) moved = true;
    cam.x = panning.x + dx;
    cam.y = panning.y + dy;
    applyCam();
  }}
}});
window.addEventListener("pointerup", () => {{
  // Dragging a node ends in a click too, and it should no more clear the
  // selection than panning does.
  if (dragging) {{ moved = true; P.get(dragging).pinned = false; }}
  dragging = null; panning = null;
  graphEl.classList.remove("panning");
}});
svgEl.addEventListener("click", ev => {{
  // Swallowed here rather than suppressed at pointerup: `preventDefault` on
  // pointerup does not stop the click, and cancelling it there would cancel a
  // genuine click on a node as well.
  if (moved) {{ moved = false; return; }}
  if (!within(ev.target, "#legend") && !within(ev.target, ".nd")) clearSel();
}});
window.addEventListener("resize", fitView);

render(VIEWS[0].name);
</script>
{annotate_block}
</body>
</html>
"""


# One colour for every action. Per-type colour was making the reader learn eight
# hues to recover something the card already says in words on its first line, and
# it competed with the edge colours, which do carry meaning (direction).
ACTION_COLOUR = "#475569"       # slate-600, the one colour actions get
FAILED_COLOUR = "#be123c"       # rose-700, a call the tool refused
TURN_COLOUR = "#15803d"         # green-700, a turn
PALETTE = {"turn": TURN_COLOUR}


# ---- card sizing ----------------------------------------------------------
# The card is as big as its text, and `dot` is told the same number, so the space
# reserved for a node is the space the drawing uses. Wrapping is done here rather
# than in the browser for that reason: two implementations of the same wrap drift,
# and the symptom is cards overlapping the row beneath them.
CARD_W = 208          # px, the old fixed card; kept as the JS fallback
ACT_W = 300           # px, an action card: a purpose and one line of command
WIDE_W = 470          # px, a turn card, which carries a sentence of prose
MONO_COL = 6.05       # px advance of one 11px monospace glyph
PROSE_COL = 6.4       # px average advance of the 12px sans face
BADGE_H = 27          # px, the badge row: 10 top pad + 17 badge
LINE_H = 15           # px between body lines
HINT_H = 15           # px for the quoted command line
PAD_B = 12            # px bottom pad
DPI = 72.0            # `dot -Tjson` reports inches; layout.py scales by this


def _wrap(text: str, cols: int) -> list[str]:
    """Break text to `cols` columns, keeping existing line breaks.

    Long unbroken tokens -- a 300-character path, or a base64 blob -- are cut
    rather than allowed to run off the card, which is what `textwrap` does with
    `break_long_words=False` and why it is not used here.
    """
    out: list[str] = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        while len(line) > cols:
            cut = line.rfind(" ", 0, cols + 1)
            if cut <= cols // 3:                     # no useful space: hard cut
                cut = cols
            out.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        out.append(line)
    return out or [""]


def _card(node: dict[str, Any]) -> dict[str, Any]:
    """The text a card shows, and the size it needs to show it.

    Cards are a fixed size, and short. An earlier pass put the whole command on the
    card -- one to fifty wrapped lines of monospace -- and the drawing stopped
    reading as a diagram: a screen held a dozen cards, each a paragraph of shell,
    and the thing the picture is for (which file went where, in what order) was
    buried in text. The full command is in the panel, one click away, where it can
    be as long as it is.

    A uniform card also lets the eye scan a column, which twelve different heights
    did not.
    """
    if node.get("action") == "turn":
        # A turn card is labelled with what the user asked, which is what makes one
        # turn recognisable. Four lines: enough for a sentence, and the whole of it
        # is in the panel.
        cols = int((WIDE_W - 26) / MONO_COL)
        body = _wrap(node.get("command") or node.get("purpose", ""), cols)
        body = _cap(body, 4, cols)
        return {"lines": body, "w": WIDE_W,
                "h": BADGE_H + 8 + max(1, len(body)) * LINE_H + PAD_B}

    # An action: its purpose, and one line of the command as a hint of what ran.
    cols = int((ACT_W - 26) / PROSE_COL)
    body = _cap(_wrap(node.get("purpose") or node.get("path", ""), cols), 2, cols)
    hint = ""
    probe = node.get("probe")
    if probe:
        # A probe shows what it looked for on the hint line: `pattern @ path`
        # or just `path` when there is no pattern, in the same monospace slot
        # the shell command would use. Keeps the probe visibly different from
        # a real read -- there is no file-shape node joined to this card --
        # while giving the reader the one line that says "this searched for
        # X in Y" without opening the panel.
        pat = probe.get("pattern") or ""
        pth = probe.get("path") or probe.get("root") or ""
        if pat and pth:
            short = pth.rsplit("/", 1)[-1] or pth
            hint = f'"{pat}" @ {short}'
        elif pat:
            hint = f'"{pat}"'
        elif pth:
            hint = pth.rsplit("/", 1)[-1] or pth
        if len(hint) > cols:
            hint = hint[:cols - 1] + "\u2026"
    elif node.get("command"):
        first = " ".join(node["command"].split("\n")[0].split())
        hint = first if len(first) <= cols else first[:cols - 1] + "\u2026"
    elif node.get("path"):
        hint = node["path"].split("/")[-1]
    return {"lines": body, "hint": hint, "w": ACT_W,
            "h": BADGE_H + 8 + 2 * LINE_H + (HINT_H if hint else 0) + PAD_B}


def _cap(lines: list[str], n: int, cols: int) -> list[str]:
    """First `n` lines, with an ellipsis when something was dropped."""
    if len(lines) <= n:
        return lines
    out = lines[:n]
    out[-1] = out[-1][:max(0, cols - 2)].rstrip() + " \u2026"
    return out


def _failed_refs(g: ProvGraph) -> set[str]:
    """Refs of calls the tool refused, read from the warnings the build recorded.

    One place, because the legend, the payload and the card colour all have to
    agree about which calls those are.
    """
    out = set()
    for w in g.warnings:
        ref, _, rest = w.partition(":")
        if "failed" in rest:
            out.add(ref.strip())
    return out


def _trails(g: ProvGraph) -> dict[str, list[dict[str, Any]]]:
    """Per artifact, the whole of its history: who wrote each version, who read it.

    Computed from the whole run rather than from the view being drawn. The overview
    keeps only the entities that two turns touched, and a turn view keeps only that
    turn -- so a file's history is exactly what a single view cannot show, and it
    is the question a multi-version file provokes. Keyed by artifact rather than by
    node so a five-version file stores its history once.
    """
    readers: dict[str, list[str]] = collections.defaultdict(list)
    for e in g.edges:
        if e.kind == USED_BY:                       # entity -> activity
            readers[e.src].append(e.dst)

    def step_of(a_id: str, verb: str) -> dict[str, Any]:
        a = g.activities.get(a_id)
        if a is None:
            return {"verb": verb, "ref": a_id, "turn": 0, "action": "",
                    "purpose": "", "node": a_id, "view": ""}
        return {"verb": verb, "ref": a.ref, "turn": a.turn, "action": a.action,
                "purpose": a.purpose or "", "node": a.node_id,
                "view": f"turn{a.turn}"}

    out: dict[str, list[dict[str, Any]]] = {}
    for key in {e.key for e in g.entities.values()}:
        versions = g.versions_of(key)
        if len(versions) < 2 and not any(readers.get(e.node_id) for e in versions):
            continue                                # nothing to trace
        steps: list[dict[str, Any]] = []
        for ent in versions:
            if ent.producer_ref:
                st = step_of(f"a:{ent.producer_ref}", "wrote")
            else:
                st = {"verb": "existed", "ref": "", "turn": 0, "action": "",
                      "purpose": "before the run started", "node": "", "view": ""}
            st["v"] = ent.version
            st["entity"] = ent.node_id
            steps.append(st)
            for a_id in readers.get(ent.node_id, ()):
                r = step_of(a_id, "read")
                r["v"] = ent.version
                r["entity"] = ent.node_id
                steps.append(r)
        # Chronological, so the trail reads the way the run happened. Writes of a
        # version come before the reads of it, which sorting on turn alone loses.
        steps.sort(key=lambda st: (st["v"], 0 if st["verb"] != "read" else 1,
                                   st["turn"]))
        out[key] = steps
    return out


def _fmt_duration(ms: int) -> str:
    """Render a millisecond count as the shortest form a reader can scan.

    < 1s   → `<1s`               keep the sign that time passed at all
    < 60s  → `12s`
    < 1h   → `3m 4s` or `3m`     drop the trailing `0s`
    ≥ 1h   → `1h 23m`
    """
    if ms <= 0:
        return "0s"
    if ms < 1000:
        return "<1s"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" + (f" {s}s" if s else "")
    h, m = divmod(m, 60)
    return f"{h}h" + (f" {m}m" if m else "")


def _view(g: ProvGraph, turns, min_confidence, hide_isolated, direction,
          timeline, ranksep, nodesep, pad, name, title,
          prompt: str = "", reply: str = "",
          duration_ms: int = 0) -> dict[str, Any]:
    """Everything one view needs: its geometry, its counts, its legend rows.

    `prompt` is the user's input for the turn this view holds, if any -- the
    renderer shows it as a banner below the breadcrumb so a reader opening a
    turn sees what was asked without going back to the overview card.

    `reply` is the model's final text for the turn, shown as a second banner
    underneath. A turn can carry several text messages (one between each
    tool); the "final" is the last of them -- it is what the reader of the
    live chat saw as the turn's conclusion.
    """
    v = select(g, turns, min_confidence, hide_isolated)

    # Size every card before laying out, so `dot` reserves the room the card
    # actually needs. A full command is the point of the card, and it is between
    # one and fifty wrapped lines depending on the command.
    cards = {}
    for a in v.activities:
        cards[a.node_id] = _card({
            "action": a.action, "purpose": a.purpose, "command": a.command,
            "path": a.path, "probe": a.probe,
        })
    sizes = {k: (c["w"] / DPI, c["h"] / DPI) for k, c in cards.items()}

    geo = layout_mod.layout(to_dot(g, turns, min_confidence, hide_isolated,
                                   direction, timeline=timeline, sizes=sizes,
                                   ranksep=ranksep, nodesep=nodesep))
    for n in geo["nodes"]:
        c = cards.get(n["id"])
        if c:                                   # the drawing's own text and size
            n["lines"] = c["lines"]
            n["cw"] = c["w"]
            n["ch"] = c["h"]
            if c.get("hint"):
                n["hint"] = c["hint"]
    # A census of action kinds was eight rows of colour that the first line of
    # every card already says in words, and it crowded out the one distinction the
    # drawing does make on its own: a red card is a call the tool refused.
    refused = {ref for ref in _failed_refs(g) if ref in {a.ref for a in v.activities}}
    return {
        "name": name,
        "title": title,
        "prompt": prompt,
        "reply": reply,
        "geometry": geo,
        "width": int(geo["width"]) + pad * 4,
        "height": int(geo["height"]) + pad,
        "stats": [("actions" if name != "overview" else "turns",
                   len(v.activities)),
                  ("files", sum(1 for e in v.entities if e.kind != "pid")),
                  # Wall-clock for this view, in milliseconds. The source is
                  # the official record's `turn_duration` -- per-action
                  # timestamps are not in the session jsonl, so this is the
                  # only way to say "how long did this run take". For a turn
                  # view the caller passes that turn's value; for the
                  # overview the caller passes the sum across all turns.
                  ("duration", _fmt_duration(duration_ms))],
        "legend": ([("turn", len(v.activities), TURN_COLOUR)]
                   if name == "overview" else
                   [("action", len(v.activities) - len(refused), ACTION_COLOUR)]
                   + ([("the tool refused this call", len(refused),
                        FAILED_COLOUR)] if refused else [])),
        "confidences": sorted({e.confidence for e in v.edges}),
    }


def _renamed(node: dict[str, Any]) -> dict[str, Any]:
    """Same node with its edge endpoints moved into the `ov:` namespace."""
    out = dict(node)
    for side in ("in", "out"):
        out[side] = [dict(e, src="ov:" + e["src"], dst="ov:" + e["dst"])
                     for e in node.get(side, ())]
    return out


def to_html(g: ProvGraph, turns: Optional[Iterable[int]] = None,
            min_confidence: str = WEAK, hide_isolated: bool = False,
            direction: str = "LR", timeline: bool = False,
            ranksep: float = 0.7, nodesep: float = 0.5,
            with_overview: bool = True,
            prompts: Sequence[str] = (),
            responses_by_turn: Optional[dict[int, list[str]]] = None,
            turn_duration_ms: Optional[dict[int, int]] = None,
            tagset: Optional[TagSet] = None) -> str:
    """One page holding an overview and a view per turn.

    A 36-turn run is 437 nodes, which is not a picture anyone reads. The overview
    is one card per turn joined by the artifacts they hand each other -- 65 nodes
    for this session -- and clicking a turn opens that turn in full.

    Everything is in one file, which was the explicit choice: the node payload is
    shared across views, so the cost of the extra views is their geometry
    (a few KB each) rather than another copy of every command. Measured on the
    reference session, the file grows from 660KB to about 1MB while the page never
    draws more than 65 nodes at once -- the opposite of the single full-graph view,
    which paid for 437 nodes on load and showed a hairball.
    """
    views: list[dict[str, Any]] = []
    pad = 40
    payload: dict[str, Any] = {}

    if with_overview and turns is None:
        ov, _ = overview_mod.build_overview(g, prompts)
        if ov.activities:
            # `timeline=True`, so the turn cards run down the page in turn
            # order. A turn's idx *is* its number, so the invisible spine that
            # pins actions in idx order pins turns in turn order -- and the
            # reader's first question of an overview is what happened when.
            # Layered by dependency instead, turn 22 sat above turn 4 whenever
            # that crossed fewer edges.
            views.append(_view(ov, None, min_confidence, hide_isolated,
                               direction, True, 0.9, 0.7, pad,
                               "overview", "all turns",
                               duration_ms=sum((turn_duration_ms or {}).values())))
            # The overview's entities carry the same `e:<key>@v<n>` ids as the
            # turn views but different edges -- theirs point at `a:turnN`. Merged
            # into one payload the later write wins and the overview's panel
            # shows a turn's edges. Namespaced instead.
            ov_payload = _node_payload(ov, select(ov, None, min_confidence,
                                                  hide_isolated))
            payload.update({("ov:" + k): _renamed(v) for k, v in ov_payload.items()})
            for n in views[-1]["geometry"]["nodes"]:
                n["id"] = "ov:" + n["id"]
            for e in views[-1]["geometry"]["edges"]:
                e["src"] = "ov:" + e["src"]
                e["dst"] = "ov:" + e["dst"]

    wanted = sorted({a.turn for a in g.activities.values()}) \
        if turns is None else sorted(set(turns))
    rbt = responses_by_turn or {}
    tdm = turn_duration_ms or {}
    for t in wanted:
        pr = prompts[t - 1] if 0 < t <= len(prompts) else ""
        # Last text of the turn: that is what the chat reader saw as the
        # turn's conclusion. The earlier texts (model's running commentary
        # between tools) are in the panel's reasoning for individual actions.
        turn_replies = rbt.get(t) or []
        rp = turn_replies[-1] if turn_replies else ""
        views.append(_view(g, [t], min_confidence, hide_isolated, direction,
                           True, ranksep, nodesep, pad,
                           f"turn{t}", f"turn {t}", prompt=pr, reply=rp,
                           duration_ms=tdm.get(t, 0)))
        payload.update(_node_payload(g, select(g, [t], min_confidence,
                                              hide_isolated)))
        if rp:
            # A pseudo-node for the final reply. Not drawn in the picture --
            # a text block has no edges that would belong in a provenance
            # graph -- but kept in the payload so `show()` can render the
            # full text in the side panel. The chip at the bottom of the
            # pane targets this id.
            payload[f"reply:turn{t}"] = {
                "type": "reply", "turn": t, "text": rp,
                "in": [], "out": [],
            }

    if not views:                                  # nothing to draw
        views.append(_view(g, turns, min_confidence, hide_isolated, direction,
                           timeline, ranksep, nodesep, pad, "all", "all turns"))
        payload.update(_node_payload(g, select(g, turns, min_confidence,
                                              hide_isolated)))

    return _PAGE.format(
        session=html.escape(g.session_id or "(no session)"),
        c_used=EDGE_COLOURS[USED_BY], c_gen=EDGE_COLOURS[GENERATED_BY],
        c_connect=EDGE_COLOURS[CONNECTED_BY],
        json_palette=json.dumps(PALETTE),
        trails=json.dumps(_trails(g)).replace("</", "<\\/"),
        payload=json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
        views=json.dumps(views, ensure_ascii=False).replace("</", "<\\/"),
        annotate_block=_annotate_block(tagset, g.session_id or ""))



# ---------------------------------------------------------------------------
# Flag-style annotation overlay (opt-in via `tagset=STARTER_TAGSET`).
#
# A step gets at most one tag plus an optional comment. Most steps stay
# unlabelled -- the point is to surface outliers for agent tuning, not to
# characterise every action. The overlay is self-contained CSS + JS appended
# before `</body>`, and hooks the base `show()` by wrapping the global.
# Persistence is localStorage per session plus explicit Export/Import JSON.
# ---------------------------------------------------------------------------
def _annotate_block(tagset: Optional[TagSet], session_id: str) -> str:
    if tagset is None or not tagset.tags:
        return ""
    ts_json = json.dumps(tagset_to_dict(tagset),
                         ensure_ascii=False).replace("</", "<\\/")
    sid = json.dumps(session_id)
    return f"""
<style>
  /* Annotation overlay. Only active when a TagSet is passed to `to_html`.
     The flag widget is a left-side fixed panel that opens below the
     `#prompt-banner` when the user clicks an activity. The right-side
     detail panel is deliberately untouched -- flagging and detail inspection
     are separate workflows and share the screen rather than stacking. */
  #ann-bar {{ position:fixed; top:52px; right:16px; z-index:30;
             display:flex; gap:6px; align-items:center;
             background:var(--chrome); border:1px solid var(--line);
             border-radius:8px; padding:6px 10px;
             font-size:12.5px; color:var(--fg2);
             box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  #ann-bar .pg {{ color:var(--write); font-weight:700; margin-right:6px; }}
  #ann-bar button, #ann-bar label.imp {{
    background:var(--code); border:1px solid var(--line);
    color:var(--fg); border-radius:6px;
    padding:3px 10px; font-size:12px; cursor:pointer;
    font-family:inherit; }}
  #ann-bar button:hover, #ann-bar label.imp:hover {{
    background:var(--chrome); border-color:var(--line3); }}

  #ann-panel {{ position:fixed; left:16px; top:94px; width:320px;
                z-index:28; display:none;
                background:var(--chrome); border:1px solid var(--line);
                border-radius:10px; padding:12px 14px;
                box-shadow:0 2px 8px rgba(0,0,0,.06);
                font-size:13px; }}
  #ann-panel.on {{ display:block; }}
  #ann-panel .ap-head {{ display:flex; align-items:center;
                         justify-content:space-between;
                         margin-bottom:8px; padding-bottom:8px;
                         border-bottom:1px dashed var(--line2); }}
  #ann-panel .ap-title {{ font-size:11px; letter-spacing:.08em;
                          color:var(--fg2); text-transform:uppercase;
                          font-weight:700; }}
  #ann-panel .ap-step {{ color:var(--dim); font-size:11.5px;
                         font-family:ui-monospace,Menlo,monospace;
                         margin-top:2px; }}
  #ann-panel .ap-close {{ background:transparent; border:none;
                          color:var(--dim); cursor:pointer;
                          font-size:18px; line-height:1; padding:0 4px;
                          font-family:inherit; }}
  #ann-panel .ap-close:hover {{ color:var(--fail-fg); }}

  .ann-tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }}
  .ann-tag {{ background:var(--chrome); border:1px solid var(--line);
              color:var(--fg); border-radius:14px;
              padding:3px 10px; font-size:12.5px; cursor:pointer;
              font-family:inherit; }}
  .ann-tag:hover {{ background:var(--code); border-color:var(--line3); }}
  .ann-tag.selected {{ background:#fff7ed; border-color:var(--write);
                       color:var(--write); font-weight:600; }}
  .ann-clear-btn {{ background:transparent; border:none; color:var(--dim);
                    cursor:pointer; font-size:12px; padding:3px 6px;
                    font-family:inherit; }}
  .ann-clear-btn:hover {{ color:var(--fail-fg); }}
  .ann-note {{ display:block; width:100%; min-height:52px; margin-top:4px;
               background:var(--chrome); color:var(--fg);
               border:1px solid var(--line); border-radius:5px;
               padding:5px 8px; font-size:12.5px; font-family:inherit;
               resize:vertical; box-sizing:border-box; }}
  .ann-note:focus {{ outline:none; border-color:var(--sel); }}
  .ann-note[disabled] {{ background:var(--code); color:var(--faint);
                         cursor:not-allowed; }}
  .ann-note-label {{ font-size:11px; color:var(--dim);
                     text-transform:uppercase; letter-spacing:.5px;
                     margin-top:6px; display:block; }}
  .ann-rules {{ font-size:11px; color:var(--dim); font-style:italic;
                margin-top:6px; }}
</style>
<script>
(function () {{
  'use strict';
  const TAGSET  = {ts_json};
  const SESSION = {sid};
  const KEY     = 'evalkit:annotate:' + SESSION;
  const TAGS    = TAGSET.tags || [];
  if (!TAGS.length) return;

  // state shape: {{ "<ref>": {{ tag: "...", note: "..." }}, ... }}
  let state = loadState();

  function loadState() {{
    try {{
      const raw = localStorage.getItem(KEY);
      if (!raw) return {{}};
      return (JSON.parse(raw) || {{}}).flags || {{}};
    }} catch (e) {{ return {{}}; }}
  }}
  function saveState() {{
    const payload = {{
      session: SESSION, tagset_id: TAGSET.id,
      flags: state, updated_at: new Date().toISOString(),
    }};
    try {{ localStorage.setItem(KEY, JSON.stringify(payload)); }}
    catch (e) {{ console.warn('localStorage write failed', e); }}
  }}
  function esc(s) {{
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }}

  // Only activity nodes are flaggable; entities / hosts / pids / overview
  // turn-cards (action === 'turn') are skipped.
  function isFlaggable(node) {{
    return node && node.type === 'activity' && node.action !== 'turn';
  }}

  function refOfNode(id, node) {{
    if (node && node.ref) return node.ref;
    const m = id && id.match(/a:(?:ov:)?(.+)$/);
    return m ? m[1] : id;
  }}

  function renderAnnotation(id) {{
    const panel = document.getElementById('ann-panel');
    if (!panel) return;
    const node = (typeof DATA !== 'undefined' ? DATA : {{}})[id];
    if (!node || !isFlaggable(node)) {{
      panel.classList.remove('on');
      bumpProgress();
      return;
    }}
    const ref = refOfNode(id, node);
    const cur = state[ref] || {{}};
    const tag = cur.tag || '';
    const note = cur.note || '';

    const tagChips = TAGS.map(t =>
      '<button type="button" class="ann-tag' + (t === tag ? ' selected' : '')
      + '" data-ref="' + esc(ref) + '" data-tag="' + esc(t) + '">'
      + esc(t) + '</button>'
    ).join('');
    const clearBtn = tag
      ? ' <button type="button" class="ann-clear-btn" data-ref="'
        + esc(ref) + '" data-clear="1">clear</button>'
      : '';
    const rules = TAGSET.house_rule
      ? '<div class="ann-rules">' + esc(TAGSET.house_rule) + '</div>' : '';

    // A note is only meaningful attached to a flag. Lock the textarea until
    // a tag is picked; otherwise orphan notes accumulate in localStorage and
    // mislead the user into thinking they annotated something they did not.
    const hasTag = !!tag;
    const notePlaceholder = hasTag ? 'why?' : 'pick a tag first';
    const noteAttrs = hasTag ? '' : ' disabled';

    panel.innerHTML =
      '<div class="ap-head">'
      +   '<div>'
      +     '<div class="ap-title">flag this step</div>'
      +     '<div class="ap-step">#' + esc(node.idx)
      +       ' &middot; ' + esc(node.action)
      +       ' &middot; <span style="color:var(--sel)">'
      +       esc(ref) + '</span></div>'
      +   '</div>'
      +   '<button type="button" class="ap-close" title="close">&times;</button>'
      + '</div>'
      + '<div class="ann-tags">' + tagChips + clearBtn + '</div>'
      + '<label class="ann-note-label">comment (optional)</label>'
      + '<textarea class="ann-note" data-ref="' + esc(ref) + '"'
      +   noteAttrs + ' placeholder="' + esc(notePlaceholder) + '">'
      +   esc(note) + '</textarea>'
      + rules;
    panel.classList.add('on');
    positionPanel();
    bumpProgress();
  }}

  // Position `#ann-panel` just below the prompt banner (`#prompt-banner`).
  // The banner is itself fixed and its height changes when the user toggles
  // it between collapsed and expanded; recompute on each `show()`, each
  // banner click, and on window resize. The header is a fixed 52px bar.
  function positionPanel() {{
    const panel = document.getElementById('ann-panel');
    if (!panel) return;
    const pb = document.getElementById('prompt-banner');
    const headerBottom = 52;      // #top is 52px fixed
    let top = headerBottom + 8;
    if (pb && pb.classList.contains('on')) {{
      const rect = pb.getBoundingClientRect();
      top = Math.max(top, rect.bottom + 8);
    }}
    panel.style.top = top + 'px';
  }}

  function bumpProgress() {{
    const el = document.getElementById('ann-progress');
    if (!el) return;
    const n = Object.keys(state).filter(k => state[k] && state[k].tag).length;
    el.textContent = n;
  }}

  function setFlag(ref, tag, note) {{
    // Invariant: a note is only ever stored alongside a tag. When the tag
    // goes empty, the note goes with it; this keeps localStorage from
    // accumulating orphan comments on steps the user never actually flagged.
    if (!tag) {{ delete state[ref]; saveState(); bumpProgress(); return; }}
    state[ref] = state[ref] || {{}};
    state[ref].tag = tag;
    if (note !== undefined) state[ref].note = note;
    state[ref].ts = new Date().toISOString();
    saveState();
    bumpProgress();
  }}

  function onClick(ev) {{
    const closeBtn = ev.target.closest('.ap-close');
    if (closeBtn) {{
      const panel = document.getElementById('ann-panel');
      if (panel) panel.classList.remove('on');
      return;
    }}
    const chip = ev.target.closest('.ann-tag');
    if (chip && chip.dataset.ref) {{
      const ref = chip.dataset.ref, tag = chip.dataset.tag;
      const cur = state[ref] || {{}};
      // Click the selected tag again to toggle it off.
      const newTag = cur.tag === tag ? '' : tag;
      setFlag(ref, newTag, cur.note || '');
      // Re-render the current panel so chip highlighting + clear button update.
      if (typeof current !== 'undefined' && current &&
          typeof show === 'function') show(current, false);
      return;
    }}
    const clr = ev.target.closest('.ann-clear-btn');
    if (clr && clr.dataset.ref) {{
      setFlag(clr.dataset.ref, '', '');
      if (typeof current !== 'undefined' && current &&
          typeof show === 'function') show(current, false);
      return;
    }}
  }}

  // Comment is auto-saved on every input event. localStorage writes are
  // synchronous and cheap, and this is simpler than a Save button for the
  // short notes this UI is for. The textarea is disabled when no tag is
  // picked, so this handler only ever fires for already-flagged steps.
  function onNoteInput(ev) {{
    const el = ev.target;
    if (!el || !el.classList || !el.classList.contains('ann-note')) return;
    if (el.disabled) return;
    const ref = el.dataset.ref;
    if (!ref) return;
    const cur = state[ref] || {{}};
    if (!cur.tag) return;         // defensive: no tag -> no note (invariant)
    setFlag(ref, cur.tag, el.value);
  }}

  function exportJSON() {{
    // Flat list of flagged steps, structured so a human reader sees the
    // *user's signal* (tag + note) first, then the agent's action, then
    // the surrounding context. Each entry is self-contained -- the
    // downstream `tune/` pipeline does not need to re-read the trajectory.
    const data = (typeof DATA !== 'undefined' ? DATA : {{}});
    const CTX_WINDOW = 2;

    // idx -> snippet: pick the single-turn entry over the overview copy
    // (first-write-wins, see refOfNode).
    const byIdx = {{}};
    const refToIdx = {{}};
    for (const id in data) {{
      const n = data[id];
      if (!n || n.type !== 'activity' || n.action === 'turn') continue;
      const ref = refOfNode(id, n);
      if (byIdx[n.idx] !== undefined) continue;
      byIdx[n.idx] = {{
        ref: ref, idx: n.idx, turn: n.turn, action: n.action,
        purpose: n.purpose || '', reasoning: n.reasoning || '',
        command: n.command || '', path: n.path || '',
      }};
      refToIdx[ref] = n.idx;
    }}

    function neighboursOf(idx) {{
      const before = [], after = [];
      for (let j = idx - CTX_WINDOW; j < idx; j++) {{
        if (byIdx[j]) before.push(byIdx[j]);
      }}
      for (let j = idx + 1; j <= idx + CTX_WINDOW; j++) {{
        if (byIdx[j]) after.push(byIdx[j]);
      }}
      return {{ window: CTX_WINDOW, before: before, after: after }};
    }}

    const flags = Object.keys(state)
      .filter(r => state[r] && state[r].tag)
      .map(r => {{
        const i = refToIdx[r];
        const step = (i !== undefined) ? byIdx[i] : {{ ref: r }};
        const ctx  = (i !== undefined) ? neighboursOf(i) : null;
        // Order matters: `json.dumps` preserves insertion order and a human
        // reading this file sees the user signal first, step second, long
        // context block last.
        const entry = {{
          tag: state[r].tag,
          note: state[r].note || '',
          ts: state[r].ts || '',
          step: step,
        }};
        if (ctx) entry.context = ctx;
        return entry;
      }})
      .sort((a, b) => ((a.step && a.step.idx) || 0)
                    - ((b.step && b.step.idx) || 0));

    const payload = {{
      session: SESSION, tagset_id: TAGSET.id, tags: TAGS,
      flags: flags, exported_at: new Date().toISOString(),
    }};
    const blob = new Blob([JSON.stringify(payload, null, 2)],
                         {{ type: 'application/json' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = SESSION + '.flags.json';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }}

  function importJSON(ev) {{
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {{
      try {{
        const parsed = JSON.parse(reader.result);
        if (parsed.session && parsed.session !== SESSION &&
            !confirm('File is for session ' + parsed.session
                     + '. Import anyway?')) return;
        // Accept either the flat exported form (preferred) or the raw dict
        // from a saved localStorage dump.
        if (Array.isArray(parsed.flags)) {{
          const next = {{}};
          for (const f of parsed.flags) {{
            if (!f || !f.tag) continue;
            // Support both the new layered shape (step.ref) and the older
            // flat shape (ref at top-level) so a user does not have to
            // re-export existing files after the format change.
            const ref = (f.step && f.step.ref) || f.ref;
            if (!ref) continue;
            next[ref] = {{ tag: f.tag, note: f.note || '',
                          ts: f.ts || '' }};
          }}
          state = next;
        }} else if (parsed.flags && typeof parsed.flags === 'object') {{
          // Dict shape (older localStorage dump). Keep the same invariant:
          // only entries with a tag survive.
          const next = {{}};
          for (const r in parsed.flags) {{
            const e = parsed.flags[r];
            if (e && e.tag) next[r] = {{
              tag: e.tag, note: e.note || '', ts: e.ts || '' }};
          }}
          state = next;
        }} else {{
          alert('Not a valid flags file.'); return;
        }}
        saveState();
        bumpProgress();
        if (typeof current !== 'undefined' && current &&
            typeof show === 'function') show(current, false);
      }} catch (e) {{ alert('Could not parse JSON: ' + e.message); }}
    }};
    reader.readAsText(f);
    ev.target.value = '';
  }}

  function clearAll() {{
    if (!confirm('Discard all flags on this page?')) return;
    state = {{}}; saveState(); bumpProgress();
    if (typeof current !== 'undefined' && current &&
        typeof show === 'function') show(current, false);
  }}

  function installBar() {{
    const bar = document.createElement('div');
    bar.id = 'ann-bar';
    bar.innerHTML =
      '<span class="pg"><span id="ann-progress">0</span> flagged</span>' +
      '<button id="ann-export" title="Download flagged steps as JSON">export</button>' +
      '<label class="imp" title="Load flagged steps from JSON">' +
        'import<input type="file" id="ann-import" accept=".json" hidden></label>' +
      '<button id="ann-clear" title="Discard all flags">clear</button>';
    document.body.appendChild(bar);
    document.getElementById('ann-export').addEventListener('click', exportJSON);
    document.getElementById('ann-import').addEventListener('change', importJSON);
    document.getElementById('ann-clear').addEventListener('click', clearAll);

    // The flag widget itself: left-side fixed panel, below `#prompt-banner`.
    const panel = document.createElement('div');
    panel.id = 'ann-panel';
    panel.setAttribute('aria-hidden', 'true');
    document.body.appendChild(panel);

    // Reposition when the user toggles the prompt banner (height changes).
    const pb = document.getElementById('prompt-banner');
    if (pb) pb.addEventListener('click', () => setTimeout(positionPanel, 0));
    window.addEventListener('resize', positionPanel);
  }}

  function installHook() {{
    const orig = window.show;
    if (typeof orig !== 'function') return;
    window.show = function (id, hovering) {{
      const r = orig.call(this, id, hovering);
      if (!hovering) renderAnnotation(id);
      return r;
    }};
    if (typeof current !== 'undefined' && current) renderAnnotation(current);
  }}

  function boot() {{
    installBar();
    installHook();
    document.addEventListener('click', onClick);
    document.addEventListener('input', onNoteInput);
    bumpProgress();
  }}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', boot);
  }} else {{
    boot();
  }}
}})();
</script>
"""
