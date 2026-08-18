"""LLM-as-judge: hand a condensed trajectory to an LLM (defaults to Kiro in
non-interactive mode) and score it against a rubric.

Design:
  - Three-part prompt = RUBRIC (chosen by dimension) + TASK (objective + the
    dimension to grade) + TRAJECTORY (plan-B view)
  - Backend defaults to `kiro-cli chat --no-interactive` (using Kiro as the
    LLM); a different caller can be injected for testing.
  - Scoring is only **advisory**: LLMs are not perfectly reproducible, so the
    result carries a confidence; when the backend is unavailable we gracefully
    skip.

The rubric skeleton borrows from AgentDiagnose (1-4 tiers + justification +
JSON output), rewritten for the Kiro/CLI context.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Dimension rubrics (system section). Each requires the LLM to return
# {"score":1-4,"justification":...}
# ---------------------------------------------------------------------------
_COMMON_TAIL = """
Output only one JSON code block, in the format:
```json
{"score": <integer 1-4>, "justification": "<short reason, in English>"}
```
Do not output anything outside of the JSON."""

RUBRICS = {
    "efficiency": """You are a strict evaluator of Agent execution trajectories. Rate this run's [efficiency]: whether the goal was achieved via the shortest, least-redundant path.
Scoring (1-4):
- 4 excellent: almost no redundancy, actions go straight to the goal, few repeated reads / trials.
- 3 good: mostly efficient, with minor repetition or skippable exploration.
- 2 average: obvious detours, notable duplicated effort (rereading the same file, retrying similar commands).
- 1 poor: many invalid/repeated actions, spinning for a long time before reaching the goal.
Reference criteria: number of repeated actions, back-and-forth reads, failed retries, length of exploration before reaching the goal.""" + _COMMON_TAIL,

    "reasoning_quality": """You are a strict evaluator of Agent execution trajectories. Rate this run's [reasoning quality] with an overall score (1-4) combining four aspects:
- Task decomposition: whether complex tasks were split into clear steps.
- Tool-choice reasonableness: whether the tool/command chosen at each step was appropriate.
- Result interpretation: whether tool returns were understood and used.
- Self-verification: whether the agent checked its own results against the goal.
Score 4 = excellent on all four aspects; 3 = generally good with room to improve; 2 = clearly lacking in some aspects; 1 = barely displayed.""" + _COMMON_TAIL,

    "authenticity": """You are a strict auditor of Agent execution trajectories. Rate this run's [authenticity]: whether the things the agent claimed / intended to do have corresponding real execution actions, rather than being "said but not done".
Scoring (1-4):
- 4 excellent: every key claim is backed by a corresponding execution action, no fabrication.
- 3 good: the vast majority of claims are backed; a few are unclear.
- 2 average: several cases of "claimed to have done something but no matching action in the trajectory".
- 1 poor: heavy signs of fabrication - conclusions were made after only reading the target/prompt, key execution actions are missing.
Reference criteria: whether claims of LIVE/execution/testing etc. have corresponding run_command / outputs / dispatch actions.""" + _COMMON_TAIL,
}


def available_dimensions() -> list[str]:
    return sorted(RUBRICS)


def assemble_prompt(dimension: str, objective: str, trajectory_view: str) -> str:
    """Three-part prompt assembly."""
    rubric = RUBRICS.get(dimension)
    if rubric is None:
        raise ValueError(f"unknown judge dimension: {dimension} (available: {available_dimensions()})")
    return (f"{rubric}\n\n"
            f"====== Run under evaluation ======\n"
            f"Evaluation dimension: {dimension}\n"
            f"Task objective of this agent:\n{objective or '(unknown)'}\n\n"
            f"====== Execution trajectory (condensed) ======\n{trajectory_view}\n")


def parse_response(text: str) -> dict:
    """Extract JSON from the LLM reply; return {'score':int,'justification':str}
    or {'error':...}."""
    if not text:
        return {"error": "empty reply"}
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    # Fallback: grab the first {...}
    if not m:
        b = re.search(r"\{.*\}", raw, re.DOTALL)
        if b:
            raw = b.group(0)
    try:
        d = json.loads(raw)
        score = d.get("score")
        if isinstance(score, str) and score.strip().upper() == "N/A":
            return {"error": "score=N/A"}
        score = int(score)
        if not 1 <= score <= 4:
            return {"error": f"score out of range: {score}"}
        return {"score": score, "justification": str(d.get("justification", ""))[:500]}
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return {"error": f"parse failed: {e}", "raw": text[:200]}


# ---------------------------------------------------------------------------
# Backend: use Kiro as the LLM
# ---------------------------------------------------------------------------
def kiro_caller(prompt: str, *, agent: str = "kiro-judge",
                effort: Optional[str] = None, timeout: int = 180) -> str:
    """Run the prompt through kiro-cli in non-interactive mode, return stdout.

    - --trust-tools= (empty) trusts no tools; the judge agent itself should also have tools:[].
    - The prompt is passed via argv list to avoid shell-escaping issues.
    """
    if shutil.which("kiro-cli") is None:
        raise RuntimeError("kiro-cli not found in environment")
    cmd = ["kiro-cli", "chat", "--no-interactive", "--trust-tools=", "--agent", agent]
    if effort:
        cmd += ["--effort", effort]
    cmd.append(prompt)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"kiro-cli exit code {r.returncode}: {r.stderr[:200]}")
    return r.stdout


def judge(dimension: str, objective: str, trajectory_view: str,
          caller: Optional[Callable[[str], str]] = None,
          **caller_kwargs) -> dict:
    """Run one judgement. caller(prompt) -> str; defaults to kiro_caller.

    Returns {'score':1-4,'justification':...} or {'error':...}; retries once on
    parse failure.
    """
    prompt = assemble_prompt(dimension, objective, trajectory_view)
    call = caller or (lambda p: kiro_caller(p, **caller_kwargs))
    last = {}
    for _ in range(2):
        try:
            out = call(prompt)
        except Exception as e:
            return {"error": f"backend call failed: {e}"}
        last = parse_response(out)
        if "error" not in last:
            return last
    return last
