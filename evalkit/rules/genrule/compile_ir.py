#!/usr/bin/env python3
"""compile_ir.py — policy IR -> .checks.json, with severity calibrated against real runs.

Both steps are deterministic Python; no LLM is involved.

1. Compile: deterministic policies become intent checks; judge_only policies are merged per
   dimension into judge checks; unobservable policies are dropped; the backbone becomes a
   pipeline plus before checks.

2. Calibrate (--calibrate): run every candidate check against a set of runs that are **known to
   be legitimate** and grade by hit rate:
     hit rate == 1.0 and enough runs -> required
     hit rate == 1.0 but too few runs -> recommended (4/4 does not prove "always")
     0 < hit rate < 1.0              -> recommended
     hit rate == 0                   -> optional, flagged REVIEW
   Prohibitions invert: if any legitimate run trips a never_* rule, the prohibition is too broad
   and gets dropped.

   This supplies what an LLM cannot derive from a prompt: severity is really the question
   "does every legitimate run do this?", and only real traces answer it.

Usage:
    python3 rules/genrule/compile_ir.py <ir.json> --calibrate <sid> ... --out <checks.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVALKIT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, EVALKIT)

from trajectory.checkers import run_check       # noqa: E402
from trajectory.rules_dsl import desugar_check  # noqa: E402

NEVER = {"never_runs", "never_reads", "never_writes", "never_dispatches"}
ORDERING = {"pipeline", "before"}
DIMS = {"efficiency", "reasoning_quality", "authenticity"}


def slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(s)).strip("_").lower()[:28] or "x"


def normalize_target(intent: str, target: str) -> str:
    """Fix the syntax mismatch an LLM keeps making.

    `write` compiles to Produces, whose name is a **substring** match rather than a glob
    (see AUTHORING.md), so `*x.json` or `a/*_b.log` can never hit. Split on `*` and keep one
    literal segment: prefer a segment carrying an extension (`_live.log`), else the longest;
    a bare extension (`.json`) is too generic and is excluded.
    """
    if intent == "write" and "*" in target:
        segs = [s for s in target.split("*") if s]
        segs = [s for s in segs if not re.fullmatch(r"\.\w{1,5}", s)] or segs
        if not segs:
            return target
        dotted = [s for s in segs if "." in s]
        return max(dotted or segs, key=len)
    return target


def build_candidates(ir: dict) -> tuple[list, list, list]:
    """IR -> candidate checks, entries dropped as unobservable, entries rejected as invalid."""
    checks, dropped, rejected, seen = [], [], [], {}
    subs = [s.lower() for s in (ir.get("known_subagents") or [])]
    for p in ir.get("policies", []):
        obs, intent, target = p.get("observability"), p.get("intent"), p.get("target")
        if obs == "unobservable" or not intent or not target:
            dropped.append({"scope": p.get("scope_id"), "policy": p.get("policy")})
            continue
        if obs == "judge_only":
            continue                                   # merged below

        # A dispatch target must be a real sub-agent. Skills are not sub-agents: invoking one
        # produces no spawn_subagent action, so such a check could never fire.
        if intent in ("dispatches", "never_dispatches"):
            bare = target.strip("*").lower()
            if not subs:
                rejected.append({"scope": p.get("scope_id"), "intent": intent, "target": target,
                                 "reason": "agent declares no sub-agents; a dispatch check "
                                           "can never fire (is this a skill?)",
                                 "policy": p.get("policy")})
                continue
            if not any(bare in s or s in bare for s in subs):
                rejected.append({"scope": p.get("scope_id"), "intent": intent, "target": target,
                                 "reason": f"not a declared sub-agent (known: {', '.join(subs)})",
                                 "policy": p.get("policy")})
                continue

        target = normalize_target(intent, target)
        key = (intent, target)
        hint = p.get("severity_hint", "recommended")
        if key in seen:                                # dedupe on (intent, target)
            idx = seen[key]
            if hint == "required":
                checks[idx]["importance"] = "required"
            checks[idx]["_policies"].append(p.get("policy"))
            continue
        cp = {"id": f"{p.get('scope_id', 'S')}_{intent}_{slug(target)}",
              intent: target,
              "importance": "forbidden" if intent in NEVER else hint,
              "as": (p.get("policy") or "")[:110],
              "_policies": [p.get("policy")]}
        seen[key] = len(checks)
        checks.append(cp)

    bydim: dict[str, list] = {}
    for p in ir.get("policies", []):
        if p.get("observability") == "judge_only":
            d = p.get("target") if p.get("target") in DIMS else "reasoning_quality"
            bydim.setdefault(d, []).append(p.get("policy"))
    for d, pols in sorted(bydim.items()):
        checks.append({"id": f"judge_{d}", "judge": d, "importance": "recommended",
                       "as": f"{len(pols)} requirements decidable only semantically "
                             f"(runs only with --llm)",
                       "_policies": pols})

    bb = ir.get("backbone") or {}
    if bb.get("pipeline"):
        checks.append({"id": "MS_backbone", "pipeline": bb["pipeline"],
                       "importance": "recommended",
                       "as": bb.get("rationale", "backbone order")[:110],
                       "_policies": []})
    for pair in (bb.get("before") or []):
        if isinstance(pair, list) and len(pair) == 2:
            checks.append({"id": f"before_{slug(pair[0])}", "before": pair,
                           "importance": "recommended",
                           "as": f"{pair[0]} must precede {pair[1]}",
                           "_policies": []})
    return checks, dropped, rejected


def load_actions(sid: str) -> list[dict]:
    from normalize import normalize_file, default_trace_dir
    ir = normalize_file(os.path.join(default_trace_dir(), sid, "trace.jsonl"))
    return [a.to_dict() for a in ir.actions]


def _rate(low: dict, runs: dict, is_forbidden: bool) -> float:
    hits = 0
    for acts in runs.values():
        r = run_check(low, acts, None)
        hits += 1 if ((not r.passed) if is_forbidden else r.passed) else 0
    return hits / len(runs) if runs else 0.0


def calibrate(checks: list, sessions: list[str],
              min_required_runs: int = 5) -> tuple[list, list, list]:
    """Grade severity from hit rates over legitimate runs.

    Returns (kept checks, report rows, dropped prohibitions).

    Two anti-overfit rules, both derived from leave-one-out validation:
      1. below min_required_runs samples nothing is promoted to required — 4/4 does not
         establish "always";
      2. ordering checks (pipeline / before) are capped at recommended, since order is the
         least stable thing across legitimate runs (every LOO misjudgement came from `before`).

    A never_* rule cannot be weakened by lowering importance — rules_dsl forces its severity to
    forbidden. A prohibition that contradicts observed legitimate behaviour is therefore
    **dropped** and reported, for a human to add an `except` clause before re-enabling.
    """
    runs = {sid: load_actions(sid) for sid in sessions}
    n = len(runs)
    report, kept, conflicts = [], [], []
    for cp in checks:
        pub = {k: v for k, v in cp.items() if not k.startswith("_")}
        low = desugar_check(pub)
        if low.get("type") in ("LLMJudge", "__dsl_error__"):
            report.append({"id": cp["id"], "hit_rate": None, "severity": cp["importance"],
                           "note": "skipped (" + str(low.get("type")) + ")"})
            kept.append(cp)
            continue
        is_forbidden = cp["importance"] == "forbidden"
        rate = _rate(low, runs, is_forbidden)
        note = ""
        if is_forbidden:
            if rate > 0:
                nk = next(k for k in NEVER if k in cp)
                conflicts.append({"id": cp["id"], "intent": nk, "target": cp[nk],
                                  "hit_rate": round(rate, 3),
                                  "policies": cp.get("_policies", []),
                                  "advice": "prohibition contradicts legitimate behaviour; "
                                            "add an except clause or narrow the glob"})
                report.append({"id": cp["id"], "hit_rate": round(rate, 3), "severity": "DROPPED",
                               "note": f"CONFLICT: tripped by {rate:.0%} of legitimate runs"})
                continue
            note = "never tripped by a legitimate run; kept"
        else:
            # A write that never hits: probe `touches` to tell "write is invisible" from
            # "never happened" (shell redirection and in-script writes are known blind spots).
            if rate == 0 and "write" in cp:
                probe = {"touches": "*" + str(cp["write"]).strip("*") + "*"}
                if _rate(desugar_check(probe), runs, False) > 0:
                    cp["touches"] = probe["touches"]
                    del cp["write"]
                    low = desugar_check({k: v for k, v in cp.items() if not k.startswith("_")})
                    rate = _rate(low, runs, False)
                    note = ("FALLBACK: no write action but the path appears "
                            "(shell redirection / in-script write); switched to touches; ")
            is_order = any(k in cp for k in ORDERING)
            if rate == 1.0 and is_order:
                cp["importance"] = "recommended"
                note += "full hit, but ordering is capped at recommended (order is unstable)"
            elif rate == 1.0 and n < min_required_runs:
                cp["importance"] = "recommended"
                note += f"full hit, but {n} < {min_required_runs} runs cannot establish required"
            elif rate == 1.0:
                cp["importance"] = "required"
                note += f"hit by all {n}/{n} legitimate runs"
            elif rate > 0:
                cp["importance"] = "recommended"
                note += "hit by some legitimate runs"
            else:
                cp["importance"] = "optional"
                note += "REVIEW: no legitimate run hit it (bad glob, or it never happens)"
        report.append({"id": cp["id"], "hit_rate": round(rate, 3),
                       "severity": cp["importance"], "note": note})
        kept.append(cp)
    return kept, report, conflicts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ir")
    ap.add_argument("--calibrate", nargs="*", default=[],
                    help="session ids of runs known to be legitimate")
    ap.add_argument("--min-required-runs", type=int, default=5,
                    help="runs needed before a check may be promoted to required (default 5)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ir = json.load(open(args.ir, encoding="utf-8"))
    checks, dropped, rejected = build_candidates(ir)
    print(f"{len(checks)} candidate checks; {len(dropped)} dropped as unobservable; "
          f"{len(rejected)} rejected as invalid")
    for r in rejected:
        print(f"  [reject] {r['intent']} {r['target']}: {r['reason']}")

    conflicts = []
    if args.calibrate:
        checks, report, conflicts = calibrate(checks, args.calibrate, args.min_required_runs)
        print(f"\ncalibration over {len(args.calibrate)} legitimate run(s):")
        print(f"  {'checkpoint':<44}{'hit':>6}  {'severity':<12}note")
        for r in report:
            hr = "-" if r["hit_rate"] is None else f"{r['hit_rate']:.2f}"
            print(f"  {r['id']:<44}{hr:>6}  {r['severity']:<12}{r['note']}")
        if conflicts:
            print(f"\n[!] dropped {len(conflicts)} prohibition(s) conflicting with legitimate "
                  f"behaviour (need a manual except clause):")
            for c in conflicts:
                print(f"  - {c['id']}: {c['intent']} {c['target']}  (tripped {c['hit_rate']:.0%})")

    task = ir.get("task_name") or ""
    spec = {"$schema": "kiro-eval-golden-v2",
            "target_agent": ir.get("target_agent"),
            "policy": "calibrated" if args.calibrate else "uncalibrated",
            "description": (f"Generated by rules/genrule from "
                            f"{os.path.basename(ir.get('source_config', ''))}"
                            + (f", task type: {task}" if task else "")
                            + (f", severity calibrated on {len(args.calibrate)} real runs"
                               if args.calibrate else "")),
            "checks": [{k: v for k, v in c.items() if not k.startswith("_")} for c in checks]}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(spec, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
