"""Evidence layer API: overview / query / retrieve / get / window / hard_check / crosscheck.

Three hard constraints (DESIGN.md §3.1):
  1. Bounded returns — the default is a summary row + total/has_more; full text
     must be requested explicitly.
  2. Read-only — nothing is written; crosscheck is constrained by a path allowlist.
  3. Auditable — every call is recorded in the ledger by the runner (only refs,
     not full text).
"""

from __future__ import annotations

import datetime as dt
import glob as _glob
import math
import os
import re
from collections import Counter
from typing import Any, Optional

from . import loader  # noqa: F401  (triggers sys.path bootstrap)
from .loader import RunTree
from trajectory.checkers import _serialize, match   # Reuse evalkit, do not fork

# Full set of action names supported by normalization (static constant, session-agnostic).
# The criteria must be able to describe "an action that did NOT happen this run", otherwise
# we cannot detect "requirement not met" — so the vocabulary cannot be derived from this
# session's by_action. See DESIGN.md §5.3.
ACTION_VOCAB = (
    "read_file", "list_dir", "read_image",
    "create_file", "modify_file", "append_file",
    "run_command",
    "search_content", "search_files",
    "spawn_subagent", "list_subagents",
    "aws_call", "docs_query", "summarize",
    "code_search_symbols", "code_get_document_symbols", "code_pattern_search",
    "knowledge_search", "knowledge_add",
)
READ_ACTIONS = ("read_file", "list_dir", "read_image", "code_get_document_symbols")
WRITE_ACTIONS = ("create_file", "modify_file", "append_file")

_SUMMARY_CAP = 220


# ---------------------------------------------------------------------------
# Summary row: retrieval / probe always returns this, never full text
# ---------------------------------------------------------------------------
def summarize(a: dict, cap: int = _SUMMARY_CAP) -> dict[str, Any]:
    tgt = (a.get("command") or a.get("path") or a.get("pattern") or a.get("root") or "")
    tgt = str(tgt).replace("\n", " \u23ce ")
    purpose = (a.get("args") or {}).get("__tool_use_purpose") or ""
    return {
        "ref": a["ref"], "sid": a["sid"][:8], "idx": a["idx"], "turn": a.get("turn"),
        "action": a.get("action"), "target": tgt[:cap],
        "purpose": str(purpose)[:160],
        "blocked": bool(a.get("blocked")), "error": a.get("error"),
    }


# ---------------------------------------------------------------------------
# 1. overview — snapshot: boundaries + capability probe
# ---------------------------------------------------------------------------
def overview(tree: RunTree) -> dict[str, Any]:
    root = tree.root_node
    ir = root.ir
    acts = tree.root_actions
    written = [a for a in tree.actions if a.get("action") in WRITE_ACTIONS and a.get("path")]
    lo, hi = tree.time_window
    idx_range = {}
    for t in range(1, ir.turns + 1):
        rows = [a for a in acts if a.get("turn") == t]
        if rows:
            idx_range[t] = [rows[0]["idx"], rows[-1]["idx"]]
    return {
        "root": tree.root, "agent_name": root.agent_name,
        "turns": ir.turns,
        "actions_root": len(acts), "actions_tree": len(tree.actions),
        "idx_range_per_turn": idx_range,
        "actions_per_turn": {t: sum(1 for a in acts if a.get("turn") == t)
                             for t in range(1, ir.turns + 1)},
        "written_dirs": sorted({os.path.dirname(a["path"]) for a in written}),
        "written": [{"ref": a["ref"], "turn": a.get("turn"), "path": a["path"]}
                    for a in written],
        "time_window": [lo.isoformat() if lo else None, hi.isoformat() if hi else None],
        # Capability probe: tells us which steps must degrade and explicitly flag "not covered"
        "has_response": any(a.get("response") for a in tree.actions),
        "has_reasoning": any(a.get("reasoning") for a in tree.actions),
        "has_timestamps": any(a.get("ts") for a in tree.actions),
        "blocked": sum(1 for a in tree.actions if a.get("blocked")),
        "orphans": sum(1 for a in tree.actions
                       if not a.get("completed") and not a.get("blocked")),
        "child_sessions": [
            {"sid": n.sid[:8], "full": n.sid, "agent": n.agent_name,
             "depth": n.depth, "parent": (n.parent or "")[:8],
             "actions": sum(1 for a in tree.actions if a["sid"] == n.sid),
             "reason": n.created_reason}
            for n in tree.nodes.values() if n.sid != tree.root
        ],
        "by_action": dict(Counter(a.get("action") for a in tree.actions).most_common()),
        "warnings": tree.warnings,
    }


