"""Data structures produced by the efficiency workflow.

Extracted into a separate module to avoid circular imports: s1/s2/... outputs
use dataclasses defined here; downstream steps consume them.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


# ---------------------------------------------------------------------------
# s2 output: task segments
# ---------------------------------------------------------------------------
@dataclass
class Segment:
    """A task segment. `[start, end]` is an inclusive turn range."""
    seg: int
    start: int
    end: int
    theme: str                          # first 60 chars of the anchor prompt (for review)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s3 output: per-segment cost/time ledger
# ---------------------------------------------------------------------------
@dataclass
class SegmentStats:
    seg: int
    range: str                          # e.g. "1-64"
    n_turns: int
    theme: str
    dur_s: float
    credits: float
    cycles: int
    llm_reqs: int
    ctx_pct_max: float
    credits_per_turn: float             # primary signal used by s8 grading
    cycles_per_turn: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s4 output: duplicate detection
# ---------------------------------------------------------------------------
@dataclass
class DuplicatePair:
    """Two actions judged semantically similar."""
    ref1: str
    ref2: str
    reason: str                         # e.g. "command_strong=0.923"
    sims: dict                          # {dim: cosine_sim} for the dims that voted

    def to_dict(self) -> dict[str, Any]:
        return {"ref1": self.ref1, "ref2": self.ref2,
                "reason": self.reason, "sims": self.sims}


@dataclass
class DuplicateCluster:
    """Connected component built from DuplicatePair edges. Larger clusters
    indicate more concentrated repetition.
    """
    refs: list[str]                     # >= 2 members
    size: int = 0
    dominant_reason: str = ""           # most common judge reason within the cluster

    def __post_init__(self):
        if not self.size:
            self.size = len(self.refs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s5 output: read that was never consumed
# ---------------------------------------------------------------------------
@dataclass
class WastedRead:
    ref: str
    turn: int
    path: str
    basename: str
    reason: str                         # "no_reference" | "no_reference_no_semantic"
    max_semantic_sim: float = 0.0       # best cosine similarity found in window

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s6 output: same path written multiple times
# ---------------------------------------------------------------------------
@dataclass
class MultiWriteFile:
    path: str
    count: int
    refs: list[str]                     # ordered refs of the writes
    turn_span: int = 0                  # last_turn - first_turn + 1
    density: float = 0.0                # count / turn_span (writes per turn)
    is_single_turn: bool = False        # all writes in the same turn (strongest signal)
    first_turn: int = 0
    last_turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s7 output: turns showing signs of the agent being stuck
# ---------------------------------------------------------------------------
@dataclass
class StuckTurn:
    turn: int
    cycles: int
    dur_s: float
    credits: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FailureGroup:
    """Consecutive blocked/errored actions inside a single segment."""
    refs: list[str]
    length: int = 0

    def __post_init__(self):
        if not self.length:
            self.length = len(self.refs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# s8 output: LLM grading + recommendations
# ---------------------------------------------------------------------------
@dataclass
class SegmentGrade:
    """One segment's audit result.

    Field order intentionally puts human-readable identifiers (range,
    theme) first; the numeric ``seg`` id is metadata and appears last.
    """
    range: str = ""                     # turn range, e.g. "1-3"
    theme: str = ""                     # human-readable segment name (from s2)
    grade: str = ""                     # A / B / C / D
    reason: str = ""
    suspicious_ops: list[str] = None    # per-segment flagged operations (may be empty)
    seg: int = 0                        # numeric seg id (metadata, for traceability)

    def __post_init__(self):
        if self.suspicious_ops is None:
            self.suspicious_ops = []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Judgment:
    global_grade: str                   # A / B / C / D
    global_reason: str
    per_segment: list[SegmentGrade]
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_grade": self.global_grade,
            "global_reason": self.global_reason,
            "per_segment": [g.to_dict() for g in self.per_segment],
            "recommendations": list(self.recommendations),
        }
