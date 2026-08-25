"""Standalone CLI runner for the efficiency workflow.

Usage examples::

    # Run a single step:
    python3 -m efficiency.runner s1 <sid> --official-dir <dir>
    python3 -m efficiency.runner s4 <sid> --official-dir <dir>

    # Run the full pipeline s1 .. s9 (fallback single-segment mode):
    python3 -m efficiency.runner all <sid> --official-dir <dir> --out report.json

    # With LLM segmentation and judgment:
    python3 -m efficiency.runner all <sid> --official-dir <dir> \\
        --use-llm --out report.json

    # With pre-defined segments file (skip s2 LLM):
    python3 -m efficiency.runner all <sid> --official-dir <dir> \\
        --segments segments.json
"""
from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from evidence.loader import load_run_tree

from . import steps
from .schema import Segment


def _load_segments_file(path: str) -> list[Segment]:
    data = json.load(open(path, encoding="utf-8"))
    segs_raw = data.get("segments") if isinstance(data, dict) else data
    return [
        Segment(seg=i + 1, start=s["start"], end=s["end"],
                theme=str(s.get("theme", ""))[:60])
        for i, s in enumerate(segs_raw)
    ]


def _single_segment(n_turns: int) -> list[Segment]:
    return [Segment(seg=1, start=1, end=n_turns, theme="whole session")]


def _dump(obj, path: str) -> None:
    def default(o):
        if hasattr(o, "to_dict"):
            return o.to_dict()
        return str(o)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=default)


def _get_tree(args):
    return load_run_tree(args.session, official_dir=args.official_dir,
                         with_children=False, include_responses=True,
                         use_cache=True)


def _get_segments(args, tree):
    if args.segments:
        return _load_segments_file(args.segments)
    return _single_segment(len(tree.root_node.ir.prompts))


# ---------------------------------------------------------------------------
# Per-step command handlers
# ---------------------------------------------------------------------------
def cmd_s1(args):
    ov = steps.s1_map(args.session, args.official_dir)
    t = ov["totals"]
    print(f"[s1] turns={ov['turns']}")
    print(f"     wall_s={t.get('wall_s') or 0:.0f}  net_s={t['net_s']:.0f}")
    print(f"     credits={t['credits']:.1f}  llm_reqs={t['llm_reqs']}  "
          f"cycles={t['cycles']}  compactions={t['compactions']}")
    if args.out:
        _dump(ov, args.out)


def cmd_s4(args):
    tree = _get_tree(args)
    segs = _get_segments(args, tree)
    dup = steps.s4_detect_duplicates(args.session, tree, segs)
    for seg_id, r in dup.items():
        print(f"[s4] seg {seg_id}: {r['n_commands']} commands, "
              f"{len(r['pairs'])} pairs, {len(r['clusters'])} clusters")
        print(f"     reason: {r['reason_distribution']}")
    if args.out:
        _dump(dup, args.out)


def cmd_s5(args):
    tree = _get_tree(args)
    segs = _get_segments(args, tree)
    wasted = steps.s5_detect_wasted_reads(args.session, tree, segs)
    for seg_id, r in wasted.items():
        pct = 100.0 * len(r["wasted"]) / max(r["n_reads"], 1)
        print(f"[s5] seg {seg_id}: {r['n_reads']} reads, "
              f"{len(r['wasted'])} wasted ({pct:.1f}%)")
    if args.out:
        _dump(wasted, args.out)


def cmd_s6(args):
    tree = _get_tree(args)
    segs = _get_segments(args, tree)
    undo = steps.s6_detect_undo(tree, segs)
    for seg_id, r in undo.items():
        print(f"[s6] seg {seg_id}: {r['n_writes']} writes, "
              f"{len(r['multi_write'])} paths written >=2 times")
        for m in r["multi_write"][:3]:
            print(f"     {m.count}x  {m.path}")
    if args.out:
        _dump(undo, args.out)


def cmd_s7(args):
    ov = steps.s1_map(args.session, args.official_dir)
    tree = _get_tree(args)
    segs = _get_segments(args, tree)
    stuck = steps.s7_detect_stuck(ov, tree, segs)
    for seg_id, r in stuck.items():
        print(f"[s7] seg {seg_id}: {len(r['stuck_turns'])} stuck turns, "
              f"{len(r['consecutive_failures'])} failure chains")
        for st in r["stuck_turns"][:5]:
            print(f"     turn {st.turn}: cycles={st.cycles} "
                  f"dur={st.dur_s:.0f}s credits={st.credits:.1f}")
    if args.out:
        _dump(stuck, args.out)


