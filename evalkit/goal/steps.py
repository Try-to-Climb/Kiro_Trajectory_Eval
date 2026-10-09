"""The nine steps of goal_completion.

Convention: each step is `def sN_xxx(ctx, **kw) -> dict`; it reads/writes intermediate
products on ctx. LLMs appear only in s2 / s3 / s4 / s8, and every output passes a
validation gate (see llm.ask's `validate`).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, Callable, Optional

from . import prompts
from . import schema
from evidence import api
from evidence.loader import RunTree
from llm import ask

# ---------------------------------------------------------------------------
# s1 map — snapshot: boundaries + capability probe (pure code)
# ---------------------------------------------------------------------------
def s1_map(tree: RunTree) -> dict[str, Any]:
    return api.overview(tree)


# ---------------------------------------------------------------------------
# s2 extract requirements — LLM + validation
# ---------------------------------------------------------------------------
def _v_requirements(max_turn: int) -> Callable[[Any], list[str]]:
    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict):
            return ["top level must be a JSON object"]
        ti = data.get("turn_intents")
        reqs = data.get("requirements")
        if not isinstance(ti, list) or not ti:
            errs.append("turn_intents missing or empty")
        if not isinstance(reqs, list):
            return errs + ["requirements must be an array"]
        intents: dict[int, str] = {}
        for row in ti or []:
            if not isinstance(row, dict):
                errs.append("turn_intents element must be an object"); continue
            t, it = row.get("turn"), row.get("intent")
            if not isinstance(t, int) or not (1 <= t <= max_turn):
                errs.append(f"turn_intents turn={t!r} out of range (should be 1..{max_turn})")
            if it not in schema.INTENTS:
                errs.append(f"intent={it!r} invalid, choose from {schema.INTENTS}")
            if isinstance(t, int):
                intents[t] = it
        seen = set()
        for r in reqs:
            if not isinstance(r, dict):
                errs.append("requirements element must be an object"); continue
            rid = r.get("id")
            if not rid or rid in seen:
                errs.append(f"requirement id missing or duplicated: {rid!r}")
            seen.add(rid)
            t = r.get("origin_turn")
            if not isinstance(t, int) or not (1 <= t <= max_turn):
                errs.append(f"{rid}: origin_turn={t!r} out of range (should be 1..{max_turn})")
            elif intents.get(t) not in (None, "request"):
                errs.append(f"{rid}: turn {t} intent is {intents[t]}, non-request turns must not yield requirements")
            for field, allowed in (("verifiable_by", schema.VERIFIABLE_BY),
                                   ("status", schema.STATUSES),
                                   ("strength", schema.STRENGTHS)):
                if r.get(field) not in allowed:
                    errs.append(f"{rid}: {field}={r.get(field)!r} invalid, choose from {allowed}")
            if not str(r.get("expect") or "").strip():
                errs.append(f"{rid}: expect must not be empty")
        return errs
    return v


def s2_extract_requirements(tree: RunTree, overview: dict, *,
                            caller=None, agent_context: str = "") -> dict[str, Any]:
    ir = tree.root_node.ir
    block = "\n".join(f"[Turn {i}] {p}" for i, p in enumerate(ir.prompts, 1))
    ctx_block = ("====== Agent-under-test context ======\n" + agent_context
                 if agent_context else "")
    data = ask(prompts.EXTRACT_REQUIREMENTS.format(
                   turns=ir.turns, block=block, agent_context=ctx_block),
               _v_requirements(ir.turns), caller=caller, label="s2")
    reqs = [schema.Requirement(
        id=r["id"], text=r.get("text", ""), origin_turn=r["origin_turn"],
        verifiable_by=r["verifiable_by"], status=r["status"],
        strength=r["strength"], expect=r["expect"]) for r in data["requirements"]]
    return {"turn_intents": data["turn_intents"], "requirements": reqs}


# ---------------------------------------------------------------------------
# s2b synthetic negatives — built-in control group (pure code)
# ---------------------------------------------------------------------------
# Both sessions judged 5/5 satisfied, meaning the verdict has not yet been tested on
# "asked for but genuinely not done". Real negative samples are slow to find, but we
# can synthesize: inject a fictional requirement guaranteed absent from the trajectory,
# and a correct system MUST judge it false. Only inject when every anchor has df=0
# across the whole tree (otherwise it is not a valid control).
CONTROL_TEMPLATES = [
    {"text": "Push the evaluation result to Slack channel #eval-alerts",
     "expect": "Trajectory contains an action sending a message to a Slack webhook",
     "anchors": ["slack", "hooks.slack.com", "#eval-alerts"],
     "verifiable_by": "action"},
    {"text": "Package all artifacts and upload to S3 bucket s3://eval-archive/",
     "expect": "Trajectory contains actions packaging and uploading to that S3 bucket",
     "anchors": ["s3://eval-archive", "aws s3 cp", "s3api"],
     "verifiable_by": "action"},
    {"text": "Create a PostgreSQL table for the evaluation result and write to it",
     "expect": "Trajectory contains actions connecting to PostgreSQL and creating/writing a table",
     "anchors": ["psql", "postgres", "CREATE TABLE"],
     "verifiable_by": "action"},
]


def s2b_inject_controls(tree: RunTree, reqs: list[schema.Requirement],
                        n: int = 1) -> tuple[list[schema.Requirement], list[dict]]:
    picked: list[schema.Requirement] = []
    meta: list[dict] = []
    for i, tpl in enumerate(CONTROL_TEMPLATES):
        if len(picked) >= n:
            break
        df = api.anchor_df(tree, tpl["anchors"])
        if any(v > 0 for v in df.values()):
            continue                          # Trajectory contains it; not a valid control
        rid = f"CTRL{i+1}"
        picked.append(schema.Requirement(
            id=rid, text=tpl["text"], origin_turn=1,
            verifiable_by=tpl["verifiable_by"], status="active",
            strength="strong", expect=tpl["expect"], synthetic=True))
        meta.append({"req_id": rid, "anchors": tpl["anchors"], "df": df})
    return reqs + picked, meta


# ---------------------------------------------------------------------------
# s3 extract claims — LLM + validation
# Structural issues send the whole batch back; a single quote failing to match the
# original only drops that one row (claims are mutually independent — no collective
# punishment).
# ---------------------------------------------------------------------------
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _norm(s: str) -> str:
    """Normalization used for comparing quote against source: keep only word
    characters (including CJK), drop all punctuation and whitespace.

    LLMs often introduce minor punctuation drift when quoting — in practice we saw
    `**sr-format FAIL**:` quoted as `sr-format FAIL):`. Punctuation drift should
    not be judged "fabricated quote", but content drift (e.g., "wrote the script"
    quoted as "deployed to production") is still caught.
    """
    return _NON_WORD.sub("", s or "")


def _v_claims(max_turn: int) -> Callable[[Any], list[str]]:
    """Only checks structure. Whether a quote is verbatim is filtered per-row in _filter_claims."""
    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
            return ["top level must be {\"claims\": [...]}"]
        for c in data["claims"]:
            if not isinstance(c, dict):
                errs.append("claims element must be an object"); continue
            cid = c.get("id")
            if c.get("kind") not in schema.CLAIM_KINDS:
                errs.append(f"{cid}: kind={c.get('kind')!r} invalid, choose from {schema.CLAIM_KINDS}")
            t = c.get("turn")
            if not isinstance(t, int) or not (1 <= t <= max_turn):
                errs.append(f"{cid}: turn={t!r} out of range (should be 1..{max_turn})")
            if len(_norm(str(c.get("quote") or ""))) < 4:
                errs.append(f"{cid}: quote too short or missing")
        return errs
    return v


def _filter_claims(rows: list[dict], corpus: str) -> tuple[list[schema.Claim], list[dict]]:
    """Row-by-row: check whether the quote appears in the source; if not, drop that row and record it
    (do not send the whole batch back)."""
    flat = _norm(corpus)
    kept: list[schema.Claim] = []
    dropped: list[dict] = []
    for c in rows:
        if _norm(str(c.get("quote") or "")) in flat:
            kept.append(schema.Claim(id=c["id"], turn=c["turn"], kind=c["kind"],
                                     text=c.get("text", ""), quote=c["quote"]))
        else:
            dropped.append({"id": c.get("id"), "quote": str(c.get("quote"))[:80],
                            "why": "quote is not a verbatim fragment (suspected rewrite/stitch)"})
    return kept, dropped


def s3_extract_claims(tree: RunTree, *, caller=None,
                      cap: int = 26000, agent_context: str = "") -> dict[str, Any]:
    resp = tree.root_node.ir.responses or []
    if not resp:
        return {"claims": [], "dropped": [], "skipped": "this session has no reply text"}
    block = "\n\n".join(f"[Turn {i} reply]\n{r}" for i, r in enumerate(resp, 1))[:cap]
    ctx_block = ("====== Agent-under-test context ======\n" + agent_context
                 if agent_context else "")
    data = ask(prompts.EXTRACT_CLAIMS.format(
                   n=len(resp), block=block, agent_context=ctx_block),
               _v_claims(len(resp)), caller=caller, label="s3")
    claims, dropped = _filter_claims(data["claims"], block)
    return {"claims": claims, "dropped": dropped}


# ---------------------------------------------------------------------------
# s4 compile — LLM + validation; scope is filled in by code, not left to the LLM
# ---------------------------------------------------------------------------
_HC_KEYS = ("read_path", "artifact_glob", "invoke_agent")
_BRACE = re.compile(r"\{.*?\}")


def _v_compiled(req_ids: set[str]) -> Callable[[Any], list[str]]:
    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict) or not isinstance(data.get("compiled"), list):
            return ["top level must be {\"compiled\": [...]}"]
        got = set()
        for c in data["compiled"]:
            if not isinstance(c, dict):
                errs.append("compiled element must be an object"); continue
            rid = c.get("req_id")
            if rid not in req_ids:
                errs.append(f"req_id={rid!r} not in the requirement list"); continue
            got.add(rid)
            hc = c.get("hard_check") or {}
            if not isinstance(hc, dict):
                errs.append(f"{rid}: hard_check must be an object")
            else:
                for k, val in hc.items():
                    if k not in _HC_KEYS:
                        errs.append(f"{rid}: hard_check has unknown key {k!r}, only {_HC_KEYS} allowed")
                    elif not isinstance(val, (str, list)) or not val:
                        errs.append(f"{rid}: hard_check.{k} must be a non-empty string or array")
                    elif k == "artifact_glob":
                        for g in ([val] if isinstance(val, str) else val):
                            if _BRACE.search(str(g)):
                                errs.append(f"{rid}: artifact_glob {g!r} uses braces, "
                                            "Python glob does not support them; split into an array")
            anchors = c.get("anchors")
            if not isinstance(anchors, list) or not anchors:
                errs.append(f"{rid}: anchors must be a non-empty array")
            else:
                for a in anchors:
                    if not isinstance(a, str) or not a.strip():
                        errs.append(f"{rid}: anchors contains an empty entry")
                    elif len(a) > 60:
                        errs.append(f"{rid}: anchor too long (should be short words): {a[:30]!r}")
        missing = req_ids - got
        if missing:
            errs.append(f"missing these requirements: {sorted(missing)}")
        return errs
    return v


def s4_compile(reqs: list[schema.Requirement], overview: dict, *,
               caller=None) -> dict[str, Any]:
    todo = [r for r in reqs if not r.synthetic]
    block = "\n".join(
        f'{r.id}  origin_turn={r.origin_turn}  verifiable_by={r.verifiable_by}  '
        f'strength={r.strength}\n    text: {r.text}\n    expect: {r.expect}'
        for r in todo)
    data = ask(prompts.COMPILE.format(block=block),
               _v_compiled({r.id for r in todo}), caller=caller, label="s4")

    idx_range = overview.get("idx_range_per_turn") or {}
    out: dict[str, schema.Criterion] = {}
    for c in data["compiled"]:
        rid = c["req_id"]
        r = next(x for x in todo if x.id == rid)
        lo = (idx_range.get(r.origin_turn) or idx_range.get(str(r.origin_turn)) or [None])[0]
        out[rid] = schema.Criterion(
            req_id=rid, hard_check=c.get("hard_check") or {},
            anchors=[a.strip() for a in c["anchors"] if a and a.strip()],
            residual=c.get("residual"), residual_needs=c.get("residual_needs") or [],
            scope=({"idx_gte": lo, "_root_sid": None} if lo is not None else {}))
    # Criteria for synthetic controls are set directly by code (bypassing the LLM) to guarantee
    # they are "genuinely nonexistent"
    for r in reqs:
        if r.synthetic:
            tpl = next(t for t in CONTROL_TEMPLATES if t["text"] == r.text)
            out[r.id] = schema.Criterion(req_id=r.id, hard_check={},
                                         anchors=list(tpl["anchors"]))
    return {"criteria": out}


# ---------------------------------------------------------------------------
# s5 search — hard_check + retrieve, tri-state (pure code)
# ---------------------------------------------------------------------------
def s5_search(tree: RunTree, reqs: list[schema.Requirement],
              criteria: dict[str, schema.Criterion], overview: dict,
              k: int = 8, score_mode: str = "idf") -> dict[str, schema.SearchResult]:
    out: dict[str, schema.SearchResult] = {}
    for r in reqs:
        cr = criteria.get(r.id)
        if cr is None:
            out[r.id] = schema.SearchResult(req_id=r.id, status="absent",
                                            absent_reason="no_criterion")
            continue
        scope = dict(cr.scope or {})
        if scope:
            scope["_root_sid"] = tree.root

        # ---- Hard check (read_path / invoke_agent; artifact_glob is handled by s7) ----
        # Same as retrieval, do a double query: no hit inside scope does not mean it was never done —
        # it might have been done before the request was raised (in practice R3.1's only read of
        # agent.py is in turn 1 while the requirement appears in turn 3).
        hc = {k2: v for k2, v in cr.hard_check.items() if k2 != "artifact_glob"}
        hard = {"hits": [], "tier": "none"}
        before_refs: list[str] = []
        if hc:
            hin = api.hard_check(tree, hc, scope=scope)
            if hin["hits"]:
                hard = hin
            else:
                hall = api.hard_check(tree, hc)
                if hall["hits"]:
                    for h in hall["hits"]:
                        h["before_request"] = True
                    hard = hall
                    before_refs += [h["ref"] for h in hall["hits"]]

        # ---- Retrieve: scope-restricted + whole-tree control (double query, to prevent scope-induced
        #      false MISS) ----
        rin = api.retrieve(tree, cr.anchors, scope=scope, k=k, score_mode=score_mode)
        rall = api.retrieve(tree, cr.anchors, k=k, score_mode=score_mode)
        in_refs = {x["ref"] for x in rin["rows"]}
        before_refs += [row["ref"] for row in rall["rows"] if row["ref"] not in in_refs]

        if hard["hits"]:
            status, tier = "hard", hard["tier"]
        elif rin["rows"] or rall["rows"]:
            status, tier = "candidates", "retrieved"
        else:
            status, tier = "absent", "none"

        df = rall["df"] or api.anchor_df(tree, cr.anchors)
        # Out-of-scope hits must also be visible to s8 (and citeable), otherwise they will be
        # misjudged as "not found"
        cands = list(rin["rows"])
        cands += [row for row in rall["rows"] if row["ref"] not in in_refs]
        out[r.id] = schema.SearchResult(
            req_id=r.id, status=status, tier=tier,
            hard_hits=hard["hits"],
            candidates=cands[:k * 2],
            anchor_df=df,
            absent_reason=(None if status != "absent"
                           else ("suspect_anchors" if all(v == 0 for v in df.values())
                                 else "no_scoring_action")),
            evidence_before_request=sorted(set(before_refs)))
    return out


# ---------------------------------------------------------------------------
# s6 probe — top-up recall for absent / weakly-hit requirements only (pure code)
# ---------------------------------------------------------------------------
def s6_probe(tree: RunTree, reqs: list[schema.Requirement],
             search: dict[str, schema.SearchResult], overview: dict,
             limit: int = 12) -> dict[str, Any]:
    """For absent / weakly-hit requirements, add context so s8 can tell "anchor written wrong"
    from "genuinely not done".

    Two things:
      1. Global vocabulary sample (vocab): filenames that were written + command heads + action
         histogram. Both "anchor wrong" and "did not happen" present as df=0 across the board;
         df alone cannot distinguish them. vocab supplies the overall look-and-feel of the
         trajectory as a reference frame.
      2. Per weak requirement, a "nearby_actions" slice: the first `limit` root-session actions
         starting from origin_turn's beginning. Gives s8 a temporally-adjacent, citeable context
         window (with refs) so it can populate evidence_actions.
    """
    vocab_needed = any(s.status == "absent" for s in search.values())
    vocab: dict[str, Any] = {}
    if vocab_needed:
        heads = Counter()
        for a in tree.actions:
            cmd = (a.get("command") or "").strip()
            if cmd:
                first = re.split(r"[\s|;&]+", cmd.lstrip("( "))[0]
                heads[os.path.basename(first)[:24]] += 1
        vocab = {
            "action_histogram": overview.get("by_action"),
            "command_heads": dict(heads.most_common(20)),
            "written_files": sorted({os.path.basename(w["path"])
                                     for w in overview.get("written", [])})[:30],
        }

    out: dict[str, Any] = {"vocab": vocab, "per_req": {}}
    idx_range = overview.get("idx_range_per_turn") or {}
    for r in reqs:
        s = search.get(r.id)
        if s is None or s.status == "hard":
            continue
        if s.status == "candidates" and len(s.candidates) >= 3:
            continue                       # Enough candidates; no need to top up
        # Slice the first `limit` root-session actions starting from origin_turn's low idx —
        # gives s8 context that is temporally near the requirement. When anchors miss, the agent
        # likely did something else here, or used an implementation shape s4 did not capture.
        # Child sessions restart idx from 0 on a different base, so we do not mix them in.
        turn_range = (idx_range.get(r.origin_turn)
                      or idx_range.get(str(r.origin_turn)) or [None])
        lo = turn_range[0]
        if lo is None:
            continue
        rows = [api.summarize(a) for a in tree.root_actions
                if a.get("idx", -1) >= lo][:limit]
        out["per_req"][r.id] = {"nearby_actions": rows}
    return out


# ---------------------------------------------------------------------------
# s7 crosscheck — filesystem cross-check (pure code)
# ---------------------------------------------------------------------------
def s7_crosscheck(reqs: list[schema.Requirement],
                  criteria: dict[str, schema.Criterion],
                  overview: dict, tree: RunTree) -> dict[str, Any]:
    roots = overview.get("written_dirs") or []
    win = tree.time_window
    out: dict[str, Any] = {}
    for r in reqs:
        cr = criteria.get(r.id)
        if not cr:
            continue
        globs = cr.hard_check.get("artifact_glob")
        if not globs:
            continue
        for g in ([globs] if isinstance(globs, str) else globs):
            res = api.crosscheck(str(g), roots, win)
            prev = out.get(r.id)
            if prev is None or res["strong"] > prev["strong"]:
                out[r.id] = res
    return out


# ---------------------------------------------------------------------------
# s8 judge — assemble evidence pack + LLM verdict + ref subset validation
# ---------------------------------------------------------------------------
def build_pack(reqs: list[schema.Requirement], criteria: dict[str, schema.Criterion],
               search: dict[str, schema.SearchResult], probe: dict[str, Any],
               cross: dict[str, Any], claims: list[schema.Claim],
               overview: dict) -> tuple[str, set[str]]:
    """Assemble all evidence into a text block, and return the set of refs it contains
    (used to validate s8's output)."""
    refs: set[str] = set()
    L: list[str] = []
    L.append(f"Run being evaluated: {overview['root'][:8]}  agent={overview.get('agent_name')}  "
             f"{overview['turns']} turns  root session {overview['actions_root']} actions / "
             f"whole tree {overview['actions_tree']} actions / child sessions {len(overview.get('child_sessions') or [])}")
    L.append(f"Capability probe: has_response={overview.get('has_response')} "
             f"has_timestamps={overview.get('has_timestamps')} "
             f"(when has_response=False, outcome-type requirements cannot be verified via command output)")
    if probe.get("vocab"):
        v = probe["vocab"]
        L.append("\n[Trajectory vocabulary sample] (use this to tell whether absent means anchors were wrong or it truly did not happen)")
        L.append(f"  action distribution: {v.get('action_histogram')}")
        L.append(f"  command heads: {v.get('command_heads')}")
        L.append(f"  files written: {v.get('written_files')}")

    for r in reqs:
        s = search.get(r.id)
        cr = criteria.get(r.id)
        L.append(f"\n--- {r.id} {r.text}")
        L.append(f"  (raised in turn {r.origin_turn}, verifiable_by={r.verifiable_by}, "
                 f"status={r.status}, strength={r.strength}"
                 + (", synthetic control" if r.synthetic else "") + ")")
        L.append(f"  expect: {r.expect}")
        if cr and cr.residual:
            L.append(f"  residual: {cr.residual}  (needs={cr.residual_needs})")
        if cr:
            L.append(f"  hard_check: {cr.hard_check or 'none'}   anchors: {cr.anchors}")
        if s is None:
            L.append("  (no search result)"); continue
        L.append(f"  search status: {s.status}" + (f" / {s.absent_reason}" if s.absent_reason else ""))
        L.append(f"  anchor hit counts (df): {s.anchor_df}")
        for h in s.hard_hits:
            refs.add(h["ref"])
            mark = " <-before request" if h.get("before_request") else ""
            L.append(f"    [hard-check hit]{mark} {h['ref']} {h.get('action')} "
                     f"{str(h.get('target'))[:150]}")
        for c in s.candidates:
            refs.add(c["ref"])
            line = (f"    [candidate score={c.get('score')}] {c['ref']} {c.get('action')} "
                    f"{str(c.get('target'))[:150]}")
            if c.get("purpose"):
                line += f"\n        purpose: {c['purpose'][:120]}"
            L.append(line)
        if s.evidence_before_request:
            # These refs must be in refs, otherwise s8 sees them but cannot cite them
            # (validation would reject as hallucination)
            refs.update(s.evidence_before_request)
            L.append(f"    [hits from before the request, citeable] {s.evidence_before_request}")
        cc = cross.get(r.id)
        if cc:
            L.append(f"  filesystem: glob={cc['glob']} matched {cc['total']} files, "
                     f"strong={cc['strong']}, tier={cc['tier']}")
            for f in cc["found"][:6]:
                L.append(f"    [{f['strength']}] {f['mtime'][11:19]} {f['path']}")
        pr = (probe.get("per_req") or {}).get(r.id)
        if pr:
            for row in pr.get("nearby_actions", [])[:8]:
                refs.add(row["ref"])
                L.append(f"    [nearby action] {row['ref']} {row.get('action')} {str(row.get('target'))[:110]}")

    if claims:
        L.append("\n--- agent self-claims (for reconciliation: claimed but unsupported by evidence -> overclaim)")
        for c in claims[:40]:
            L.append(f"  {c.id} [{c.kind}] {c.text[:110]}")
    return "\n".join(L), refs


def _v_judgments(req_ids: set[str], allowed_refs: set[str]) -> Callable[[Any], list[str]]:
    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict) or not isinstance(data.get("judgments"), list):
            return ["top level must be {\"judgments\": [...]}"]
        got = set()
        for j in data["judgments"]:
            if not isinstance(j, dict):
                errs.append("judgments element must be an object"); continue
            rid = j.get("req_id")
            if rid not in req_ids:
                errs.append(f"req_id={rid!r} not in the requirement list"); continue
            got.add(rid)
            if str(j.get("satisfied")) not in schema.SATISFIED:
                errs.append(f"{rid}: satisfied={j.get('satisfied')!r} invalid, "
                            f"choose from {schema.SATISFIED}")
            if j.get("tier") not in schema.EVIDENCE_TIERS:
                errs.append(f"{rid}: tier={j.get('tier')!r} invalid, "
                            f"choose from {tuple(schema.EVIDENCE_TIERS)}")
            ev = j.get("evidence_actions") or []
            if not isinstance(ev, list):
                errs.append(f"{rid}: evidence_actions must be an array")
            else:
                bad = [r for r in ev if r not in allowed_refs]
                if bad:
                    errs.append(f"{rid}: evidence_actions cites refs not in the evidence pack "
                                f"{bad[:4]} (suspected hallucination)")
            if not str(j.get("reason") or "").strip():
                errs.append(f"{rid}: reason must not be empty")
        missing = req_ids - got
        if missing:
            errs.append(f"missed judgment for these requirements: {sorted(missing)}")
        return errs
    return v


