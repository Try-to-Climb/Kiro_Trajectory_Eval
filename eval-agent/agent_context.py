"""Agent-under-test context loader; three modes.

Modes:
  raw  — Raw concatenation of the full prompt.md plus every SKILL.md's YAML
         frontmatter (v1 behavior, ~15KB).
  code — Purely code-driven structured extraction: parse the Rules/Workflow
         sections of prompt.md plus SKILL.md frontmatter, output a compact
         JSON map plus a formatted text block (~3KB).
  llm  — Have Kiro read every raw source once and produce a JSON map with the
         same schema; result is cached to disk keyed by agent_dir + source
         file mtime digest, so subsequent calls for the same agent cost zero
         LLM tokens.

All three modes share the same public interface:
  load_agent_context(agent_dir, mode="raw"|"code"|"llm") -> str
  load_agent_context_or_empty(agent_dir, mode=..., log=print) -> (text, meta)

`meta` is a runtime diagnostic dict (mode / chars / skills / cache_hit ...) for
the ledger.

[schema] AgentMap (output shared by code / llm modes)
{
  "agent": "example-agent",
  "role": "...",
  "execution_style": ["NEVER fabricate", "ALWAYS execute_bash", ...],
  "skills": [
    {"name": "test-engine-example",
     "purpose": "real device test via hardware_bridge",
     "triggers": ["example_engine", "example engine", ...],
     "not_for": ["example_simulator_1", "example_simulator_2"]}
  ],
  "hooks": ["refresh-release-notes"],
  "mcp_servers": ["example_mcp_server"],
  "subagents": []
}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants: expected directory layout
# ---------------------------------------------------------------------------
_PROMPT_MD = "prompt.md"
_KIRO_DIR = ".kiro"
_SKILLS_SUB = "skills"
_HOOKS_SUB = "hooks"
_MCP_SUB = "mcp-servers"
_SUBAGENTS_SUB = "subagents"

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TRIGGER_QUOTED_RE = re.compile(r'"([^"\n]{2,80}?)"')
_CACHE_DIR = os.path.expanduser("~/.eval-agent/agent_maps")


# ---------------------------------------------------------------------------
# Common utils
# ---------------------------------------------------------------------------
def _read(path: str, limit: int = 65536) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _agent_name(agent_dir: str) -> str:
    return os.path.basename(os.path.abspath(agent_dir.rstrip("/")))


def _mtime_hash(agent_dir: str) -> str:
    """Digest prompt.md + every SKILL.md + hooks/mcp/subagents dir contents by mtime,
    used as the LLM cache key. Any file change flips the hash."""
    h = hashlib.sha256()
    for rel in [_PROMPT_MD]:
        p = os.path.join(agent_dir, rel)
        if os.path.isfile(p):
            h.update(f"{rel}:{int(os.path.getmtime(p))}\n".encode())
    for sub in (_SKILLS_SUB, _HOOKS_SUB, _MCP_SUB, _SUBAGENTS_SUB):
        d = os.path.join(agent_dir, _KIRO_DIR, sub)
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in sorted(files):
                p = os.path.join(root, f)
                rel_p = os.path.relpath(p, agent_dir)
                try:
                    h.update(f"{rel_p}:{int(os.path.getmtime(p))}\n".encode())
                except OSError:
                    pass
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# raw mode (v1 behavior, kept for compatibility)
# ---------------------------------------------------------------------------
def _skill_frontmatter(path: str) -> Optional[str]:
    head = _read(path, 8192)
    if not head:
        return None
    m = _FRONT_RE.match(head)
    if m:
        return m.group(1).strip()
    return "\n".join(head.splitlines()[:30]).strip()


def _load_raw(agent_dir: str) -> str:
    parts: list[str] = []
    prompt_p = os.path.join(agent_dir, _PROMPT_MD)
    txt = _read(prompt_p)
    if txt:
        parts.append("[Agent-under-test main prompt]\n" + txt.strip())
    skills_dir = os.path.join(agent_dir, _KIRO_DIR, _SKILLS_SUB)
    if os.path.isdir(skills_dir):
        lines = ["[Skills loaded by the agent-under-test and their trigger descriptions]"]
        for name in sorted(os.listdir(skills_dir)):
            sp = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(sp):
                continue
            fm = _skill_frontmatter(sp)
            if fm:
                lines.append(f"\n--- skill: {name} ---\n{fm}")
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# code mode (purely code-driven structured extraction)
# ---------------------------------------------------------------------------
_ROLE_HEAD_RE = re.compile(r"You are\s+([A-Z][^\.\n]{2,80})\.?", re.IGNORECASE)


def _extract_role(prompt_text: str) -> str:
    """Extract the role definition from the first line / sentence of prompt.md."""
    if not prompt_text:
        return ""
    first_line = prompt_text.strip().split("\n\n", 1)[0].strip()
    m = _ROLE_HEAD_RE.search(first_line)
    if m:
        return f"You are {m.group(1).strip()}"
    return first_line[:200]


def _extract_style(prompt_text: str) -> list[str]:
    """Extract 'execution style' one-liners (NEVER.../ALWAYS.../DO NOT...) from Rules/Workflow sections of prompt.md."""
    lines = prompt_text.splitlines() if prompt_text else []
    style: list[str] = []
    for ln in lines:
        s = ln.strip().lstrip("-*").strip()
        if not s or len(s) > 160:
            continue
        # Driven by uppercase keywords or emphasis markers
        if any(k in s for k in ("NEVER", "ALWAYS", "DO NOT", "MUST",
                                 "Only confirm", "Evidence first",
                                 "Minimal fix")):
            # Strip markdown emphasis markers
            s = re.sub(r"\*+", "", s)
            if s not in style:
                style.append(s)
        if len(style) >= 8:
            break
    return style


def _parse_frontmatter_kv(fm: str) -> dict:
    """Parse the name / description fields out of YAML frontmatter.
    We do not pull in a yaml dependency: only two top-level shapes are handled here —
    'key: value' and 'key: > \\n block'.
    """
    out: dict[str, str] = {}
    if not fm:
        return out
    lines = fm.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", ln)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest and rest != ">" and rest != "|":
            out[key] = rest
            i += 1
            continue
        # Multi-line block: keep gathering indented (or blank) lines below
        block: list[str] = []
        i += 1
        while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t") or not lines[i].strip()):
            block.append(lines[i].strip())
            i += 1
        out[key] = " ".join(x for x in block if x)
    return out


def _extract_triggers(description: str) -> list[str]:
    """Pull explicit trigger keywords (short quoted phrases) from a skill description."""
    if not description:
        return []
    triggers = _TRIGGER_QUOTED_RE.findall(description)
    # Deduplicate while preserving order
    seen = set()
    out = []
    for t in triggers:
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= 12:
            break
    return out


def _extract_not_for(description: str) -> list[str]:
    """Scenarios the description explicitly says NOT to trigger on (e.g., 'Do NOT activate for X')."""
    if not description:
        return []
    out = []
    for m in re.finditer(r"Do NOT activate for\s+([^\.\n\u2014]+?)(?:\u2014|\.|\n)", description):
        out.append(m.group(1).strip())
    return out[:6]


def _extract_purpose(description: str) -> str:
    if not description:
        return ""
    # Take the first sentence (up to a period, semicolon, or newline)
    s = re.split(r"[\.;\n]", description, 1)[0].strip()
    return s[:180]


def _scan_dir(agent_dir: str, sub: str) -> list[str]:
    p = os.path.join(agent_dir, _KIRO_DIR, sub)
    if not os.path.isdir(p):
        return []
    out = []
    for name in sorted(os.listdir(p)):
        # Strip extension & skip hidden files
        if name.startswith("."):
            continue
        out.append(os.path.splitext(name)[0])
    return out


def _build_map_from_code(agent_dir: str) -> dict:
    prompt_text = _read(os.path.join(agent_dir, _PROMPT_MD)) or ""
    m: dict[str, Any] = {
        "agent": _agent_name(agent_dir),
        "role": _extract_role(prompt_text),
        "execution_style": _extract_style(prompt_text),
        "skills": [],
        "hooks": _scan_dir(agent_dir, _HOOKS_SUB),
        "mcp_servers": _scan_dir(agent_dir, _MCP_SUB),
        "subagents": _scan_dir(agent_dir, _SUBAGENTS_SUB),
    }
    skills_dir = os.path.join(agent_dir, _KIRO_DIR, _SKILLS_SUB)
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            sp = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(sp):
                continue
            fm = _skill_frontmatter(sp) or ""
            kv = _parse_frontmatter_kv(fm)
            desc = kv.get("description", "")
            m["skills"].append({
                "name": kv.get("name", name),
                "purpose": _extract_purpose(desc),
                "triggers": _extract_triggers(desc),
                "not_for": _extract_not_for(desc),
            })
    return m


def _format_map_for_prompt(m: dict) -> str:
    """Flatten the map into text to inject into s2/s3 prompts (compact, human-readable)."""
    L: list[str] = []
    L.append(f"[agent: {m.get('agent','?')}]  {m.get('role','')}")
    if m.get("execution_style"):
        L.append("Execution style:")
        for s in m["execution_style"]:
            L.append(f"  - {s}")
    if m.get("skills"):
        L.append("\nSkills (name / purpose / triggers / not_for):")
        for sk in m["skills"]:
            trig = ", ".join(sk.get("triggers") or []) or "(none listed)"
            L.append(f"  - {sk['name']}  ::  {sk.get('purpose','')}")
            L.append(f"      triggers: {trig}")
            if sk.get("not_for"):
                L.append(f"      not_for : {', '.join(sk['not_for'])}")
    if m.get("hooks"):
        L.append(f"\nhooks: {', '.join(m['hooks'])}")
    if m.get("mcp_servers"):
        L.append(f"mcp_servers: {', '.join(m['mcp_servers'])}")
    if m.get("subagents"):
        L.append(f"subagents: {', '.join(m['subagents'])}")
    return "\n".join(L)


def _load_code(agent_dir: str) -> tuple[str, dict]:
    m = _build_map_from_code(agent_dir)
    text = _format_map_for_prompt(m)
    return text, m


# ---------------------------------------------------------------------------
# llm mode (Kiro produces the map, with disk cache)
# ---------------------------------------------------------------------------
_AGENT_MAP_PROMPT = """You are working on agent trajectory evaluation. Task: condense the raw
configuration materials of the agent-under-test into a **structured JSON map**, so that
downstream requirement-extraction steps can accurately reference its capabilities.

