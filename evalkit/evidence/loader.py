"""Load the run tree (parent session + recursive child sessions), rebuild time window.

Why a tree rather than a single session: in practice the aaaa1111 orchestrator run
dispatched 19 times, all to its own eval-* subagents; the only root-session action
that touched the agent-under-test was a ping probe. The real live invocations were
inside 6+ child sessions running *_acp.py scripts. A single-session view produces
false MISS on the most important requirements. See DESIGN.md P7.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from normalize.official import default_official_dir
from normalize.official_loader import load_trace_from_official

_PARENT_RE = re.compile(r'"parent_session_id"\s*:\s*"([0-9a-fA-F-]{36})"')
_HEAD_BYTES = 4096          # parent_session_id is a top-level meta field, appears at the start of the file


@dataclass
class SessionNode:
    sid: str
    agent_name: Optional[str]
    parent: Optional[str]
    depth: int
    ir: Any                                   # TraceIR
    time_window: tuple[Optional[dt.datetime], Optional[dt.datetime]]
    created_reason: Optional[str] = None


@dataclass
class RunTree:
    root: str
    nodes: dict[str, SessionNode] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)   # All actions in the tree, with sid / ref
    warnings: list[str] = field(default_factory=list)

    # ---- Views ----
    @property
    def root_node(self) -> SessionNode:
        return self.nodes[self.root]

    @property
    def root_actions(self) -> list[dict]:
        return [a for a in self.actions if a["sid"] == self.root]

    def by_ref(self, ref: str) -> Optional[dict]:
        for a in self.actions:
            if a["ref"] == ref:
                return a
        return None

    @property
    def time_window(self) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
        """Time window of the whole tree = union of every session's time window."""
        los = [w[0] for n in self.nodes.values() if (w := n.time_window)[0]]
        his = [w[1] for n in self.nodes.values() if (w := n.time_window)[1]]
        return (min(los) if los else None, max(his) if his else None)


def _parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(
            s.replace("Z", "+00:00").split(".")[0] + "+00:00")
    except ValueError:
        return None


def session_time_window(sid: str, official_dir: str
                        ) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """Rebuild the run's wall-clock time window.

    In the normalized official source, Action.ts is empty for all actions (measured
    76/76) and duration_s only reflects "net working time" (measured 13.7min while
    the wall-clock span is 40min); summing that up would misclassify every artifact
    as stale. So: take meta.created_at as the lower bound and the session record
    file's last-write time as the upper bound. See DESIGN.md P5 / P6.
    """
    meta_p = os.path.join(official_dir, f"{sid}.json")
    jl_p = os.path.join(official_dir, f"{sid}.jsonl")
    lo = hi = None
    if os.path.isfile(meta_p):
        try:
            lo = _parse_iso(json.load(open(meta_p, encoding="utf-8")).get("created_at"))
        except (OSError, json.JSONDecodeError):
            pass
    for p in (jl_p, meta_p):
        if os.path.isfile(p):
            hi = dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc)
            break
    return lo, hi


def _read_parent(meta_path: str) -> Optional[str]:
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return None
    m = _PARENT_RE.search(head)
    if m:
        return m.group(1)
    if '"parent_session_id"' not in head:      # Not in the header — fall back to full parse
        try:
            return json.load(open(meta_path, encoding="utf-8")).get("parent_session_id")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _child_index(official_dir: str) -> dict[str, list[str]]:
    """Sweep the official records directory once and build parent -> [children] index."""
    idx: dict[str, list[str]] = {}
    if not os.path.isdir(official_dir):
        return idx
    for name in os.listdir(official_dir):
        if not name.endswith(".json") or name.endswith(".jsonl"):
            continue
        sid = name[:-5]
        parent = _read_parent(os.path.join(official_dir, name))
        if parent:
            idx.setdefault(parent, []).append(sid)
    return idx


