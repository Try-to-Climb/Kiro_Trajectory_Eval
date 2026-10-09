"""efficiency workflow steps 1-4.

s1_map              pure code; snapshot turn_metadata + totals from raw .json
s2_segment          LLM; conservative segmentation, falls back to 1 segment on failure
s3_cost_stats       pure code; per-segment aggregation
s4_detect_duplicates pure code; embed-based multi-dim voting (no more regex normalizer)

Dependencies:
- evalkit.normalize (TraceIR, load_trace_from_official)
- evidence.loader (RunTree, load_run_tree)
- efficiency.embed_cache / similarity
- llm.ask (used only in s2)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import Counter
from typing import Any, Callable, Optional


from evidence.loader import RunTree
from evidence import api as evapi
from llm import ask as llm_ask
from kiro_acp import KiroAcpClient, acp_caller_from

from . import embed_cache, prompts, similarity
from .schema import (
    DuplicateCluster,
    DuplicatePair,
    Segment,
    SegmentStats,
)


# ============================================================================
# s1 · map
# ============================================================================
def _to_seconds(td: Any) -> float:
    if isinstance(td, dict):
        return td.get("secs", 0) + td.get("nanos", 0) / 1e9
    return td or 0


def _count_compactions(jsonl_path: str) -> int:
    """Scan the .jsonl and count Compaction events."""
    if not os.path.isfile(jsonl_path):
        return 0
    n = 0
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"kind":"Compaction"' in line:
                n += 1
    return n


def s1_map(sid: str, official_dir: str) -> dict[str, Any]:
    """Extract turn_metadata, global totals, and Compaction count from raw .json.

    Returns::

        {
          "sid":           str,
          "turns":         int,
          "wall_window":   [iso_start, iso_end],
          "turn_metadata": list[dict],   # one entry per turn
          "totals":        dict,         # global summary
        }
    """
    json_p = os.path.join(official_dir, f"{sid}.json")
    if not os.path.isfile(json_p):
        raise FileNotFoundError(f"session json not found: {json_p}")

    with open(json_p, encoding="utf-8") as f:
        d = json.load(f)

    utm = (d.get("session_state") or {}) \
        .get("conversation_metadata", {}) \
        .get("user_turn_metadatas", []) or []

    tm: list[dict] = []
    for i, u in enumerate(utm):
        metering = u.get("metering_usage") or []
        credits = (sum(m.get("value", 0) for m in metering)
                   if isinstance(metering, list) else 0)
        tm.append({
            "turn":       i + 1,
            "dur_s":      _to_seconds(u.get("turn_duration")),
            "credits":    credits,
            "cycles":     u.get("number_of_cycles") or 0,
            "llm_reqs":   u.get("total_request_count") or 0,
            "ctx_pct":    u.get("context_usage_percentage") or 0.0,
            "end_reason": u.get("end_reason"),
            "end_ts":     u.get("end_timestamp"),
            "prompt_len": u.get("user_prompt_length") or 0,
        })

    # wall-clock window
    lo = hi = None
    c_at, u_at = d.get("created_at"), d.get("updated_at")
    if c_at:
        try:
            lo = dt.datetime.fromisoformat(
                c_at.replace("Z", "+00:00").split(".")[0] + "+00:00")
        except ValueError:
            pass
    if u_at:
        try:
            hi = dt.datetime.fromisoformat(
                u_at.replace("Z", "+00:00").split(".")[0] + "+00:00")
        except ValueError:
            pass
    wall_s = (hi - lo).total_seconds() if (lo and hi) else None

    # global totals
    total_dur = sum(r["dur_s"] for r in tm)
    total_credits = sum(r["credits"] for r in tm)
    total_reqs = sum(r["llm_reqs"] for r in tm)
    total_cycles = sum(r["cycles"] for r in tm)
    n_compactions = _count_compactions(os.path.join(official_dir, f"{sid}.jsonl"))

    totals = {
        "wall_s":      wall_s,
        "net_s":       total_dur,
        "credits":     total_credits,
        "llm_reqs":    total_reqs,
        "cycles":      total_cycles,
        "compactions": n_compactions,
    }
    if wall_s and wall_s > 0:
        totals["idle_pct"] = round((1 - total_dur / wall_s) * 100, 1)

    return {
        "sid":           sid,
        "turns":         len(tm),
        "wall_window":   [lo.isoformat() if lo else None,
                          hi.isoformat() if hi else None],
        "turn_metadata": tm,
        "totals":        totals,
    }


# ============================================================================
# s2 · segment (LLM)
# ============================================================================
def _v_segments(n_turns: int) -> Callable[[Any], list[str]]:
    """Validation gate for LLM output: no gaps, no overlaps, full coverage."""
    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            return ['top level must be {"segments": [...]}']
        segs = data["segments"]
        if not segs:
            return ["segments cannot be empty (need at least 1)"]
        cur = 1
        for i, s in enumerate(segs):
            if not isinstance(s, dict):
                errs.append(f"segments[{i}] must be an object"); continue
            st, en = s.get("start"), s.get("end")
            if not isinstance(st, int) or not isinstance(en, int):
                errs.append(f"segments[{i}] start/end must be integers"); continue
            if st != cur:
                errs.append(f"segments[{i}] start={st} does not follow previous end "
                            f"(expected {cur}; segments must be contiguous)")
            if st > en:
                errs.append(f"segments[{i}] start > end")
            if en > n_turns:
                errs.append(f"segments[{i}] end={en} exceeds total turns {n_turns}")
            theme = s.get("theme")
            if not theme or not str(theme).strip():
                errs.append(f"segments[{i}] theme cannot be empty")
            elif len(str(theme)) > 60:
                errs.append(f"segments[{i}] theme exceeds 60 chars")
            cur = en + 1
        if cur - 1 != n_turns:
            errs.append(f"last segment end={cur-1} does not equal total turns {n_turns}")
        return errs
    return v


def _build_segment_block(prompts_: list[str], responses: list[str],
                         turn_metadata: list[dict],
                         max_chars: int = 40000) -> str:
    """Assemble the segmentation input block: user prompt + agent reply + metrics."""
    lines = []
    n = min(len(prompts_), len(turn_metadata))
    for i in range(n):
        tm = turn_metadata[i]
        p = (prompts_[i] or "")[:400]
        r = (responses[i] if i < len(responses) else "") or ""
        r = r[:200]
        header = (f"[turn {i+1}] duration={tm['dur_s']:.0f}s "
                  f"cycles={tm['cycles']} end={tm['end_reason']}")
        lines.append(header)
        lines.append(f"  user: {p}")
        if r:
            lines.append(f"  agent: {r}")
        lines.append("")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n\n...(truncated)..."
    return block


def s2_segment(overview: dict, tree: RunTree, *,
               caller: Optional[Callable[[str], str]] = None,
               use_llm: bool = True) -> list[Segment]:
    """Conservative segmentation via LLM, or fallback to a single segment.

    Args:
        overview: output of s1_map
        tree: run tree; used to fetch prompts / responses
        caller: LLM caller (for tests). None means use the default ACP backend.
        use_llm: if False, skip LLM entirely and return a single segment
            (offline mode).

    Returns:
        list[Segment], guaranteed to have no gaps or overlaps and to cover
        turns 1..N.
    """
    tm = overview["turn_metadata"]
    n_turns = len(tm)
    prompts_ = tree.root_node.ir.prompts
    responses = tree.root_node.ir.responses

    def _fallback_single_segment() -> list[Segment]:
        theme = (prompts_[0][:60] if prompts_ else "whole session")
        return [Segment(seg=1, start=1, end=n_turns, theme=theme)]

    if not use_llm or n_turns <= 1:
        return _fallback_single_segment()

    block = _build_segment_block(prompts_, responses, tm)
    prompt = prompts.SEGMENT_PROMPT.format(block=block, n_turns=n_turns)
    try:
        data = llm_ask(prompt, _v_segments(n_turns), caller=caller, label="s2")
    except Exception:
        # LLM unavailable, backend error, or validation kept failing -> fallback
        return _fallback_single_segment()

    segs = []
    for i, s in enumerate(data["segments"], start=1):
        segs.append(Segment(seg=i, start=s["start"], end=s["end"],
                            theme=str(s["theme"])[:60]))
    return segs


# ============================================================================
# s3 · cost_stats
# ============================================================================
def s3_cost_stats(overview: dict, segments: list[Segment]) -> list[SegmentStats]:
    """Aggregate turn_metadata by segment."""
    tm = overview["turn_metadata"]
    out: list[SegmentStats] = []
    for s in segments:
        rows = [r for r in tm if s.start <= r["turn"] <= s.end]
        n = len(rows)
        credits = sum(r["credits"] for r in rows)
        cycles = sum(r["cycles"] for r in rows)
        llm_reqs = sum(r["llm_reqs"] for r in rows)
        out.append(SegmentStats(
            seg=s.seg,
            range=f"{s.start}-{s.end}",
            n_turns=n,
            theme=s.theme,
            dur_s=sum(r["dur_s"] for r in rows),
            credits=credits,
            cycles=cycles,
            llm_reqs=llm_reqs,
            ctx_pct_max=max((r["ctx_pct"] for r in rows), default=0.0),
            credits_per_turn=credits / n if n else 0.0,
            cycles_per_turn=cycles / n if n else 0.0,
        ))
    return out


# ============================================================================
# s4 · detect_duplicates (embed-only, 3-dim voting)
# ============================================================================
# Dimensions used for judgment (others are skipped):
#   command   - command text
#   purpose   - intent (args.__tool_use_purpose)
#   reasoning - thinking (sibling filter required)
# Explicitly excluded:
#   response - false positives are high (many "Success"/"None" outputs
#              coincidentally look similar)
#   path     - already covered by undo detection in s6
#   error    - two failed actions are not necessarily doing the same thing
S4_DIMS = ("command", "purpose", "reasoning")

S4_THRESHOLDS = {
    "command_strong":   0.90,
    "purpose_strong":   0.85,
    "reasoning_strong": 0.85,
    "combined_min":     0.80,
}


def _dominant_reason(pairs: list[DuplicatePair]) -> str:
    """Most common judge reason (e.g. command_strong / purpose_strong) in a cluster."""
    reasons = Counter()
    for p in pairs:
        key = re.split(r"[=(]", p.reason)[0]
        reasons[key] += 1
    return reasons.most_common(1)[0][0] if reasons else ""


def s4_detect_duplicates(sid: str, tree: RunTree,
                         segments: list[Segment]) -> dict[int, dict]:
    """Embed-based duplicate detection. Runs per segment with sibling filtering.

    Returns::

        {seg_id: {
            "n_commands": int,      # total run_command actions in the segment
            "pairs":      list[DuplicatePair],
            "clusters":   list[DuplicateCluster],
            "reason_distribution": {reason_type: count}
        }}
    """
    # 1. compute / reuse the embed cache
    res = embed_cache.compute_embeds(
        sid,
        [dict(a) for a in tree.actions],
        verbose=False,
    )
    cache = res["cache"]

    # 2. build the sibling map (map Thinking.action_refs from int idx -> ref str)
    idx_to_ref = {a["idx"]: a["ref"] for a in tree.actions}
    sib_thinkings = []
    for t in tree.root_node.ir.thinkings:
        refs = [idx_to_ref.get(i) for i in getattr(t, "action_refs", None) or []]
        refs = [r for r in refs if r]
        if len(refs) >= 2:
            class _T:
                pass
            _t = _T(); _t.action_refs = refs
            sib_thinkings.append(_t)
    sib_map = similarity.build_sibling_map(sib_thinkings)

    # 3. per-segment detection
    out: dict[int, dict] = {}
    for s in segments:
        cmds = [a for a in tree.actions
                if a.get("action") == "run_command"
                and s.start <= (a.get("turn") or 0) <= s.end]
        refs = [a["ref"] for a in cmds]

        raw_pairs = similarity.find_duplicate_pairs(
            cache, refs,
            thresholds=S4_THRESHOLDS,
            sibling_map=sib_map,
        )
        pairs = [
            DuplicatePair(ref1=rp["ref1"], ref2=rp["ref2"],
                          reason=rp["reason"], sims=rp["sims"])
            for rp in raw_pairs
        ]

        # cluster
        raw_pair_dicts = [{"ref1": p.ref1, "ref2": p.ref2} for p in pairs]
        cluster_lists = similarity.cluster_from_pairs(raw_pair_dicts)
        clusters = []
        for refs_in_c in cluster_lists:
            in_cluster = [p for p in pairs
                          if p.ref1 in refs_in_c or p.ref2 in refs_in_c]
            clusters.append(DuplicateCluster(
                refs=refs_in_c,
                dominant_reason=_dominant_reason(in_cluster),
            ))

        reason_dist = Counter(
            re.split(r"[=(]", p.reason)[0] for p in pairs
        )

        out[s.seg] = {
            "n_commands": len(refs),
            "pairs":      pairs,
            "clusters":   clusters,
            "reason_distribution": dict(reason_dist),
        }
    return out


# ============================================================================
# s5 . detect_wasted_reads
# ============================================================================
# Approach:
#   Stage 1 (substring): if basename(read.path) appears anywhere in the next K
#                        actions' command/path/purpose/reasoning fields, the
#                        read is considered consumed.
#   Stage 2 (embed):     when stage 1 misses, compare the read's response
#                        vector against subsequent actions' command/purpose/
#                        reasoning vectors. If any cosine similarity exceeds
#                        the threshold, still consumed.
#
# Only reads that fail BOTH stages are flagged as wasted.
S5_WINDOW_K = 20
S5_MIN_BASENAME_LEN = 5
S5_SEMANTIC_THRESHOLD = 0.60


def _fetch_vector(entry: dict, dim: str):
    if not entry:
        return None
    return entry.get("vectors", {}).get(dim)


def _cosine(v1, v2) -> float:
    import numpy as np
    if v1 is None or v2 is None:
        return 0.0
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return float(np.dot(v1, v2) / (n1 * n2))


def s5_detect_wasted_reads(sid: str, tree: RunTree,
                           segments: list[Segment],
                           K: int = S5_WINDOW_K,
                           semantic_threshold: float = S5_SEMANTIC_THRESHOLD
                           ) -> dict[int, dict]:
    """Detect reads whose content was never referenced by subsequent actions.

    Returns::

        {seg_id: {
            "n_reads":  int,                # read_file actions in the segment
            "wasted":   list[WastedRead],   # ones that failed both checks
        }}
    """
    from .schema import WastedRead

    # Build embed cache once (reuses across segments)
    res = embed_cache.compute_embeds(
        sid,
        [dict(a) for a in tree.actions],
        verbose=False,
    )
    cache = res["cache"]

    # Index actions by position for window slicing
    action_pos = {a["ref"]: i for i, a in enumerate(tree.actions)}

    out: dict[int, dict] = {}
    for s in segments:
        reads = [a for a in tree.actions
                 if a.get("action") == "read_file" and a.get("path")
                 and s.start <= (a.get("turn") or 0) <= s.end]

        wasted: list[WastedRead] = []
        for a in reads:
            basename = os.path.basename(a["path"])
            if len(basename) < S5_MIN_BASENAME_LEN:
                continue

            i = action_pos.get(a["ref"])
            if i is None:
                continue
            window = tree.actions[i + 1: i + 1 + K]

            # Stage 1: substring match on basename
            consumed_substring = any(
                basename in (
                    (b.get("command") or "")
                    + " " + (b.get("path") or "")
                    + " " + (b.get("purpose") or "")
                    + " " + (b.get("reasoning") or "")
                )
                for b in window
            )
            if consumed_substring:
                continue

            # Stage 2: embed similarity between read response and window actions
            read_entry = cache["entries"].get(a["ref"])
            read_resp_vec = _fetch_vector(read_entry, "response")

            max_sim = 0.0
            if read_resp_vec is not None:
                for b in window:
                    b_entry = cache["entries"].get(b["ref"])
                    for dim in ("command", "purpose", "reasoning"):
                        vec = _fetch_vector(b_entry, dim)
                        sim = _cosine(read_resp_vec, vec)
                        if sim > max_sim:
                            max_sim = sim
                            if max_sim > semantic_threshold:
                                break
                    if max_sim > semantic_threshold:
                        break

            if max_sim <= semantic_threshold:
                wasted.append(WastedRead(
                    ref=a["ref"],
                    turn=a.get("turn") or 0,
                    path=a["path"],
                    basename=basename,
                    reason=("no_reference_no_semantic" if read_resp_vec is not None
                            else "no_reference"),
                    max_semantic_sim=max_sim,
                ))

        out[s.seg] = {"n_reads": len(reads), "wasted": wasted}
    return out


# ============================================================================
# s6 . detect_undo (same path written multiple times)
# ============================================================================
_WRITE_ACTIONS = {"write_file", "modify_file", "create_file", "edit_file"}


def s6_detect_undo(tree: RunTree, segments: list[Segment]) -> dict[int, dict]:
    """Find paths written more than once inside a segment.

    Reports both raw count AND turn-density signals:
      - turn_span:      how many turns the writes span
      - density:        count / turn_span (writes-per-turn; higher = more concentrated)
      - is_single_turn: all writes in the same turn (strongest thrashing signal)

    Sorted by density desc, then count desc: the most suspicious paths float up.
    Diff-based undo detection (write X, revert X) is Phase 2.

    Returns::

        {seg_id: {"n_writes": int, "multi_write": list[MultiWriteFile]}}
    """
    from .schema import MultiWriteFile

    out: dict[int, dict] = {}
    for s in segments:
        writes = [a for a in tree.actions
                  if a.get("action") in _WRITE_ACTIONS and a.get("path")
                  and s.start <= (a.get("turn") or 0) <= s.end]

        # group by path (preserving action order via list append)
        by_path: dict[str, list[dict]] = {}
        for a in writes:
            by_path.setdefault(a["path"], []).append(a)

        multi: list[MultiWriteFile] = []
        for p, acts in by_path.items():
            if len(acts) < 2:
                continue
            turns = [a.get("turn") or 0 for a in acts]
            first_t, last_t = min(turns), max(turns)
            span = last_t - first_t + 1
            multi.append(MultiWriteFile(
                path=p,
                count=len(acts),
                refs=[a["ref"] for a in acts],
                turn_span=span,
                density=(len(acts) / span if span > 0 else float(len(acts))),
                is_single_turn=(first_t == last_t),
                first_turn=first_t,
                last_turn=last_t,
            ))

        # Sort: single-turn thrashing first, then by density desc, then by count desc.
        multi.sort(key=lambda m: (not m.is_single_turn, -m.density, -m.count))

        out[s.seg] = {"n_writes": len(writes), "multi_write": multi}
    return out


# ============================================================================
# s7 . detect_stuck (high-cycle turns + consecutive failures)
# ============================================================================
S7_CYCLES_THRESHOLD = 20
S7_MIN_FAILURE_CHAIN = 3


def s7_detect_stuck(overview: dict, tree: RunTree,
                    segments: list[Segment],
                    cycles_threshold: int = S7_CYCLES_THRESHOLD
                    ) -> dict[int, dict]:
    """Detect turns where the agent appears stuck.

    Two signals:
      1. High-cycle turns: number_of_cycles > threshold (from turn_metadata).
      2. Consecutive failure groups of length >= 3 (blocked or error != None).

    Returns::

        {seg_id: {
            "stuck_turns":          list[StuckTurn],
            "consecutive_failures": list[FailureGroup],
        }}
    """
    from .schema import FailureGroup, StuckTurn

    tm = overview["turn_metadata"]
    out: dict[int, dict] = {}

    for s in segments:
        # Stuck turns by cycles threshold
        stuck: list[StuckTurn] = [
            StuckTurn(turn=r["turn"], cycles=r["cycles"],
                      dur_s=r["dur_s"], credits=r["credits"])
            for r in tm
            if s.start <= r["turn"] <= s.end and r["cycles"] > cycles_threshold
        ]

        # Consecutive failures within the segment's action range
        seg_actions = [a for a in tree.actions
                       if s.start <= (a.get("turn") or 0) <= s.end]
        fails: list[FailureGroup] = []
        run_refs: list[str] = []
        for a in seg_actions:
            failed = a.get("blocked") or a.get("error")
            if failed:
                run_refs.append(a["ref"])
            else:
                if len(run_refs) >= S7_MIN_FAILURE_CHAIN:
                    fails.append(FailureGroup(refs=list(run_refs)))
                run_refs = []
        if len(run_refs) >= S7_MIN_FAILURE_CHAIN:
            fails.append(FailureGroup(refs=list(run_refs)))

        out[s.seg] = {"stuck_turns": stuck, "consecutive_failures": fails}
    return out


# ============================================================================
# s8 . judge (LLM)
# ============================================================================
def _v_judgment(seg_ids: set[int]) -> Callable[[Any], list[str]]:
    """Validation gate for s8 LLM output."""
    valid_grades = {"A", "B", "C", "D"}

    def v(data: Any) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict):
            return ["top level must be a JSON object"]
        if data.get("global_grade") not in valid_grades:
            errs.append(f"global_grade={data.get('global_grade')!r} not in {valid_grades}")
        if not str(data.get("global_reason") or "").strip():
            errs.append("global_reason cannot be empty")

        ps = data.get("per_segment")
        if not isinstance(ps, list):
            errs.append("per_segment must be a list")
        else:
            got_seg_ids = set()
            for i, g in enumerate(ps):
                if not isinstance(g, dict):
                    errs.append(f"per_segment[{i}] must be an object"); continue
                sid_ = g.get("seg")
                if sid_ not in seg_ids:
                    errs.append(f"per_segment[{i}].seg={sid_!r} not a known segment")
                    continue
                got_seg_ids.add(sid_)
                if g.get("grade") not in valid_grades:
                    errs.append(f"per_segment[{i}].grade={g.get('grade')!r} not in {valid_grades}")
                if not str(g.get("reason") or "").strip():
                    errs.append(f"per_segment[{i}].reason cannot be empty")
                # Optional per-segment suspicious_ops list; empty allowed.
                ops = g.get("suspicious_ops", [])
                if ops is None:
                    ops = []
                if not isinstance(ops, list):
                    errs.append(f"per_segment[{i}].suspicious_ops must be a list (or omitted)")
                else:
                    if len(ops) > 6:
                        errs.append(f"per_segment[{i}].suspicious_ops has {len(ops)} items "
                                    f"(max 6)")
                    # Detect bare "seg N" phrases; ref tokens like "b7f5#12"
                    # or "#12" are exempt because they contain # or precede a
                    # ref, not a bare "seg" word.
                    bare_seg_re = re.compile(r"\bseg\s*\d+\b", re.IGNORECASE)
                    for j, a in enumerate(ops):
                        if not isinstance(a, str) or not a.strip():
                            errs.append(f"per_segment[{i}].suspicious_ops[{j}] must be a "
                                        f"non-empty string")
                            continue
                        if len(a) < 120:
                            errs.append(f"per_segment[{i}].suspicious_ops[{j}] is only "
                                        f"{len(a)} chars, need >=120 for enough "
                                        f"context (target 200-500)")
                        digits = re.findall(r"\d", a)
                        if len(digits) < 2:
                            errs.append(f"per_segment[{i}].suspicious_ops[{j}] must contain "
                                        f"at least two numeric data points (got "
                                        f"{len(digits)})")
                        if bare_seg_re.search(a):
                            errs.append(f"per_segment[{i}].suspicious_ops[{j}] contains "
                                        f"bare 'seg N' phrase; refer to the segment by "
                                        f"its theme name, not the seg id")
            missing = seg_ids - got_seg_ids
            if missing:
                errs.append(f"per_segment missing segments: {sorted(missing)}")

        recs = data.get("recommendations")
        if not isinstance(recs, list) or not (3 <= len(recs) <= 8):
            errs.append(f"recommendations must be a list of 3-8 items")
        else:
            for i, r in enumerate(recs):
                if not isinstance(r, str) or not r.strip():
                    errs.append(f"recommendations[{i}] must be a non-empty string")
                elif not re.search(r"\d", r):
                    errs.append(f"recommendations[{i}] must contain at least one digit "
                                f"or specific identifier")
        return errs
    return v


def _build_judge_pack(overview: dict, seg_stats: list, dup: dict,
                      wasted: dict, undo: dict, stuck: dict,
                      tree: Optional[RunTree] = None) -> str:
    """Assemble the evidence pack shown to the s8 judge.

    When `tree` is passed, each referenced ref is enriched with a short
    command / target preview so the LLM (and later, human readers of the
    audit output) can see WHAT the operation was, not just the ref id.
    """
    def _preview(ref: str, cap: int = 300) -> str:
        """Return `"<cmd or path preview>"` for `ref`, else empty string."""
        if tree is None:
            return ""
        a = tree.by_ref(ref)
        if a is None:
            return ""
        txt = (a.get("command") or a.get("path") or a.get("pattern")
               or (a.get("args") or {}).get("__tool_use_purpose") or "")
        txt = str(txt).replace("\n", " ⏎ ").strip()
        if len(txt) > cap:
            txt = txt[:cap - 1] + "…"
        return txt

    lines: list[str] = []
    t = overview["totals"]
    lines.append("[1. Global totals]")
    lines.append(f"  turns: {overview['turns']}")
    lines.append(f"  wall_s: {t.get('wall_s') or 0:.0f}  net_s: {t['net_s']:.0f}"
                 f"  idle_pct: {t.get('idle_pct', '?')}")
    lines.append(f"  credits: {t['credits']:.0f}  llm_reqs: {t['llm_reqs']}"
                 f"  cycles: {t['cycles']}  compactions: {t['compactions']}")

    lines.append("\n[2. Per-segment cost]")
    lines.append("  seg  range      turns  dur     credits  cycles  cr/turn  cy/turn  ctx_max  theme")
    for s in seg_stats:
        lines.append(
            f"  {s.seg:>3}  {s.range:>9}  {s.n_turns:>5}  {s.dur_s:>4.0f}s  "
            f"{s.credits:>7.1f}  {s.cycles:>6}  {s.credits_per_turn:>6.2f}  "
            f"{s.cycles_per_turn:>6.2f}  {s.ctx_pct_max:>6.1f}%  {s.theme[:32]}"
        )

    lines.append("\n[3. Duplicate detection (s4)]")
    for seg_id, r in dup.items():
        lines.append(f"  seg {seg_id}: {r['n_commands']} commands, "
                     f"{len(r['pairs'])} pairs, {len(r['clusters'])} clusters")
        lines.append(f"    reasons: {r['reason_distribution']}")
        top_clusters = sorted(r["clusters"], key=lambda c: -c.size)[:3]
        for c in top_clusters:
            lines.append(f"    cluster size={c.size} ({c.dominant_reason}):")
            for ref in c.refs[:6]:
                prev = _preview(ref)
                if prev:
                    lines.append(f"      {ref}  {prev}")
                else:
                    lines.append(f"      {ref}")
            if len(c.refs) > 6:
                lines.append(f"      ... (+{len(c.refs) - 6} more)")
        # Top pairs with previews on each side.
        def _pair_score(p):
            try:
                return max(p.sims.values()) if p.sims else 0.0
            except Exception:
                return 0.0
        top_pairs = sorted(r["pairs"], key=_pair_score, reverse=True)[:8]
        for p in top_pairs:
            sims_str = ", ".join(f"{k}={v:.2f}" for k, v in
                                 sorted(p.sims.items(), key=lambda kv: -kv[1])[:3])
            lines.append(f"    pair [{p.reason}] ({sims_str})")
            lines.append(f"      A {p.ref1}  {_preview(p.ref1)}")
            lines.append(f"      B {p.ref2}  {_preview(p.ref2)}")

    lines.append("\n[4. Wasted reads (s5)]")
    for seg_id, r in wasted.items():
        pct = 100.0 * len(r["wasted"]) / max(r["n_reads"], 1)
        lines.append(f"  seg {seg_id}: {r['n_reads']} reads, "
                     f"{len(r['wasted'])} wasted ({pct:.1f}%)")
        for w in r["wasted"][:5]:
            prev = _preview(w.ref)
            lines.append(f"    {w.ref} turn={w.turn} {w.basename}"
                         f" (max_sim={w.max_semantic_sim:.2f})")
            if prev and prev != w.basename:
                lines.append(f"        purpose/target: {prev}")

    lines.append("\n[5. Multi-write files (s6) - sorted single-turn thrashing first, then density desc]")
    for seg_id, r in undo.items():
        n_multi = len(r["multi_write"])
        n_single_turn = sum(1 for m in r["multi_write"] if m.is_single_turn)
        lines.append(f"  seg {seg_id}: {r['n_writes']} writes, "
                     f"{n_multi} paths written >=2 times "
                     f"({n_single_turn} of them thrashed within one turn)")
        for m in r["multi_write"][:5]:
            marker = "!" if m.is_single_turn else ("*" if m.density > 0.5 else " ")
            lines.append(f"    {marker} {m.count}x turn {m.first_turn}-{m.last_turn} "
                         f"(span={m.turn_span}, density={m.density:.2f})  {m.path}")

    lines.append("\n[6. Stuck signals (s7)]")
    for seg_id, r in stuck.items():
        n_stuck = len(r["stuck_turns"])
        n_fails = len(r["consecutive_failures"])
        lines.append(f"  seg {seg_id}: {n_stuck} high-cycle turns, "
                     f"{n_fails} consecutive-failure groups")
        for st in r["stuck_turns"][:5]:
            lines.append(f"    turn {st.turn}: cycles={st.cycles} "
                         f"dur={st.dur_s:.0f}s credits={st.credits:.1f}")

    return "\n".join(lines)


# ============================================================================
# s8 . judge (LLM tool-use loop)
# ============================================================================
_S8_BUDGET_DEFAULT = 20
_S8_TOOL_RESULT_CAP = 4000


def _v_final_or_tool(seg_ids: set[int]) -> Callable[[Any], list[str]]:
    """Validator: accept EITHER a tool_call OR a wrapped final judgment."""
    inner_v = _v_judgment(seg_ids)

    def v(data: Any) -> list[str]:
        if not isinstance(data, dict):
            return ["top level must be a JSON object"]
        if "tool_call" in data and "final" in data:
            return ["output must contain either tool_call OR final, not both"]
        if "tool_call" in data:
            tc = data["tool_call"]
            errs = []
            if not isinstance(tc, dict):
                return ["tool_call must be an object"]
            name = tc.get("name")
            if name not in {"get_action", "list_turn", "search_actions", "read_file"}:
                errs.append(f"unknown tool name: {name!r}")
            if not isinstance(tc.get("args", {}), dict):
                errs.append("tool_call.args must be an object")
            return errs
        if "final" in data:
            return inner_v(data["final"])
        return ["output must contain either 'tool_call' or 'final'"]
    return v


def _tool_get_action(tree: RunTree, args: dict) -> dict:
    ref = str(args.get("ref", "")).strip()
    if not ref:
        return {"error": "missing 'ref'"}
    return evapi.get(tree, ref,
                     fields=["action", "tool", "command", "path", "pattern",
                             "reasoning", "completed", "blocked", "error",
                             "turn", "response"],
                     cap=_S8_TOOL_RESULT_CAP)


def _tool_list_turn(tree: RunTree, args: dict) -> dict:
    try:
        turn = int(args.get("turn"))
    except (TypeError, ValueError):
        return {"error": "turn must be an integer"}
    rows = [evapi.summarize(a) for a in tree.root_actions
            if a.get("turn") == turn]
    return {"turn": turn, "n": len(rows), "rows": rows[:60],
            "truncated": len(rows) > 60}


def _tool_search_actions(tree: RunTree, args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    if not q or len(q) > 200:
        return {"error": "query must be 1..200 chars"}
    try:
        k = int(args.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    k = max(1, min(k, 15))
    scope = {}
    if args.get("turn_gte") is not None:
        scope["turn_gte"] = int(args["turn_gte"])
    if args.get("turn_lte") is not None:
        scope["turn_lte"] = int(args["turn_lte"])
    scope["_root_sid"] = tree.root
    anchors = [w.strip() for w in re.split(r"[|,]", q) if w.strip()] or [q]
    return evapi.retrieve(tree, anchors, scope=scope or None,
                          k=k, root_only=True, score_mode="idf")


def _tool_read_file(tree: RunTree, allowed_roots: list[str], args: dict) -> dict:
    path = str(args.get("path", "")).strip()
    if not path:
        return {"error": "missing 'path'"}
    try:
        max_bytes = int(args.get("max_bytes", 4000))
    except (TypeError, ValueError):
        max_bytes = 4000
    max_bytes = max(200, min(max_bytes, 16000))
    apath = os.path.abspath(path)
    ok = False
    for root in allowed_roots:
        if not root:
            continue
        try:
            if os.path.commonpath([apath, os.path.abspath(root)]) == os.path.abspath(root):
                ok = True; break
        except ValueError:
            pass
    if not ok:
        return {"error": "path is outside this run's written_dirs; refused",
                "allowed_roots": allowed_roots}
    if not os.path.isfile(apath):
        return {"error": f"not a file: {apath}"}
    try:
        with open(apath, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        return {"error": f"read failed: {e}"}
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    # crude binary check: too many NULs
    if raw.count(b"\x00") > 4:
        return {"error": "binary file; refused"}
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception:
        content = raw.decode("latin-1", errors="replace")
    return {"path": apath, "size": os.path.getsize(apath),
            "mtime": dt.datetime.fromtimestamp(
                os.path.getmtime(apath), dt.timezone.utc).isoformat(),
            "content": content, "truncated": truncated}


def _run_tool(tree: RunTree, allowed_roots: list[str], tc: dict) -> dict:
    name = tc.get("name")
    args = tc.get("args") or {}
    try:
        if name == "get_action":     return _tool_get_action(tree, args)
        if name == "list_turn":      return _tool_list_turn(tree, args)
        if name == "search_actions": return _tool_search_actions(tree, args)
        if name == "read_file":      return _tool_read_file(tree, allowed_roots, args)
    except Exception as e:
        return {"error": f"tool {name} raised: {e!r}"}
    return {"error": f"unknown tool: {name!r}"}


def s8_judge(overview: dict, seg_stats: list, dup: dict,
             wasted: dict, undo: dict, stuck: dict, tree: RunTree, *,
             caller: Optional[Callable[[str], str]] = None,
             use_llm: bool = True,
             budget: int = _S8_BUDGET_DEFAULT,
             trace_sink: Optional[list] = None):
    """Grade the run via an LLM tool-use loop.

    The LLM sees the summary pack first; if it needs more, it calls one of
    four read-only tools (get_action / list_turn / search_actions / read_file)
    and re-decides. Falls back to a code-computed grade if the LLM is
    unavailable or busts the budget without producing a valid final.

    `trace_sink`, if provided, is appended to with one dict per LLM turn:
        {"iter": i, "prompt": str, "reply_raw": str, "parsed": dict,
         "tool_result": dict?}
    so the caller can persist the full transcript for auditing.
    """
    from .schema import Judgment, SegmentGrade

    seg_ids = {s.seg for s in seg_stats}

    def _fallback_grade(note: str = "") -> Judgment:
        """Evidence-based fallback grade (no cy/turn or cr/turn thresholds).

        For each segment, count concrete evidence items and derive a grade:

            E1  large duplicate clusters (size >= 3)
            E2  same-turn multi-write thrash (density >= 3 in one turn) or
                cross-turn heavy rewrite (>= 5 writes to one file)
            E3  stuck turns and consecutive-failure groups
            E4  single-turn credit share vs run total

        The mapping is intentionally conservative -- the LLM path is the
        authoritative one; fallback only fires when the LLM is unavailable.
        """
        # Precompute total credits for cost-concentration analysis.
        totals = overview.get("totals") or {}
        tot_cr = totals.get("credits") or 0.0
        # Build turn -> credit share.
        turn_share: dict[int, float] = {}
        for r in overview.get("turn_metadata", []):
            if tot_cr > 0:
                turn_share[r["turn"]] = r["credits"] / tot_cr

        # For each SegmentStats s, find turns that fall inside [s.range]
        # and derive the highest single-turn credit share in the segment.
        def _turns_in_range(rng: str) -> list[int]:
            try:
                a, b = rng.split("-", 1)
                return list(range(int(a), int(b) + 1))
            except Exception:
                return []

        per_seg = []
        for s in seg_stats:
            n_big_clusters = sum(
                1 for c in dup.get(s.seg, {}).get("clusters", [])
                if getattr(c, "size", 0) >= 3
            )
            mw = undo.get(s.seg, {}).get("multi_write", [])
            n_thrash_1turn = sum(1 for m in mw
                                 if getattr(m, "is_single_turn", False)
                                 and getattr(m, "count", 0) >= 3)
            n_heavy_rewrite = sum(1 for m in mw
                                  if getattr(m, "count", 0) >= 5)
            n_stuck = len(stuck.get(s.seg, {}).get("stuck_turns", []))
            n_fails = len(stuck.get(s.seg, {}).get("consecutive_failures", []))

            seg_turns = _turns_in_range(s.range)
            max_share = max((turn_share.get(t, 0.0) for t in seg_turns),
                            default=0.0)

            # Decide grade.
            if (max_share >= 0.25 or n_thrash_1turn >= 1 or n_fails >= 1
                    or n_heavy_rewrite >= 2):
                g = "D"
            elif (max_share >= 0.15 or n_big_clusters >= 2
                  or n_heavy_rewrite >= 1 or n_stuck >= 2):
                g = "C"
            elif (max_share >= 0.10 or n_big_clusters >= 1 or n_stuck >= 1):
                g = "B"
            else:
                g = "A"

            reason = (
                f"[fallback] clusters(size>=3)={n_big_clusters}, "
                f"single-turn thrash={n_thrash_1turn}, "
                f"heavy rewrite={n_heavy_rewrite}, "
                f"stuck={n_stuck}, fail groups={n_fails}, "
                f"max single-turn share={max_share*100:.1f}%"
            )
            per_seg.append(SegmentGrade(
                seg=s.seg, grade=g, reason=reason,
                theme=s.theme, range=s.range,
            ))

        grades_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
        worst = max((grades_rank[g.grade] for g in per_seg), default=0)
        global_grade = "ABCD"[worst]
        total_stuck = sum(len(r["stuck_turns"]) for r in stuck.values())
        total_multi = sum(len(r["multi_write"]) for r in undo.values())
        total_dup_pairs = sum(len(r["pairs"]) for r in dup.values())
        total_wasted = sum(len(r["wasted"]) for r in wasted.values())
        recs = [
            f"[fallback] stuck turns detected: {total_stuck}; review turns "
            f"with the highest cycles first.",
            f"[fallback] files written >=2 times: {total_multi}; check if any "
            f"are semantic reverts or single-turn thrash.",
            f"[fallback] duplicate pairs: {total_dup_pairs}; suspicious reads: "
            f"{total_wasted}.",
        ]
        reason = (f"[fallback{': ' + note if note else ''}] evidence-based "
                  f"grade over {len(per_seg)} segment(s). "
                  f"segment grades: {[g.grade for g in per_seg]}")
        return Judgment(global_grade=global_grade, global_reason=reason,
                        per_segment=per_seg, recommendations=recs)

    if not use_llm:
        return _fallback_grade()

    pack = _build_judge_pack(overview, seg_stats, dup, wasted, undo, stuck, tree)
    base_prompt = prompts.JUDGE_PROMPT.format(pack=pack, budget=budget)
    allowed_roots = list(overview.get("written_dirs") or [])
    if not allowed_roots:
        allowed_roots = sorted({os.path.dirname(a["path"]) for a in tree.actions
                                if a.get("action") in ("create_file", "modify_file",
                                                       "append_file")
                                and a.get("path")})

    validator = _v_final_or_tool(seg_ids)

    # Persistent ACP session across the whole tool-use loop: the agent side
    # keeps our conversation history keyed by sessionId, so each follow-up
    # only needs to send TOOL_RESULT for the previous call -- no manual
    # concat of prior prompts on our side.
    #
    # If the caller injected a `caller` (e.g. a test stub or a capture wrapper
    # for auditing), honor it: the caller is expected to preserve history
    # itself, or the loop degrades to stateless mode -- we still send just
    # the delta each turn, matching what a stateful caller would need.
    acp_ctx = None
    call_wrapper = caller
    if call_wrapper is None:
        acp_ctx = KiroAcpClient(agent="kiro-judge", timeout=480)
        try:
            acp_ctx.start_session()
        except Exception as e:
            if trace_sink is not None:
                trace_sink.append({"iter": -1, "error": f"acp start failed: {e!r}"})
            return _fallback_grade(f"acp start failed: {e!r}")
        call_wrapper = acp_caller_from(acp_ctx)

    final_data = None
    try:
        for i in range(budget + 1):
            remaining = budget - i
            if i == 0:
                # First turn carries the full evidence pack + tool protocol.
                turn_prompt = base_prompt + (
                    "\n\n[iter 0, {rem} tool call(s) remaining] "
                    "Emit either one tool_call or the final judgment."
                    .format(rem=remaining))
            # (else: turn_prompt was set at the end of the previous iter)

            try:
                data = llm_ask(turn_prompt, validator, caller=call_wrapper,
                               label=f"s8.iter{i}")
            except Exception as e:
                if trace_sink is not None:
                    trace_sink.append({"iter": i, "error": repr(e)})
                return _fallback_grade(f"llm error at iter {i}: {e!r}")

            rec = {"iter": i, "parsed": data}
            if "final" in data:
                final_data = data["final"]
                if trace_sink is not None:
                    trace_sink.append(rec)
                break

            # tool_call branch
            tc = data["tool_call"]
            if remaining <= 0:
                rec["note"] = "budget exhausted before final; forcing fallback"
                if trace_sink is not None:
                    trace_sink.append(rec)
                return _fallback_grade("budget exhausted without final")

            tool_result = _run_tool(tree, allowed_roots, tc)
            rec["tool_call"] = tc
            rec["tool_result"] = tool_result
            if trace_sink is not None:
                trace_sink.append(rec)

            # Next turn: agent already remembers everything through the ACP
            # session, so just send the delta (previous call's result +
            # nudge). Truncate tool_result payload defensively so a runaway
            # response doesn't blow the prompt size.
            result_json = json.dumps(tool_result, ensure_ascii=False,
                                     default=str)[:8000]
            turn_prompt = (
                "TOOL_RESULT for your previous call "
                "({name}, args={args}):\n"
                "```json\n{res}\n```\n\n"
                "[iter {i}, {rem} tool call(s) remaining] "
                "Emit either the next tool_call or the final judgment."
            ).format(name=tc.get("name"),
                     args=json.dumps(tc.get("args") or {}, ensure_ascii=False),
                     res=result_json, i=i + 1, rem=budget - (i + 1))
        else:
            return _fallback_grade("loop ran out without emitting final")
    finally:
        if acp_ctx is not None:
            acp_ctx.close()

    if final_data is None:
        return _fallback_grade("no final emitted")

    # Enrich per-segment output with the segment's theme/range so callers
    # (and downstream reports) show a human-readable name, not just seg N.
    theme_by = {s.seg: s.theme for s in seg_stats}
    range_by = {s.seg: s.range for s in seg_stats}
    per_seg = [
        SegmentGrade(seg=g["seg"], grade=g["grade"], reason=g["reason"],
                     suspicious_ops=list(g.get("suspicious_ops") or []),
                     theme=theme_by.get(g["seg"], ""),
                     range=range_by.get(g["seg"], ""))
        for g in final_data["per_segment"]
    ]
    return Judgment(
        global_grade=final_data["global_grade"],
        global_reason=final_data["global_reason"],
        per_segment=per_seg,
        recommendations=list(final_data["recommendations"]),
    )


# ============================================================================
# s9 . aggregate (final JSON report)
# ============================================================================
def _build_narrative_facts(overview: dict, seg_stats: list, dup: dict,
                           wasted: dict, undo: dict, stuck: dict,
                           tree: Optional[RunTree] = None) -> str:
    """Objective facts pack fed to the s9 narrative LLM call.

    Everything here is a number or an identifier that came from s1..s7 --
    the LLM's only job is to describe them in prose, not to add analysis.
    Also carries the user's original prompt for each top-cost turn so the
    LLM can name WHAT each hotspot turn was trying to do.
    """
    t = overview["totals"]
    lines: list[str] = []
    lines.append("[Run totals]")
    lines.append(f"  agent: {overview.get('agent_name') or 'n/a'}")
    lines.append(f"  turns: {overview['turns']}")
    lines.append(f"  wall_s: {t.get('wall_s') or 0:.0f}   "
                 f"net_s: {t['net_s']:.0f}   "
                 f"idle_pct: {t.get('idle_pct', 'n/a')}")
    lines.append(f"  credits: {t['credits']:.1f}   "
                 f"llm_reqs: {t['llm_reqs']}   "
                 f"cycles: {t['cycles']}   "
                 f"compactions: {t['compactions']}")

    # User prompt per turn, for weaving purposes into hotspot narration.
    prompts_per_turn: dict[int, str] = {}
    if tree is not None and getattr(tree.root_node, "ir", None) is not None:
        raw = list(getattr(tree.root_node.ir, "prompts", []) or [])
        for i, p in enumerate(raw, 1):
            s = str(p).replace("\n", " ").strip()
            if len(s) > 220:
                s = s[:219] + "…"
            prompts_per_turn[i] = s

    def _turn_prompt(turn_n: int) -> str:
        return prompts_per_turn.get(turn_n, "")

    lines.append("\n[Top turns by credits] (turn, dur_s, cycles, credits, share_of_total, user_prompt)")
    tot_credits = t["credits"] or 1.0
    tm_sorted = sorted(overview["turn_metadata"],
                       key=lambda r: -r["credits"])[:5]
    for r in tm_sorted:
        p = _turn_prompt(r["turn"])
        lines.append(f"  turn {r['turn']:>3}: dur={r['dur_s']:>5.0f}s  "
                     f"cycles={r['cycles']:>3}  "
                     f"credits={r['credits']:>5.1f}  "
                     f"({100.0*r['credits']/tot_credits:>4.1f}%)  "
                     f"end={r.get('end_reason','?')}")
        if p:
            lines.append(f"       user_prompt: {p}")

    lines.append("\n[Top turns by cycles]")
    tm_by_cy = sorted(overview["turn_metadata"],
                      key=lambda r: -r["cycles"])[:5]
    for r in tm_by_cy:
        p = _turn_prompt(r["turn"])
        lines.append(f"  turn {r['turn']:>3}: cycles={r['cycles']:>3}  "
                     f"credits={r['credits']:>5.1f}")
        if p:
            lines.append(f"       user_prompt: {p}")

    lines.append("\n[Segments and their cost]")
    for s in seg_stats:
        lines.append(f"  [{s.range}] {s.theme[:40]}  "
                     f"cy/turn={s.cycles_per_turn:.2f}  "
                     f"cr/turn={s.credits_per_turn:.2f}  "
                     f"credits={s.credits:.1f}  cycles={s.cycles}")

    # Anomaly counters -- concise, no ref lists (s8 already surfaces those).
    lines.append("\n[Anomaly counts (per segment)]")
    for s in seg_stats:
        d = dup.get(s.seg, {})
        w = wasted.get(s.seg, {})
        u = undo.get(s.seg, {})
        st = stuck.get(s.seg, {})
        lines.append(f"  seg {s.seg} ({s.theme[:24]}): "
                     f"dup_pairs={len(d.get('pairs', []))}  "
                     f"dup_clusters={len(d.get('clusters', []))}  "
                     f"wasted_reads={len(w.get('wasted', []))}/"
                     f"{w.get('n_reads', 0)}  "
                     f"multi_write_files={len(u.get('multi_write', []))}  "
                     f"stuck_turns={len(st.get('stuck_turns', []))}")

    return "\n".join(lines)


def _v_narrative(data: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]
    ov = data.get("overview")
    hs = data.get("hotspots")
    if not isinstance(ov, str) or not ov.strip():
        errs.append("overview must be a non-empty string")
    elif not (80 <= len(ov) <= 400):
        errs.append(f"overview length {len(ov)} not in [80, 400]")
    if not isinstance(hs, str) or not hs.strip():
        errs.append("hotspots must be a non-empty string")
    elif not (250 <= len(hs) <= 900):
        errs.append(f"hotspots length {len(hs)} not in [250, 900]")
    elif len(re.findall(r"\d", hs)) < 3:
        errs.append("hotspots must contain at least 3 digits (turn/cost numbers)")
    # Forbid grade letters and fix-advice verbs in either paragraph.
    banned = re.compile(r"\b(?:grade|recommend|recommendation|should|need to|"
                        r"could|consider|ought|"
                        r"[A-D]-?grade|grade[- ]?[A-D])\b", re.IGNORECASE)
    for name, text in (("overview", ov or ""), ("hotspots", hs or "")):
        m = banned.search(text)
        if m:
            errs.append(f"{name} contains forbidden token {m.group(0)!r} "
                        f"(grade/advice belongs to s8, not narrative)")
    return errs


def s9_narrative(overview: dict, seg_stats: list, dup: dict, wasted: dict,
                 undo: dict, stuck: dict, *,
                 tree: Optional[RunTree] = None,
                 caller: Optional[Callable[[str], str]] = None,
                 use_llm: bool = True) -> dict[str, str]:
    """LLM-generated narrative intro for the final report.

    Two paragraphs: run overview + cost hotspots. Everything the LLM says
    is grounded in the objective facts pack; validator rejects mention of
    grades or fix advice (that belongs to s8's judgment).

    Falls back to a code-computed one-liner on LLM failure.
    """
    def _fallback() -> dict[str, str]:
        t = overview["totals"]
        tot = t.get("credits") or 0
        worst = max(overview["turn_metadata"],
                    key=lambda r: r["credits"], default=None)
        overview_txt = (f"{overview['turns']} turns total, "
                        f"wall {int(t.get('wall_s') or 0)}s, "
                        f"net {int(t['net_s'])}s, "
                        f"credits {tot:.1f}, cycles {t['cycles']}"
                        f", compactions {t['compactions']}.")
        if worst:
            hs = (f"Most expensive single turn is turn {worst['turn']}, "
                  f"consuming {worst['credits']:.1f} credits / "
                  f"{worst['cycles']} cycles / {worst['dur_s']:.0f}s, "
                  f"which is "
                  f"{100.0*worst['credits']/(tot or 1):.1f}% of total credits. "
                  f"All other turns are below this. Fallback narrative, LLM not called.")
        else:
            hs = "No turn metadata. Fallback narrative."
        return {"overview": overview_txt, "hotspots": hs}

    if not use_llm:
        return _fallback()

    facts = _build_narrative_facts(overview, seg_stats, dup, wasted, undo, stuck, tree)
    prompt = prompts.NARRATIVE_PROMPT.format(facts=facts)
    try:
        data = llm_ask(prompt, _v_narrative, caller=caller, label="s9.narrative")
    except Exception:
        return _fallback()
    return {"overview": data["overview"], "hotspots": data["hotspots"]}


def s9_aggregate(overview: dict, segments: list[Segment],
                 seg_stats: list, dup: dict, wasted: dict, undo: dict,
                 stuck: dict, judgment, *,
                 narrative: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Assemble the final report.

    Pure aggregation over s1..s8 outputs; the only LLM piece is the
    ``narrative`` (produced by ``s9_narrative``) which the caller passes in.
    Passing ``narrative=None`` skips the narrative section entirely.
    """
    # Locate the "worst turn" -- the single biggest credit sink.
    worst_turn = None
    total_credits = overview["totals"].get("credits") or 0
    for r in overview["turn_metadata"]:
        if worst_turn is None or r["credits"] > worst_turn["credits"]:
            worst_turn = r
    worst_out = None
    if worst_turn and total_credits > 0:
        worst_out = {
            "turn": worst_turn["turn"],
            "dur_s": worst_turn["dur_s"],
            "cycles": worst_turn["cycles"],
            "credits": worst_turn["credits"],
            "share_of_total_credits_pct":
                round(100.0 * worst_turn["credits"] / total_credits, 2),
        }

    def _short_dup(r):
        def _pair_key(p):
            try:
                return max(p.sims.values()) if p.sims else 0.0
            except Exception:
                return 0.0
        return {
            "n_commands": r["n_commands"],
            "n_pairs":    len(r["pairs"]),
            "n_clusters": len(r["clusters"]),
            "reason_distribution": r["reason_distribution"],
            "top_clusters": [c.to_dict()
                             for c in sorted(r["clusters"], key=lambda x: -x.size)[:5]],
            "top_pairs": [p.to_dict()
                          for p in sorted(r["pairs"], key=_pair_key, reverse=True)[:8]],
        }

    def _short_wasted(r):
        return {
            "n_reads": r["n_reads"],
            "n_wasted": len(r["wasted"]),
            "wasted_pct": (round(100.0 * len(r["wasted"]) / r["n_reads"], 1)
                           if r["n_reads"] else 0.0),
            "wasted": [w.to_dict() for w in r["wasted"][:10]],
        }

    def _short_undo(r):
        return {
            "n_writes": r["n_writes"],
            "n_multi": len(r["multi_write"]),
            "multi_write": [m.to_dict() for m in r["multi_write"][:10]],
        }

    def _short_stuck(r):
        return {
            "stuck_turns": [st.to_dict() for st in r["stuck_turns"]],
            "consecutive_failures": [fg.to_dict() for fg in r["consecutive_failures"]],
        }

    return {
        "verdict": judgment.global_grade,
        "narrative": narrative or {},
        "totals": overview["totals"],
        "segments":   [s.to_dict() for s in segments],
        "cost_stats": [s.to_dict() for s in seg_stats],
        "duplicates": {str(sid): _short_dup(r) for sid, r in dup.items()},
        "wasted_reads": {str(sid): _short_wasted(r) for sid, r in wasted.items()},
        "multi_writes": {str(sid): _short_undo(r) for sid, r in undo.items()},
        "stuck": {str(sid): _short_stuck(r) for sid, r in stuck.items()},
        "worst_turn": worst_out,
        "judgment": judgment.to_dict(),
    }


# ============================================================================
# s9 . markdown rendering (final human-readable report)
# ============================================================================
def _cmd_preview(tree: Optional[RunTree], ref: str, cap: int = 260) -> str:
    """Return a one-line command / target preview for a ref, or empty."""
    if tree is None:
        return ""
    a = tree.by_ref(ref)
    if a is None:
        return ""
    txt = (a.get("command") or a.get("path") or a.get("pattern")
           or (a.get("args") or {}).get("__tool_use_purpose") or "")
    txt = str(txt).replace("\n", " ⏎ ").strip()
    if len(txt) > cap:
        txt = txt[:cap - 1] + "…"
    return txt


def _theme_of(report: dict, seg_id: int) -> tuple[str, str]:
    """Return (range, theme) for a segment id, or ('', '') if unknown."""
    for s in report.get("cost_stats", []):
        if s.get("seg") == seg_id:
            return s.get("range", ""), s.get("theme", "")
    return "", ""


def render_report_markdown(report: dict, tree: Optional[RunTree] = None) -> str:
    """Render the final efficiency report as Markdown.

    Layout:
      §1 verdict / §2 hotspots       — narrative prose
      §3 s8 audit & recommendations  — LLM-generated findings up front
      §4 per-segment overview        — grade + reason + metrics
      §5 duplicates / §6 read log /
      §7 multi-writes / §8 stuck     — structured evidence with commands
      §9 worst turn                  — one-liner
    """
    L: list[str] = []
    verdict = report.get("verdict", "?")
    narr = report.get("narrative") or {}
    totals = report.get("totals") or {}
    judgment = report.get("judgment") or {}

    # ---- header ----
    L.append(f"# Efficiency Evaluation Report")
    L.append("")
    L.append(f"**Final grade: `{verdict}`**")
    L.append("")

    # ---- 1. overview (narrative) ----
    L.append("## 1. Run Overview")
    L.append("")
    if narr.get("overview"):
        L.append(narr["overview"].strip())
    else:
        L.append(f"{len(report.get('segments') or [])} segments, "
                 f"credits {totals.get('credits', 0):.1f}, "
                 f"cycles {totals.get('cycles', 0)}, "
                 f"wall {int(totals.get('wall_s') or 0)}s, "
                 f"net work {int(totals.get('net_s') or 0)}s.")
    L.append("")

    # ---- 2. hotspots (narrative) ----
    L.append("## 2. Cost Hotspots")
    L.append("")
    if narr.get("hotspots"):
        L.append(narr["hotspots"].strip())
    else:
        wt = report.get("worst_turn") or {}
        if wt:
            L.append(f"Most expensive single turn is turn {wt.get('turn')}, "
                     f"{wt.get('credits', 0):.1f} credits / "
                     f"{wt.get('cycles')} cycles / {wt.get('dur_s', 0):.0f}s, "
                     f"{wt.get('share_of_total_credits_pct', 0):.1f}% of total.")
    L.append("")

    # ---- 3. s8 audit & recommendations (promoted from tail) ----
    per_seg = judgment.get("per_segment") or []
    seg_by_id = {s.get("seg"): s for s in per_seg}
    L.append("## 3. Audit & Recommendations")
    L.append("")
    L.append(f"**Global grade: `{judgment.get('global_grade', '-')}`**")
    L.append("")
    gr = judgment.get("global_reason") or ""
    if gr:
        L.append(f"> {gr}")
        L.append("")
    for g in per_seg:
        theme = g.get("theme") or ""
        rng = g.get("range") or ""
        L.append(f"### [{rng}] {theme} — `{g.get('grade')}`")
        L.append("")
        if g.get("reason"):
            L.append(f"> {g['reason']}")
            L.append("")
        for i, so in enumerate(g.get("suspicious_ops") or [], 1):
            L.append(f"**⚠ Suspicious operation {i}:** {so}")
            L.append("")

    recs = judgment.get("recommendations") or []
    if recs:
        L.append("### Overall Recommendations")
        L.append("")
        for i, r in enumerate(recs, 1):
            L.append(f"{i}. {r}")
        L.append("")

    # ---- 4. per-segment cost breakdown ----
    L.append("## 4. Per-Segment Overview")
    L.append("")
    for cs in report.get("cost_stats", []):
        sid = cs.get("seg")
        theme = cs.get("theme") or "(no theme)"
        rng = cs.get("range") or ""
        grade_info = seg_by_id.get(sid, {})
        grade = grade_info.get("grade") or "-"
        reason = grade_info.get("reason") or ""
        L.append(f"**[{rng}] {theme}** — grade `{grade}`  "
                 f"· `credits={cs.get('credits', 0):.1f}` "
                 f"`cycles={cs.get('cycles', 0)}` "
                 f"`turns={cs.get('n_turns', 0)}` "
                 f"`dur={cs.get('dur_s', 0):.0f}s`")
        if reason:
            L.append("")
            L.append(f"> {reason}")
        L.append("")

    # ---- 5. duplicates ----
    L.append("## 5. Duplicate Action Detection (s4)")
    L.append("")
    dup_map = report.get("duplicates") or {}
    if not dup_map:
        L.append("*(No duplicate actions detected.)*")
        L.append("")
    for sid_str, d in dup_map.items():
        rng, theme = _theme_of(report, int(sid_str))
        L.append(f"### [{rng}] {theme}")
        L.append("")
        L.append(f"- Total commands: **{d.get('n_commands', 0)}**")
        L.append(f"- Duplicate pairs: **{d.get('n_pairs', 0)}**")
        L.append(f"- Duplicate clusters: **{d.get('n_clusters', 0)}**")
        rd = d.get("reason_distribution") or {}
        if rd:
            L.append(f"- Trigger reason distribution: " +
                     ", ".join(f"`{k}={v}`" for k, v in rd.items()))
        L.append("")
        clusters = d.get("top_clusters") or []
        if clusters:
            L.append("**Top clusters** (one sample command per group; not listed individually):")
            L.append("")
            for c in clusters:
                refs = c.get("refs") or []
                sample_ref = refs[0] if refs else ""
                sample_cmd = _cmd_preview(tree, sample_ref, cap=220) if sample_ref else ""
                refs_str = ", ".join(f"`{r}`" for r in refs[:12])
                if len(refs) > 12:
                    refs_str += f", …(+{len(refs) - 12})"
                L.append(f"- **Cluster `size={c.get('size')}`** "
                         f"({c.get('dominant_reason')})")
                if sample_cmd:
                    L.append(f"    - Sample (`{sample_ref}`): `{sample_cmd}`")
                L.append(f"    - Covered refs: {refs_str}")
            L.append("")
        pairs = d.get("top_pairs") or []
        if pairs:
            L.append("**Top pairs:**")
            L.append("")
            for p in pairs:
                sims = p.get("sims") or {}
                sims_str = ", ".join(f"{k}={v:.2f}"
                                     for k, v in sorted(sims.items(),
                                                        key=lambda kv: -kv[1])[:3])
                L.append(f"- `{p.get('ref1')}` ↔ `{p.get('ref2')}` "
                         f"— **{p.get('reason')}** ({sims_str})")
                pa = _cmd_preview(tree, p.get("ref1") or "")
                pb = _cmd_preview(tree, p.get("ref2") or "")
                if pa:
                    L.append(f"    - A: `{pa}`")
                if pb:
                    L.append(f"    - B: `{pb}`")
            L.append("")

    # ---- 6. read log (formerly "wasted reads") ----
    L.append("## 6. Read Log (s5)")
    L.append("")
    wr = report.get("wasted_reads") or {}
    any_wasted = False
    for sid_str, w in wr.items():
        if not w.get("wasted"):
            continue
        any_wasted = True
        rng, theme = _theme_of(report, int(sid_str))
        L.append(f"### [{rng}] {theme}")
        L.append("")
        L.append(f"- Total files read: **{w.get('n_reads', 0)}**  "
                 f"·  Suspicious: **{w.get('n_wasted', 0)}** "
                 f"({w.get('wasted_pct', 0):.1f}%)")
        L.append("")
        for x in w.get("wasted") or []:
            prev = _cmd_preview(tree, x.get("ref") or "")
            path = x.get("path") or x.get("basename") or ""
            L.append(f"- `{x.get('ref')}` turn {x.get('turn')} — "
                     f"`{path}` (max_sim={x.get('max_semantic_sim', 0):.2f}, "
                     f"reason={x.get('reason')})")
            if prev and prev != path:
                L.append(f"    - Context: `{prev}`")
        L.append("")
    if not any_wasted:
        L.append("*(No suspicious reads detected.)*")
        L.append("")

    # ---- 7. multi-writes ----
    L.append("## 7. Multiple Writes to Same Path (s6)")
    L.append("")
    mw = report.get("multi_writes") or {}
    any_mw = False
    for sid_str, u in mw.items():
        if not u.get("multi_write"):
            continue
        any_mw = True
        rng, theme = _theme_of(report, int(sid_str))
        L.append(f"### [{rng}] {theme}")
        L.append("")
        L.append(f"- Total writes: **{u.get('n_writes', 0)}**  ·  "
                 f"Duplicate paths: **{u.get('n_multi', 0)}**")
        L.append("")
        L.append("| count | turn span | density | single-turn thrash | path |")
        L.append("|---:|:---:|---:|:---:|:---|")
        for m in u.get("multi_write") or []:
            marker = "⚠" if m.get("is_single_turn") else (
                "★" if m.get("density", 0) > 0.5 else "")
            L.append(f"| {m.get('count')} | "
                     f"{m.get('first_turn')}–{m.get('last_turn')} "
                     f"(span={m.get('turn_span')}) | "
                     f"{m.get('density', 0):.2f} | {marker} | "
                     f"`{m.get('path')}` |")
        L.append("")
    if not any_mw:
        L.append("*(No multiple writes to the same path detected.)*")
        L.append("")

    # ---- 8. stuck ----
    L.append("## 8. Stuck Signals (s7)")
    L.append("")
    st = report.get("stuck") or {}
    any_stuck = False
    for sid_str, s in st.items():
        stuck_turns = s.get("stuck_turns") or []
        fails = s.get("consecutive_failures") or []
        if not stuck_turns and not fails:
            continue
        any_stuck = True
        rng, theme = _theme_of(report, int(sid_str))
        L.append(f"### [{rng}] {theme}")
        L.append("")
        if stuck_turns:
            L.append("| turn | cycles | dur_s | credits |")
            L.append("|---:|---:|---:|---:|")
            for x in stuck_turns:
                L.append(f"| {x.get('turn')} | {x.get('cycles')} | "
                         f"{x.get('dur_s', 0):.0f} | "
                         f"{x.get('credits', 0):.1f} |")
            L.append("")
        if fails:
            L.append("**Consecutive failure groups:**")
            for fg in fails:
                refs = fg.get("refs") or []
                L.append(f"- length={fg.get('length')}: "
                         + " -> ".join(f"`{r}`" for r in refs[:8]))
            L.append("")
    if not any_stuck:
        L.append("*(No high-cycle turns or consecutive failures detected.)*")
        L.append("")

    # ---- 9. worst turn (was §8) ----
    L.append("## 9. Most Expensive Single Turn")
    L.append("")
    wt = report.get("worst_turn") or {}
    if wt:
        L.append(f"- turn **{wt.get('turn')}** — "
                 f"credits **{wt.get('credits', 0):.1f}** "
                 f"({wt.get('share_of_total_credits_pct', 0):.1f}% of total),"
                 f" cycles **{wt.get('cycles')}**,"
                 f" dur **{wt.get('dur_s', 0):.0f}s**")
    else:
        L.append("*(No turn metadata.)*")
    L.append("")

    return "\n".join(L)