def s8_judge(reqs: list[schema.Requirement], criteria, search, probe, cross,
             claims, overview, *, caller=None) -> dict[str, Any]:
    pack, refs = build_pack(reqs, criteria, search, probe, cross, claims, overview)
    data = ask(prompts.JUDGE.format(pack=pack),
               _v_judgments({r.id for r in reqs}, refs), caller=caller, label="s8")
    findings: list[schema.Finding] = []
    for j in data["judgments"]:
        r = next(x for x in reqs if x.id == j["req_id"])
        cr = criteria.get(r.id)
        tier = j["tier"]
        base = 1.0 if j["satisfied"] == "true" else 0.85
        findings.append(schema.Finding(
            req_id=r.id, satisfied=str(j["satisfied"]), tier=tier,
            confidence=schema.cap_confidence(tier, base,
                                             bool(cr and cr.residual)),
            evidence_actions=[x for x in (j.get("evidence_actions") or []) if x in refs],
            evidence_files=[{"path": p} for p in (j.get("evidence_files") or [])],
            overclaim=bool(j.get("overclaim")),
            reason=str(j.get("reason") or "")[:1200],
            residual=cr.residual if cr else None,
            synthetic=r.synthetic))
    return {"findings": findings, "pack": pack, "pack_refs": sorted(refs)}


