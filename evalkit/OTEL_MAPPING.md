# TraceIR ↔ OpenTelemetry GenAI Semantic Alignment

evalkit's `TraceIR` is field-level aligned with the OpenTelemetry **GenAI semantic conventions**, so the normalized output "speaks OTel's lingua franca".

## Architectural Decision: Align on Semantics, Not the Wire Format

We explicitly **align only on the vocabulary (semantic conventions); we do not adopt the OTLP wire format as our native model.** Reasons:

- **Stability**: the GenAI agent span spec is currently `Status: Development` (verified 2026-08, split off into `open-telemetry/semantic-conventions-genai`), and its fields will change. Building the foundation on an evolving spec is unstable; keeping it at the boundary (a projection layer) is right.
- **Don't sacrifice the rule engine**: trajectory's checkers depend on `TraceIR`'s flat `actions[]` (`path`/`command`/`idx` are directly queryable). If the native format became a nested OTLP span tree, matcher/checker would all have to be rewritten to traverse the tree and unpack `arguments` JSON — rewriting the one irreplaceable part with zero functional gain.
- **Ergonomics**: OTLP is a protobuf wire format, not ideal for direct manipulation in analysis code. Even OTel tools work with in-memory objects and only serialize at the boundary.

Hence: `TraceIR` remains a stable, flat, handy **working model**; `normalize/otel_semconv.py` provides a **pure-dict projection** (no protobuf, no SDK dependency); if we ever really need to emit OTLP, feed this projection into the official SDK — 1:1 lossless.

## Usage

```python
from normalize import normalize_file
ir = normalize_file("traces/<session>/trace.jsonl")

ir.otel_trace_attributes()      # trace-level: gen_ai.conversation.id / gen_ai.agent.name ...
ir.to_otel_spans()              # each action → {name, operation, status, start, duration_ms, attributes}

a = ir.actions[0]
a.otel_operation()              # "execute_tool" | "invoke_agent"
a.otel_attributes()             # gen_ai.* / error.* / kiro.* attribute dict
```

## operation.name Mapping

| TraceIR action | OTel operation.name | span name |
|----------------|---------------------|-----------|
| `spawn_subagent` | `invoke_agent` | `invoke_agent {invoked agent name}` |
| Everything else (read/write/shell/grep/code/aws/...) | `execute_tool` | `execute_tool {tool}` |

## Field Mapping

### Standard attributes (with OTel conventions)

| TraceIR field | OTel attribute | Spec status |
|---------------|----------------|-------------|
| `tool` | `gen_ai.tool.name` | Stable (registry) |
| `tool_use_id` | `gen_ai.tool.call.id` | registry |
| `pattern` (spawn_subagent only) | `gen_ai.agent.name` | Development |
| `path` / `command` / `pattern` / `root` | Composed into `gen_ai.tool.call.arguments` (dict) | registry |
| `error` | `error.type` + span status=ERROR | **Stable** |
| `session_id` (trace) | `gen_ai.conversation.id` | — |
| `agent_name` (trace) | `gen_ai.agent.name` | Development |
| `ts` | span start time | native |
| `duration_ms` | span duration | native |
| `completed` / `blocked` / `error` | span status (OK/ERROR/UNSET) | native |

### Extension attributes (`kiro.*`, no OTel counterpart)

| TraceIR field | Extension attribute | Why an extension |
|---------------|---------------------|-------------------|
| `action` | `kiro.action` | OTel only has `execute_tool`; no semantic split like read_file/modify_file |
| `raw_tool` | `kiro.raw_tool` | Original tool name |
| `reasoning` | `kiro.reasoning` | Agent thinking (official thinking); OTel has no tool-level reasoning. **Collected by default** (only projected when present) |
| `response` | `kiro.tool.response` | Full tool reply; **not collected by default**, only present with `--with-responses` / `include_responses=True` (can be huge) |
| `blocked` | `kiro.blocked` | **Blocked by policy = intended to call but not executed**; standard spans (representing operations that happened) can't express this |
| `completed` | `kiro.completed` | — |
| `turn` / `run` / `idx` | `kiro.turn` / `kiro.run` / `kiro.idx` | Conversation turn / invocation / order; used by evalkit for order judgments |
| `official_verified` | `kiro.official_verified` | Success/fail was corrected by official records |