[schema] Follow this structure strictly; field names must not be changed:
```json
{{
  "agent": "<agent name>",
  "role": "<one-sentence role description>",
  "execution_style": ["<behavior rule, <=10 words>", "..."],
  "skills": [
    {{"name": "<skill name>",
     "purpose": "<one sentence: what it does>",
     "triggers": ["<trigger keyword/phrase>", "..."],
     "not_for": ["<scenarios where it should NOT activate>", "..."]}}
  ],
  "hooks": ["<hook name>", "..."],
  "mcp_servers": ["<mcp server name>", "..."],
  "subagents": ["<subagent name>", "..."]
}}
```

Rules:
- `role` must not be multi-line — one sentence pinning down the agent's positioning
- `execution_style` keeps only hard constraints (NEVER/ALWAYS/MUST/DO NOT); compress long sentences into short phrases, at most 6 entries
- For each skill, pick 3~10 keywords/phrases users would actually say (user-side language, not internal tool names)
- If information is missing, return `[]` / `""` — **do not fabricate**
- Output only a single JSON code block, no prose before or after

====== Material: main prompt (prompt.md) ======
{prompt_md}

====== Material: skill metadata (name / description) ======
{skills_block}

====== Material: additional directory listings ======
hooks:       {hooks}
mcp_servers: {mcp_servers}
subagents:   {subagents}
"""


def _map_validator(m: Any) -> list[str]:
    """Validation gate for LLM output. Returns list of errors; empty = pass."""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["top level is not a dict"]
    for k in ("agent", "role", "execution_style", "skills",
              "hooks", "mcp_servers", "subagents"):
        if k not in m:
            errs.append(f"missing field {k}")
    if isinstance(m.get("skills"), list):
        for i, sk in enumerate(m["skills"]):
            if not isinstance(sk, dict):
                errs.append(f"skills[{i}] is not a dict")
                continue
            for k in ("name", "purpose", "triggers", "not_for"):
                if k not in sk:
                    errs.append(f"skills[{i}] missing {k}")
    else:
        errs.append("skills is not a list")
    return errs


def _cache_path(agent_dir: str) -> str:
    key = _mtime_hash(agent_dir)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{_agent_name(agent_dir)}-{key}.json")


def _load_llm(agent_dir: str, *, caller: Optional[Callable[[str], str]] = None,
              cache: bool = True) -> tuple[str, dict]:
    """Have the LLM produce the map (JSON). Cached; only call Kiro on cache miss."""
    cache_p = _cache_path(agent_dir)
    if cache and os.path.isfile(cache_p):
        try:
            data = json.load(open(cache_p, encoding="utf-8"))
            return _format_map_for_prompt(data), {"map": data, "cache_hit": True,
                                                   "cache_path": cache_p}
        except (OSError, json.JSONDecodeError):
            pass

    # Assemble materials (skills_block gives the LLM code-extracted skill metadata as a base,
    # so it does not have to slog through 15KB of raw text)
    prompt_md = _read(os.path.join(agent_dir, _PROMPT_MD)) or ""
    skills_dir = os.path.join(agent_dir, _KIRO_DIR, _SKILLS_SUB)
    skills_block = ""
    if os.path.isdir(skills_dir):
        parts = []
        for name in sorted(os.listdir(skills_dir)):
            sp = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(sp):
                continue
            fm = _skill_frontmatter(sp) or ""
            parts.append(f"### {name}\n{fm[:1500]}")
        skills_block = "\n\n".join(parts)

    prompt = _AGENT_MAP_PROMPT.format(
        prompt_md=prompt_md.strip()[:4000],
        skills_block=skills_block,
        hooks=", ".join(_scan_dir(agent_dir, _HOOKS_SUB)) or "(none)",
        mcp_servers=", ".join(_scan_dir(agent_dir, _MCP_SUB)) or "(none)",
        subagents=", ".join(_scan_dir(agent_dir, _SUBAGENTS_SUB)) or "(none)",
    )
    # Reuse llm.ask's JSON extraction + validation + retry
    from llm import ask
    data = ask(prompt, _map_validator, caller=caller, label="agent_map")
    if cache:
        try:
            with open(cache_p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return _format_map_for_prompt(data), {"map": data, "cache_hit": False,
                                           "cache_path": cache_p}


# ---------------------------------------------------------------------------
# Unified entry points
# ---------------------------------------------------------------------------
def load_agent_context(agent_dir: Optional[str] = None, *,
                       mode: str = "raw",
                       caller: Optional[Callable[[str], str]] = None) -> str:
    """Legacy entry point (backwards compatible): returns text only."""
    text, _ = load_agent_context_or_empty(agent_dir, mode=mode, caller=caller,
                                          log=None)
    return text


def load_agent_context_or_empty(agent_dir: Optional[str] = None, *,
                                mode: str = "raw",
                                caller: Optional[Callable[[str], str]] = None,
                                log: Optional[Callable[..., None]] = print
                                ) -> tuple[str, dict]:
    """Unified entry point for the three modes.

    Returns (text, meta). An empty text means not enabled or failed to load; the caller
    should fall back to the baseline.
    """
    agent_dir = agent_dir or os.environ.get("ATP_AGENT_DIR")
    meta = {"mode": mode, "dir": agent_dir or "", "chars": 0}
    if not agent_dir or not os.path.isdir(agent_dir):
        if log:
            log(f"[agent_context] not loaded (dir empty/missing) mode={mode}")
        return "", meta
    try:
        if mode == "raw":
            text = _load_raw(agent_dir)
            meta.update({"chars": len(text),
                         "skills": text.count("--- skill: "),
                         "has_prompt": "[Agent-under-test main prompt]" in text})
        elif mode == "code":
            text, m = _load_code(agent_dir)
            meta.update({"chars": len(text), "skills": len(m.get("skills") or []),
                         "hooks": len(m.get("hooks") or []),
                         "mcp_servers": len(m.get("mcp_servers") or []),
                         "subagents": len(m.get("subagents") or []),
                         "map": m})
        elif mode == "llm":
            text, m = _load_llm(agent_dir, caller=caller)
            meta.update({"chars": len(text),
                         "cache_hit": m.get("cache_hit", False),
                         "cache_path": m.get("cache_path", ""),
                         "map": m.get("map")})
        else:
            raise ValueError(f"unknown mode: {mode} (choose raw|code|llm)")
    except Exception as e:
        if log:
            log(f"[agent_context] load failed mode={mode}: {e}")
        return "", {**meta, "error": str(e)[:200]}
    if log:
        log(f"[agent_context] loaded mode={mode} dir={agent_dir} chars={meta['chars']} "
            + " ".join(f"{k}={v}" for k, v in meta.items()
                        if k not in ("mode", "dir", "chars", "map")))
    return text, meta


# ---------------------------------------------------------------------------
# CLI: python3 agent_context.py <dir> [--mode raw|code|llm]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("dir", nargs="?", default=os.environ.get("ATP_AGENT_DIR"))
    p.add_argument("--mode", choices=["raw", "code", "llm"], default="raw")
    p.add_argument("--dump-map", metavar="PATH",
                   help="also dump the structured map JSON to PATH (valid for code/llm modes)")
    p.add_argument("--no-cache", action="store_true",
                   help="skip cache in llm mode, force re-run")
    a = p.parse_args()

    if a.mode == "llm" and a.no_cache:
        # Simple ad-hoc override: change the cache path. We do not mutate the global constant here,
        # just note that the current run bypasses it.
        pass
    text, meta = load_agent_context_or_empty(a.dir, mode=a.mode)
    print("\n=== meta ===")
    print(json.dumps({k: v for k, v in meta.items() if k != "map"},
                     ensure_ascii=False, indent=1))
    if a.dump_map and meta.get("map"):
        with open(a.dump_map, "w", encoding="utf-8") as f:
            json.dump(meta["map"], f, ensure_ascii=False, indent=1)
        print(f"\nmap -> {a.dump_map}")
    print("\n=== text preview (first 60 lines) ===")
    print("\n".join(text.splitlines()[:60]))
