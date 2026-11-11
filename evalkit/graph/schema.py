"""Provenance graph data model.

A trajectory becomes a bipartite graph following AgentTrails / W3C PROV:

    G = (A ∪ E, R),   R ⊆ (A × E) ∪ (E × A)

`A` holds activities (one per tool call) and `E` holds entities (data
artifacts: files, logs, PIDs, URLs, returned values). Edges only ever run
between the two sets -- never activity→activity or entity→entity. See
DESIGN.md §2.1.

Two invariants make the graph readable, and both are enforced here rather
than left to callers:

**Bipartite.** `add_edge` rejects same-kind endpoints outright. Without this
the graph degenerates into a call chain and stops carrying dataflow.

**Acyclic via time.** Every edge must run from a lower `idx` to a higher one
(DESIGN.md §2.2: `e_j → a_i` requires `j < i`). Time is not a separate axis
laid over the graph -- it is where edge direction comes from, which is why no
cycle can be constructed. Agent retry loops therefore surface as repeated
activities rather than cycles.

Entities are versioned: an activity that modifies a file does not mutate a
node, it *generates a new entity*. One file becomes a chain
`f@v0 → f@v1 → f@v2`, so a read/modify interleaving is preserved as topology.
Task 2 builds those chains; this module only defines the shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# Edge kinds -- PROV vocabulary, kept verbatim so the mapping to the paper and
# to W3C PROV stays obvious.
# ---------------------------------------------------------------------------
GENERATED_BY = "generatedBy"      # activity → its output.  Always emitted, certain.
USED_BY = "usedBy"                # entity → a later activity that consumes it.
CONNECTED_BY = "connectedBy"      # host entity → activity that contacted it.
                                   # Separate from `usedBy` so a reader can ask
                                   # "did this run touch an external system"
                                   # without having to list file paths. The host
                                   # is at `born_idx = -1`: a host existed before
                                   # the run and the time-order check accepts
                                   # the edge for every activity that connects.
INFORMED_BY = "informedBy"        # weak association; a candidate, not an assertion.

EDGE_KINDS = (GENERATED_BY, USED_BY, CONNECTED_BY, INFORMED_BY)

# ---------------------------------------------------------------------------
# Confidence -- mirrors the evidence grading already used elsewhere in the
# repo (goal's strong/stale, efficiency's strong/combined signals). Keeping
# exact, inferred and guessed edges distinguishable is what makes the graph
# auditable: an analysis can ask for `certain` only and get a defensible
# answer.
# ---------------------------------------------------------------------------
CERTAIN = "certain"               # from a structured field (Action.path)
LIKELY = "likely"                 # inferred from command text / response
WEAK = "weak"                     # semantic or pattern match; candidate only

CONFIDENCE_LEVELS = (CERTAIN, LIKELY, WEAK)
_CONFIDENCE_RANK = {CERTAIN: 0, LIKELY: 1, WEAK: 2}


@dataclass(frozen=True)
class Activity:
    """One tool call.

    Wraps the fields of a normalized Action that matter for reading a graph.
    `purpose` is the label: it covers 277/277 actions in the reference session
    and is a single sentence. `reasoning` covers only 70/277 and runs ~683
    chars, so it is detail-on-demand, not a label (DESIGN.md §3).
    """
    ref: str                      # "<sid8>#<idx>", unique across sessions
    idx: int                      # position within its session; orders edges
    turn: int
    action: str                   # read_file / run_command / modify_file / ...
    purpose: Optional[str] = None
    reasoning: Optional[str] = None
    # The literal fact behind the label. `purpose` is the agent's summary of
    # its intent; the command is what actually ran, and it is the evidence
    # every `likely` edge was derived from -- so reading an edge critically
    # requires it.
    command: Optional[str] = None
    path: Optional[str] = None
    # Wall-clock milliseconds this one tool call took, from the official
    # record. Summed per turn (and across all turns, for the overview) to
    # show "where the time went" without having to open each action.
    duration_ms: Optional[int] = None
    # What a probe action looked for -- `grep`, `find`, `ls`, LSP symbol
    # queries. A `dict` with any of `pattern`, `path`, `root`. Deliberately
    # not an edge: a probe that misses touches nothing, so claiming it did
    # would be a fabrication. The field is here so a reader of the picture
    # can still see what the probe was aimed at (which is often why the
    # action is on the graph at all) without crawling the raw session.
    probe: Optional[dict[str, Any]] = None

    @property
    def node_id(self) -> str:
        return f"a:{self.ref}"

    def label(self, cap: int = 60) -> str:
        text = self.purpose or self.action
        return text if len(text) <= cap else text[: cap - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Entity:
    """One version of a data artifact.

    `key` identifies the artifact (path, filename, URL, PID, ...); `version`
    distinguishes successive states of it. `producer_ref` is the activity that
    generated this version, or None for `@v0` -- pre-existing input that the
    run only ever read.
    """
    key: str
    version: int = 0
    kind: str = "file"            # file / log / pid / url / dir / value / host
    producer_ref: Optional[str] = None
    born_idx: int = -1            # idx of the producing activity; -1 = pre-existing

    @property
    def node_id(self) -> str:
        return f"e:{self.key}@v{self.version}"

    def label(self, cap: int = 40) -> str:
        if self.kind == "host":
            # `ssh://user@h` is a key, not something a reader wants in a card.
            # The scheme carries the protocol (ssh / docker / k8s) and the rest
            # is the endpoint identity; both are kept, separated for the eye.
            scheme, _, endpoint = self.key.partition("://")
            tag = scheme or "host"
            base = endpoint or self.key
            if len(base) > cap:
                base = "…" + base[-(cap - 1):]
            return f"{tag}: {base}"
        base = self.key.rsplit("/", 1)[-1] or self.key
        if len(base) > cap:
            base = "…" + base[-(cap - 1):]
        if self.kind == "pid":
            return f"PID {base}"          # a bare number reads as nothing
        return f"{base}@v{self.version}" if self.version else base

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Edge:
    """A provenance relation.

    `evidence` records *why* this edge exists -- which path field, which
    literal in which command, which value from which response. An edge whose
    provenance cannot be stated is not auditable, so this is required rather
    than optional.
    """
    src: str                      # node_id
    dst: str                      # node_id
    kind: str                     # one of EDGE_KINDS
    confidence: str               # one of CONFIDENCE_LEVELS
    evidence: str                 # human-readable justification

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphError(ValueError):
    """Raised when an operation would break a graph invariant."""


@dataclass
class ProvGraph:
    """A provenance graph for one run.

    Nodes are keyed by `node_id` (`a:<ref>` / `e:<key>@v<n>`), so adding the
    same node twice is idempotent and edges can be created by id alone.
    """
    session_id: str = ""
    activities: dict[str, Activity] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- construction ----------------------------------------------------
    def add_activity(self, a: Activity) -> str:
        self.activities[a.node_id] = a
        return a.node_id

    def add_entity(self, e: Entity) -> str:
        self.entities[e.node_id] = e
        return e.node_id

    def add_edge(self, src: str, dst: str, kind: str, confidence: str,
                 evidence: str) -> Edge:
        """Add an edge, enforcing every invariant at the entry point.

        Rejects, in order: unknown kind or confidence level, unknown endpoint,
        same-kind endpoints (bipartiteness), wrong direction for the kind,
        backwards-in-time direction, and an edge whose reverse already exists
        (2-cycle). Enforcing here rather than trusting callers is deliberate:
        a silently violated invariant makes every downstream analysis
        meaningless.
        """
        if kind not in EDGE_KINDS:
            raise GraphError(f"unknown edge kind {kind!r}")
        if confidence not in CONFIDENCE_LEVELS:
            raise GraphError(f"unknown confidence {confidence!r}")

        src_is_a, dst_is_a = self._is_activity(src), self._is_activity(dst)
        if src_is_a is None:
            raise GraphError(f"unknown src node {src!r}")
        if dst_is_a is None:
            raise GraphError(f"unknown dst node {dst!r}")
        if src_is_a == dst_is_a:
            side = "activity" if src_is_a else "entity"
            raise GraphError(
                f"edge would join two {side} nodes ({src} → {dst}); "
                f"the graph must stay bipartite")

        # generatedBy runs activity → entity, usedBy runs entity → activity.
        if kind == GENERATED_BY and not src_is_a:
            raise GraphError(f"{GENERATED_BY} must run activity → entity")
        if kind == USED_BY and src_is_a:
            raise GraphError(f"{USED_BY} must run entity → activity")
        if kind == CONNECTED_BY and src_is_a:
            raise GraphError(f"{CONNECTED_BY} must run entity → activity")

        self._check_time_order(src, dst)
        self._check_no_reverse(src, dst)

        e = Edge(src=src, dst=dst, kind=kind, confidence=confidence,
                 evidence=evidence)
        self.edges.append(e)
        return e

    # ---- invariants ------------------------------------------------------
    def _is_activity(self, node_id: str) -> Optional[bool]:
        if node_id in self.activities:
            return True
        if node_id in self.entities:
            return False
        return None

    def _idx_of(self, node_id: str) -> int:
        """Position of a node on the time axis.

        An entity sits at the idx of the activity that produced it; a
        pre-existing entity (`born_idx == -1`) sits before everything.
        """
        if node_id in self.activities:
            return self.activities[node_id].idx
        return self.entities[node_id].born_idx

    def _check_time_order(self, src: str, dst: str) -> None:
        lo, hi = self._idx_of(src), self._idx_of(dst)
        if lo < 0:                       # pre-existing input, always earliest
            return
        if hi < lo:
            raise GraphError(
                f"edge {src} → {dst} runs backwards in time "
                f"(idx {lo} → {hi}); DESIGN.md §2.2 requires j < i")

    def _check_no_reverse(self, src: str, dst: str) -> None:
        """Reject an edge whose reverse already exists.

        Time ordering alone does not forbid a 2-cycle: an activity and the
        entity it generates share an idx, so `a → e` and `e → a` are both
        "in order". That pair is nonsense as provenance -- an activity cannot
        consume what it produced -- and it would break any downstream
        reachability analysis (Task 9), so it is rejected structurally rather
        than left to callers to avoid.
        """
        for e in self.edges:
            if e.src == dst and e.dst == src:
                raise GraphError(
                    f"edge {src} → {dst} reverses an existing edge "
                    f"({e.src} → {e.dst}); that would form a 2-cycle")

    def assert_bipartite(self) -> None:
        """Re-check the invariants over the whole graph.

        `add_edge` already enforces these, so a failure here means something
        mutated the graph directly. Cheap enough to call in tests and after a
        build.
        """
        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            s, d = self._is_activity(e.src), self._is_activity(e.dst)
            if s is None or d is None:
                raise GraphError(f"edge references unknown node: {e}")
            if s == d:
                raise GraphError(f"edge joins two same-kind nodes: {e}")
            self._check_time_order(e.src, e.dst)
            if (e.dst, e.src) in seen:
                raise GraphError(f"graph contains a 2-cycle: {e}")
            seen.add((e.src, e.dst))

    # ---- queries ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self.activities) + len(self.entities)

    @property
    def n_activities(self) -> int:
        return len(self.activities)

    @property
    def n_entities(self) -> int:
        return len(self.entities)

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def isolated_activities(self) -> list[Activity]:
        """Activities with no edge at all.

        The headline quality metric: a high isolate rate means entity-key
        extraction is missing dependencies (Task 7).
        """
        touched = {e.src for e in self.edges} | {e.dst for e in self.edges}
        return [a for nid, a in self.activities.items() if nid not in touched]

    def versions_of(self, key: str) -> list[Entity]:
        """All versions of one artifact, oldest first."""
        return sorted((e for e in self.entities.values() if e.key == key),
                      key=lambda e: e.version)

    def latest_version(self, key: str, before_idx: Optional[int] = None
                       ) -> Optional[Entity]:
        """Newest version of `key` produced strictly before `before_idx`.

        This is what a read consumes (Task 3). `before_idx=None` means "newest
        overall".
        """
        cands = self.versions_of(key)
        if before_idx is not None:
            cands = [e for e in cands if e.born_idx < before_idx]
        return cands[-1] if cands else None

    def filter_edges(self, min_confidence: str = WEAK,
                     kinds: Optional[tuple[str, ...]] = None) -> list[Edge]:
        """Edges at or above a confidence level, optionally by kind.

        `min_confidence=CERTAIN` yields only structurally derived edges --
        the defensible subgraph.
        """
        if min_confidence not in _CONFIDENCE_RANK:
            raise GraphError(f"unknown confidence {min_confidence!r}")
        cap = _CONFIDENCE_RANK[min_confidence]
        return [e for e in self.edges
                if _CONFIDENCE_RANK[e.confidence] <= cap
                and (kinds is None or e.kind in kinds)]

    def iter_nodes(self) -> Iterator[tuple[str, Any]]:
        yield from self.activities.items()
        yield from self.entities.items()

    def stats(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        for e in self.edges:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            by_conf[e.confidence] = by_conf.get(e.confidence, 0) + 1
        iso = self.isolated_activities()
        return {
            "session_id": self.session_id,
            "activities": self.n_activities,
            "entities": self.n_entities,
            "edges": len(self.edges),
            "edges_by_kind": by_kind,
            "edges_by_confidence": by_conf,
            "isolated_activities": len(iso),
            "isolated_pct": (round(100 * len(iso) / self.n_activities, 1)
                             if self.n_activities else 0.0),
            "warnings": len(self.warnings),
        }
