"""Task 6: `python3 -m graph.cli <session-id> [--turns 6]`.

Output goes to stdout, matching `rule.runner` and `normalize.cli`. The graph is
not cached: building it from the official record takes about a second, and the
record is the truth -- a cache of a derived graph would need its own schema
fingerprint, which the IR pickle cache already taught us to be wary of.
"""
from __future__ import annotations

import argparse
import os
import sys

if __package__ in (None, ""):             # allow `python3 graph/cli.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph import artifacts, export                               # noqa: E402
from graph.build import build_skeleton                             # noqa: E402
from graph.schema import CERTAIN, CONFIDENCE_LEVELS, WEAK          # noqa: E402


def _load(session: str, official_dir: str, use_responses: bool):
    from normalize import load_trace_from_official
    return load_trace_from_official(session, official_dir or None,
                                    include_responses=use_responses)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m graph.cli",
        description="Render the provenance graph of one run.")
    p.add_argument("session", help="session id")
    p.add_argument("--official-dir", default="",
                   help="where the official session records live")
    p.add_argument("--turns", type=int, nargs="+", metavar="N",
                   help="restrict to these turns (default: all)")
    p.add_argument("--min-confidence", default=WEAK, choices=CONFIDENCE_LEVELS,
                   help=f"drop weaker edges (default: {WEAK}, i.e. keep all)")
    p.add_argument("--format", default="text", choices=("text", "mermaid", "dot", "html"),
                   help="text reads in a terminal; html is interactive "
                        "(needs graphviz); mermaid and dot need a renderer")
    p.add_argument("--annotate", action="store_true",
                   help="overlay step-level annotation controls on the graph "
                        "page (adds a Label section to each activity's side "
                        "panel, with Export/Import JSON in the top-right)")
    p.add_argument("--direction", default="LR", choices=("LR", "TD"),
                   help="mermaid layout direction")
    p.add_argument("--timeline", action="store_true",
                   help="one activity per row in idx order, arrows starting at "
                        "the action: for reading a single turn (--turns N)")
    p.add_argument("--hide-isolated", action="store_true",
                   help="omit activities with no edges (they are information: "
                        "an action nothing links to is worth seeing)")
    p.add_argument("--no-responses", action="store_true",
                   help="skip Task 5 resolution, which needs recorded output")
    p.add_argument("--stats", action="store_true", help="print counts only")
    p.add_argument("--artifacts", action="store_true",
                   help="report what became of each file the run wrote: left "
                        "behind, read downstream, or replaced by a later version")
    p.add_argument("--include-pids", action="store_true",
                   help="count PIDs as artifacts in --artifacts (a process "
                        "handle is not a file, so they are excluded by default)")
    p.add_argument("--warnings", action="store_true",
                   help="print the recorded extraction gaps")
    args = p.parse_args(argv)

    ir = _load(args.session, args.official_dir, not args.no_responses)
    if not ir.actions:
        print(f"[warn] no actions for session {args.session}", file=sys.stderr)

    g = build_skeleton([a.to_dict() for a in ir.actions],
                       session_id=ir.session_id)

    if args.stats:
        for k, v in g.stats().items():
            print(f"{k}: {v}")
    elif args.artifacts:
        print(artifacts.to_text(g, include_pids=args.include_pids))
    elif args.annotate:
        # Flag-style annotation overlay: on the graph page, each activity's
        # side panel gets a tag chip set + optional comment. Most steps stay
        # unlabelled; the export surfaces outliers for agent tuning.
        from graph.annotate_schema import STARTER_TAGSET
        from graph import html as html_mod
        prompts = list(getattr(ir, "prompts", ()) or ())
        responses = dict(getattr(ir, "responses_by_turn", {}) or {})
        tdm: dict[int, int] = {}
        official = getattr(ir, "official", None)
        if official is not None:
            for ot in getattr(official, "turns", ()) or ():
                if ot.duration_s is not None:
                    tdm[ot.turn] = int(ot.duration_s * 1000)
        try:
            print(html_mod.to_html(
                g, turns=args.turns, min_confidence=args.min_confidence,
                hide_isolated=args.hide_isolated, direction=args.direction,
                prompts=prompts, responses_by_turn=responses,
                turn_duration_ms=tdm, tagset=STARTER_TAGSET))
        except html_mod.HtmlError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
    elif args.format != "text":
        from graph import html as html_mod
        fn = {"mermaid": export.to_mermaid, "dot": export.to_dot,
              "html": html_mod.to_html}[args.format]
        extra = {} if args.format == "mermaid" else {"timeline": args.timeline}
        if args.format == "html":
            # What the user asked, per turn: the label on an overview card.
            extra["prompts"] = list(getattr(ir, "prompts", ()) or ())
            # What the model replied, per turn. A turn can have several text
            # messages (one before each tool, one after); the renderer picks
            # the last as the "final reply", which is what a reader sees as
            # the turn's conclusion.
            extra["responses_by_turn"] = dict(
                getattr(ir, "responses_by_turn", {}) or {})
            # Per-turn wall-clock from the official record (`turn_duration`
            # in Kiro's own metering). Per-action timestamps are not in the
            # jsonl, so this is the only source of "how long did this turn
            # last" short of file mtimes. ms, so the html side has one unit.
            tdm: dict[int, int] = {}
            official = getattr(ir, "official", None)
            if official is not None:
                for ot in getattr(official, "turns", ()) or ():
                    if ot.duration_s is not None:
                        tdm[ot.turn] = int(ot.duration_s * 1000)
            extra["turn_duration_ms"] = tdm
        if args.timeline and args.format == "mermaid":
            print("[warn] --timeline has no effect on mermaid output",
                  file=sys.stderr)
        try:
            print(fn(g, turns=args.turns, min_confidence=args.min_confidence,
                     hide_isolated=args.hide_isolated, direction=args.direction,
                     **extra))
        except html_mod.HtmlError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
    else:
        print(export.to_text(g, turns=args.turns,
                             min_confidence=args.min_confidence,
                             hide_isolated=args.hide_isolated))

    if args.warnings:
        print(f"\n--- {len(g.warnings)} warnings ---", file=sys.stderr)
        for w in g.warnings:
            print(w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
