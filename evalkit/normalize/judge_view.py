"""Project the normalized trajectory into a compact view for an LLM judge (read-only, does not modify the normalization result).

Plan B: one line per action `[idx] action(tool) target [status]`,
with indented attachments below:
  - purpose: the __tool_use_purpose that the tool call itself carries (present on almost every step)
  - think  : the thinking (reasoning) for this step (attached only when present)

Full response text / other args are not included in this projection
(they are always retained in the normalized output).
"""

from __future__ import annotations

from typing import Any


def _cap(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "…"


def _target(a: dict, cmd_cap: int = 200) -> str:
    return _cap(a.get("command") or a.get("path") or a.get("pattern") or a.get("root") or "", cmd_cap)


def _status(a: dict) -> str:
    if a.get("blocked"):
        return " [BLOCKED]"
    if a.get("error"):
        return f" [ERROR:{_cap(a.get('error'), 40)}]"
    if a.get("completed") is False:
        return " [incomplete]"
    return ""


def _line(a: dict, cmd_cap: int) -> str:
    s = f"[{a.get('idx')}] {a.get('action')}({a.get('tool')}) {_target(a, cmd_cap)}{_status(a)}"
    purpose = (a.get("args") or {}).get("__tool_use_purpose")
    if purpose:
        s += f"\n    ↳ purpose: {_cap(purpose, 300)}"
    if a.get("reasoning"):
        s += f"\n    ↳ think: {a['reasoning']}"       # keep full reasoning
    return s


def build_view_from_actions(actions, cmd_cap: int = 200) -> str:
    """Build the Plan B view from a list of actions (dicts or Action objects)."""
    acts = [a.to_dict() if hasattr(a, "to_dict") else a for a in actions]
    return "\n".join(_line(a, cmd_cap) for a in acts)


def build_judge_view(ir, cmd_cap: int = 200) -> str:
    """Project a TraceIR into a Plan B view string. Accepts TraceIR or any object with .actions."""
    return build_view_from_actions(ir.actions, cmd_cap)


def objective_of(ir) -> str:
    """The objective of this run = the first user prompt (truncated to avoid excessive length)."""
    prompts = getattr(ir, "prompts", None) or []
    return _cap(prompts[0], 1500) if prompts else ""
