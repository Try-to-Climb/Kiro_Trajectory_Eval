"""Build a TraceIR directly from a Kiro official session record.

Why this loader exists:
    The official record (`$KIRO_HOME/sessions/cli/<sid>.json{,l}`) is a
    better source for **offline trajectory evaluation** than the hook trace
    -- tool names use the modern convention (no alias folding needed),
    results are structured (Success/Error, no need to guess success/fail
    from "post present or not"), it includes every assistant message plus
    thinking, and it carries token / cost / context usage. Parent and child
    agents naturally live in separate session files, so the hook's
    KIRO_SESSION_ID directory-sharing problem does not apply.

    Three things the hook trace is still irreplaceable for (see README):
    policy-blocked operations, precise millisecond timing of each call, and
    live tailing. So both loaders coexist: evaluation uses official,
    auditing uses hook.

    The TraceIR produced here is **isomorphic** to the hook version,
    reusing the same fan-out / semantic mapping / run/turn logic, so the
    downstream rule engine and agentevals adapter do not need to know which
    source they got.

.jsonl structure (empirical):
    one object per line, kind in {Prompt, AssistantMessage, ToolResults}
      Prompt            data.content[].data is user input text
      AssistantMessage  each data.content[] has kind in {text, thinking, toolUse}
                        one message may hold multiple toolUses (parallel; up to 4 observed)
      ToolResults       data.results is a {toolUseId: {tool, result}} dict,
                        where result is {"Success": {...}} or {"Error": {...}}
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .core import _expand_from_tool          # reuse fan-out + semantic mapping
from .official import default_official_dir, load_official, _content_text
from .schema import Action, Thinking, TraceIR


def _tool_input(tooluse_data: dict) -> dict:
    v = tooluse_data.get("input")
    return v if isinstance(v, dict) else {}


def _result_status(result_entry: dict) -> tuple[bool, Optional[str]]:
    """Return (success, error-message).

    Only an explicit Error counts as failure. A null result or one that is
    neither Success nor Error (e.g. a reporting tool like `summary` that
    does not produce a standard result) is treated as success -- otherwise
    a legitimate summary would be misjudged as a failure (observed:
    sessions where the summary tool returns result=null).
    """
    r = (result_entry or {}).get("result")
    if isinstance(r, dict) and "Error" in r:
        err = r["Error"]
        msg = json.dumps(err, ensure_ascii=False) if not isinstance(err, str) else err
        return False, msg[:300]
    return True, None


def _result_size(result_entry: dict) -> Optional[int]:
    try:
        return len(json.dumps(result_entry.get("result"), ensure_ascii=False))
    except (TypeError, ValueError):
        return None


def _result_content(result_entry: dict) -> Optional[str]:
    """Full tool response text, serialized. Only called when include_responses is on."""
    r = (result_entry or {}).get("result")
    if r is None:
        return None
    if isinstance(r, str):
        return r
    try:
        return json.dumps(r, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(r)


def load_trace_from_official(session_id: str,
                             official_dir: Optional[str] = None,
                             include_responses: bool = False) -> TraceIR:
    """Build a TraceIR from the official session record.

    include_responses: whether to fill the full tool reply into
        Action.response (**default False**, since a reply can be a whole
        file or command output and the size may be huge).
    """
    base = official_dir or default_official_dir()
    rec = load_official(session_id, base)          # reuse the metadata parse (agent / usage / credits)
    jl_path = os.path.join(base, f"{session_id}.jsonl")

    ir = TraceIR(session_id=session_id, source=jl_path)
    ir.official = rec if rec.found else None
    ir.agent_name = rec.agent_name if rec.found else None

    if not os.path.isfile(jl_path):
        ir.warnings.append(f"official jsonl does not exist: {jl_path}")
        return ir

    # in the official record each sub-agent is a separate file, so the
    # hook-side directory-sharing problem does not apply here
    ir.run_count = 1

    turn = 0
    idx = 0
    call_idx = 0
    warnings = ir.warnings

    for line in open(jl_path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec_obj = json.loads(line)
        except json.JSONDecodeError:
            warnings.append("official jsonl: some lines failed to parse, skipped")
            continue

        kind = rec_obj.get("kind")
        data = rec_obj.get("data") or {}

        if kind == "Prompt":
            turn += 1
            text = ""
            for c in (data.get("content") or []):
                d = c.get("data")
                text += d if isinstance(d, str) else (d.get("text", "") if isinstance(d, dict) else "")
            ir.prompts.append(text)
            ir.run_prompts.setdefault(1, text if turn == 1 else ir.run_prompts.get(1, ""))
            ts = str((data.get("meta") or {}).get("timestamp", ""))

        elif kind == "AssistantMessage":
            # collect the final text from this message (as part of the turn's reply)
            final_text = "".join(
                c.get("data", "") if isinstance(c.get("data"), str)
                else (c.get("data", {}).get("text", "") if isinstance(c.get("data"), dict) else "")
                for c in (data.get("content") or []) if c.get("kind") == "text")
            if final_text:
                ir.responses.append(final_text)
                # Mirror the append into the per-turn index so a reader of the
                # graph can show "assistant replied" alongside "user asked"
                # for the right turn. `turn` here is the one bumped by the
                # most recent Prompt above.
                ir.responses_by_turn.setdefault(turn, []).append(final_text)

            # the thinking from this message is attributed to every toolUse
            # action it produces. A message can contain several parallel
            # toolUses that share one thinking segment.
            msg_thinking = "".join(
                _content_text(c) for c in (data.get("content") or [])
                if c.get("kind") == "thinking")

            # Collect all toolUse in this message and remember the starting
            # position (used to back-fill Thinking.action_refs).
            tool_use_contents = [c for c in (data.get("content") or [])
                                 if c.get("kind") == "toolUse"]
            has_tool_use = bool(tool_use_contents)
            actions_before = len(ir.actions)

            for c in tool_use_contents:
                td = c.get("data") or {}
                tool = td.get("name") or "unknown"
                args = _tool_input(td)
                # reuse the hook-side fan-out + semantic mapping
                parts = _expand_from_tool(tool, args, warnings)
                for op_idx, part in enumerate(parts):
                    ir.actions.append(Action(
                        idx=idx, call_idx=call_idx, op_idx=op_idx,
                        ts="", turn=max(turn, 1), run=1,
                        raw_tool=tool, tool=_canonical(tool),
                        action=part["action"],
                        sid=session_id, ref=f"{session_id[:8]}#{idx}",
                        path=part.get("path"), root=part.get("root"),
                        cwd=part.get("cwd"),
                        command=part.get("command"),
                        subcommands=part.get("subcommands", []),
                        pattern=part.get("pattern"),
                        purpose=(args.get("__tool_use_purpose") if isinstance(args, dict) else None),
                        reasoning=msg_thinking,
                        completed=True,          # tentative; back-filled from ToolResults below
                        blocked=False,
                        args=args,
                        tool_use_id=td.get("toolUseId"),
                    ))
                    idx += 1
                call_idx += 1

            # Store the thinking unconditionally, independent of toolUse.
            # This is the patch to IR's "Action-centric" design: without it,
            # thinking on pure-reply / pure-thinking messages (no toolUse)
            # would be lost.
            if msg_thinking:
                ir.thinkings.append(Thinking(
                    turn=max(turn, 1),
                    text=msg_thinking,
                    has_tool_use=has_tool_use,
                    action_refs=[a.idx for a in ir.actions[actions_before:]],
                ))

        elif kind == "ToolResults":
            results = data.get("results")
            if isinstance(results, dict):
                for tid, entry in results.items():
                    ok, err = _result_status(entry)
                    size = _result_size(entry)
                    content = _result_content(entry) if include_responses else None
                    for a in ir.actions:
                        if a.tool_use_id == tid:
                            a.completed = ok
                            a.resp_size = size
                            if err:
                                a.error = err
                            if content is not None:
                                a.response = content

    return ir


def _canonical(tool: str) -> str:
    from .mapping import canonical_tool
    return canonical_tool(tool)


def iter_official_sessions(official_dir: Optional[str] = None,
                           cwd_filter: Optional[str] = None):
    """List session ids from the official records (those with a .jsonl), with optional cwd filter."""
    base = official_dir or default_official_dir()
    if not os.path.isdir(base):
        return
    for name in sorted(os.listdir(base)):
        if not name.endswith(".jsonl"):
            continue
        sid = name[:-6]
        if cwd_filter:
            meta_p = os.path.join(base, f"{sid}.json")
            try:
                if json.load(open(meta_p, encoding="utf-8")).get("cwd") != cwd_filter:
                    continue
            except (OSError, json.JSONDecodeError):
                continue
        yield sid
