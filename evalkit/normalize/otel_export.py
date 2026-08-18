"""TraceIR → OpenTelemetry **OTLP/JSON** export.

This is one step of "carrying alignment": project a TraceIR into OTLP's
official JSON encoding (``ExportTraceServiceRequest``, i.e. the
resourceSpans → scopeSpans → spans tree), which can be ingested by any
OTLP/HTTP collector (Jaeger / Tempo / otel-collector) directly.

We do not depend on the opentelemetry SDK — OTLP/JSON is a pure JSON spec,
and we construct it by hand, with zero third-party dependencies. Attribute
vocabulary follows the alignment in otel_semconv.py (gen_ai.* / kiro.*).

Span tree structure:
    Each run (agent startup) → one root span ``invoke_agent {agent}``;
    each action under that run → one child span (parent = that run's root span).
    The entire session shares a single traceId.

Time: hook-source actions have ts (+ duration_ms); the official source has
empty ts, in which case we synthesize monotonically increasing nanosecond
times by idx (order-preserving only; not a real wall clock).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from .schema import TraceIR
from . import otel_semconv as sem

# span status code: 0=UNSET 1=OK 2=ERROR
_STATUS_CODE = {"UNSET": 0, "OK": 1, "ERROR": 2}
# span kind: GenAI convention uses CLIENT(3) for spans
_KIND_CLIENT = 3
_KIND_INTERNAL = 1

# When the official source has no wall clock, use this base + idx to synthesize order-preserving times
_SYNTH_BASE_NS = 1_700_000_000_000_000_000   # near 2023-11-14; an arbitrary fixed base
_SYNTH_STEP_NS = 1_000_000                    # 1ms between actions


def _trace_id(session_id: str) -> str:
    """Deterministically derive a 16-byte (32 hex) traceId from session_id."""
    return hashlib.sha256(("trace:" + session_id).encode()).hexdigest()[:32]


def _span_id(session_id: str, *parts: Any) -> str:
    """Deterministically derive an 8-byte (16 hex) spanId from session_id + locator parts."""
    key = "span:" + session_id + ":" + ":".join(str(p) for p in parts)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _parse_ns(ts: Optional[str]) -> Optional[int]:
    """ISO time string → epoch nanoseconds; None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, AttributeError):
        return None


def _attr(key: str, value: Any) -> Optional[dict]:
    """Construct an OTLP KeyValue. Skip None; encode composite values as a JSON stringValue
    (the GenAI convention permits a JSON string when the structural type is unsupported)."""
    if value is None:
        return None
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}          # OTLP encodes int64 as a string
    elif isinstance(value, float):
        v = {"doubleValue": value}
    elif isinstance(value, str):
        v = {"stringValue": value}
    else:  # dict / list → JSON string
        v = {"stringValue": json.dumps(value, ensure_ascii=False)}
    return {"key": key, "value": v}


def _attrs(d: dict) -> list[dict]:
    out = []
    for k, val in d.items():
        kv = _attr(k, val)
        if kv is not None:
            out.append(kv)
    return out


def build_otlp(ir: TraceIR, source: str = "hook") -> dict:
    """Build the TraceIR into an OTLP/JSON ExportTraceServiceRequest.

    source: "hook" | "official" | "hook+official"; written to the resource's kiro.trace.source.
    """
    sid = ir.session_id or "unknown"
    trace_id = _trace_id(sid)

    # ---- resource ----
    res_attrs = {
        "service.name": "kiro-cli",
        "kiro.trace.source": source,
    }
    res_attrs.update(ir.otel_trace_attributes())   # gen_ai.conversation.id / agent.name / kiro.credits
    resource = {"attributes": _attrs(res_attrs)}

    spans: list[dict] = []

    # ---- one root invoke_agent span per run ----
    runs = sorted({a.run for a in ir.actions}) or [1]
    run_root_id: dict[int, str] = {}
    for r in runs:
        root_id = _span_id(sid, "run", r)
        run_root_id[r] = root_id
        run_actions = [a for a in ir.actions if a.run == r]
        # Root span time range = envelope of action times within this run
        ns_list = [n for a in run_actions
                   for n in [_action_start_ns(a, ir)] if n is not None]
        start = min(ns_list) if ns_list else _SYNTH_BASE_NS
        end = max(ns_list) if ns_list else start
        spans.append({
            "traceId": trace_id,
            "spanId": root_id,
            "name": f"{sem.OP_INVOKE_AGENT} {ir.agent_name or sid[:8]}",
            "kind": _KIND_CLIENT,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(max(end, start)),
            "attributes": _attrs({
                "gen_ai.operation.name": sem.OP_INVOKE_AGENT,
                "gen_ai.agent.name": ir.agent_name,
                "gen_ai.conversation.id": sid,
                "kiro.run": r,
            }),
            "status": {"code": _STATUS_CODE["OK"]},
        })

    # ---- one child span per action ----
    for a in ir.actions:
        d = a.to_dict()
        start = _action_start_ns(a, ir)
        if start is None:
            start = _SYNTH_BASE_NS + a.idx * _SYNTH_STEP_NS
        dur = (a.duration_ms or 0) * 1_000_000
        end = start + dur
        status = sem.span_status(d)
        span = {
            "traceId": trace_id,
            "spanId": _span_id(sid, "act", a.run, a.idx),
            "parentSpanId": run_root_id.get(a.run, run_root_id[runs[0]]),
            "name": sem.span_name(d),
            "kind": _KIND_CLIENT if a.otel_operation() == sem.OP_INVOKE_AGENT
                    else _KIND_INTERNAL,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(max(end, start)),
            "attributes": _attrs(a.otel_attributes()),
            "status": {"code": _STATUS_CODE[status]},
        }
        spans.append(span)

    return {
        "resourceSpans": [{
            "resource": resource,
            "scopeSpans": [{
                "scope": {"name": "evalkit.normalize", "version": "1.0"},
                "spans": spans,
            }],
        }]
    }


# Synthesized time allocation: for the official source, monotonically increasing by idx within a session
def _action_start_ns(a, ir: TraceIR) -> Optional[int]:
    """Action start nanoseconds: prefer real ts; otherwise None (caller synthesizes)."""
    return _parse_ns(a.ts)


def to_otlp_json(ir: TraceIR, source: str = "hook", indent: int | None = None) -> str:
    return json.dumps(build_otlp(ir, source), ensure_ascii=False, indent=indent)
