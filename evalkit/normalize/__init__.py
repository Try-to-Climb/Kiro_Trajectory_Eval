"""trace.jsonl normalization layer.

Usage:
    from normalize import normalize_file
    ir = normalize_file("traces/<session>/trace.jsonl")
    for a in ir.actions:
        print(a.turn, a.action, a.path)
"""

from .core import (
    default_trace_dir,
    iter_sessions,
    load_jsonl,
    normalize_events,
    normalize_file,
)
from .official import OfficialRecord, TurnMeta, load_official
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
