"""Flag-style annotation for exceptional steps.

The schema is a single tag per step plus an optional free-text comment.
Most steps are *not* labelled -- only clearly-exceptional ones are. The export
is a flat list of flagged steps that an agent-improvement loop can consume
directly: "here are the four steps that went wrong, here is why."

This replaces an earlier dense-matrix design (one answer per question per
step). The paper (Annotrace, Huang et al. 2026) says the schema is user-defined
and ships no taxonomy; this file ships one *starter* set adapted for the
"optimise an agent" use case the user cares about. Change `STARTER_TAGS` to
match a different workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Tag catalogue -- the four flags a user can attach to a step
# ---------------------------------------------------------------------------
STARTER_TAGS: tuple[str, ...] = (
    "Standout",       # exceeded expectation; worth using as a positive example
    "Violation",      # broke the agent's declared rules / skills / pipeline
    "Duplication",    # repeated work that could have been skipped
    "Mistake",        # misjudgment (wrong tool / wrong file / wrong assumption)
)


HOUSE_RULE = (
    "Flag only when a step is clearly exceptional. "
    "Leave the vast majority of steps unlabelled -- the export is meant to "
    "surface outliers for agent tuning, not characterise every action."
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Flag:
    """One flag on one activity, optionally with a comment."""
    activity_ref: str                       # "a1b2c3d4-xxxx#1"
    tag: str                                # one of STARTER_TAGS
    note: str = ""                          # free text, optional
    answerer: str = "user"                  # "user" | "model:<name>" (Phase 3)
    timestamp: str = ""                     # ISO-8601, filled by the UI


@dataclass(frozen=True)
class TagSet:
    """A named set of tags. Parallels Annotrace's `Codebook` but simpler --
    one dimension, no sub-questions. Phase 2 will let a user edit this; Phase
    3 will let an LLM propose it from sample traces."""
    id: str                                 # "default-v1"
    description: str
    tags: tuple[str, ...]
    house_rule: str = ""

    def has(self, tag: str) -> bool:
        return tag in self.tags


STARTER_TAGSET = TagSet(
    id="default-v1",
    description="Flag exceptional steps for later agent tuning.",
    tags=STARTER_TAGS,
    house_rule=HOUSE_RULE,
)


# ---------------------------------------------------------------------------
# Serialisation (for embedding in the HTML page)
# ---------------------------------------------------------------------------
def tagset_to_dict(ts: TagSet) -> dict:
    return {
        "id": ts.id,
        "description": ts.description,
        "tags": list(ts.tags),
        "house_rule": ts.house_rule,
    }


# ---------------------------------------------------------------------------
# Legacy shim: the previous dense-matrix schema is deprecated but kept as a
# thin alias so any Python-level caller does not break mid-migration. The CLI
# and `html.py` only use `STARTER_TAGSET` now.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Question:                 # deprecated
    id: str
    prompt: str
    labels: tuple[str, ...]
    applies_to: tuple[str, ...] = ()
    house_rules: str = ""


@dataclass(frozen=True)
class Codebook:                 # deprecated
    id: str
    description: str
    questions: tuple[Question, ...] = ()

    def question(self, qid: str) -> Optional[Question]:
        return next((q for q in self.questions if q.id == qid), None)


# Preserved for any older import; new code should use STARTER_TAGSET.
STARTER_CODEBOOK = Codebook(
    id="default-v1",
    description=STARTER_TAGSET.description,
    questions=(
        Question(
            id="flag",
            prompt="Flag this step?",
            labels=STARTER_TAGS,
            house_rules=HOUSE_RULE,
        ),
    ),
)


def codebook_to_dict(cb: Codebook) -> dict:          # deprecated
    return {
        "id": cb.id,
        "description": cb.description,
        "questions": [
            {"id": q.id, "prompt": q.prompt,
             "labels": list(q.labels), "applies_to": list(q.applies_to),
             "house_rules": q.house_rules}
            for q in cb.questions
        ],
    }
