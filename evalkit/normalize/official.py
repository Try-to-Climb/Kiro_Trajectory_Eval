"""Reading and enrichment from Kiro's official session record.

What the hook cannot see, Kiro itself already records. Official records live in
`$KIRO_HOME/sessions/cli/<session-id>.json` (metadata) and `.jsonl` (full
dialog), and provide four kinds of information completely absent from the
hook payload:

  1. `agent_name`        — authoritative value; no more guessing from /proc
  2. Per-turn usage      — builtin_tool_uses / token / credits / context%
  3. `end_reason`        — UserTurnEnd / ToolUseRejected / ...
  4. Full assistant messages — including thinking; the hook's stop event only gives the last one

Once wired in, the normalization layer can also **self-verify**: the
official per-turn tool-use count should match the hook per-turn call count
turn by turn; a mismatch means the hook missed events or paired incorrectly.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .mapping import canonical_tool


def default_official_dir() -> str:
    """Official session record directory. KIRO_HOME is set by Kiro."""
    explicit = os.environ.get("KIRO_SESSIONS_DIR")
    if explicit:
        return explicit
    home = os.environ.get("KIRO_HOME")
    if home:
        return os.path.join(home, "sessions", "cli")
    return os.path.join(os.path.expanduser("~"), ".kiro", "sessions", "cli")


@dataclass
class TurnMeta:
    """Per-turn usage and end status from the official record."""

    turn: int
    tool_uses: Optional[int] = None          # Kiro's count of tool calls in this turn
    request_count: Optional[int] = None
    end_reason: Optional[str] = None         # UserTurnEnd / ToolUseRejected / ...
    duration_s: Optional[int] = None
    credits: Optional[float] = None          # actual billing
    context_pct: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    prompt_len: Optional[int] = None
    agent: Optional[str] = None              # the agent that actually executed this turn (differs for child agents)
    parent_agent: Optional[str] = None
    assistant_text: str = ""                 # everything the agent said in this turn
    thinking_text: str = ""                  # agent thinking for this turn
    tool_names: list[str] = field(default_factory=list)  # tool names from the official view
    rejected_tools: list[str] = field(default_factory=list)  # rejected at argument validation; hook cannot see these

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OfficialRecord:
    session_id: str
    meta_path: Optional[str] = None
    jsonl_path: Optional[str] = None
    agent_name: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    created_reason: Optional[str] = None     # subagent / rewind / ...
    title: Optional[str] = None
    turns: list[TurnMeta] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.meta_path is not None

    @property
    def total_credits(self) -> float:
        return round(sum(t.credits or 0.0 for t in self.turns), 6)

    @property
    def tool_uses_per_turn(self) -> list[Optional[int]]:
        return [t.tool_uses for t in self.turns]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "meta_path": self.meta_path,
            "jsonl_path": self.jsonl_path,
            "agent_name": self.agent_name,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "created_reason": self.created_reason,
            "title": self.title,
            "total_credits": self.total_credits,
            "turns": [t.to_dict() for t in self.turns],
        }


_VALIDATION_REJECT = "Failed to parse the tool use"


def _content_text(item: dict) -> str:
    """Text of a content element. For text, data is a bare string; for thinking, data is an object."""
    d = item.get("data")
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("text") or ""
    return ""


def load_official(session_id: str, official_dir: Optional[str] = None) -> OfficialRecord:
    base = official_dir or default_official_dir()
    rec = OfficialRecord(session_id=session_id)
    meta_p = os.path.join(base, f"{session_id}.json")
    jl_p = os.path.join(base, f"{session_id}.jsonl")
    if not os.path.isfile(meta_p):
        return rec
    rec.meta_path = meta_p
    rec.jsonl_path = jl_p if os.path.isfile(jl_p) else None

    try:
        meta = json.load(open(meta_p, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return rec

    state = meta.get("session_state") or {}
    rec.agent_name = state.get("agent_name")
    rec.cwd = meta.get("cwd")
    rec.created_at = meta.get("created_at")
    rec.created_reason = meta.get("session_created_reason")
    rec.title = meta.get("title")

    tms = (state.get("conversation_metadata") or {}).get("user_turn_metadatas") or []
    # message_id → turn number, for mapping .jsonl records to their turn
    mid2turn: dict[str, int] = {}
    for i, tm in enumerate(tms, 1):
        loop = (tm.get("loop_id") or {}).get("agent_id") or {}
        rec.turns.append(TurnMeta(
            turn=i,
            tool_uses=tm.get("builtin_tool_uses"),
            request_count=tm.get("total_request_count"),
            end_reason=tm.get("end_reason"),
            duration_s=(tm.get("turn_duration") or {}).get("secs"),
            credits=round(sum(v.get("value", 0.0)
                              for v in (tm.get("metering_usage") or [])), 6) or None,
            context_pct=tm.get("context_usage_percentage"),
            input_tokens=tm.get("input_token_count"),
            output_tokens=tm.get("output_token_count"),
            prompt_len=tm.get("user_prompt_length"),
            agent=loop.get("name"),
            parent_agent=loop.get("parent_id"),
        ))
        for mid in (tm.get("message_ids") or []):
            mid2turn[mid] = i

    if rec.jsonl_path:
        # toolUseId → (turn number, tool name), for back-filling execution results to the right turn
        use_index: dict[str, tuple[int, str]] = {}
        results: dict[str, dict] = {}
        try:
            for line in open(rec.jsonl_path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                kind = r.get("kind")
                data = r.get("data") or {}
                if kind == "ToolResults":
                    rs = data.get("results")
                    if isinstance(rs, dict):
                        results.update(rs)
                    continue
                if kind != "AssistantMessage":
                    continue
                t = mid2turn.get(data.get("message_id"))
                if t is None or t > len(rec.turns):
                    continue
                tm = rec.turns[t - 1]
                for item in (data.get("content") or []):
                    k = item.get("kind")
                    if k == "text":
                        tm.assistant_text += _content_text(item)
                    elif k == "thinking":
                        tm.thinking_text += _content_text(item)
                    elif k == "toolUse":
                        d = item.get("data") or {}
                        if d.get("name"):
                            tm.tool_names.append(d["name"])
                            if d.get("toolUseId"):
                                use_index[d["toolUseId"]] = (t, d["name"])
        except (json.JSONDecodeError, OSError):
            pass

        # Mark calls rejected at the argument-validation phase: preToolUse
        # does not fire for these, so the hook cannot possibly see them.
        for tid, (t, name) in use_index.items():
            res = results.get(tid) or {}
            rr = res.get("result") or {}
            if "Success" in rr:
                continue
            msg = json.dumps(rr, ensure_ascii=False)
            if _VALIDATION_REJECT in msg or "failed validation" in msg:
                rec.turns[t - 1].rejected_tools.append(name)

    return rec


def cross_check(official: OfficialRecord,
                hook_turns: int,
                hook_calls_per_turn: list[int],
                hook_tools_per_turn: Optional[list[list[str]]] = None) -> list[str]:
    """Verify the hook record against the official record. Returns discrepancy notes.

    Several points that must be handled (all learned empirically):

    1. **Fold aliases on both sides**. The official record mostly uses modern
       names (read/write/shell), but sessions using old names have also been
       seen (c9312b31 records fs_read / use_subagent), so you cannot fold
       only the hook side.
    2. **Multiset-equal but order-different is not a miss**. With parallel
       tool calls, the hook writes in completion order and the official
       record in issue order; the sequences get misaligned. Only a
       multiset mismatch is a true miss/extra.
    3. **The official record may span a longer time range than the trace**
       (session resumed multiple times, or the hook was only attached for
       part of the runs); in that case, a large total-count difference is
       not a hook bug. Downgrade to a hint when the deviation exceeds 10%.
    4. **Calls rejected at the validation phase are invisible to the hook**.
       The model issued the call, Kiro rejected it during argument
       validation ("The tool arguments failed validation"), and preToolUse
       does not fire. Measured on a51bdca0 and f4b86d8c, the missing counts
       tool-by-tool correspond exactly to the rejected counts.
    """
    out: list[str] = []
    if not official.found:
        return out

    if official.turns and hook_turns != len(official.turns):
        out.append(f"turn count does not match official record: hook={hook_turns} official={len(official.turns)}")

    off_counts = [t.tool_uses for t in official.turns]
    if off_counts and all(x is not None for x in off_counts) and off_counts != hook_calls_per_turn:
        out.append(f"tool call count per turn does not match official record: hook={hook_calls_per_turn} official={off_counts}")

    if hook_tools_per_turn is None:
        return out

    off_flat = [canonical_tool(n) for t in official.turns for n in t.tool_names]
    hook_flat = [canonical_tool(n) for names in hook_tools_per_turn for n in names]
    if not off_flat:
        return out

    off_c, hook_c = Counter(off_flat), Counter(hook_flat)
    if off_c == hook_c:
        # Multisets agree; if the sequences differ it is just the
        # parallel-call write-order difference, not an issue.
        return out

    missing = off_c - hook_c
    extra = hook_c - off_c

    # Calls rejected at validation are necessarily invisible to the hook;
    # explain the corresponding "misses" with them first.
    rejected = Counter(canonical_tool(n) for t in official.turns for n in t.rejected_tools)
    explained = missing & rejected
    unexplained = missing - explained

    if explained and not unexplained and not extra:
        out.append(
            f"official has {sum(explained.values())} more calls than hook, already rejected via parameter validation "
            f"({dict(explained)}); preToolUse does not fire for such calls, this is expected")
        return out

    span = abs(len(off_flat) - len(hook_flat))
    tol = max(3, len(off_flat) // 10)
    level = "coverage scope may differ" if span > tol else "suspected missing record"
    msg = (f"tool calls do not match official record ({level}): official={len(off_flat)} hook={len(hook_flat)}")
    if unexplained:
        msg += f" unexplained missing={dict(unexplained)}"
    if explained:
        msg += f" already rejected via validation={dict(explained)}"
    if extra:
        msg += f" in hook but not official={dict(extra)}"
    out.append(msg)
    return out
