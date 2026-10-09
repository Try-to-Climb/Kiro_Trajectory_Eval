"""evalkit one-shot pipeline: session -> (optional archive) -> normalize -> pick rule -> evaluate -> (optional OTLP/LLM).

Usage (from evalkit/):
    python3 pipeline.py <session-id>                     # official source, auto-pick rule by agent name
    python3 pipeline.py <session-id> --rule rules/x.json # explicit rule
    python3 pipeline.py <session-id> --archive           # first archive the whole dispatch tree via pack_run.sh
    python3 pipeline.py <session-id> --otel out.json     # also export OTLP/JSON
    python3 pipeline.py <session-id> --llm               # enable LLMJudge (kiro-as-LLM)
    python3 pipeline.py path/to/normalized.json          # can also consume a normalized file directly
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from normalize import load_trace_from_official, normalize_file, default_trace_dir  # noqa: E402
from normalize.otel_export import to_otlp_json                                     # noqa: E402
from rule.runner import run, load_actions                                   # noqa: E402

RULES_DIR = os.path.join(_HERE, "rules")
PACK_SH = os.path.abspath(os.path.join(_HERE, "..", "archive", "pack_run.sh"))


def rules_for_agent(agent: str) -> list[str]:
    hits = []
    for f in sorted(glob.glob(os.path.join(RULES_DIR, "*.checks.json"))):
        try:
            if json.load(open(f, encoding="utf-8")).get("target_agent") == agent:
                hits.append(f)
        except (OSError, json.JSONDecodeError):
            continue
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description="evalkit one-shot pipeline")
    ap.add_argument("session", help="session-id (official source) or path to normalized.json")
    ap.add_argument("--rule", help="rule file; if omitted, auto-pick by agent name")
    ap.add_argument("--source", choices=["official", "hook", "both"], default="official")
    ap.add_argument("--archive", action="store_true", help="first archive the whole dispatch tree via pack_run.sh")
    ap.add_argument("--otel", metavar="OUT", help="export OTLP/JSON to this file")
    ap.add_argument("--scoring", help="scoring config (default rules/scoring.json)")
    ap.add_argument("--llm", action="store_true", help="enable LLMJudge rule (kiro-as-LLM)")
    ap.add_argument("--judge-agent", default="kiro-judge")
    ap.add_argument("--effort")
    ap.add_argument("--json", action="store_true", help="output machine-readable JSON report")
    args = ap.parse_args()

    log = []                         # pipeline step log
    is_file = os.path.isfile(args.session)

    # -- 1. archive (optional; only meaningful for a session-id) --
    if args.archive and not is_file:
        if os.path.isfile(PACK_SH):
            r = subprocess.run(["bash", PACK_SH, args.session])
            log.append(f"archive: pack_run.sh exit code {r.returncode}")
        else:
            log.append(f"archive: skipped ({PACK_SH} not found)")

    # -- 2. normalize --
    ir = None
    if is_file:
        actions, d = load_actions(args.session)
        agent = d.get("agent_name")
        objective = (d.get("prompts") or [""])[0]
        log.append(f"normalize: read normalized file {args.session}  actions={len(actions)}")
    else:
        if args.source == "official":
            ir = load_trace_from_official(args.session)
        else:
            path = os.path.join(default_trace_dir(), args.session, "trace.jsonl")
            ir = normalize_file(path, enrich=(args.source == "both"))
        actions = [a.to_dict() for a in ir.actions]
        agent = ir.agent_name
        objective = (ir.prompts or [""])[0]
        log.append(f"normalize: source={args.source}  agent={agent}  actions={len(actions)}  turns={ir.turns}")

    # -- 3. pick rule --
    rule = args.rule
    if not rule:
        hits = rules_for_agent(agent) if agent else []
        if len(hits) == 1:
            rule = hits[0]
        elif not hits:
            sys.exit(f"[pipeline] no rule with target_agent=={agent!r}; pass --rule explicitly.\n"
                     f"  available rules: {[os.path.basename(f) for f in glob.glob(os.path.join(RULES_DIR,'*.checks.json'))]}")
        else:
            sys.exit(f"[pipeline] agent {agent!r} matches multiple rules; pass --rule to pick one:\n"
                     + "\n".join(f"  {f}" for f in hits))
    log.append(f"rule: {os.path.relpath(rule, _HERE)}")

    # -- 4. evaluate --
    context = {"objective": objective, "llm_caller": None}
    if args.llm:
        from rule import llm_judge
        context["llm_caller"] = lambda p: llm_judge.kiro_caller(
            p, agent=args.judge_agent, effort=args.effort)
        log.append(f"llm: enabled (judge-agent={args.judge_agent}, effort={args.effort or 'default'})")
    out = run(rule, actions, args.scoring, context)

    # -- 5. OTLP (optional) --
    if args.otel and ir is not None:
        open(args.otel, "w", encoding="utf-8").write(to_otlp_json(ir, source=args.source) + "\n")
        log.append(f"otel: exported -> {args.otel}")
    elif args.otel:
        log.append("otel: skipped (cannot export when starting from a normalized file; pass a session-id)")

    # -- report --
    if args.json:
        print(json.dumps({"pipeline": log, **out}, ensure_ascii=False, indent=2))
        return
    print("=" * 60)
    print("evalkit one-shot pipeline")
    for s in log:
        print("  \u2022", s)
    print("-" * 60)
    icon = {"PASS": "\u2705", "WEAK_PASS": "\u26a0\ufe0f", "FAIL": "\u274c"}.get(out["verdict"], "?")
    print(f"{icon} {out['verdict']}   health={out['health']}")
    print(f"{'checkpoint':<24}{'checker':<11}{'sev':<12}{'':<3}reason")
    print("-" * 90)
    for r in out["results"]:
        mark = "\u2713" if r["passed"] else "\u2717"
        conf = "" if r.get("confidence", 1.0) == 1.0 else f" (conf={r['confidence']})"
        print(f"{r['checkpoint_id']:<24}{r['checker']:<11}{r['severity']:<12}{mark:<3}{r['reason'][:52]}{conf}")


if __name__ == "__main__":
    main()
