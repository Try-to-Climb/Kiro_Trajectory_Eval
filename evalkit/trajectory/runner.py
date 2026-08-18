"""Trajectory runner: run checkpoint rules against a normalized session and
output the aggregate verdict.

Usage:
    python3 -m trajectory.runner <checks.json> <normalized.json>
    python3 -m trajectory.runner <checks.json> --session <sid>   # normalize hook trace directly
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trajectory.checkers import run_check  # noqa: E402


def load_actions(normalized_path: str) -> tuple[list[dict], dict]:
    d = json.load(open(normalized_path, encoding="utf-8"))
    return d.get("actions", []), d


DEFAULT_SCORING = {
    "weights": {"required": 1.0, "forbidden": 1.0, "recommended": 0.5, "optional": 0.25},
    "binarize_checkers": ["Milestone"],
    "verdict": {"fail_on_required_miss": True, "fail_on_forbidden_hit": True,
                "weak_on_recommended_miss": True},
}


def load_scoring(path: str | None) -> dict:
    """Load the scoring config.

    - When path is explicitly given: it must be readable and valid; if
      corrupt, **fall back to built-in defaults and warn on stderr**. Never
      silently switch to rules/scoring.json (avoid making the user think
      their own config is being used).
    - When path is not given: look for rules/scoring.json; if missing or
      corrupt, use built-in defaults.
    """
    import os as _os
    def _load(c):
        cfg = json.load(open(c, encoding="utf-8"))
        return {**DEFAULT_SCORING, **{k: v for k, v in cfg.items() if not k.startswith("_")}}

    if path:
        try:
            return _load(path)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] scoring file could not be loaded ({path}): {e}; using built-in defaults", file=sys.stderr)
            return DEFAULT_SCORING

    default_path = _os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "rules", "scoring.json")
    if _os.path.isfile(default_path):
        try:
            return _load(default_path)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_SCORING


def verdict(results: list, scoring: dict | None = None) -> tuple[str, float]:
    """Aggregate verdict + health score, driven by the scoring config.

    Verdict: any fail condition met -> FAIL; else any weak condition met ->
    WEAK_PASS; else PASS.
    Health score: Sigma(weight * score) / Sigma(weight); types in
    binarize_checkers are counted all-or-nothing (no partial credit).
    """
    sc = scoring or DEFAULT_SCORING
    w = sc.get("weights", DEFAULT_SCORING["weights"])
    binarize = set(sc.get("binarize_checkers", []))
    vrule = sc.get("verdict", DEFAULT_SCORING["verdict"])

    hard_fail = weak = False
    num = den = 0.0
    _known = {"required", "recommended", "forbidden", "optional"}
    for r in results:
        # LLMJudge results that did not actually run (no backend / error,
        # confidence=0) are excluded from both the verdict and the health score.
        if getattr(r, "checker", "") == "LLMJudge" and getattr(r, "confidence", 1.0) == 0.0:
            continue
        sev = r.severity if r.severity in _known else "required"
        if sev == "forbidden" and not r.passed and vrule.get("fail_on_forbidden_hit", True):
            hard_fail = True
        elif sev == "required" and not r.passed and vrule.get("fail_on_required_miss", True):
            hard_fail = True
        elif sev == "recommended" and not r.passed and vrule.get("weak_on_recommended_miss", True):
            weak = True

        # Health-score contribution: for a binarize checker, "not satisfied" is 0 (ignore progress score).
        score = (1.0 if r.passed else 0.0) if r.checker in binarize else r.score
        wt = w.get(sev, w.get('required', 1.0))
        num += wt * score
        den += wt

    health = (num / den) if den else 1.0
    if hard_fail:
        return "FAIL", health
    if weak:
        return "WEAK_PASS", health
    return "PASS", health


def run(checks_path: str, actions: list[dict], scoring_path: str | None = None,
        context: dict | None = None) -> dict:
    spec = json.load(open(checks_path, encoding="utf-8"))
    from trajectory.rules_dsl import desugar_checks
    checks = desugar_checks(spec.get("checks", []))     # intent -> low-level checker (rules with type are passed through)
    results = [run_check(cp, actions, context) for cp in checks]
    v, health = verdict(results, load_scoring(scoring_path))
    return {"verdict": v, "health": round(health, 3),
            "results": [r.to_dict() for r in results]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("checks")
    ap.add_argument("normalized", nargs="?")
    ap.add_argument("--session")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--scoring", help="scoring config file (default rules/scoring.json)")
    ap.add_argument("--llm", action="store_true",
                    help="enable LLMJudge rules (uses kiro-cli non-interactive as the LLM); if omitted, such rules are skipped")
    ap.add_argument("--judge-agent", default="kiro-judge", help="agent name used for judging (must have no tools)")
    ap.add_argument("--effort", help="--effort for the judge (default: unspecified, use Kiro default)")
    ap.add_argument("--compile", action="store_true",
                    help="only print the low-level checkers compiled from the intent rules (JSON); do not evaluate")
    args = ap.parse_args()

    if args.compile:
        try:
            spec = json.load(open(args.checks, encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[compile] rule file could not be parsed: {e}", file=sys.stderr)
            sys.exit(2)
        from trajectory.rules_dsl import desugar_checks
        from trajectory.checkers import _validate
        checks = desugar_checks(spec.get("checks", []))
        print(json.dumps({"target_agent": spec.get("target_agent"), "checks": checks},
                         ensure_ascii=False, indent=2))
        # Self-check: unrecognized intents + validation errors after compilation
        problems = []
        for c in checks:
            cid = c.get("id", "?")
            if c.get("type") == "__dsl_error__":
                problems.append((cid, c.get("_msg", "intent unrecognized")))
                continue
            err = _validate(c)
            if err:
                problems.append((cid, err))
        if problems:
            print(f"\n[compile] found {len(problems)} problem(s):", file=sys.stderr)
            for cid, msg in problems:
                print(f"  x {cid}: {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"\n[compile] OK: {len(checks)} rules compiled and validated", file=sys.stderr)
        return

    objective = ""
    if args.session:
        from normalize import normalize_file, default_trace_dir
        ir = normalize_file(os.path.join(default_trace_dir(), args.session, "trace.jsonl"))
        actions = [a.to_dict() for a in ir.actions]
        objective = (ir.prompts or [""])[0]
    elif args.normalized:
        actions, d = load_actions(args.normalized)
        objective = (d.get("prompts") or [""])[0]
    else:
        ap.error("provide normalized.json or --session")

    context = {"objective": objective, "llm_caller": None}
    if args.llm:
        from trajectory import llm_judge
        context["llm_caller"] = lambda p: llm_judge.kiro_caller(
            p, agent=args.judge_agent, effort=args.effort)

    out = run(args.checks, actions, args.scoring, context)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    icon = {"PASS": "✅", "WEAK_PASS": "⚠️", "FAIL": "❌"}[out["verdict"]]
    print(f"{icon} {out['verdict']}   health score={out['health']}")
    print(f"{'checkpoint':<26}{'checker':<11}{'sev':<12}{'':<4}reason")
    print("-" * 100)
    for r in out["results"]:
        mark = "✓" if r["passed"] else "✗"
        print(f"{r['checkpoint_id']:<26}{r['checker']:<11}{r['severity']:<12}{mark:<4}{r['reason'][:56]}")


if __name__ == "__main__":
    main()
