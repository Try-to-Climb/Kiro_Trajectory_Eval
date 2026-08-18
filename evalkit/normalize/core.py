"""Normalization core.

The unit of input is the **whole session's trace.jsonl**; it cannot be
processed line-by-line independently — the two derived fields `completed`
and `turn` only exist in the relationships between events.

Three passes:
  pass 1  Line-by-line scan: split turns, collect tool calls, pair pre/post
  pass 2  Fan-out: expand batched calls with array arguments into multiple Actions
  pass 3  Semantic extraction: attach action labels via the mapping table, move paths into unified fields
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Iterable, Optional

from .mapping import (
    READ_MODE_ACTIONS,
    SIMPLE_ACTIONS,
    WRITE_ACTIONS,
    canonical_tool,
    split_subcommands,
    norm_param,
    unknown_action,
)
from .attribution import resolve_runs
from .official import cross_check, load_official
from .schema import Action, TraceIR


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _canon_input(d: Any) -> str:
    """Serialize tool_input into a comparable string, for pre/post pairing."""
    try:
        return json.dumps(d, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(d)


def _hook_response_text(resp: Any) -> Optional[str]:
    """Serialize a hook post's tool_response into a string. Called only when include_responses is set."""
    if resp is None:
        return None
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(resp)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_ms(pre_ts: Optional[str], post_ts: Optional[str]) -> Optional[int]:
    a, b = _parse_ts(pre_ts), _parse_ts(post_ts)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def _norm_path(p: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """Normalize a path so that a single file has only one spelling.

    - Relative paths are resolved against the cwd recorded in the trace
      (every pre_tool_use carries a cwd).
    - Collapse ./ ../ duplicate slashes and trailing slashes: `/a/b/` and
      `/a/b` are considered the same file.
    - Pure string processing only; symlinks are not resolved (the file may
      no longer exist).
    """
    if not p:
        return p
    if not p.startswith("/") and cwd:
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


# ---------------------------------------------------------------------------
# pass 1 — scan + pair
# ---------------------------------------------------------------------------

class _Call:
    """A tool call before fan-out."""

    __slots__ = ("call_idx", "turn", "run", "pre", "post", "blocked")

    def __init__(self, call_idx: int, turn: int, pre: dict, run: int = 1):
        self.call_idx = call_idx
        self.turn = turn
        self.run = run
        self.pre = pre
        self.post: Optional[dict] = None
        self.blocked = False

    @property
    def tool_raw(self) -> str:
        return self.pre.get("tool") or ""

    @property
    def args(self) -> dict:
        v = self.pre.get("tool_input")
        return v if isinstance(v, dict) else {}


def _scan(raw_events: list[dict], warnings: list[str]) -> tuple[list[_Call], list[str], list[str], Optional[str], int, dict, dict]:
    turn = 0
    run = 0
    calls: list[_Call] = []
    run_prompts: dict[int, str] = {}
    run_started: dict[int, str] = {}
    pending: list[int] = []          # indices into calls waiting for post
    prompts: list[str] = []
    responses: list[str] = []
    agent_name: Optional[str] = None

    for rec in raw_events:
        ev = rec.get("event")

        if ev == "agent_spawn":
            # Not a turn boundary, but a boundary between "agent startups".
            # Child agents inherit the parent's KIRO_SESSION_ID and their
            # events land in the same directory; this is the only signal to
            # separate them.
            run += 1
            run_started.setdefault(run, rec.get("ts"))
            if agent_name is None and rec.get("agent_name"):
                agent_name = rec["agent_name"]

        elif ev == "user_prompt":
            # Use user_prompt as the turn boundary, not stop — stop may be missing
            turn += 1
            prompts.append(rec.get("prompt", "") or "")
            run_prompts.setdefault(max(run, 1), rec.get("prompt", "") or "")

        elif ev == "stop":
            responses.append(rec.get("response_preview", "") or "")

        elif ev == "pre_tool_use":
            calls.append(_Call(len(calls), max(turn, 1), rec, max(run, 1)))
            pending.append(len(calls) - 1)

        elif ev == "post_tool_use":
            idx = _match_pending(calls, pending, rec)
            if idx is None:
                warnings.append(
                    f"post_tool_use unable to pair: tool={rec.get('tool')} ts={rec.get('ts')}"
                )
            else:
                calls[idx].post = rec
                pending.remove(idx)

        elif ev == "tool_blocked":
            idx = _match_pending(calls, pending, rec, compare_args=False)
            if idx is not None:
                calls[idx].blocked = True
                pending.remove(idx)

    if pending:
        warnings.append(f"{len(pending)} calls have pre without post (execution incomplete)")

    if run > 1:
        warnings.append(
            f"this session directory contains {run} agent spawns: Kiro allocates independent sessions to sub-agents, "
            f"but the KIRO_SESSION_ID seen by hook is still the parent's, and sub-agent events land here too; "
            f"must group by run before evaluation")

    if prompts and responses and len(responses) < len(prompts):
        warnings.append(
            f"stop event missing: {len(prompts)} input turns but only {len(responses)} response records"
        )

    return calls, prompts, responses, agent_name, max(run, 1), run_prompts, run_started


def _match_pending(
    calls: list[_Call],
    pending: list[int],
    rec: dict,
    compare_args: bool = True,
) -> Optional[int]:
    """Pair a post/blocked event with the most recent unpaired pre.

    First match by tool + tool_input exactly; on failure, fall back to
    matching by tool alone. We iterate in reverse because normally a post
    follows immediately after its pre; long-running calls (measured up to
    6 minutes) would be mispaired to a later call if matched by "the next post".
    """
    tool = rec.get("tool")
    if compare_args:
        want = _canon_input(rec.get("tool_input"))
        for idx in reversed(pending):
            c = calls[idx]
            if c.tool_raw == tool and _canon_input(c.args) == want:
                return idx
    for idx in reversed(pending):
        if calls[idx].tool_raw == tool:
            return idx
    return None


# ---------------------------------------------------------------------------
# pass 2 + 3 — fan-out + semantic extraction
# ---------------------------------------------------------------------------

def _expand(call: _Call, warnings: list[str]) -> list[dict]:
    """Expand a call into 1..N "half-baked" actions (without idx yet)."""
    return _expand_from_tool(call.tool_raw, call.args, warnings)


def _expand_from_tool(raw_tool: str, args: dict, warnings: list[str]) -> list[dict]:
    """Expand into 1..N "half-baked" actions based on raw tool name + arguments.

    The hook version (_Call) and the official-record version (official_loader)
    share this fan-out + semantic mapping so that both sources produce
    isomorphic Actions. Each returned dict contains
    action / path / root / command / subcommands / pattern.
    """
    tool = canonical_tool(raw_tool)

    if tool == "read":
        return _expand_read(raw_tool, args, warnings)

    if tool == "write":
        cmd = args.get("command")
        action = WRITE_ACTIONS.get(norm_param(cmd))
        if action is None:
            warnings.append(f"write encountered unknown command={cmd!r}, treated as modify_file")
            action = "modify_file"
        return [{"action": action, "path": args.get("path")}]

    if tool == "shell":
        command = args.get("command") or ""
        if not isinstance(command, str):
            command = str(command)   # N5: command is occasionally non-string; coerce before splitting
        return [{
            "action": "run_command",
            "command": command,
            "subcommands": split_subcommands(command),
            "root": args.get("working_dir"),
        }]

    if tool in ("grep", "glob"):
        # Note: grep/glob's path is "search root", not a file accessed, so it
        # lands in root, not path, to avoid polluting the file set.
        return [{
            "action": SIMPLE_ACTIONS[tool],
            "root": args.get("path"),
            "pattern": args.get("pattern"),
        }]

    if tool == "code":
        # code's file_path points at a specific file; path is the search root;
        # they have different semantics.
        op = args.get("operation") or "query"
        return [{
            "action": f"code_{op}",
            "path": args.get("file_path"),
            "root": args.get("path"),
            "pattern": args.get("symbol_name") or args.get("pattern"),
        }]

    if tool == "use_aws":
        svc = args.get("service_name") or "?"
        op = args.get("operation_name") or "?"
        return [{"action": "aws_call", "pattern": f"{svc}/{op}"}]

    if tool == "knowledge":
        cmd = args.get("command") or "op"
        # Note: context_id is an opaque ID, not a path. Putting it in root
        # would be path-normalized into "<cwd>/kb-1"; keep it in args and
        # do not move it into a semantic field.
        return [{
            "action": f"knowledge_{cmd}",
            "path": args.get("path"),
            "pattern": args.get("query"),
        }]

    if tool == "introspect":
        return [{"action": "docs_query", "pattern": args.get("query")}]

    if tool == "summary":
        # summary's taskDescription is the topic of this report; needed for evaluation
        return [{"action": "summarize", "pattern": args.get("taskDescription")}]

    if raw_tool == "use_subagent":
        cmd = args.get("command") or ""
        if norm_param(cmd) == "listagents":
            return [{"action": "list_subagents"}]
        subs = ((args.get("content") or {}).get("subagents")
                if isinstance(args.get("content"), dict) else None)
        if isinstance(subs, list) and subs:
            return [{
                "action": "spawn_subagent",
                "pattern": (s.get("agent_name") if isinstance(s, dict) else None),
            } for s in subs]
        return [{"action": "spawn_subagent", "pattern": None}]

    if tool == "subagent":
        # A single subagent call may dispatch multiple stages; each stage is
        # a separate child-agent dispatch and must be fanned out — otherwise
        # the "which child agent was invoked" information is completely lost.
        stages = args.get("stages")
        if isinstance(stages, list) and stages:
            return [{
                "action": "spawn_subagent",
                "pattern": (s.get("role") if isinstance(s, dict) else None),
                "command": (s.get("name") if isinstance(s, dict) else None),
            } for s in stages]
        return [{"action": "spawn_subagent", "pattern": args.get("task")}]

    if tool in SIMPLE_ACTIONS:
        return [{"action": SIMPLE_ACTIONS[tool]}]

    warnings.append(f"unknown tool {raw_tool!r}, action marked as {unknown_action(tool)}")
    return [{"action": unknown_action(tool)}]


def _expand_read(raw_tool: str, args: dict, warnings: list[str]) -> list[dict]:
    ops = args.get("operations")
    if not isinstance(ops, list) or not ops:
        warnings.append("read call missing operations array")
        return [{"action": "read_file", "path": args.get("path")}]

    out: list[dict] = []
    for op in ops:
        if not isinstance(op, dict):
            warnings.append("read.operations element is not an object")
            continue
        mode = op.get("mode")
        action = READ_MODE_ACTIONS.get(norm_param(mode))
        if action is None:
            warnings.append(f"read encountered unknown mode={mode!r}, treated as read_file")
            action = "read_file"

        if action == "read_image":
            # In Image mode the paths are in the image_paths array; secondary fan-out needed
            paths = op.get("image_paths")
            if isinstance(paths, list) and paths:
                out.extend({"action": action, "path": p} for p in paths)
                continue
        out.append({"action": action, "path": op.get("path")})

    if not out:
        out.append({"action": "read_file", "path": None})
    return out


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def normalize_events(
    raw_events: list[dict],
    session_id: str = "",
    source: str = "",
    include_responses: bool = False,
) -> TraceIR:
    warnings: list[str] = []
    (calls, prompts, responses, agent_name, run_count,
     run_prompts, run_started) = _scan(raw_events, warnings)

    actions: list[Action] = []
    idx = 0
    for call in calls:
        completed = call.post is not None
        duration = _duration_ms(call.pre.get("ts"), (call.post or {}).get("ts"))
        resp_size = (call.post or {}).get("response_size")
        response = None
        if include_responses and call.post is not None:
            response = _hook_response_text(call.post.get("tool_response"))

        for op_idx, part in enumerate(_expand(call, warnings)):
            actions.append(Action(
                idx=idx,
                call_idx=call.call_idx,
                op_idx=op_idx,
                ts=call.pre.get("ts", ""),
                turn=call.turn,
                run=call.run,
                raw_tool=call.tool_raw,
                tool=canonical_tool(call.tool_raw),
                action=part["action"],
                path=_norm_path(part.get("path"), call.pre.get("cwd")),
                root=_norm_path(part.get("root"), call.pre.get("cwd")),
                command=part.get("command"),
                subcommands=part.get("subcommands", []),
                pattern=part.get("pattern"),
                reasoning="",              # hook source has no per-call thinking; leave empty
                response=response,
                completed=completed,
                blocked=call.blocked,
                duration_ms=duration,
                resp_size=resp_size,
                args=call.args,
            ))
            idx += 1

    return TraceIR(
        session_id=session_id,
        source=source,
        agent_name=agent_name,
        prompts=prompts,
        responses=responses,
        actions=actions,
        warnings=warnings,
        run_count=run_count,
        run_prompts=run_prompts,
        run_started=run_started,
    )


def load_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A bad line must not fail parsing of the whole session
                out.append({"event": "__parse_error__", "lineno": lineno})
    return out


def normalize_file(path: str, enrich: bool = True,
                   official_dir: Optional[str] = None,
                   include_responses: bool = False) -> TraceIR:
    raw = load_jsonl(path)
    bad = [r for r in raw if r.get("event") == "__parse_error__"]
    raw = [r for r in raw if r.get("event") != "__parse_error__"]

    session_id = next((r.get("session_id") for r in raw if r.get("session_id")), "")
    if not session_id:
        session_id = os.path.basename(os.path.dirname(os.path.abspath(path)))

    ir = normalize_file_events(raw, session_id, path, include_responses)
    if bad:
        ir.warnings.insert(0, f"{len(bad)} lines failed JSON parsing, skipped")

    # Enrich and self-verify with Kiro's official session record
    if enrich:
        rec = load_official(session_id, official_dir)
        if rec.found:
            ir.official = rec
            # The official agent_name is authoritative; prefer it over the hook's /proc inference
            if rec.agent_name:
                ir.agent_name = rec.agent_name

        if ir.run_count <= 1:
            # Single startup: the whole directory is this one session; compare it as a whole
            if rec.found:
                ir.warnings.extend(cross_check(rec, ir.turns, ir.calls_per_turn,
                                               ir.tools_per_turn))
                # Use the official record to correct hook orphan misjudgments:
                # hook lost the post due to timeout etc., but the official
                # record confirms the call succeeded (measured on nested
                # kiro-cli long commands).
                n = _backfill_orphans(ir, session_id, official_dir)
                if n:
                    ir.warnings.append(
                        f"{n} hook orphans confirmed successful by official record, completed corrected")
        else:
            # Multiple startups: the directory mixes parent and child activity.
            # Comparing as a whole necessarily fails to line up; we must first
            # restore each run to its real official session and then compare
            # run by run.
            cwd = next((r.get("cwd") for r in raw if r.get("cwd")), None)
            started = {k: _parse_ts(v) for k, v in ir.run_started.items()}
            ir.run_attribution = resolve_runs(
                session_id, ir.run_prompts, started, cwd, official_dir)
            unresolved = [a.run for a in ir.run_attribution if not a.resolved]
            if unresolved:
                ir.warnings.append(f"the following runs could not be restored to official session: {unresolved}")
            ir.warnings.extend(_cross_check_per_run(ir, official_dir))
    return ir


def _action_key(a) -> tuple:
    """Content fingerprint of an action, used to match the same call across sources."""
    return (canonical_tool(a.tool), a.action,
            (a.command or a.path or a.pattern or a.root or "")[:200])


def _backfill_orphans(ir: TraceIR, session_id: str,
                      official_dir: Optional[str]) -> int:
    """Use the official record to correct hook orphan misjudgments.

    A hook orphan = has a pre but no post, usually meaning the execution did
    not complete; but measured on long commands whose "internal execution
    runs a whole agent dialog itself" (e.g., nested kiro-cli), the
    postToolUse hook times out and is lost, and those orphans actually
    succeeded. If the corresponding call in the official record is Success,
    we correct completed.

    Matching is by content fingerprint (tool + action + command/path);
    positional alignment is avoided so validation rejections and similar
    offsets do not misalign.
    """
    orphans = ir.orphans
    if not orphans:
        return 0
    from .official_loader import load_trace_from_official
    off = load_trace_from_official(session_id, official_dir)
    if not off.actions:
        return 0

    # Official side: content fingerprint → count of successes (multiple entries can share a fingerprint)
    ok_keys: dict[tuple, int] = {}
    for a in off.actions:
        if a.completed:            # official completed comes from Success result
            k = _action_key(a)
            ok_keys[k] = ok_keys.get(k, 0) + 1

    fixed = 0
    for a in orphans:
        k = _action_key(a)
        if ok_keys.get(k, 0) > 0:
            a.completed = True
            a.official_verified = True
            ok_keys[k] -= 1
            fixed += 1
    return fixed


def _cross_check_per_run(ir: TraceIR, official_dir: Optional[str]) -> list[str]:
    """Compare each run against its own official session."""
    out: list[str] = []
    for att in ir.run_attribution:
        if not att.resolved:
            continue
        rec = load_official(att.session_id, official_dir)  # type: ignore[arg-type]
        if not rec.found:
            continue
        acts = ir.by_run(att.run)
        if not acts:
            continue
        seen: set[int] = set()
        names: list[str] = []
        for a in acts:
            if a.call_idx not in seen:
                seen.add(a.call_idx)
                names.append(a.tool)

        official_total = sum(len(t.tool_names) for t in rec.turns)
        if att.is_trace_dir_session and len(names) <= official_total:
            # This run corresponds to the session that owns the trace directory
            # (the parent). After dispatching child agents the parent often
            # has several more turns, but the hook trace's run only covers
            # the part before dispatch, so hook < official is expected here
            # and we do not report it. Only hook > official is a real anomaly.
            continue

        msgs = cross_check(rec, 1, [len(names)], [names])
        for m in msgs:
            # Per-run comparison makes "turn count" meaningless (one run may span multiple turns); skip it
            if "turn count does not match official record" in m:
                continue
            out.append(f"run{att.run}({att.session_id[:8]}): {m}")
    return out


def normalize_file_events(raw: list[dict], session_id: str, path: str,
                          include_responses: bool = False) -> TraceIR:
    return normalize_events(raw, session_id=session_id, source=path,
                            include_responses=include_responses)


def iter_sessions(trace_dir: str) -> Iterable[str]:
    """List trace.jsonl paths of all sessions, sorted by mtime descending.

    Compatible with two layouts:
      flat   $TRACE_DIR/<session>/trace.jsonl
      daily  $TRACE_DIR/<YYYY-MM-DD>/<session>/trace.jsonl
    Recursive search via os.walk covers both (and tolerates deeper nesting).
    """
    if not os.path.isdir(trace_dir):
        return []
    paths = []
    for root, _dirs, files in os.walk(trace_dir):
        if "trace.jsonl" in files:
            paths.append(os.path.join(root, "trace.jsonl"))
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths


def default_trace_dir() -> str:
    """Hook trace data directory (output of the collection layer, not an evalkit internal path).

    This is the **external data source** location, written by the collection-
    layer hook; evalkit only reads it. Default: ~/agent-trace/traces; override
    with KIRO_TRACE_DIR to any location.
    """
    return os.environ.get(
        "KIRO_TRACE_DIR",
        os.path.join(os.path.expanduser("~"), "agent-trace", "traces"),
    )