## span status Rules

| Condition | status |
|-----------|--------|
| `error` non-empty or `blocked=True` | `ERROR` |
| `completed=True` | `OK` |
| Otherwise (pre orphan without post) | `UNSET` |

## Known Limitations

- Producing actions like `create_file` are also grouped under `execute_tool` in OTel (the tool is write); the artifact name doesn't go into a standard field — read from `gen_ai.tool.call.arguments.path` when needed.
- Token / credit info from official records is currently only projected to `kiro.credits`; `gen_ai.usage.*_tokens` will be added later once official records reliably contain per-item token breakdowns (avoid fabricating fields).
- The agent span spec is still evolving; `gen_ai.agent.*` field names may change; if so, only `otel_semconv.py` needs updating in one place.

## Related Files

- `normalize/otel_semconv.py` — Semantic projection (vocabulary alignment) + `FIELD_MAP`
- `normalize/otel_export.py` — OTLP/JSON export (wire alignment)
- `normalize/schema.py` — Trailing `[→ ...]` annotations on fields + `Action.otel_*()` / `TraceIR.to_otel_spans()`
- `normalize/tests/test_otel_semconv.py` — 8 semantic-alignment tests
- `normalize/tests/test_otel_export.py` — 11 OTLP-export tests

---

# OTLP/JSON Export (Wire Alignment)

On top of "semantic alignment", `normalize/otel_export.py` further exports TraceIR as the
**official OTLP JSON encoding** (`ExportTraceServiceRequest`), directly ingestible by any OTLP/HTTP
collector (Jaeger / Grafana Tempo / otel-collector). **Zero third-party dependencies**
— OTLP/JSON is a pure JSON spec, hand-constructed per spec.

## One-command Export (three data sources)

```bash
# hook trace (pure hook source)
python3 -m normalize.cli export-otel <session-id> --source hook     --out out.json
# Kiro official session records (pure official source)
python3 -m normalize.cli export-otel <session-id> --source official --out out.json
# Both combined (hook trace enriched with official records: orphan correction, authoritative agent name, cross-source validation)
python3 -m normalize.cli export-otel <session-id> --source both     --out out.json
```

Without `--out`, prints to stdout; `--compact` emits single-line JSON.

## Export Structure

```
ExportTraceServiceRequest
└─ resourceSpans[]
   ├─ resource            service.name=kiro-cli, kiro.trace.source=<source>,
   │                      gen_ai.conversation.id, gen_ai.agent.name
   └─ scopeSpans[]
      ├─ scope            name=evalkit.normalize, version=1.0
      └─ spans[]
         ├─ each run  → root span  invoke_agent {agent} (no parentSpanId)
         └─ each action → child span  execute_tool {tool} / invoke_agent {sub}
                       parentSpanId = the containing run's root span
```

- **traceId** (32 hex) is stably derived from session_id; **spanId** (16 hex) is derived from session+run+idx → idempotent, re-runnable
- **Time**: hook source uses real ts + duration_ms; official source has empty ts, synthesizes order-preserving nanoseconds by idx (order-preserving only, not wall-clock)
- **status**: `error`/`blocked` → ERROR (2); `completed` → OK (1); orphan → UNSET (0)
- **Attributes**: scalars per OTLP types (int64 encoded as string); composite values (tool arguments) encoded as JSON stringValue

## Verification

`normalize.otel_export.build_otlp(ir, source)` returns a dict; `to_otlp_json()` returns a string.
All sessions × three scenarios have been structurally validated (see "tests"): traceId/spanId hex length,
resolvable parent refs, start≤end, valid status/kind enums, unique attribute typed value, single traceId per trace.
