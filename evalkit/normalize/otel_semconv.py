"""**Alignment layer** between TraceIR and the OpenTelemetry GenAI semantic conventions.

Does only "semantic alignment": project TraceIR fields into the attribute
dictionaries defined by the OTel GenAI conventions (``gen_ai.*`` / ``error.*``
standard vocabulary + ``kiro.*`` extensions).

**Intentionally not** done here (see the README architecture decisions):
  - No OTLP wire format (protobuf), no dependency on the opentelemetry SDK;
  - No changes to TraceIR's flat structure (the rule engine depends on it).
This layer emits plain dicts; when we eventually export real OTLP, those
dicts serve as a 1:1 source for span attributes, lossless and unambiguous.

Spec sources (aligned with ``open-telemetry/semantic-conventions-genai`` 2026-10;
the convention remains ``Status: Development`` and fields may continue to evolve):
  - Agent spans are now split into **invoke_agent client** (cross-process,
    CLIENT kind, requires ``gen_ai.provider.name``) and **invoke_agent
    internal** (same-process, INTERNAL kind). Kiro's sub-agent dispatch
    maps to **internal**.
  - execute_tool spans gain recommended ``gen_ai.tool.type`` /
    ``gen_ai.tool.description``; ``gen_ai.tool.call.arguments`` and
    ``gen_ai.tool.call.result`` have been demoted to **Opt-In** (must be
    explicitly enabled to emit).
  - ``error.type`` is Stable; ``gen_ai.agent.id`` no longer appears on
    internal spans.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# GenAI operation.name vocabulary (a subset of well-known values)
#   The full spec list also includes: create_agent / invoke_workflow / plan /
#   retrieval / fetch_response / search_memory / create_memory / update_memory
#   / upsert_memory / delete_memory / create_memory_store / delete_memory_store.
#   Kiro has no corresponding actions today, so these are not listed in
#   _AGENT_ACTIONS; when adding one, consult the upstream vocabulary first.
# ---------------------------------------------------------------------------
OP_INVOKE_AGENT = "invoke_agent"   # invoke a (child) agent
OP_EXECUTE_TOOL = "execute_tool"   # execute a tool call

# Semantic actions that represent "invoking an agent"; everything else is a tool execution
_AGENT_ACTIONS = {"spawn_subagent"}


def operation_of(action: str) -> str:
    """Map a TraceIR semantic action to an OTel operation.name."""
    return OP_INVOKE_AGENT if action in _AGENT_ACTIONS else OP_EXECUTE_TOOL


# ---------------------------------------------------------------------------
# Kiro built-in tool -> gen_ai.tool.type / description (execute_tool recommended)
#
# Spec-sanctioned values of gen_ai.tool.type include:
#   - "function":  executes within the client / agent-controlled environment
#                  (file read/write, code intelligence)
#   - "extension": bridges to a real external system (shell, cloud APIs)
#   - "datastore": structured knowledge retrieval
# Omitted when unknown (avoid guessing).
# ---------------------------------------------------------------------------
_TOOL_TYPES: dict[str, str] = {
    "read":       "function",
    "write":      "function",
    "code":       "function",
    "grep":       "function",
    "glob":       "function",
    "introspect": "function",
    "todo_list":  "function",
    "shell":      "extension",
    "use_aws":    "extension",
    "knowledge":  "datastore",
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read":       "Read files, directories, or images.",
    "write":      "Create or modify text files.",
    "code":       "Language-aware code intelligence (search / AST ops).",
    "grep":       "Regex search over files.",
    "glob":       "Find paths matching a glob pattern.",
    "shell":      "Execute shell commands.",
    "use_aws":    "Invoke an AWS CLI operation.",
    "introspect": "Query this assistant's own documentation.",
    "todo_list":  "Create / update the task list.",
    "knowledge":  "Semantic / keyword search over indexed knowledge bases.",
}


# ---------------------------------------------------------------------------
# Field alignment table (for docs / introspection / validation)
#   TraceIR field -> OTel attribute (None means it lands on span metadata rather than attributes)
# ---------------------------------------------------------------------------
FIELD_MAP: dict[str, str] = {
    # Standard gen_ai.* / error.* (with corresponding conventions)
    "tool":         "gen_ai.tool.name",
    "tool_use_id":  "gen_ai.tool.call.id",
    "pattern(spawn_subagent)": "gen_ai.agent.name",  # on dispatch, pattern is the invoked agent name
    "path|command|pattern|root": "gen_ai.tool.call.arguments (Opt-In)",
    "response":     "gen_ai.tool.call.result (Opt-In)",
    "error":        "error.type",
    "ts":           "(span start time)",
    "duration_ms":  "(span duration)",
    "completed/blocked/error": "(span status)",
    # trace level
    "session_id":   "gen_ai.conversation.id",
    "agent_name":   "gen_ai.agent.name",
    # kiro.* extensions (no OTel counterpart; evalkit-specific)
    "action":       "kiro.action",       # fine-grained semantic action (read_file/modify_file...)
    "raw_tool":     "kiro.raw_tool",
    "reasoning":    "kiro.reasoning",     # agent thinking (only projected when present)
    "purpose":      "kiro.tool.purpose",  # call reason (args.__tool_use_purpose)
    "blocked":      "kiro.blocked",       # blocked by policy (cannot be expressed by standard span)
    "completed":    "kiro.completed",
    "turn":         "kiro.turn",
    "run":          "kiro.run",
    "idx":          "kiro.idx",
    "official_verified": "kiro.official_verified",
}


def span_name(action: dict) -> str:
    """OTel-convention span name: ``execute_tool {tool}`` / ``invoke_agent {agent}``."""
    a = action
    op = operation_of(a.get("action", ""))
    if op == OP_INVOKE_AGENT:
        return f"{OP_INVOKE_AGENT} {a.get('pattern') or ''}".strip()
    return f"{OP_EXECUTE_TOOL} {a.get('tool') or a.get('raw_tool') or ''}".strip()


def action_attributes(action: dict, *, include_tool_io: bool = False) -> dict[str, Any]:
    """Project an action (Action.to_dict() or an equivalent dict) into an OTel span attribute dict.

    Returns ``gen_ai.*`` / ``error.*`` standard attributes plus ``kiro.*``
    extensions. Does not include span name/time/status (those are span
    metadata; see span_name / span_status).

    ``include_tool_io``: whether to project the Opt-In
    ``gen_ai.tool.call.arguments`` and ``gen_ai.tool.call.result``. The spec
    marks these as Opt-In (may contain sensitive data); off by default. Even
    when off, the call reason is still visible via the ``kiro.*`` extension
    (``kiro.tool.purpose``).
    """
    a = action
    op = operation_of(a.get("action", ""))
    attrs: dict[str, Any] = {"gen_ai.operation.name": op}

    if op == OP_INVOKE_AGENT:
        if a.get("pattern"):
            attrs["gen_ai.agent.name"] = a["pattern"]
    else:  # execute_tool
        if a.get("tool"):
            attrs["gen_ai.tool.name"] = a["tool"]
        if a.get("tool_use_id"):
            attrs["gen_ai.tool.call.id"] = a["tool_use_id"]
        tool_type = _TOOL_TYPES.get(a.get("tool") or "")
        if tool_type:
            attrs["gen_ai.tool.type"] = tool_type
        tool_desc = _TOOL_DESCRIPTIONS.get(a.get("tool") or "")
        if tool_desc:
            attrs["gen_ai.tool.description"] = tool_desc
        if include_tool_io:
            call_args = {k: a[k] for k in ("path", "command", "pattern", "root")
                         if a.get(k)}
            if call_args:
                attrs["gen_ai.tool.call.arguments"] = call_args
            if a.get("response") is not None:
                attrs["gen_ai.tool.call.result"] = a["response"]

    if a.get("error"):
        attrs["error.type"] = a["error"]

    # kiro.* extensions: signals specific to evalkit with no OTel counterpart
    attrs["kiro.action"] = a.get("action")
    if a.get("raw_tool"):
        attrs["kiro.raw_tool"] = a["raw_tool"]
    if a.get("reasoning"):
        attrs["kiro.reasoning"] = a["reasoning"]        # agent thinking (OTel has no tool-level reasoning)
    if a.get("purpose"):
        attrs["kiro.tool.purpose"] = a["purpose"]        # Kiro's call-reason field
    attrs["kiro.blocked"] = bool(a.get("blocked"))
    attrs["kiro.completed"] = bool(a.get("completed"))
    for k in ("turn", "run", "idx"):
        if a.get(k) is not None:
            attrs[f"kiro.{k}"] = a[k]
    if a.get("official_verified"):
        attrs["kiro.official_verified"] = True
    return attrs


def span_status(action: dict) -> str:
    """OTel span status: ERROR (error/blocked) / OK (completed) / UNSET (unclosed orphan)."""
    a = action
    if a.get("error") or a.get("blocked"):
        return "ERROR"
    if a.get("completed"):
        return "OK"
    return "UNSET"


def trace_attributes(ir) -> dict[str, Any]:
    """Trace(whole-session)-level alignment attributes, for the root span / resource."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": OP_INVOKE_AGENT,
        "gen_ai.conversation.id": getattr(ir, "session_id", None),
    }
    if getattr(ir, "agent_name", None):
        attrs["gen_ai.agent.name"] = ir.agent_name
    credits = getattr(ir, "credits", None)
    if credits is not None:
        attrs["kiro.credits"] = credits          # no OTel "credits"; use extension
    return {k: v for k, v in attrs.items() if v is not None}
