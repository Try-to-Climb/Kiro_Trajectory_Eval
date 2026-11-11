"""Load agent definition from a Kiro-style `.kiro/agents/<name>.json`.

The the-agent-style loader in `goal/agent_context.py` expects a `prompt.md` next to
a `.kiro/` directory; the uplift family uses a different layout:

    .../
    └── .kiro/
        ├── agents/<name>.json      ← the handle the user passes in
        │     { "name": ..., "prompt": "file://../prompts/x.md",
        │       "tools": [...], ... }
        ├── prompts/<name>.md       ← resolved from `prompt` field above
        └── skills/*.md             ← picked up alongside

This loader resolves that structure into a compact text block the LLM can
consume, plus a `meta` dict (name, model, tool count, skill count) a caller
can log for provenance.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data class returned to the caller
# ---------------------------------------------------------------------------
@dataclass
class AgentInfo:
    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    prompt_text: str          # full prompt.md body
    prompt_path: str          # resolved absolute path (empty if unresolved)
    skills: tuple[tuple[str, str], ...]   # [(skill_name, frontmatter_or_head)]
    kiro_dir: str             # the enclosing .kiro directory

    def to_prompt_block(self, prompt_max_chars: int = 20000,
                        skill_max_chars: int = 400) -> str:
        """Formatted text to drop into an LLM prompt. Caps long bodies so the
        request stays within sensible token budgets."""
        head = (
            f"# Agent: {self.name}\n"
            f"- model: {self.model or '(unspecified)'}\n"
            f"- tools: {', '.join(self.tools) if self.tools else '(none)'}\n"
            f"- description: {self.description or '(none)'}\n"
        )
        prompt_section = ""
        if self.prompt_text:
            body = self.prompt_text
            if len(body) > prompt_max_chars:
                body = body[:prompt_max_chars] + \
                       f"\n\n[... truncated, {len(self.prompt_text) - prompt_max_chars} more chars]"
            prompt_section = (
                f"\n## System prompt ({os.path.basename(self.prompt_path) or 'inline'})\n"
                f"```\n{body}\n```\n"
            )
        skills_section = ""
        if self.skills:
            lines = [f"\n## Skills ({len(self.skills)})"]
            for name, head_text in self.skills:
                snippet = head_text.strip()
                if len(snippet) > skill_max_chars:
                    snippet = snippet[:skill_max_chars] + "…"
                lines.append(f"\n### {name}\n{snippet}")
            skills_section = "\n".join(lines) + "\n"
        return head + prompt_section + skills_section


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
_FILE_URI_RE = re.compile(r"^file://")


def _resolve_prompt_path(raw: str, agent_json_dir: str) -> Optional[str]:
    """Turn the `prompt` field (`file://../prompts/x.md`) into an abs path."""
    if not raw:
        return None
    cleaned = _FILE_URI_RE.sub("", raw).strip()
    if not cleaned:
        return None
    if os.path.isabs(cleaned):
        return cleaned
    return os.path.normpath(os.path.join(agent_json_dir, cleaned))


def _find_kiro_dir(start: str) -> Optional[str]:
    """Walk up from `start` looking for a `.kiro` segment. Returns the path to
    that `.kiro` directory, or None."""
    cur = os.path.abspath(start)
    while cur and cur != "/":
        base = os.path.basename(cur)
        if base == ".kiro":
            return cur
        cur = os.path.dirname(cur)
    return None


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _skill_summary(path: str, head_lines: int = 20) -> str:
    """Return the frontmatter if present, else the first `head_lines` lines.

    Skills in this layout carry YAML frontmatter with purpose + triggers.
    Falling back to the head keeps unformatted skill files readable too.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(16384)
    except OSError:
        return ""
    m = _FRONT_RE.match(text)
    if m:
        return m.group(1).strip()
    return "\n".join(text.splitlines()[:head_lines]).strip()


def load_agent_from_json(json_path: str) -> AgentInfo:
    """Load an agent definition from its Kiro JSON file.

    Raises `FileNotFoundError` / `ValueError` for a path that is not a readable
    JSON object, so a caller does not have to re-check.
    """
    if not os.path.isfile(json_path):
        raise FileNotFoundError(json_path)
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{json_path}: not a JSON object")

    json_dir = os.path.dirname(os.path.abspath(json_path))
    prompt_path = _resolve_prompt_path(data.get("prompt") or "", json_dir)
    prompt_text = ""
    if prompt_path and os.path.isfile(prompt_path):
        try:
            with open(prompt_path, encoding="utf-8") as f:
                prompt_text = f.read()
        except OSError:
            prompt_text = ""

    kiro_dir = _find_kiro_dir(json_dir) or ""
    skills: list[tuple[str, str]] = []
    if kiro_dir:
        skills_dir = os.path.join(kiro_dir, "skills")
        if os.path.isdir(skills_dir):
            for name in sorted(os.listdir(skills_dir)):
                if not name.endswith(".md"):
                    continue
                s = _skill_summary(os.path.join(skills_dir, name))
                if s:
                    skills.append((os.path.splitext(name)[0], s))

    return AgentInfo(
        name=str(data.get("name") or os.path.splitext(os.path.basename(json_path))[0]),
        description=str(data.get("description") or ""),
        model=str(data.get("model") or ""),
        tools=tuple(data.get("tools") or ()),
        prompt_text=prompt_text,
        prompt_path=prompt_path or "",
        skills=tuple(skills),
        kiro_dir=kiro_dir,
    )
