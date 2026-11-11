"""Compose the final prompt sent to Kiro.

Kept separate from the runner so a reviewer can read the exact text we ask
the model to produce without wading through I/O glue, and so a Phase 2 can
swap templates without touching the pipeline.
"""
from __future__ import annotations

from .agent_loader import AgentInfo
from .extractor import FlagContext


# ---------------------------------------------------------------------------
# Tag glossary -- repeated inside the prompt so the model does not have to
# infer meanings from four one-word labels alone.
# ---------------------------------------------------------------------------
TAG_GLOSSARY = {
    "Standout": "exemplary; encode this pattern (positive example).",
    "Violation": "broke the agent's declared rules / skills / pipeline.",
    "Duplication": "repeated work that could have been skipped.",
    "Mistake": "misjudgment -- wrong tool, wrong file, wrong assumption.",
}


SYSTEM_INSTRUCTIONS = """\
You are helping tune an AI agent based on user feedback on a real run.

Below you will see:
  1. The agent's current definition (system prompt, skills, tools).
  2. Steps from a real session that the user flagged as exceptional,
     each with context (two steps before + two steps after) and the user's
     own note explaining why they flagged it.

Produce **concrete, minimal suggestions** for the agent's prompt / skills /
pipeline that would address the flagged behaviours. For each suggestion:

  * Name the exact file (prompt.md or a specific skill) and the location
    inside it (a heading, a section, or quote the current line to change).
  * Give the before/after text, or the new text to insert.
  * Say which flag(s) the change addresses, by ref.

Rules:
  * Do not restate the trajectory back -- the reader has already seen it.
  * Prefer small surgical edits over sweeping rewrites.
  * If a flag is unclear or looks like a user mistake, say so and skip it;
    bad feedback is not useful feedback.
  * Group suggestions by section (`## Prompt changes`, `## Skill changes`,
    `## Pipeline / workflow changes`, `## Not actionable`).
"""


def compose(agent: AgentInfo, contexts: list[FlagContext],
            session_id: str = "") -> str:
    """The full text sent to the model."""
    tag_block = "\n".join(f"- **{k}** — {v}" for k, v in TAG_GLOSSARY.items())
    flag_blocks = "\n\n".join(c.to_block() for c in contexts)
    sid_line = f"session: {session_id}\n" if session_id else ""
    n = len(contexts)

    return (
        SYSTEM_INSTRUCTIONS
        + "\n\n## Tag glossary\n" + tag_block
        + "\n\n## Agent definition\n" + agent.to_prompt_block()
        + f"\n\n## Flagged steps ({n})\n{sid_line}"
        + flag_blocks
        + "\n\nNow write your suggestions as Markdown."
    )