# ---------------------------------------------------------------------------
# 2. query — structured filter (reuses evalkit's `match`)
# ---------------------------------------------------------------------------
def query(tree: RunTree, spec: dict, scope: Optional[dict] = None,
          limit: int = 20, root_only: bool = False) -> dict[str, Any]:
    pool = _scope(tree.root_actions if root_only else tree.actions, scope)
    hits = [a for a in pool if match(a, spec)]
    return {"total": len(hits), "has_more": len(hits) > limit,
            "rows": [summarize(a) for a in hits[:limit]]}


# ---------------------------------------------------------------------------
# 3. retrieve — anchor-based retrieval (the core of v2: only candidates, no verdicts)
# ---------------------------------------------------------------------------
def searchable_text(a: dict) -> str:
    """The retrievable text for an action.

    On top of evalkit's `_serialize` (6 fields), we add the two most semantically
    loaded fields:
      __tool_use_purpose — the agent's own note "why I called this tool"
      reasoning          — the official-source thinking
    Both are natural language in the same idiom as the requirement text, so anchor
    match rate is much higher than digging for words inside shell commands. See
    DESIGN.md optimization item 4.
    """
    parts = [_serialize(a)]
    purpose = (a.get("args") or {}).get("__tool_use_purpose")
    if purpose:
        parts.append(str(purpose))
    if a.get("reasoning"):
        parts.append(str(a["reasoning"]))
    return "\n".join(parts).lower()


def anchor_df(tree: RunTree, anchors: list[str],
              pool: Optional[list[dict]] = None) -> dict[str, int]:
    """How many actions each anchor hit (document frequency).

    Used when absent: distinguishes "anchor written wrong" (all df=0) from
    "genuinely did not happen". See DESIGN.md P4.
    """
    pool = tree.actions if pool is None else pool
    texts = [searchable_text(a) for a in pool]
    return {t: sum(1 for x in texts if t.lower() in x) for t in anchors}


def retrieve(tree: RunTree, anchors: list[str], scope: Optional[dict] = None,
             k: int = 8, root_only: bool = False,
             score_mode: str = "count") -> dict[str, Any]:
    """Score actions by anchor hit count and return top-k.

    Granularity = a single action (post fan-out), aligned with the evidence
    reference unit.
    score_mode:
      count — how many distinct anchors hit (default; length bias is limited)
      idf   — weight by anchor rarity, suppresses generic terms like ".json"
              (DESIGN.md P12)
    """
    anchors = [a for a in anchors if a and a.strip()]
    if not anchors:
        return {"anchors": [], "df": {}, "rows": [], "total_scored": 0}
    pool = _scope(tree.root_actions if root_only else tree.actions, scope)
    texts = {a["ref"]: searchable_text(a) for a in pool}
    df = {t: sum(1 for x in texts.values() if t.lower() in x) for t in anchors}
    n = max(len(pool), 1)
    weight = {t: (1.0 if score_mode != "idf"
                  else math.log(1 + n / (1 + df[t]))) for t in anchors}

    scored = []
    for a in pool:
        txt = texts[a["ref"]]
        hit = [t for t in anchors if t.lower() in txt]
        if not hit:
            continue
        s = sum(weight[t] for t in hit)
        scored.append((s, len(hit), a, hit))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["idx"]))
    rows = []
    for s, nhit, a, hit in scored[:k]:
        row = summarize(a)
        row.update({"score": round(s, 3), "anchors_hit": hit})
        rows.append(row)
    return {"anchors": anchors, "df": df, "rows": rows,
            "total_scored": len(scored), "has_more": len(scored) > k,
            "score_mode": score_mode}