# ---------------------------------------------------------------------------
# s9 aggregate — summarize (pure code)
# ---------------------------------------------------------------------------
def s9_aggregate(findings: list[schema.Finding],
                 reqs: list[schema.Requirement]) -> dict[str, Any]:
    real = [f for f in findings if not f.synthetic]
    ctrl = [f for f in findings if f.synthetic]
    strong = [f for f in real
              if next(r for r in reqs if r.id == f.req_id).strength == "strong"
              and next(r for r in reqs if r.id == f.req_id).status == "active"]
    sat = [f for f in strong if f.satisfied == "true"]
    unver = [f for f in strong if f.satisfied == "unverifiable"]
    unmet = [f for f in strong if f.satisfied == "false"]

    # Built-in control group: synthetic requirements must be judged false, otherwise the verdict
    # is too lax
    ctrl_pass = all(f.satisfied == "false" for f in ctrl) if ctrl else None

    verdict = "PASS"
    if unmet:
        verdict = "FAIL"
    elif any(f.overclaim for f in real):
        verdict = "FAIL"
    elif unver:
        verdict = "WEAK_PASS"
    if ctrl_pass is False:
        verdict = "INVALID"          # Control group did not pass; the verdict is unreliable

    denom = len(strong) or 1
    return {
        "verdict": verdict,
        "score": round(len(sat) / denom, 3),
        "counts": {"strong_active": len(strong), "satisfied": len(sat),
                   "unverifiable": len(unver), "unmet": len(unmet),
                   "overclaim": sum(1 for f in real if f.overclaim),
                   "weak_or_superseded": len(real) - len(strong)},
        "control": {"injected": len(ctrl), "passed": ctrl_pass,
                    "detail": [{"req_id": f.req_id, "satisfied": f.satisfied}
                               for f in ctrl]},
        "unmet_ids": [f.req_id for f in unmet],
        "overclaim_ids": [f.req_id for f in real if f.overclaim],
    }
