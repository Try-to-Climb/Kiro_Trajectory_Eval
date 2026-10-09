"""Data structures for the normalization layer.

A single raw trace.jsonl record can produce 0..N Actions after normalization:
  - read's operations array fans out into multiple Actions
  - user_prompt / stop / agent_spawn produce no Action (only used to derive turn)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Action:
    """A single "action" after normalization.

    Fields fall into three groups:
      1. positional   idx / call_idx / op_idx / turn / ts
      2. semantic     action / path / root / command / subcommands / pattern
      3. derived      completed / blocked / duration_ms / resp_size

    OTel GenAI semantic alignment (see otel_semconv.py / OTEL_MAPPING.md):
    end-of-line [→ ...] notes which OpenTelemetry attribute the field projects to.
    """

    # ---- position ----
    idx: int                      # global order after fan-out; rule engine uses it to judge sequencing  [→ kiro.idx]
    call_idx: int                 # which tool call this comes from (pre-fan-out sequence)
    op_idx: int                   # which operation within a batch call; 0 for singleton operations
    ts: str                       # start time (taken from the pre_tool_use ts)                            [→ span start time]
    turn: int                     # which conversation turn; split by user_prompt; first turn is 1        [→ kiro.turn]
    run: int                      # which agent startup. Child agents inherit the parent's KIRO_SESSION_ID;
                                  # their hook events land in the same directory, split by agent_spawn   [→ kiro.run]

    # ---- tool ----
    raw_tool: str                 # raw tool name, e.g. execute_bash                                       [→ kiro.raw_tool]
    tool: str                     # canonical tool name after alias normalization, e.g. shell             [→ gen_ai.tool.name]

    # ---- semantic ----
    action: str                   # semantic action, e.g. read_file / modify_file                          [→ kiro.action; drives operation.name]

    # ---- provenance ----
    # idx is only unique within one session -- merging across sessions collides
    # (observed: several child sessions each have idx=7). Once an Action goes
    # through to_dict() it loses its provenance, and all three consumers pass
    # bare dicts around, so provenance must live on a field, not a @property.
    # ref is the single source of goal's evidence-bank references, judge-output
    # validation, and efficiency reports' `<sid8>#<idx>` identifiers.
    sid: str = ""                 # owning session id
    ref: str = ""                 # "<first 8 chars of sid>#<idx>", unique across sessions
    path: Optional[str] = None    # the specific file this action targets                                  [→ gen_ai.tool.call.arguments]
    root: Optional[str] = None    # search root for search-type actions (grep/glob path lands here)        [→ gen_ai.tool.call.arguments]
    command: Optional[str] = None       # full command for shell-type actions                             [→ gen_ai.tool.call.arguments]
    subcommands: list[str] = field(default_factory=list)  # subcommands after splitting by && ; || newline
    pattern: Optional[str] = None       # grep/glob pattern; on spawn, the invoked agent's name           [→ arguments or gen_ai.agent.name]
    purpose: Optional[str] = None       # Short intent from args.__tool_use_purpose (Kiro CLI's built-in call reason)  [→ kiro.tool.purpose]

    # ---- reasoning / response ----
    reasoning: str = ""           # agent thinking before this tool call (official thinking; hook source has none) [→ kiro.reasoning]
    response: Optional[str] = None      # full tool response text (**not collected by default**; only when include_responses) [→ kiro.tool.response]

    # ---- derived ----
    completed: bool = False       # whether a paired post_tool_use exists                                  [→ kiro.completed; drives span status]
    blocked: bool = False         # whether blocked by a preToolUse policy (no OTel equivalent)            [→ kiro.blocked]
    duration_ms: Optional[int] = None   # elapsed time from pre to post                                    [→ span duration]
    resp_size: Optional[int] = None     # response byte size
    tool_use_id: Optional[str] = None   # toolUseId from the official record, used to back-fill results    [→ gen_ai.tool.call.id]
    error: Optional[str] = None         # failure info (official record Error; None on the hook side)      [→ error.type; drives span status]
    official_verified: bool = False     # completed corrected by the official record                       [→ kiro.official_verified]

    # ---- raw arguments (preserved as-is; this revision does not filter noise) ----
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ---- OTel GenAI semantic projection (delegated to otel_semconv to avoid a circular dependency) ----
    def otel_operation(self) -> str:
        """The OTel operation.name for this action (execute_tool / invoke_agent)."""
        from .otel_semconv import operation_of
        return operation_of(self.action)

    def otel_attributes(self) -> dict[str, Any]:
        """Project into an OTel span attribute dict (gen_ai.* / error.* / kiro.*)."""
        from .otel_semconv import action_attributes
        return action_attributes(self.to_dict())


@dataclass
class Thinking:
    """A single agent thinking segment.

    Stored as a peer of Action (independent list):
      - Message with toolUse -> thinking is *also* copied into Action.reasoning
        for backward compatibility, and recorded here so any consumer can
        retrieve the full text.
      - Message without toolUse (pure thinking / pure reply) -> Action.reasoning
        has nowhere to attach and the thinking would be lost; this list is the
        only place it lives. Critical signal for efficiency evaluation when
        judging whether a read was actually consumed by the agent.
    """
    turn: int                                   # dialogue turn number
    text: str                                   # full thinking text
    has_tool_use: bool                          # whether the same message also had a toolUse
    action_refs: list[int] = field(default_factory=list)  # Action.idx(es) associated with this thinking, if any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceIR:
    """The full intermediate representation of a session after normalization."""

    session_id: str
    source: str                                    # trace.jsonl path
    agent_name: Optional[str] = None
    prompts: list[str] = field(default_factory=list)      # user input per turn
    responses: list[str] = field(default_factory=list)     # response preview from stop events
    actions: list[Action] = field(default_factory=list)
    thinkings: list[Thinking] = field(default_factory=list)  # all thinking segments (including those without toolUse)
    warnings: list[str] = field(default_factory=list)      # data issues discovered during parsing
    official: Any = None      # OfficialRecord: Kiro's own session record (optional enrichment)
    run_count: int = 1        # number of agent_spawn events; comes directly from the scan (actions may be empty)
    run_prompts: dict = field(default_factory=dict)   # run → first user_prompt of that run
    run_started: dict = field(default_factory=dict)    # run → agent_spawn timestamp
    run_attribution: list = field(default_factory=list)  # RunAttribution: restored real sessions

    # ---- convenience views ----
    @property
    def turns(self) -> int:
        return len(self.prompts)

    def by_turn(self, turn: int) -> list[Action]:
        return [a for a in self.actions if a.turn == turn]

    @property
    def runs(self) -> int:
        """Number of agent startups in this directory. >1 means child agents or several independent invocations are mixed in."""
        return max(self.run_count, max((a.run for a in self.actions), default=1))

    def by_run(self, run: int) -> list[Action]:
        return [a for a in self.actions if a.run == run]

    def of_action(self, *names: str) -> list[Action]:
        s = set(names)
        return [a for a in self.actions if a.action in s]

    @property
    def calls_per_turn(self) -> list[int]:
        return [len({a.call_idx for a in self.actions if a.turn == t})
                for t in range(1, self.turns + 1)]

    @property
    def tools_per_turn(self) -> list[list[str]]:
        """Tool-name sequence per turn, in call order; each call counts once (fan-out is not double-counted)."""
        out: list[list[str]] = []
        for t in range(1, self.turns + 1):
            seen: set[int] = set()
            names: list[str] = []
            for a in self.actions:
                if a.turn == t and a.call_idx not in seen:
                    seen.add(a.call_idx)
                    names.append(a.tool)
            out.append(names)
        return out

    @property
    def credits(self):
        """Actual billed credits for this session, from the official record; None when not enriched."""
        return self.official.total_credits if self.official is not None else None

    @property
    def orphans(self) -> list[Action]:
        """Actions that were started but never returned."""
        return [a for a in self.actions if not a.completed and not a.blocked]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "agent_name": self.agent_name,
            "turns": self.turns,
            "runs": self.runs,
            "prompts": self.prompts,
            "responses": self.responses,
            "warnings": self.warnings,
            "official": (self.official.to_dict() if self.official is not None else None),
            "actions": [a.to_dict() for a in self.actions],
        }

    # ---- OTel GenAI semantic projection ----
    def otel_trace_attributes(self) -> dict[str, Any]:
        """Trace(session)-level alignment attributes for the root span / resource."""
        from .otel_semconv import trace_attributes
        return trace_attributes(self)

    def to_otel_spans(self) -> list[dict[str, Any]]:
        """Project the whole trajectory into a list of "semantic spans" (pure dicts, not the OTLP wire format).

        One span per action: {name, operation, status, start, duration_ms, attributes}.
        This is the product of "semantic alignment"; when you actually need to emit
        OTLP, feed these dicts to the official SDK.
        """
        from .otel_semconv import span_name, span_status
        spans = []
        for a in self.actions:
            d = a.to_dict()
            spans.append({
                "name": span_name(d),
                "operation": a.otel_operation(),
                "status": span_status(d),
                "start": a.ts,
                "duration_ms": a.duration_ms,
                "attributes": a.otel_attributes(),
            })
        return spans