def cmd_all(args):
    use_llm = not args.no_llm

    ov = steps.s1_map(args.session, args.official_dir)
    print(f"[s1] turns={ov['turns']} credits={ov['totals']['credits']:.1f}")

    tree = _get_tree(args)

    if args.segments:
        segs = _load_segments_file(args.segments)
        print(f"[s2] loaded {len(segs)} segments from {args.segments}")
    else:
        segs = steps.s2_segment(ov, tree, use_llm=use_llm)
        mode = "LLM" if use_llm else "no-llm"
        print(f"[s2] {mode} produced {len(segs)} segment(s)")

    seg_stats = steps.s3_cost_stats(ov, segs)
    for s in seg_stats:
        print(f"[s3] seg {s.seg} ({s.range}, {s.n_turns} turns): "
              f"credits/turn={s.credits_per_turn:.2f} "
              f"cycles/turn={s.cycles_per_turn:.2f}")

    dup = steps.s4_detect_duplicates(args.session, tree, segs)
    for seg_id, r in dup.items():
        print(f"[s4] seg {seg_id}: {len(r['pairs'])} pairs / "
              f"{len(r['clusters'])} clusters")

    wasted = steps.s5_detect_wasted_reads(args.session, tree, segs)
    for seg_id, r in wasted.items():
        pct = 100.0 * len(r["wasted"]) / max(r["n_reads"], 1)
        print(f"[s5] seg {seg_id}: {r['n_reads']} reads, "
              f"{len(r['wasted'])} wasted ({pct:.1f}%)")

    undo = steps.s6_detect_undo(tree, segs)
    for seg_id, r in undo.items():
        print(f"[s6] seg {seg_id}: {r['n_writes']} writes, "
              f"{len(r['multi_write'])} paths written >=2 times")

    stuck = steps.s7_detect_stuck(ov, tree, segs)
    for seg_id, r in stuck.items():
        print(f"[s7] seg {seg_id}: {len(r['stuck_turns'])} stuck turns, "
              f"{len(r['consecutive_failures'])} failure chains")

    judgment = steps.s8_judge(ov, seg_stats, dup, wasted, undo, stuck, tree,
                              use_llm=use_llm)
    narrative = steps.s9_narrative(ov, seg_stats, dup, wasted, undo, stuck,
                                   tree=tree, use_llm=use_llm)
    print(f"[s8] global grade: {judgment.global_grade}")
    for g in judgment.per_segment:
        print(f"     seg {g.seg}: {g.grade}  {g.reason[:80]}")
    for i, r in enumerate(judgment.recommendations, 1):
        print(f"     rec {i}: {r[:120]}")

    report = steps.s9_aggregate(ov, segs, seg_stats, dup, wasted, undo,
                                stuck, judgment, narrative=narrative)
    print(f"[s9] verdict={report['verdict']}  "
          f"worst_turn={report['worst_turn']}")
    if args.out:
        _dump(report, args.out)
        print(f"[all] final report written to {args.out}")
        # Alongside the JSON, always emit the human-readable Markdown.
        # If --out is foo.json it goes to foo.md; if foo it goes to foo.md.
        import os as _os
        out_json = args.out
        stem, ext = _os.path.splitext(out_json)
        md_path = (stem + ".md") if ext else (out_json + ".md")
        try:
            md = steps.render_report_markdown(report, tree)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"[all] markdown report written to {md_path}")
        except Exception as e:
            print(f"[all] markdown render failed: {e!r}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="efficiency.runner",
        description="Run individual efficiency workflow steps.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p, need_segments: bool = False):
        p.add_argument("session", help="session id")
        p.add_argument("--official-dir", required=True,
                       help="directory containing <sid>.json and <sid>.jsonl")
        p.add_argument("--out", help="dump result to JSON file")
        if need_segments:
            p.add_argument("--segments",
                           help="pre-defined segments JSON file")

    p_s1 = sub.add_parser("s1", help="map: turn_metadata + totals")
    add_common(p_s1)
    p_s1.set_defaults(func=cmd_s1)

    for step_id, cmd, help_text in [
        ("s4", cmd_s4, "detect duplicates (embed-based)"),
        ("s5", cmd_s5, "detect wasted reads"),
        ("s6", cmd_s6, "detect multi-write paths"),
        ("s7", cmd_s7, "detect stuck turns and failure chains"),
    ]:
        p = sub.add_parser(step_id, help=help_text)
        add_common(p, need_segments=True)
        p.set_defaults(func=cmd)

    p_all = sub.add_parser("all", help="run the full pipeline s1..s9")
    add_common(p_all, need_segments=True)
    p_all.add_argument("--no-llm", action="store_true",
                       help="disable LLM for s2 (segmentation) and s8 (judge). "
                            "Default: LLM ON; falls back automatically on error.")
    p_all.set_defaults(func=cmd_all)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
