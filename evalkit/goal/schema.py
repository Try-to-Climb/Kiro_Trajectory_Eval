"""Verdict-layer data structures for goal.

We do not shoehorn into evalkit's CheckResult: goal_completion is inherently a
tri-state judgment with evidence tiers + residual + overclaim, and squeezing it
into passed/score/severity loses information. Convert at the last mile if you
need to merge reports (see to_check_result).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Enums (validated in code — do not trust the LLM to obey)
# ---------------------------------------------------------------------------
INTENTS = ("request", "question", "clarification", "cancellation")
VERIFIABLE_BY = ("action", "artifact", "outcome")
STATUSES = ("active", "superseded", "cancelled")
STRENGTHS = ("strong", "weak")
CLAIM_KINDS = ("did", "produced", "verified", "declined")
SATISFIED = ("true", "false", "unverifiable")

# Evidence tier → confidence ceiling (DESIGN.md optimization item 2)
EVIDENCE_TIERS = {
    "direct":        1.00,   # Structured action field directly proves it
    "artifact":      0.95,   # Artifact on disk with mtime inside the run time window
    "derived":       0.85,   # Write/call inferred from command text
    "retrieved":     0.75,   # Retrieval candidate confirmed by the LLM
    "cross_session": 0.70,   # Evidence lives in a child session, attributed via dispatch
    "testimonial":   0.30,   # Only agent's self-claim, no action/artifact backing
    "none":          0.00,
}

# Tri-state for step 5
SEARCH_STATUS = ("hard", "candidates", "absent")


@dataclass
class Requirement:
    """A single atomic requirement extracted by s2."""
    id: str
    text: str
    origin_turn: int
    verifiable_by: str
    status: str
    strength: str
    expect: str
    synthetic: bool = False          # Synthesized negative sample (built-in control group); must be judged false

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Claim:
    """A single self-claim extracted by s3."""
    id: str
    turn: int
    kind: str
    text: str
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Criterion:
    """s4 output: hard check + retrieval anchors + residual not mechanizable."""
    req_id: str
    hard_check: dict[str, Any] = field(default_factory=dict)
    anchors: list[str] = field(default_factory=list)
    residual: Optional[str] = None
    residual_needs: list[str] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    """s5 output: tri-state + evidence. No verdict."""
    req_id: str
    status: str                                     # hard / candidates / absent
    tier: str = "none"                              # Evidence tier of the hit
    hard_hits: list[dict] = field(default_factory=list)     # Structured hits (with ref)
    candidates: list[dict] = field(default_factory=list)    # Retrieval candidates top-K
    anchor_df: dict[str, int] = field(default_factory=dict)  # Per-anchor document frequency across the whole tree
    absent_reason: Optional[str] = None             # suspect_anchors / no_scoring_action
    evidence_before_request: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """s8 verdict."""
    req_id: str
    satisfied: str                                  # true / false / unverifiable
    tier: str
    confidence: float
    evidence_actions: list[str] = field(default_factory=list)   # ref: "<sid8>#<idx>"
    evidence_files: list[dict] = field(default_factory=list)
    overclaim: bool = False
    reason: str = ""
    residual: Optional[str] = None
    synthetic: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d

    # ---- Adapter for merging reports with evalkit (verdicts stay separate; align only at output) ----
    def to_check_result(self, severity: str = "required") -> dict[str, Any]:
        return {
            "checkpoint_id": self.req_id,
            "checker": "GoalCompletion",
            "severity": severity,
            "passed": self.satisfied == "true",
            "score": 1.0 if self.satisfied == "true" else 0.0,
            "reason": self.reason[:400],
            "evidence": [int(r.split("#")[-1]) for r in self.evidence_actions
                         if "#" in r and r.split("#")[-1].isdigit()],
            "confidence": round(self.confidence, 3),
        }


def cap_confidence(tier: str, base: float, has_residual: bool = False) -> float:
    """Derive confidence from the evidence tier; do not let the LLM make one up."""
    ceiling = EVIDENCE_TIERS.get(tier, 0.0)
    val = min(max(base, 0.0), ceiling)
    if has_residual:
        val *= 0.9
    return round(val, 3)