# ---------------------------------------------------------------------------
# 4. get / window — fetch details
# ---------------------------------------------------------------------------
def get(tree: RunTree, ref: str, fields: Optional[list[str]] = None,
        cap: int = 4000) -> dict[str, Any]:
    a = tree.by_ref(ref)
    if a is None:
        return {"error": f"ref not found: {ref}"}
    fields = fields or ["action", "tool", "command", "path", "pattern",
                        "reasoning", "completed", "error", "turn"]
    out = {"ref": ref, "sid": a["sid"][:8], "idx": a["idx"]}
    for f in fields:
        v = a.get(f)
        if f == "args":
            v = (a.get("args") or {}).get("__tool_use_purpose")
        out[f] = str(v)[:cap] if isinstance(v, str) else v
    out["truncated"] = any(isinstance(a.get(f), str) and len(a[f]) > cap for f in fields)
    return out


def window(tree: RunTree, ref: str, before: int = 3, after: int = 3) -> dict[str, Any]:
    a = tree.by_ref(ref)
    if a is None:
        return {"error": f"ref not found: {ref}"}
    same = [x for x in tree.actions if x["sid"] == a["sid"]]
    same.sort(key=lambda x: x["idx"])
    pos = next((i for i, x in enumerate(same) if x["ref"] == ref), None)
    lo, hi = max(0, pos - before), min(len(same), pos + after + 1)
    return {"center": ref, "rows": [summarize(x) for x in same[lo:hi]]}


# ---------------------------------------------------------------------------
# 5. hard_check — deterministic verdict (structured fields + filesystem only, no command text)
# ---------------------------------------------------------------------------
def hard_check(tree: RunTree, hc: dict, scope: Optional[dict] = None
               ) -> dict[str, Any]:
    """Three kinds of hard checks. A hit means the requirement is definitely satisfied — no LLM needed.

      read_path     read a file       -> path contains
      invoke_agent  called an agent   -> spawn's pattern equals / child session's agent_name
      artifact_glob produced a file   -> deferred to crosscheck (see s7)
    """
    out: dict[str, Any] = {"kinds": [], "hits": [], "tier": "none"}
    if not hc:
        return out
    pool = _scope(tree.actions, scope)

    if hc.get("read_path"):
        val = hc["read_path"]
        hits = [a for a in pool if a.get("action") in READ_ACTIONS
                and val in (a.get("path") or "")]
        out["kinds"].append("read_path")
        if hits:
            out["hits"] += [summarize(a) for a in hits[:5]]
            out["tier"] = "direct"

    if hc.get("invoke_agent"):
        name = hc["invoke_agent"]
        hits = [a for a in pool
                if a.get("action") == "spawn_subagent" and a.get("pattern") == name]
        out["kinds"].append("invoke_agent")
        if hits:
            out["hits"] += [summarize(a) for a in hits[:5]]
            out["tier"] = "direct"
        else:
            # Fallback 1: the child session's agent_name (in practice child sessions'
            # agent_name is often None, so we cannot rely on this alone).
            kid = [n for n in tree.nodes.values()
                   if n.sid != tree.root and (n.agent_name or "") == name]
            if kid:
                out["hits"] += [{"ref": f"{n.sid[:8]}#session", "sid": n.sid[:8],
                                 "action": "child_session", "target": n.agent_name}
                                for n in kid[:5]]
                out["tier"] = "cross_session"
            # Fallback 2: named-flag form. Not "guessing regexes" — this is a small,
            # well-defined set of invocation conventions (--agent / --target /
            # --developer_agent), reliability close to structured fields. In practice
            # every real live invocation on aaaa1111 looked like:
            #   python3 example_target_runner.py --target example-dev-agent ...
            flag = re.compile(
                r"(?:--agent|--target|--developer[_-]agent)[=\s]+['\"]?" + re.escape(name),
                re.I)
            byflag = [a for a in pool
                      if a.get("action") == "run_command" and flag.search(a.get("command") or "")]
            if byflag:
                out["hits"] += [summarize(a) for a in byflag[:8]]
                in_root = any(a["sid"] == tree.root for a in byflag)
                out["tier"] = "derived" if in_root else "cross_session"
                out["invoke_by_flag"] = True
    return out


