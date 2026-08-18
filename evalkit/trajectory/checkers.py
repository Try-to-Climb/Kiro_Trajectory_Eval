"""Trajectory checker library.

Design (finalized with the user, item by item):
  - matcher supports both field matching and regex: only keys present in a spec
    are checked (AND). Discrete fields action/tool/pattern use equality;
    path/command/root use substring containment; regex searches the serialized
    string form of the action. Fields and regex can be mixed (fields narrow the
    scope first, then regex extracts details).
  - Every checker returns a uniform CheckResult (score + passed + reason +
    evidence), borrowing strands EvaluationOutput's triple but adding evidence
    (matched action idx).
  - Checkers have single, composable responsibilities:
      Exists     single action existence (unordered)
      Count      occurrence-count interval
      Forbidden  must not appear (with exclude to suppress false positives)
      Before     partial order between two actions (one pair)
      Milestone  order-preserving subsequence across the trajectory (anything
                 in between is ignored, progress score emitted; plan A: only
                 forward direction is enforced)
      IfThen     if a appears then b must appear (conditional implication,
                 ignoring order)
  - Continuous scoring is inspired by strands, but counting uses rigorous
    logic; we do not copy its zip-truncation / set-dedup implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Discrete fields use equality; path/command-like fields use containment.
_EQ_FIELDS = ("action", "tool", "raw_tool", "pattern")
_CONTAINS_FIELDS = ("path", "command", "root")


@dataclass
class CheckResult:
    checkpoint_id: str
    checker: str
    severity: str                 # required / recommended / forbidden / optional
    passed: bool
    score: float                  # 0.0~1.0
    reason: str
    evidence: list[int] = field(default_factory=list)   # related action idx
    confidence: float = 1.0        # deterministic checkers are always 1.0; LLMJudge uses this to express confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id, "checker": self.checker,
            "severity": self.severity, "passed": self.passed,
            "score": round(self.score, 3), "reason": self.reason,
            "evidence": self.evidence, "confidence": round(self.confidence, 3),
        }


def _serialize(action: dict) -> str:
    """Stitch an action into a regex-searchable string.

    Fields are joined by newlines (not spaces): regex `.` does not cross lines
    by default, avoiding false matches from "tail of previous field + head of
    next field" being concatenated into an adjacent substring (B7). Within-
    field matching is unaffected.
    """
    parts = [str(action.get(k, "") or "") for k in
             ("action", "tool", "command", "path", "root", "pattern")]
    return "\n".join(parts)


def match(action: dict, spec: dict) -> bool:
    """Check whether an action matches a spec. Only keys present are checked;
    all must be satisfied to match.

    Illegal regex does not crash: returns False (the real error is raised in
    run_check's validation gate; this is only a defensive fallback).
    """
    for k in _EQ_FIELDS:
        if k in spec and action.get(k) != spec[k]:
            return False
    for k in _CONTAINS_FIELDS:
        if k in spec:
            val = action.get(k) or ""
            if spec[k] not in val:
                return False
    if "regex" in spec:
        try:
            if not re.search(spec["regex"], _serialize(action)):
                return False
        except re.error:
            return False
    if "program" in spec:
        # Match by "invoked program": some subcommand (after whitespace strip)
        # must match this regex from its start. The `program` regex is emitted
        # by the DSL and already consumes optional prefixes (timeout/sudo/...).
        subs = action.get("subcommands") or (
            [action["command"]] if action.get("command") else [])
        try:
            if not any(isinstance(s, str) and re.match(spec["program"], s.strip())
                       for s in subs):
                return False
        except re.error:
            return False
    # A completely empty spec matches nothing (guard against accidental configs).
    return any(k in spec for k in (*_EQ_FIELDS, *_CONTAINS_FIELDS, "regex", "program"))


def _find(actions: list[dict], spec: dict) -> list[int]:
    """Return idx of all actions matching spec (in order of occurrence). If an
    action lacks idx, fall back to its list position (B2)."""
    return [a.get("idx", i) for i, a in enumerate(actions) if match(a, spec)]


# ---------------------------------------------------------------------------
# checker implementations
# ---------------------------------------------------------------------------

def check_exists(cp: dict, actions: list[dict]) -> CheckResult:
    hits = _find(actions, cp["match"])
    ok = len(hits) > 0
    return CheckResult(
        cp["id"], "Exists", cp.get("severity", "required"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or (f"action appeared (idx={hits[0]})" if ok else "expected action did not appear"),
        hits[:5],
    )


def check_count(cp: dict, actions: list[dict]) -> CheckResult:
    hits = _find(actions, cp["match"])
    n = len(hits)
    lo, hi = cp.get("min_count"), cp.get("max_count")
    ok = (lo is None or n >= lo) and (hi is None or n <= hi)
    bound = f"[{lo or 0}, {hi if hi is not None else '∞'}]"
    return CheckResult(
        cp["id"], "Count", cp.get("severity", "required"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or f"appeared {n} times, expected interval {bound}",
        hits[:10],
    )


def check_forbidden(cp: dict, actions: list[dict]) -> CheckResult:
    excl = cp.get("exclude")
    hits = []
    for a in actions:
        if not match(a, cp["match"]):
            continue
        if excl and match(a, excl):   # matched exclude -> not a violation
            continue
        hits.append(a["idx"])
    ok = len(hits) == 0            # forbidden: pass only when nothing matched
    return CheckResult(
        cp["id"], "Forbidden", cp.get("severity", "forbidden"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or ("no forbidden action appeared" if ok else f"forbidden action appeared (idx={hits})"),
        hits[:5],
    )


def check_before(cp: dict, actions: list[dict]) -> CheckResult:
    a_hits = _find(actions, cp["a"])
    b_hits = _find(actions, cp["b"])
    if not a_hits or not b_hits:
        # If either side is missing, partial order is meaningless -> treat as
        # pass (existence is guaranteed separately by Exists).
        return CheckResult(
            cp["id"], "Before", cp.get("severity", "required"), True, 1.0,
            cp.get("reason_tmpl") or "a or b did not both appear; partial order not applicable", [])
    ok = min(a_hits) < min(b_hits)
    return CheckResult(
        cp["id"], "Before", cp.get("severity", "required"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or
        (f"a(idx={min(a_hits)}) precedes b(idx={min(b_hits)})" if ok
         else f"wrong order: a(idx={min(a_hits)}) did not precede b(idx={min(b_hits)})"),
        [min(a_hits), min(b_hits)],
    )


def check_milestone(cp: dict, actions: list[dict]) -> CheckResult:
    """Order-preserving subsequence: advance a pointer through steps in order;
    anything in between is ignored."""
    steps = cp["steps"]
    idx = 0
    reached: list[int] = []
    ordered = sorted(enumerate(actions), key=lambda t: t[1].get("idx", t[0]))
    for pos, a in ordered:
        if idx < len(steps) and match(a, steps[idx]):
            reached.append(a.get("idx", pos))
            idx += 1
    total = len(steps)
    score = idx / total if total else 1.0
    ok = idx == total
    if ok:
        reason = f"all milestones hit in order ({total}/{total})"
    else:
        reason = f"stopped at milestone {idx}/{total} (remaining ones did not appear in order)"
    return CheckResult(
        cp["id"], "Milestone", cp.get("severity", "required"), ok, score,
        cp.get("reason_tmpl", "") + (f" — {reason}" if cp.get("reason_tmpl") else reason),
        reached,
    )


# Produce-style actions: represent the agent truly writing files via a tool.
# Scripts writing files inside run_command are invisible to the hook (no
# corresponding action), so Produces only recognizes tool-level outputs. Users
# should target "key artifacts written via write tools" (like
# test_cases_*.json, report_*.md), not the small files a script batch-creates.
PRODUCE_ACTIONS = ("create_file", "modify_file", "write", "append_file")


def check_produces(cp: dict, actions: list[dict]) -> CheckResult:
    """Produce check: user writes only the artifact name, and this auto-matches
    any produce-style action whose path contains that name.

    Check spec examples:
        {"id":"...","type":"Produces","name":"test_cases_security"}
        {"id":"...","type":"Produces","name":"report_", "min_count":4}   # at least 4 reports
    name is substring-matched against path; optional min_count (default 1).
    """
    name = cp.get("name", "")
    lo = cp.get("min_count", 1)
    hits = [a.get("idx", i) for i, a in enumerate(actions)
            if a.get("action") in PRODUCE_ACTIONS
            and name in (a.get("path") or "")
            and a.get("completed", True) is not False]   # N7: not completed (completed=False) doesn't count as produced
    ok = len(hits) >= lo
    return CheckResult(
        cp["id"], "Produces", cp.get("severity", "required"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or
        (f"produced '{name}' x {len(hits)} (need >= {lo})" if ok
         else f"did not produce '{name}' (need >= {lo}, actual {len(hits)}; note: files written by scripts are invisible to the hook)"),
        hits[:10],
    )


def check_ifthen(cp: dict, actions: list[dict]) -> CheckResult:
    """If a appears then b must appear (unordered). If a does not appear, this
    is vacuously true."""
    a_hits = _find(actions, cp["a"])
    if not a_hits:
        return CheckResult(
            cp["id"], "IfThen", cp.get("severity", "required"), True, 1.0,
            cp.get("reason_tmpl") or "premise action did not appear; condition not triggered", [])
    b_hits = _find(actions, cp["b"])
    ok = len(b_hits) > 0
    return CheckResult(
        cp["id"], "IfThen", cp.get("severity", "required"), ok,
        1.0 if ok else 0.0,
        cp.get("reason_tmpl") or
        (f"a appeared and b also appeared (idx={b_hits[0]})" if ok
         else f"a appeared (idx={a_hits[0]}) but required b is missing"),
        a_hits[:3] + b_hits[:3],
    )


CHECKERS = {
    "Exists": check_exists,
    "Count": check_count,
    "Forbidden": check_forbidden,
    "Before": check_before,
    "Milestone": check_milestone,
    "IfThen": check_ifthen,
    "Produces": check_produces,
}


_VALID_SEVERITY = {"required", "recommended", "forbidden", "optional"}

# Required keys per checker type
_REQUIRED_KEYS = {
    "Exists": ["match"], "Count": ["match"], "Forbidden": ["match"],
    "Before": ["a", "b"], "Milestone": ["steps"], "IfThen": ["a", "b"],
    "Produces": ["name"], "LLMJudge": ["dimension"],
}


def _collect_regexes(cp: dict) -> list[str]:
    """Collect every regex in a rule (match/a/b/exclude/steps[]) for pre-flight
    validity checks."""
    out = []
    def grab(spec):
        if isinstance(spec, dict) and isinstance(spec.get("regex"), str):
            out.append(spec["regex"])
    for k in ("match", "a", "b", "exclude"):
        grab(cp.get(k))
    for st in (cp.get("steps") or []):
        grab(st)
    return out


def _validate(cp: dict) -> Optional[str]:
    """Return an error description; None means the rule is valid."""
    t = cp.get("type")
    if t not in CHECKERS and t != "LLMJudge":
        return f"unknown checker type: {t}"
    for k in _REQUIRED_KEYS.get(t, []):
        if k not in cp:
            return f"{t} missing required key '{k}'"
    if t == "LLMJudge":
        from . import llm_judge
        if cp["dimension"] not in llm_judge.RUBRICS:
            return f"unknown judge dimension '{cp['dimension']}' (available: {llm_judge.available_dimensions()})"
        thr = cp.get("pass_threshold", 0.75)
        if not isinstance(thr, (int, float)) or not 0 <= thr <= 1:
            return f"pass_threshold must be in [0,1], got {thr!r}"
    if t == "Milestone" and not (isinstance(cp.get("steps"), list) and cp["steps"]):
        return "Milestone steps must be a non-empty list"
    if t == "Produces" and not cp.get("name"):
        return "Produces name must be non-empty (otherwise it would match all outputs)"
    if t == "Count":
        if cp.get("min_count") is None and cp.get("max_count") is None:
            return "Count must have at least one of min_count or max_count"
        if cp.get("max_count") is None and (cp.get("min_count") or 0) <= 0:
            return "Count lower bound is 0 and no upper bound -> always passes, meaningless (set min_count>=1 or add max_count)"
    for k in ("min_count", "max_count"):
        v = cp.get(k)
        if v is not None and (not isinstance(v, int) or v < 0):
            return f"{k} must be a non-negative integer, got {v!r}"
    if cp.get("severity") is not None and cp["severity"] not in _VALID_SEVERITY:
        return f"unknown severity '{cp['severity']}' (must be {sorted(_VALID_SEVERITY)})"
    for rx in _collect_regexes(cp):
        try:
            re.compile(rx)
        except re.error as e:
            return f"illegal regex {rx!r}: {e}"
    return None


def run_check(cp: dict, actions: list[dict], context: Optional[dict] = None) -> CheckResult:
    # Intent compilation failed -> raise error visibly
    if cp.get("type") == "__dsl_error__":
        return CheckResult(cp.get("id", "?"), "DSL", "required", False, 0.0,
                           f"rule error: {cp.get('_msg', 'intent compilation failed')}", [])
    # Rule is invalid -> return a "failing and visible" result (severity forced
    # to required so it neither silently passes nor crashes).
    err = _validate(cp)
    if err is not None:
        sev = cp.get("severity")
        sev = sev if sev in _VALID_SEVERITY else "required"
        return CheckResult(cp.get("id", "?"), cp.get("type") or "?", sev,
                           False, 0.0, f"rule error: {err}", [])
    if cp["type"] == "LLMJudge":
        return _run_llm_judge(cp, actions, context or {})
    try:
        return CHECKERS[cp["type"]](cp, actions)
    except Exception as e:   # runtime fallback so a single checker cannot crash the whole run
        return CheckResult(cp.get("id", "?"), cp.get("type") or "?",
                           cp.get("severity", "required"), False, 0.0,
                           f"checker runtime error: {e!r}", [])


def _run_llm_judge(cp: dict, actions: list[dict], context: dict) -> CheckResult:
    """LLMJudge: advisory scoring. No backend / any error -> non-penalizing
    pass (confidence=0). Only when the LLM actually runs and the score is
    below threshold is the result marked not passed."""
    from . import llm_judge
    from normalize.judge_view import build_view_from_actions
    cid = cp.get("id", "?")
    sev = cp.get("severity", "recommended")
    dim = cp["dimension"]
    thr = cp.get("pass_threshold", 0.75)
    caller = context.get("llm_caller")
    if caller is None:
        return CheckResult(cid, "LLMJudge", sev, True, 0.0,
                           f"LLM judge[{dim}] not run (no backend / not enabled)", [], confidence=0.0)
    view = build_view_from_actions(actions)
    res = llm_judge.judge(dim, context.get("objective", ""), view, caller=caller)
    if "error" in res:
        return CheckResult(cid, "LLMJudge", sev, True, 0.0,
                           f"LLM judge[{dim}] error: {res['error']}", [], confidence=0.0)
    score01 = res["score"] / 4.0
    return CheckResult(cid, "LLMJudge", sev, score01 >= thr, score01,
                       f"[{dim} {res['score']}/4] {res['justification']}", [], confidence=0.9)
