"""Semantic similarity + duplicate detection over multi-dim action embeddings.

Design:
- Per-dim voting: cosine similarity is computed independently for command /
  purpose / reasoning / response.
- Judgment: any single "strong" signal triggers a positive; otherwise
  combined-dim rules may still fire.
- Only dimensions present on both sides are compared (a missing dim doesn't
  vote either way).
"""
from __future__ import annotations

from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Dimensions considered in the judgment.
#   path is excluded: weak semantic signal.
#   error is excluded: "both failed" doesn't mean "same task".
# ---------------------------------------------------------------------------
_JUDGE_DIMS = ("command", "purpose", "reasoning", "response")

# Default thresholds (CLI-tunable).
DEFAULT_THRESHOLDS = {
    "command_strong":   0.90,   # commands are nearly identical
    "purpose_strong":   0.85,   # doing the same thing
    "reasoning_strong": 0.85,   # thinking is nearly identical
    "combined_min":     0.80,   # per-dim floor for combined-signal rules
}


def cosine(v1: np.ndarray, v2: np.ndarray) -> float:
    """Standard cosine similarity."""
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return float(np.dot(v1, v2) / (n1 * n2))


def similarity_map(entry1: dict, entry2: dict) -> dict[str, float]:
    """Cosine similarity for every dim present on both sides. Returns {dim: sim}."""
    sims = {}
    v1 = entry1.get("vectors", {})
    v2 = entry2.get("vectors", {})
    for dim in _JUDGE_DIMS:
        a = v1.get(dim)
        b = v2.get(dim)
        if a is not None and b is not None:
            sims[dim] = cosine(a, b)
    return sims


def judge_similar(entry1: dict, entry2: dict,
                  thresholds: Optional[dict] = None,
                  sibling_map: Optional[dict] = None,
                  ref1: Optional[str] = None,
                  ref2: Optional[str] = None) -> tuple[bool, str, dict]:
    """Judge whether two actions are semantically duplicate.

    Returns (is_similar, reason, sims_dict).

    Sibling filter: multiple toolUses inside the same AssistantMessage share
    the same thinking, so their `reasoning` field is identical. Those pairs
    are *not* duplicates -- they are one thought producing multiple actions.
    When a sibling_map is provided, sibling pairs return False upfront.

    Rules (multi-dim voting):
        Strong (any single hit -> similar):
          - command similarity  > 0.90
          - purpose similarity  > 0.85
          - reasoning similarity > 0.85
        Combined (both dims above `combined_min`):
          - purpose & reasoning both > 0.80 (thought and intent both align)
          - command & purpose both > 0.80 (command and intent both align)
    """
    # Sibling filter first: skip actions from the same message.
    if sibling_map and ref1 and ref2:
        if ref2 in sibling_map.get(ref1, set()):
            return False, "sibling(same_thinking)", {}

    thr = thresholds or DEFAULT_THRESHOLDS
    sims = similarity_map(entry1, entry2)

    # Strong signals.
    if sims.get("command", 0.0) > thr["command_strong"]:
        return True, f"command_strong={sims['command']:.3f}", sims
    if sims.get("purpose", 0.0) > thr["purpose_strong"]:
        return True, f"purpose_strong={sims['purpose']:.3f}", sims
    if sims.get("reasoning", 0.0) > thr["reasoning_strong"]:
        return True, f"reasoning_strong={sims['reasoning']:.3f}", sims

    # Combined signals.
    tm = thr["combined_min"]
    if (sims.get("purpose", 0.0) > tm and sims.get("reasoning", 0.0) > tm):
        return True, (f"combined_pr(purpose={sims['purpose']:.3f}, "
                      f"reasoning={sims['reasoning']:.3f})"), sims
    if (sims.get("command", 0.0) > tm and sims.get("purpose", 0.0) > tm):
        return True, (f"combined_cp(command={sims['command']:.3f}, "
                      f"purpose={sims['purpose']:.3f})"), sims

    return False, "", sims


def build_sibling_map(thinkings: list) -> dict[str, set]:
    """Build a sibling map from ir.thinkings::

        {ref: {other refs sharing the same AssistantMessage thinking}}

    Multiple toolUses in a single AssistantMessage share the same
    ``msg_thinking``, so their reasoning field is identical. That is normal
    fan-out from one thought, not duplicate work.

    The caller must have already resolved Thinking.action_refs from
    integer idx to ref strings before passing them in.
    """
    sib: dict[str, set] = {}
    for t in thinkings:
        refs = getattr(t, "action_refs", None) or []
        if len(refs) < 2:
            continue
        s = set(refs)
        for r in refs:
            sib.setdefault(r, set()).update(s - {r})
    return sib


def find_duplicate_pairs(cache: dict, refs: list[str],
                         thresholds: Optional[dict] = None,
                         sibling_map: Optional[dict] = None) -> list[dict]:
    """Pairwise scan; return every pair judged similar.

    O(N^2) but each segment stays under ~500 entries so this is fine.

    Returns::

        [{"ref1", "ref2", "reason", "sims"}, ...]
    """
    entries = cache["entries"]
    out = []
    for i, r1 in enumerate(refs):
        e1 = entries.get(r1)
        if not e1:
            continue
        for r2 in refs[i + 1:]:
            e2 = entries.get(r2)
            if not e2:
                continue
            is_sim, reason, sims = judge_similar(
                e1, e2, thresholds, sibling_map=sibling_map,
                ref1=r1, ref2=r2)
            if is_sim:
                out.append({"ref1": r1, "ref2": r2,
                            "reason": reason, "sims": sims})
    return out


def cluster_from_pairs(pairs: list[dict]) -> list[list[str]]:
    """Union-find over pair edges. Returns connected components as sorted lists."""
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for p in pairs:
        r1, r2 = p["ref1"], p["ref2"]
        parent.setdefault(r1, r1)
        parent.setdefault(r2, r2)
        union(r1, r2)

    groups = {}
    for x in parent:
        r = find(x)
        groups.setdefault(r, []).append(x)
    return [sorted(v) for v in groups.values() if len(v) >= 2]
