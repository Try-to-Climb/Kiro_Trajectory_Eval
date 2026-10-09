"""Load the run tree (parent session + recursive child sessions), rebuild time window.

Why a tree rather than a single session: in practice the aaaa1111 orchestrator run
dispatched 19 times, all to its own eval-* subagents; the only root-session action
that touched the agent-under-test was a ping probe. The real live invocations were
inside 6+ child sessions running *_acp.py scripts. A single-session view produces
false MISS on the most important requirements. See DESIGN.md P7.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from normalize.official import default_official_dir
from normalize.official import child_index as normalize_child_index
from normalize.official_loader import load_trace_from_official

_HEAD_BYTES = 4096          # kept for session_time_window's meta-head reads


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


def _child_index(official_dir: str) -> dict[str, list[str]]:
    """parent -> [children] index.

    Parsing the parent/child relation is record-format knowledge and has moved
    into the normalization layer (normalize.official.child_index). This stays
    as a thin delegation so callers of _child_index don't have to change.

    The criterion is `parent_session_id`; **do not** use
    `created_reason=='subagent'` (top-level sessions can also be marked
    subagent). See the comment on normalize.official.child_index for details.
    """
    return normalize_child_index(official_dir)


# ---------------------------------------------------------------------------
# IR pickle cache
# ---------------------------------------------------------------------------
# Normalization takes 3-5 seconds; pickle deserialization takes tens of ms.
# The same session gets loaded many times across workflows and debug reruns,
# so caching is a big win. Caches are keyed by include_responses (wr/nr).
#
# Invalidation: cache stale when .jsonl mtime > cache mtime -> recompute.
# Versioning:   the cache directory name embeds a hash of the pickled dataclass
#               field names *and types*, so adding a field to Action / TurnMeta /
#               OfficialRecord automatically lands in a fresh directory. This
#               replaces a hand-bumped vN: relying on someone remembering to
#               bump it already produced wrong numbers once -- stale caches held
#               OfficialRecords predating `cycles` / `updated_at`, so
#               efficiency's s1 reported cycles=0 and wall_s=0.
_IR_CACHE_VERSION = "v2"


def _schema_fingerprint() -> str:
    """8-hex digest over the field names and types of the pickled dataclasses.

    Types matter, not just names: TurnMeta.duration_s went from int (secs only)
    to float (secs + nanos) without renaming, and a name-only digest let stale
    caches survive, so net_s stayed seconds short.
    """
    import hashlib
    from normalize.schema import Action, Thinking, TraceIR
    from normalize.official import OfficialRecord, TurnMeta
    parts = []
    for cls in (Action, Thinking, TraceIR, OfficialRecord, TurnMeta):
        names = ",".join(f"{f.name}:{f.type}" for f in dataclasses.fields(cls))
        parts.append(f"{cls.__name__}({names})")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]


_IR_CACHE_ROOT = os.path.expanduser(
    f"~/.eval-agent/ir_cache/{_IR_CACHE_VERSION}-{_schema_fingerprint()}")


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
            # sid / ref are filled by the normalization layer (Action.sid /
            # Action.ref). This fallback only exists for old IR pickles cached
            # before those fields were added -- they deserialize without them.
            if not d.get("sid"):
                d["sid"] = sid
            if not d.get("ref"):
                d["ref"] = f"{sid[:8]}#{d['idx']}"
            d["depth"] = depth
            tree.actions.append(d)
        for kid in sorted(kids.get(sid, [])):
            frontier.append((kid, sid, depth + 1))

    if root_sid not in tree.nodes:
        raise RuntimeError(f"root session {root_sid} has no usable official record")
    return tree
