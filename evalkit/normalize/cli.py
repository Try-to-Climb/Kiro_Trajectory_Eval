"""Command-line entry point for the normalization layer.

    python3 -m normalize.cli dump   <session-id|path>   emit normalized actions (JSONL)
    python3 -m normalize.cli stats  <session-id|path>   stats for a single session
    python3 -m normalize.cli table  <session-id|path>   action timeline table
    python3 -m normalize.cli all    [--limit N]         batch stats across all sessions
    python3 -m normalize.cli export-otel <id|path> [--source hook|official|both]
                                                        one-shot export as OTLP/JSON
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys

from .core import default_trace_dir, iter_sessions, normalize_file
from .official_loader import load_trace_from_official
from .schema import TraceIR


def resolve(target: str) -> str:
    """Accepts a session-id, a session directory, or a trace.jsonl path."""
    if os.path.isfile(target):
        return target
    if os.path.isdir(target):
        p = os.path.join(target, "trace.jsonl")
        if os.path.isfile(p):
            return p
    base = default_trace_dir()
    p = os.path.join(base, target, "trace.jsonl")
    if os.path.isfile(p):
        return p
    # Compatible with the daily layout (TRACE_DIR/<date>/<session>/) and session-id prefixes:
    # recursively collect all session directories, match by exact name or prefix.
    if os.path.isdir(base):
        matches = []
        for root, _dirs, files in os.walk(base):
            if "trace.jsonl" not in files:
                continue
            name = os.path.basename(root)
            if name == target:
                return os.path.join(root, "trace.jsonl")
            if name.startswith(target):
                matches.append(os.path.join(root, "trace.jsonl"))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            sys.exit(f"session prefix {target!r} matched {len(matches)} entries, please give the full id")
    sys.exit(f"session not found: {target}")


def _load(args):
    incl = getattr(args, 'with_responses', False)
    if getattr(args, 'official', False):
        return load_trace_from_official(args.target, include_responses=incl)
    return normalize_file(resolve(args.target), include_responses=incl)


def cmd_dump(args) -> None:
    ir = _load(args)
    for a in ir.actions:
        print(json.dumps(a.to_dict(), ensure_ascii=False))


def cmd_table(args) -> None:
    ir = _load(args)
    extra = ""
    if ir.official is not None and ir.official.found:
        extra = f"  credits={ir.official.total_credits}"
    print(f"session {ir.session_id}  agent={ir.agent_name or '-'}  "
          f"{ir.runs} spawns  {ir.turns} turns  {len(ir.actions)} actions{extra}")
    print(f"{'idx':>4} {'run':>4} {'turn':>4} {'time':>9} {'action':<16} {'done':<5} target")
    for a in ir.actions:
        if a.command is not None:
            target = a.command.replace("\n", "⏎")[:70]
        else:
            target = a.path or a.pattern or a.root or ""
        flag = "ok" if a.completed else ("blocked" if a.blocked else "NO-RESP")
        print(f"{a.idx:>4} {a.run:>4} {a.turn:>3} {a.ts[11:19]:>9} {a.action:<16} {flag:<5} {target}")
    if ir.warnings:
        print("\nwarnings:")
        for w in ir.warnings:
            print(f"  - {w}")


def _stats(ir: TraceIR) -> dict:
    by_action = collections.Counter(a.action for a in ir.actions)
    by_tool = collections.Counter(a.tool for a in ir.actions)
    files_read = {a.path for a in ir.actions if a.action == "read_file" and a.path}
    files_written = {a.path for a in ir.actions
                     if a.action in ("create_file", "modify_file") and a.path}
    subcmds = sum(len(a.subcommands) for a in ir.actions)
    orphans = ir.orphans
    return {
        "session": ir.session_id,
        "agent": ir.agent_name,
        "turns": ir.turns,
        "runs": ir.runs,
        "calls": len({a.call_idx for a in ir.actions}),
        "actions": len(ir.actions),
        # Orphans have two counts: the number of unfinished "calls",
        # and the number of "actions" after fan-out.
        "orphan_calls": len({a.call_idx for a in orphans}),
        "orphans": len(orphans),
        "blocked": sum(1 for a in ir.actions if a.blocked),
        "subcommands": subcmds,
        "files_read": len(files_read),
        "files_written": len(files_written),
        "by_action": dict(by_action.most_common()),
        "by_tool": dict(by_tool.most_common()),
        "warnings": ir.warnings,
        "official": ({
            "found": True,
            "agent": ir.official.agent_name,
            "created_reason": ir.official.created_reason,
            "turns": len(ir.official.turns),
            "tool_uses_per_turn": ir.official.tool_uses_per_turn,
            "credits": ir.official.total_credits,
            "end_reasons": [t.end_reason for t in ir.official.turns],
            "assistant_chars": sum(len(t.assistant_text) for t in ir.official.turns),
            "thinking_chars": sum(len(t.thinking_text) for t in ir.official.turns),
            "rejected_tools": [t.rejected_tools for t in ir.official.turns],
        } if (ir.official is not None and ir.official.found) else {"found": False}),
    }


def cmd_stats(args) -> None:
    ir = _load(args)
    s = _stats(ir)
    print(json.dumps(s, ensure_ascii=False, indent=2))


def cmd_all(args) -> None:
    paths = list(iter_sessions(args.trace_dir or default_trace_dir()))
    if args.limit:
        paths = paths[: args.limit]

    tot_calls = tot_actions = tot_orphan_calls = tot_orphan_actions = tot_sub = 0
    tot_credits = 0.0
    multi_run = 0
    enriched = 0
    action_counter: collections.Counter = collections.Counter()
    tool_counter: collections.Counter = collections.Counter()
    warn_counter: collections.Counter = collections.Counter()

    print(f"{'session':<10} {'turn':>4} {'calls':>5} {'act':>5} {'fanout':>6} "
          f"{'orph':>5} {'subcmd':>6}  agent")
    for p in paths:
        ir = normalize_file(p)
        s = _stats(ir)
        fan = s["actions"] - s["calls"]
        print(f"{ir.session_id[:8]:<10} {s['turns']:>3} {s['calls']:>5} "
              f"{s['actions']:>5} {fan:>+5} {s['orphan_calls']:>5} {s['subcommands']:>6}  "
              f"{s['agent'] or '-'}")
        tot_calls += s["calls"]
        tot_actions += s["actions"]
        tot_orphan_calls += s["orphan_calls"]
        tot_orphan_actions += s["orphans"]
        tot_sub += s["subcommands"]
        if s["runs"] > 1:
            multi_run += 1
        if s["official"].get("found"):
            enriched += 1
            tot_credits += s["official"]["credits"] or 0
        action_counter.update(s["by_action"])
        tool_counter.update(s["by_tool"])
        for w in ir.warnings:
            warn_counter[w.split(":")[0].split("(")[0].strip()] += 1

    print(f"\n{len(paths)} sessions total")
    print(f"  tool calls        {tot_calls}")
    print(f"  normalized actions {tot_actions}   (fan-out added {tot_actions - tot_calls})")
    print(f"  orphan calls      {tot_orphan_calls}   ({tot_orphan_actions} actions incomplete after fan-out)")
    print(f"  shell subcommands {tot_sub}")
    print(f"  sessions with multiple agent spawns: {multi_run} (parent/child activity mixed in same directory)")
    print(f"  enrichable with official record: {enriched}, total billed {tot_credits:.4f} credits")
    print("\naction distribution:")
    for k, v in action_counter.most_common():
        print(f"  {v:>6}  {k}")
    if warn_counter:
        print("\nwarning types:")
        for k, v in warn_counter.most_common():
            print(f"  {v:>6}  {k}")


def cmd_timeline(args) -> None:
    from . import viz
    ir = _load(args)
    if args.format == "text":
        out = viz.text_timeline(ir)
        if not args.no_summary:
            out += "\n\n" + viz.turn_summary(ir)
        if args.out:
            open(args.out, "w", encoding="utf-8").write(out + "\n")
            print(f"wrote {args.out}")
        else:
            print(out)
    elif args.format == "mermaid":
        out = f"# {ir.agent_name or ir.session_id[:8]} timeline\n\n" + viz.to_mermaid(ir)
        if not args.no_summary:
            out += "\n\n```\n" + viz.turn_summary(ir) + "\n```\n"
        if args.out:
            open(args.out, "w", encoding="utf-8").write(out + "\n")
            print(f"wrote {args.out}")
        else:
            print(out)
    elif args.format == "png":
        out = args.out or f"{ir.session_id[:8]}_timeline.png"
        viz.to_png(ir, out)
        print(f"rendered {out}")


def cmd_replay(args) -> None:
    from . import viz
    ir = _load(args)
    script = viz.to_replay(ir)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(script)
        print(f"wrote {args.out}")
    else:
        print(script)


def cmd_compare(args) -> None:
    from . import viz
    sid = args.target
    # Locate the hook trace (tolerate the daily layout and prefixes); the official source uses sid directly
    hook_path = None
    try:
        hook_path = resolve(sid)
    except SystemExit:
        hook_path = None
    # resolve may return a directory or file path; normalize to a session id for the official source
    real_sid = os.path.basename(os.path.dirname(hook_path)) if hook_path else sid
    print(viz.compare_sources(real_sid, hook_path))


def _load_by_source(target: str, source: str, include_responses: bool = False) -> TraceIR:
    """Load a TraceIR by source:
        hook     — pure hook trace (enrich=False)
        official — pure Kiro official session record
        both     — hook trace enriched with the official record (enrich=True)
    """
    if source == "official":
        return load_trace_from_official(target, include_responses=include_responses)
    if source == "both":
        return normalize_file(resolve(target), enrich=True, include_responses=include_responses)
    return normalize_file(resolve(target), enrich=False, include_responses=include_responses)


def cmd_export_otel(args) -> None:
    """One-shot export as OpenTelemetry OTLP/JSON."""
    from .otel_export import to_otlp_json
    src_label = {"hook": "hook", "official": "official", "both": "hook+official"}[args.source]
    ir = _load_by_source(args.target, args.source,
                         include_responses=getattr(args, "with_responses", False))
    out_json = to_otlp_json(ir, source=src_label, indent=(None if args.compact else 2))
    if args.out:
        open(args.out, "w", encoding="utf-8").write(out_json + "\n")
        n_spans = sum(len(ss["spans"]) for rs in json.loads(out_json)["resourceSpans"]
                      for ss in rs["scopeSpans"])
        print(f"exported OTLP/JSON → {args.out}  ({n_spans} spans, source={src_label})")
    else:
        print(out_json)


def cmd_orchestration(args) -> None:
    """Cross-session panorama: parent + child agents share a single timeline."""
    from . import viz
    from .official_loader import load_trace_from_official
    sids = [s.strip() for s in args.sessions.split(",") if s.strip()]
    lanes = []
    override = {}
    for sid in sids:
        if args.official:
            ir = load_trace_from_official(sid)
        else:
            ir = normalize_file(resolve(sid))
        label = f"{ir.agent_name or sid[:8]} ({sid[:8]})"
        lanes.append((label, ir))
        # --fix-status: hook timeline + official success/failure. Actions from
        # the two sources correspond one-to-one by idx (same fan-out logic
        # yields the same sequence); use the official completed to override
        # the hook's misjudgments.
        if args.fix_status and not args.official:
            off = load_trace_from_official(sid)
            if off.official and off.official.found and len(off.actions) == len(ir.actions):
                override[ir.session_id] = {h.idx: o.completed
                                           for h, o in zip(ir.actions, off.actions)}
    if args.format == "png":
        out = args.out or "orchestration.png"
        viz.multi_session_png(lanes, out, completed_override=override or None)
        print(f"rendered {out}"
              + ("  (success/failure corrected using official source)" if override else ""))
    else:
        out = viz.multi_session_text(lanes)
        if args.out:
            open(args.out, "w", encoding="utf-8").write(out + "\n")
            print(f"wrote {args.out}")
        else:
            print(out)


def main(argv: list[str] | None = None) -> None:
    # When output pipes to head/less the pipe closes early; follow Unix convention
    # and exit silently rather than throwing a traceback.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(prog="normalize", description="trace.jsonl normalization")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("dump", cmd_dump), ("stats", cmd_stats), ("table", cmd_table)):
        p = sub.add_parser(name)
        p.add_argument("target", help="session-id / directory / trace.jsonl path")
        p.add_argument("--official", action="store_true",
                       help="read from Kiro official session record instead of hook trace")
        if name == "dump":
            p.add_argument("--with-responses", action="store_true",
                           help="also normalize full tool responses (default off, may be very large)")
        p.set_defaults(func=fn)

    p = sub.add_parser("all")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--trace-dir", default="")
    p.set_defaults(func=cmd_all)

    # timeline: timing chart (text lanes / Mermaid / PNG) + per-turn summary
    p = sub.add_parser("timeline", help="timeline chart + turn summary")
    p.add_argument("target")
    p.add_argument("--official", action="store_true")
    p.add_argument("--format", choices=["text", "mermaid", "png"], default="text")
    p.add_argument("--out", default="", help="output file (recommended for mermaid/png)")
    p.add_argument("--no-summary", action="store_true", help="only emit timeline, without turn summary")
    p.set_defaults(func=cmd_timeline)

    # replay: reconstruct action sequence as an equivalent script
    p = sub.add_parser("replay", help="restore action sequence into equivalent shell script")
    p.add_argument("target")
    p.add_argument("--official", action="store_true")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_replay)

    # compare: hook source vs official source for the same session
    p = sub.add_parser("compare", help="compare normalized results from hook source and official source")
    p.add_argument("target", help="session-id")
    p.set_defaults(func=cmd_compare)

    # export-otel: one-shot OTLP/JSON export (hook / official / both)
    p = sub.add_parser("export-otel", help="export as OpenTelemetry OTLP/JSON")
    p.add_argument("target", help="session-id / directory / trace.jsonl path")
    p.add_argument("--source", choices=["hook", "official", "both"], default="hook",
                   help="hook=pure hook trace  official=pure Kiro official record  both=hook+official enrichment")
    p.add_argument("--out", default="", help="output file; prints to stdout if omitted")
    p.add_argument("--compact", action="store_true", help="compact single-line JSON (default indent=2)")
    p.add_argument("--with-responses", action="store_true",
                   help="project full tool responses as kiro.tool.response (default off, may be very large)")
    p.set_defaults(func=cmd_export_otel)

    # orchestration: multi-session panorama (parent + children share a timeline)
    p = sub.add_parser("orchestration", help="cross-session panorama timeline (parent + child agents)")
    p.add_argument("sessions", help="comma-separated session-ids, e.g. parent,child1,child2")
    p.add_argument("--official", action="store_true")
    p.add_argument("--fix-status", action="store_true",
                   help="use official source success/failure to correct red crosses on hook timeline (recommended)")
    p.add_argument("--format", choices=["text", "png"], default="text")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_orchestration)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
