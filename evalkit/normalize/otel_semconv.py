"""**Alignment layer** between TraceIR and the OpenTelemetry GenAI semantic conventions.

Does only "semantic alignment": project TraceIR fields into the attribute
dictionaries defined by the OTel GenAI conventions (``gen_ai.*`` / ``error.*``
standard vocabulary + ``kiro.*`` extensions).

**Intentionally not** done here (see the README architecture decisions):
  - No OTLP wire format (protobuf), no dependency on the opentelemetry SDK;
  - No changes to TraceIR's flat structure (the rule engine depends on it).
This layer emits plain dicts; when we eventually export real OTLP, those
dicts serve as a 1:1 source for span attributes, lossless and unambiguous.

Spec sources (verified online 2026-08):
  - Agent/tool spans: github.com/open-telemetry/semantic-conventions-genai
    (``gen-ai-agent-spans.md``, currently **Status: Development**, fields may evolve)
  - ``gen_ai.tool.*`` / ``error.type``: core semantic-conventions registry
    (``error.type`` is already Stable)
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# GenAI operation.name vocabulary (a subset of well-known values)
# ---------------------------------------------------------------------------
OP_INVOKE_AGENT = "invoke_agent"   # invoke a (child) agent
OP_EXECUTE_TOOL = "execute_tool"   # execute a tool call

# Semantic actions that represent "invoking an agent"; everything else is a tool execution
_AGENT_ACTIONS = {"spawn_subagent"}


def operation_of(action: str) -> str:
    """Map a TraceIR semantic action to an OTel operation.name."""
    return OP_INVOKE_AGENT if action in _AGENT_ACTIONS else OP_EXECUTE_TOOL


# ---------------------------------------------------------------------------
# Field alignment table (for docs / introspection / validation)
#   TraceIR field -> OTel attribute (None means it lands on span metadata rather than attributes)
# ---------------------------------------------------------------------------
FIELD_MAP: dict[str, str] = {
    # Standard gen_ai.* / error.* (with corresponding conventions)
    "tool":         "gen_ai.tool.name",
    "tool_use_id":  "gen_ai.tool.call.id",
    "pattern(spawn_subagent)": "gen_ai.agent.name",  # on dispatch, pattern is the invoked agent name
    "path|command|pattern|root": "gen_ai.tool.call.arguments",  # synthesized as tool call arguments
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
    "response":     "kiro.tool.response", # full tool response (only projected when present; not collected by default)
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


def action_attributes(action: dict) -> dict[str, Any]:
    """Project an action (Action.to_dict() or an equivalent dict) into an OTel span attribute dict.

    Returns ``gen_ai.*`` / ``error.*`` standard attributes plus ``kiro.*`` extensions.
    Does not include span name/time/status (those are span metadata; see span_name / span_status).
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
        call_args = {k: a[k] for k in ("path", "command", "pattern", "root")
                     if a.get(k)}
        if call_args:
            attrs["gen_ai.tool.call.arguments"] = call_args

    if a.get("error"):
        attrs["error.type"] = a["error"]

    # kiro.* extensions: signals specific to evalkit with no OTel counterpart
    attrs["kiro.action"] = a.get("action")
    if a.get("raw_tool"):
        attrs["kiro.raw_tool"] = a["raw_tool"]
    if a.get("reasoning"):
        attrs["kiro.reasoning"] = a["reasoning"]        # agent thinking (OTel has no tool-level reasoning)
    if a.get("response") is not None:
        attrs["kiro.tool.response"] = a["response"]     # full tool response (present only when include_responses)
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
