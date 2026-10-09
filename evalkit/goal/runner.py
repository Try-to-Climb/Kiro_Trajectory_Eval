"""goal orchestrator.

The first version is an **orchestrator**, not an autonomous agent: the nine steps run in a fixed
order, and the LLM is only invoked as a function in s2/s3/s4/s8. Upsides: unit-testable,
reproducible, predictable cost. Once this is stable we can consider giving s8 bounded follow-up
(a planned mode).

Usage (from the evalkit/goal/ directory):
  python3 runner.py <session-id>                       # full pipeline
  python3 runner.py <session-id> --no-llm              # deterministic steps only (s1)
  python3 runner.py <session-id> --requirements f.json # inject human-confirmed requirements, skip s2
  python3 runner.py <session-id> --json out.json       # machine-readable result
  python3 runner.py <session-id> --no-children         # only look at the root session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import schema                 # noqa: E402
from . import steps                  # noqa: E402
from evidence.loader import load_run_tree   # noqa: E402
from llm import LLMOutputError, LLMUnavailable   # noqa: E402


# ---------------------------------------------------------------------------
# ledger + budget
# ---------------------------------------------------------------------------
@dataclass
class Ledger:
    """Bookkeeping for every step. Stores refs/counts only, not full text — this is the
    foundation for controlling context and being auditable."""
    rows: list[dict] = field(default_factory=list)

    def add(self, step: str, kind: str, detail: dict) -> None:
        self.rows.append({"step": step, "kind": kind, "at": round(time.time(), 3),
                          **detail})

    @property
    def llm_calls(self) -> int:
        return sum(1 for r in self.rows if r["kind"] == "llm")

    def to_dict(self) -> list[dict]:
        return self.rows


@dataclass
class Budget:
    llm_calls: int = 8
    seconds: int = 3600
    _t0: float = field(default_factory=time.time)

    def check(self, used_llm: int) -> Optional[str]:
        if used_llm >= self.llm_calls:
            return f"LLM call count hit the limit {self.llm_calls}"
        if time.time() - self._t0 > self.seconds:
            return f"timed out after {self.seconds}s"
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run(session_id: str, *, official_dir: Optional[str] = None,
        with_children: bool = True, use_llm: bool = True,
        requirements_file: Optional[str] = None, controls: int = 1,
        k: int = 8, score_mode: str = "idf", caller=None,
        budget: Optional[Budget] = None, verbose: bool = True,
        agent_dir: Optional[str] = None,
        agent_mode: str = "raw") -> dict[str, Any]:
    led = Ledger()
    bud = budget or Budget()
    log = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    # ---- s0: agent-under-test context (optional; when injected, s2/s3 extraction better fits agent capabilities) ----
    from .agent_context import load_agent_context_or_empty  # noqa: E402
    agent_ctx, ctx_meta = load_agent_context_or_empty(
        agent_dir, mode=agent_mode, caller=caller,
        log=lambda *a: log(*a))
    if ctx_meta["chars"]:
        # Strip the map itself (may be large); only keep the summary in the ledger
        led.add("s0", "code", {"agent_context": {
            k: v for k, v in ctx_meta.items() if k != "map"}})

    # ---- s1 ----
    t0 = time.time()
    tree = load_run_tree(session_id, official_dir=official_dir,
                         with_children=with_children)
    ov = steps.s1_map(tree)
    led.add("s1", "code", {"actions_tree": ov["actions_tree"],
                           "children": len(ov.get("child_sessions") or []),
                           "ms": int((time.time() - t0) * 1000)})
    log(f"[s1] {ov['root'][:8]} agent={ov.get('agent_name')} "
        f"{ov['turns']} turns / root {ov['actions_root']} actions / whole tree {ov['actions_tree']} actions / "
        f"child sessions {len(ov.get('child_sessions') or [])}")

    out: dict[str, Any] = {"session": session_id, "overview": ov}
    if not use_llm and not requirements_file:
        out["note"] = "--no-llm: only s1 ran (s2 needs LLM or --requirements)"
        out["ledger"] = led.to_dict()
        return out

    # ---- s2 (can be injected from file, skipping the LLM; matches the open question of whether
    # requirement extraction needs human confirmation) ----
    if requirements_file:
        raw = json.load(open(requirements_file, encoding="utf-8"))
        reqs = [schema.Requirement(**r) for r in raw["requirements"]]
        out["turn_intents"] = raw.get("turn_intents", [])
        led.add("s2", "injected", {"n": len(reqs), "file": requirements_file})
    else:
        r2 = steps.s2_extract_requirements(tree, ov, caller=caller,
                                           agent_context=agent_ctx)
        led.add("s2", "llm", {"n": len(r2["requirements"])})
        reqs, out["turn_intents"] = r2["requirements"], r2["turn_intents"]
    log(f"[s2] requirements={len(reqs)} "
        f"(active/strong {sum(1 for r in reqs if r.status=='active' and r.strength=='strong')})")

    # ---- s2b synthetic control group ----
    ctrl_meta: list[dict] = []
    if controls > 0:
        reqs, ctrl_meta = steps.s2b_inject_controls(tree, reqs, n=controls)
        led.add("s2b", "code", {"injected": len(ctrl_meta)})
        log(f"[s2b] injected {len(ctrl_meta)} synthetic control(s) (must be judged false)")

    # ---- s3 ----
    claims: list[schema.Claim] = []
    dropped_claims: list[dict] = []
    if use_llm and not bud.check(led.llm_calls):
        try:
            r3 = steps.s3_extract_claims(tree, caller=caller,
                                         agent_context=agent_ctx)
            claims = r3.get("claims", [])
            dropped_claims = r3.get("dropped", [])
            led.add("s3", "llm", {"n": len(claims), "dropped": len(dropped_claims)})
        except (LLMOutputError, LLMUnavailable) as e:
            led.add("s3", "error", {"err": str(e)[:200]})
            log(f"[s3] skipped: {e}")
    log(f"[s3] self-claims {len(claims)}"
        + (f" (dropped {len(dropped_claims)} more whose quotes did not match the source)" if dropped_claims else ""))

    # ---- s4 ----
    r4 = steps.s4_compile(reqs, ov, caller=caller)
    criteria = r4["criteria"]
    led.add("s4", "llm", {"n": len(criteria)})
    log(f"[s4] criteria {len(criteria)} "
        f"(with hard_check {sum(1 for c in criteria.values() if c.hard_check)})")

    # ---- s5 / s6 / s7 (all deterministic) ----
    search = steps.s5_search(tree, reqs, criteria, ov, k=k, score_mode=score_mode)
    led.add("s5", "code", {"hard": sum(1 for s in search.values() if s.status == "hard"),
                           "candidates": sum(1 for s in search.values() if s.status == "candidates"),
                           "absent": sum(1 for s in search.values() if s.status == "absent")})
    log("[s5] " + "  ".join(f"{r.id}={search[r.id].status}" for r in reqs))

    probe = steps.s6_probe(tree, reqs, search, ov)
    led.add("s6", "code", {"probed": len(probe.get("per_req") or {})})

    cross = steps.s7_crosscheck(reqs, criteria, ov, tree)
    led.add("s7", "code", {"n": len(cross),
                           "strong": sum(c["strong"] for c in cross.values())})
    if cross:
        log("[s7] " + "  ".join(f"{k2}:{v['strong']}/{v['total']} strong"
                                for k2, v in cross.items()))

    # ---- s8 ----
    r8 = steps.s8_judge(reqs, criteria, search, probe, cross, claims, ov, caller=caller)
    findings = r8["findings"]
    led.add("s8", "llm", {"n": len(findings), "pack_chars": len(r8["pack"])})

    # ---- s9 ----
    agg = steps.s9_aggregate(findings, reqs)
    led.add("s9", "code", agg["counts"])

    out.update({
        "requirements": [r.to_dict() for r in reqs],
        "claims": [c.to_dict() for c in claims],
        "claims_dropped": dropped_claims,
        "criteria": {k2: v.to_dict() for k2, v in criteria.items()},
        "search": {k2: v.to_dict() for k2, v in search.items()},
        "crosscheck": cross,
        "findings": [f.to_dict() for f in findings],
        "aggregate": agg,
        "control_meta": ctrl_meta,
        "ledger": led.to_dict(),
        "pack_chars": len(r8["pack"]),
    })
    return out


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------
_ICON = {"true": "\u2713", "false": "\u2717", "unverifiable": "?"}


def render(res: dict[str, Any]) -> str:
    if "findings" not in res:
        return json.dumps(res.get("overview", res), ensure_ascii=False, indent=1)
    agg = res["aggregate"]
    L = [f"{agg['verdict']}   completion={agg['score']}   "
         f"(strong/active {agg['counts']['satisfied']}/{agg['counts']['strong_active']} satisfied, "
         f"{agg['counts']['unverifiable']} unverifiable, {agg['counts']['unmet']} unmet, "
         f"overclaim {agg['counts']['overclaim']})"]
    c = agg["control"]
    if c["injected"]:
        L.append(f"Built-in control group: {c['injected']} entries, "
                 + ("passed (all judged false)" if c["passed"] else "\u2605 FAILED - verdict too lax, result untrustworthy"))
    L.append("")
    L.append(f"{'req':<8}{'verdict':<9}{'tier':<15}{'conf':<7}reason")
    L.append("-" * 108)
    reqs = {r["id"]: r for r in res["requirements"]}
    for f in res["findings"]:
        r = reqs.get(f["req_id"], {})
        tag = "[ctrl]" if f.get("synthetic") else ("[weak]" if r.get("strength") == "weak" else "")
        L.append(f"{f['req_id']:<8}{_ICON.get(f['satisfied'],'?'):<9}"
                 f"{f['tier']:<15}{f['confidence']:<7}{tag}{f['reason'][:150]}")
        ev = (f.get("evidence_actions") or [])[:6]
        if ev:
            L.append(f"{'':<8}evidence: {', '.join(ev)}")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="goal",
                                description="goal_completion: did the agent do what the user asked?")
    p.add_argument("session")
    p.add_argument("--official-dir")
    p.add_argument("--no-children", action="store_true", help="only look at the root session")
    p.add_argument("--no-llm", action="store_true", help="run deterministic steps only")
    p.add_argument("--requirements", help="inject human-confirmed requirements JSON, skip s2")
    p.add_argument("--controls", type=int, default=1, help="how many synthetic control requirements to inject")
    p.add_argument("-k", type=int, default=8, help="retrieval candidate count")
    p.add_argument("--score-mode", choices=["count", "idf"], default="idf",
                   help="retrieval scoring: idf weights by anchor rarity (default), count just tallies hits")
    p.add_argument("--max-llm", type=int, default=8)
    p.add_argument("--agent-dir", help="agent-under-test directory (with prompt.md + .kiro/skills) - "
                                       "injected into s2/s3 prompts so extraction better fits the agent's capabilities")
    p.add_argument("--agent-mode", choices=["raw", "code", "llm"], default="raw",
                   help="agent context extraction mode: raw=raw concatenation / code=pure code structured / "
                        "llm=Kiro produces the structured map (disk-cached)")
    p.add_argument("--json", metavar="OUT", help="write the full result to a file")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    try:
        res = run(a.session, official_dir=a.official_dir,
                  with_children=not a.no_children, use_llm=not a.no_llm,
                  requirements_file=a.requirements, controls=a.controls,
                  k=a.k, score_mode=a.score_mode,
                  budget=Budget(llm_calls=a.max_llm), verbose=not a.quiet,
                  agent_dir=a.agent_dir, agent_mode=a.agent_mode)
    except (LLMUnavailable, LLMOutputError) as e:
        print(f"LLM step failed: {e}", file=sys.stderr)
        return 2
    except Exception as e:                        # loading / argument errors
        print(f"run failed: {e}", file=sys.stderr)
        return 1

    print(render(res))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f"\nfull result -> {a.json}", file=sys.stderr)
    v = res.get("aggregate", {}).get("verdict")
    return 0 if v in (None, "PASS", "WEAK_PASS") else 3


if __name__ == "__main__":
    raise SystemExit(main())
