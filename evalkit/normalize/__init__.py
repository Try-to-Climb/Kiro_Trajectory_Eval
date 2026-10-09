"""Trajectory normalization layer -- collapses two heterogeneous record formats
into one Action sequence.

The three evaluation features on top (rule / goal / efficiency) consume only the
structures produced here and never touch raw records. Before changing this
layer, read "What to run after changing normalize/" in README.md -- an Action
field is shared by all three consumers, and a change usually only breaks
downstream.

================================================================================
INPUT -- two sources
================================================================================

**A. Kiro official session record (default; carries more information)**

  <official_dir>/<sid>.json    session metadata + per-turn usage
  <official_dir>/<sid>.jsonl   event stream, one {"version","kind","data"} per line

  official_dir resolution order: KIRO_SESSIONS_DIR -> $KIRO_HOME/sessions/cli
  -> ~/.kiro/sessions/cli. Note that when running inside a Kiro session,
  KIRO_HOME points at the *current* session's workspace, which has nothing to do
  with the session being analysed.

  Fields read from .json::

      session_id / cwd / created_at / updated_at / title
      parent_session_id             child -> parent; the ONLY parent/child criterion
      session_created_reason        subagent / rewind / ... (top-level sessions can
                                    also be marked subagent, so it must NOT be used
                                    to identify children -- see child_index)
      session_state.agent_name
      session_state.conversation_metadata.user_turn_metadatas[]
          builtin_tool_uses / total_request_count / number_of_cycles
          / end_reason / turn_duration{secs,nanos} / metering_usage[].value
          / context_usage_percentage / input_token_count / output_token_count
          / user_prompt_length / message_ids[] / loop_id.agent_id{name,parent_id}

  Only three .jsonl kinds are parsed; anything else lands in
  OfficialRecord.unknown_kinds::

      Prompt            data.content[].data              one turn of user input
      AssistantMessage  three content kinds:
                          text      -> reply body
                          thinking  -> Action.reasoning and Thinking
                          toolUse   -> {toolUseId, name, input{}} -> **Action**
      ToolResults       data.results{toolUseId: {tool{}, result{Success|Error}}}
                        -> backfills completed / error / resp_size / response

      Compaction        counted only (OfficialRecord.compactions), yields no Action

**B. hook trace (requires the hooks/ collector to be installed)**

  <trace_dir>/<sid>/trace.jsonl   one hook event per line. trace_dir defaults to
                                  ~/agent-trace/traces, override with KIRO_TRACE_DIR

  Event types::

      user_prompt     splits turns
      agent_spawn     splits runs. A sub-agent inherits its parent's
                      KIRO_SESSION_ID, so its events land in the parent's
                      directory; this event is what pulls them apart.
      pre_tool_use    {ts, tool, tool_input, cwd} -> **Action**
      post_tool_use   paired with pre; derives completed / duration_ms / resp_size
      stop            end of a turn

  The hook source has no reasoning and no error (official-only); the official
  source has no ts, run or blocked (hook-only). Both produce **isomorphic
  Actions** -- downstream does not need to know which source it got.

================================================================================
OUTPUT -- TraceIR
================================================================================

    TraceIR
      session_id / source / agent_name
      prompts[]         user input per turn
      responses[]       reply preview per turn
      actions[]         Action -- what downstream mainly consumes
      thinkings[]       Thinking, including segments with no toolUse
      warnings[]        data problems found while parsing. **When the record
                        cannot be read the loader records a warning and returns
                        an empty IR rather than raising** -- callers must echo
                        these, or they get a verdict computed over nothing.
      official          OfficialRecord (optional enrichment)
      run_count / run_prompts / run_started / run_attribution

One tool call can **fan out** into several Actions: the operations[] of a batch
read, multiple image_paths, or several toolUse items in one message. Each gets
its own Action, distinguished by op_idx, so len(actions) >= number of calls.

Action's 27 fields, in five groups (details in schema.py)::

    position    idx call_idx op_idx ts turn run
    provenance  sid ref              ref = "<first 8 of sid>#<idx>", unique across
                                     sessions (idx alone is not -- several child
                                     sessions each have idx=7)
    tool        raw_tool tool         raw_tool is as recorded, tool is alias-folded
    semantic    action path root command subcommands pattern purpose
    reasoning   reasoning response    response is off by default; pass
                                      include_responses=True
    derived     completed blocked duration_ms resp_size tool_use_id error
                official_verified
    raw         args                  kept verbatim, no noise stripping

`action` is the semantic action name (read_file / modify_file / run_command /
search_content / search_files / code_lookup_symbols / spawn_subagent / ...),
decided by tool name *and* argument shape -- e.g. a write to an existing file
becomes modify_file while a write to a new one becomes create_file. To find out
what actions a given run produced, start with
`python3 -m normalize.cli dump <sid> --official`.

================================================================================
USAGE
================================================================================

    # official source (preferred)
    from normalize import load_trace_from_official
    ir = load_trace_from_official("<sid>", official_dir=None)

    # hook source
    from normalize import normalize_file
    ir = normalize_file("traces/<sid>/trace.jsonl")

    # consumed identically either way
    for a in ir.actions:
        print(a.ref, a.turn, a.action, a.path or a.command)
    for w in ir.warnings:                      # do not skip this
        print("[warn]", w)

    # parent/child relations (for building a run tree)
    from normalize import child_index
    kids = child_index(official_dir)           # {parent_sid: [child_sid, ...]}

Command line: cli.py -- dump / table / stats / timeline / replay / compare /
export-otel. OTel GenAI projection: otel_semconv.py and ../OTEL_MAPPING.md.
"""

from .core import (
    default_trace_dir,
    iter_sessions,
    load_jsonl,
    normalize_events,
    normalize_file,
)
from .official import OfficialRecord, TurnMeta, child_index, load_official, read_parent
from .official_loader import iter_official_sessions, load_trace_from_official
from .mapping import TOOL_ALIASES, canonical_tool, split_subcommands
from .schema import Action, TraceIR
from .otel_semconv import (
    action_attributes,
    operation_of,
    span_name,
    span_status,
    trace_attributes,
    FIELD_MAP,
)
from .otel_export import build_otlp, to_otlp_json

__all__ = [
    "Action",
    "TraceIR",
    "OfficialRecord",
    "TurnMeta",
    "load_official",
    "child_index",
    "read_parent",
    "load_trace_from_official",
    "iter_official_sessions",
    "normalize_file",
    "normalize_events",
    "load_jsonl",
    "iter_sessions",
    "default_trace_dir",
    "canonical_tool",
    "split_subcommands",
    "TOOL_ALIASES",
    # OTel GenAI semantic alignment
    "action_attributes",
    "operation_of",
    "span_name",
    "span_status",
    "trace_attributes",
    "FIELD_MAP",
    "build_otlp",
    "to_otlp_json",
]