# ---------------------------------------------------------------------------
# 6. crosscheck — filesystem crosscheck, two strengths
# ---------------------------------------------------------------------------
def crosscheck(pattern: str, roots: list[str],
               window_: tuple[Optional[dt.datetime], Optional[dt.datetime]],
               limit: int = 20) -> dict[str, Any]:
    """Tier 1: file (attributable). mtime inside the run's time window -> strong, else stale."""
    lo, hi = window_
    found = []
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for p in _glob.glob(os.path.join(r, "**", pattern), recursive=True):
            if not os.path.isfile(p):
                continue
            m = dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc)
            strong = bool(lo and hi and lo <= m <= hi)
            found.append({"path": p, "mtime": m.isoformat(),
                          "size": os.path.getsize(p),
                          "strength": "strong" if strong else "stale"})
    found.sort(key=lambda x: x["path"])
    n_strong = sum(1 for f in found if f["strength"] == "strong")
    return {"glob": pattern, "searched": roots,
            "found": found[:limit], "total": len(found),
            "strong": n_strong,
            "tier": "artifact" if n_strong else ("derived" if found else "none")}


def crosscheck_shared_state(kind: str, value: Any) -> dict[str, Any]:
    """Tier 2: shared state (port / process / service). Always info, never used as evidence.

    "Port 8765 is listening" cannot prove this run started it — anyone could have.
    """
    return {"kind": kind, "value": value, "strength": "info",
            "tier": "none", "note": "shared state cannot be attributed to this run, not used as verdict input"}


# ---------------------------------------------------------------------------
# Internal: scope slicing
# ---------------------------------------------------------------------------
def _scope(actions: list[dict], scope: Optional[dict]) -> list[dict]:
    """Filter by scope.

    Two semantics:
      1. idx/turn bounds apply **only to the root session**. Child sessions restart
         idx from 0 on a different base (in practice multiple child sessions all
         have idx=7), so applying the parent's bounds is meaningless; also child
         sessions are born from dispatch and are inherently later than the dispatch
         point, so we always let them through.
      2. `scope` is used to "prefer evidence after the request was made", not as a
         hard filter — in practice R3.1's evidence came before the request (the
         agent did it early), so a hard lower bound would cause false MISS. Callers
         should double-query, and out-of-scope hits are tagged
         evidence_before_request. See DESIGN.md P3.
    """
    if not scope:
        return actions
    lo, hi = scope.get("idx_gte"), scope.get("idx_lte")
    tlo, thi = scope.get("turn_gte"), scope.get("turn_lte")
    only_sid = scope.get("sid")
    root_sid = scope.get("_root_sid")

    out = []
    for a in actions:
        if only_sid and a["sid"] != only_sid:
            continue
        is_root = (root_sid is None) or (a["sid"] == root_sid)
        if is_root:
            if lo is not None and a["idx"] < lo:
                continue
            if hi is not None and a["idx"] > hi:
                continue
            if tlo is not None and (a.get("turn") or 0) < tlo:
                continue
            if thi is not None and (a.get("turn") or 0) > thi:
                continue
        out.append(a)
    return out