# ---------------------------------------------------------------------------
# IR pickle cache
# ---------------------------------------------------------------------------
# Normalization takes 3-5 seconds; pickle deserialization takes tens of ms.
# The same session gets loaded many times across workflows and debug reruns,
# so caching is a big win. Caches are keyed by include_responses (wr/nr).
#
# Invalidation: cache stale when .jsonl mtime > cache mtime -> recompute.
# Versioning:   directory holds a vN prefix; schema bumps switch to vN+1 and
#               old caches naturally expire.
_IR_CACHE_VERSION = "v1"
_IR_CACHE_ROOT = os.path.expanduser(f"~/.eval-agent/ir_cache/{_IR_CACHE_VERSION}")


def _cache_path(sid: str, include_responses: bool) -> str:
    suffix = "wr" if include_responses else "nr"
    return os.path.join(_IR_CACHE_ROOT, f"{sid}_{suffix}.pkl")


def _load_ir_cached(sid: str, official_dir: str,
                    include_responses: bool, use_cache: bool):
    """IR loader with a pickle cache. Falls through to a fresh load when the
    cache is stale or use_cache is False."""
    if not use_cache:
        return load_trace_from_official(sid, official_dir=official_dir,
                                        include_responses=include_responses)

    cache_p = _cache_path(sid, include_responses)
    jl_p = os.path.join(official_dir, f"{sid}.jsonl")

    if os.path.isfile(cache_p) and os.path.isfile(jl_p):
        if os.path.getmtime(cache_p) > os.path.getmtime(jl_p):
            try:
                import pickle
                with open(cache_p, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass                                # bad cache: silently fall through to recompute

    ir = load_trace_from_official(sid, official_dir=official_dir,
                                  include_responses=include_responses)
    if ir is None:
        return None
    try:
        import pickle
        os.makedirs(_IR_CACHE_ROOT, exist_ok=True)
        # Write to tmp then rename to avoid truncating an existing cache on
        # a full disk (partial-write scenario).
        tmp = cache_p + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(ir, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache_p)
    except Exception:
        pass                                        # cache write failure must not kill the load
    return ir


def load_run_tree(root_sid: str, official_dir: Optional[str] = None,
                  max_depth: int = 3, include_responses: bool = False,
                  with_children: bool = True, use_cache: bool = True) -> RunTree:
    """Load root_sid and its recursive child sessions, merged into one run tree.

    Every action gets two extra fields:
      sid — the session it belongs to
      ref — "<first 8 of sid>#<idx>", unique across sessions (within a single session
            idx numbers are reused; in practice multiple child sessions have idx=7,
            see DESIGN.md P9).

    use_cache: whether to use the IR pickle cache (default True). Cache location:
               ~/.eval-agent/ir_cache/v1/. Invalidation rule: .jsonl mtime > cache
               mtime -> recompute.
    """
    base = official_dir or default_official_dir()
    tree = RunTree(root=root_sid)
    kids = _child_index(base) if with_children else {}

    frontier = [(root_sid, None, 0)]
    seen: set[str] = set()
    while frontier:
        sid, parent, depth = frontier.pop(0)
        if sid in seen or depth > max_depth:
            continue
        seen.add(sid)
        try:
            ir = _load_ir_cached(sid, base, include_responses, use_cache)
        except Exception as e:                       # A child session with missing records should not sink the whole tree
            tree.warnings.append(f"session {sid[:8]} failed to load: {e}")
            continue
        if ir is None:
            tree.warnings.append(f"session {sid[:8]} has no official record")
            continue
        node = SessionNode(
            sid=sid, agent_name=ir.agent_name, parent=parent, depth=depth, ir=ir,
            time_window=session_time_window(sid, base),
            created_reason=getattr(ir.official, "created_reason", None) if ir.official else None,
        )
        tree.nodes[sid] = node
        for a in ir.actions:
            d = a.to_dict()
            d["sid"] = sid
            d["ref"] = f"{sid[:8]}#{d['idx']}"
            d["depth"] = depth
            tree.actions.append(d)
        for kid in sorted(kids.get(sid, [])):
            frontier.append((kid, sid, depth + 1))

    if root_sid not in tree.nodes:
        raise RuntimeError(f"root session {root_sid} has no usable official record")
    return tree
