# Kiro_Trajectory_Eval — Evaluation toolkit for Kiro CLI agent sessions

**Kiro_Trajectory_Eval** turns a Kiro CLI agent run into a structured, auditable evaluation. It reads the session record that Kiro writes to `$KIRO_HOME/sessions/cli/`, normalizes it into a stable action sequence, and judges whether the agent **actually did what it said it did** — matching the run's real behavior against its stated intent, produced artifacts, and user requirements.

Two evaluation paths share the same normalization layer:

| Component | What it does | When to use |
|---|---|---|
| **[`evalkit/`](evalkit/README.md)** | Declarative rules over the normalized action sequence. Seven built-in checkers (`Exists` / `Count` / `Forbidden` / `Before` / `Milestone` / `IfThen` / `Produces`) plus optional `LLMJudge`. Outputs `PASS` / `WEAK_PASS` / `FAIL` and a health score. Can export **OpenTelemetry OTLP/JSON**. | "Did this run follow the expected trajectory? Does the agent's behavior match its self-reported actions?" |
| **[`eval-agent/`](eval-agent/README.md)** | Forensic 9-step pipeline. Extracts atomic requirements from user prompts and self-claims from agent responses, then searches the trajectory for evidence. Each conclusion cites the action IDs it depends on, with a confidence score. | "Did the agent actually accomplish what the user asked for?" |
| **[`hooks/`](hooks/)** *(optional)* | Runtime Kiro CLI hook collector. Adds signals the built-in session records don't capture: `preToolUse` policy blocks, precise millisecond timing, live tracing. **Not required** — evaluation works out of the box using `--official`. | Only when you need signals beyond what Kiro's built-in records provide. |

## Why look at the trajectory instead of the final output?

Judging an agent from its final reply misses the failure modes that hide in the *process*: skipped steps, silently swallowed errors, self-reports that don't match what the tools actually did, orchestrators that overstep their scope. This toolkit exposes those at the action-sequence level so they can be caught by rules or by evidence-cited verdicts, rather than by manual reading.

## Quick start

Python 3.10+. Core has **zero third-party dependencies** (only `matplotlib` for optional PNG timelines).

```bash
# 1. Try the built-in sample (no data or setup needed)
cd evalkit
python3 -m trajectory.runner rules/example-minimal.checks.json examples/sample.normalized.json
# expected: PASS  health=1.0

# 2. Evaluate one of your own Kiro sessions
python3 -m trajectory.runner rules/<your-rule>.checks.json --session <session-id> --official

# 3. Export to OpenTelemetry OTLP/JSON (view in Jaeger / Tempo / otel-collector)
python3 -m normalize.cli export-otel <session-id> --source official --out trace.json
```

For the forensic evaluator:
```bash
cd eval-agent
python3 runner.py <session-id>            # full pipeline (uses kiro-cli for LLM steps)
python3 runner.py <session-id> --no-llm   # deterministic-only, no LLM calls
```

## Repository layout

```
Kiro_Trajectory_Eval/
├── evalkit/          Declarative-rule evaluator (rules + engine + normalizer + OTel export)
├── eval-agent/       Forensic evaluator (9-step pipeline, uses evalkit for normalization)
├── hooks/            Optional Kiro CLI hook collector (bash + jq)
├── bin/kiro-trace    CLI to inspect collected traces
├── config/           Sample configs: policy.json, traced-agent.json, kiro-judge.json
├── install.sh        Installs the hook collector into ~/.kiro/agents/
└── docs/TOUR.md      5-minute tour: how the pieces fit together
```

## Note on `kiro-cli`

Kiro CLI is the AI coding agent whose sessions this toolkit evaluates. You do **not** need it to run the core evaluators — declarative rules and normalization work directly on session records that Kiro writes to `$KIRO_HOME/sessions/cli/`.

`kiro-cli` is only required for:
- `eval-agent/runner.py` full pipeline (skip with `--no-llm` for deterministic-only evaluation)
- `evalkit/trajectory` `LLMJudge` checker (opt-in via `--llm`)
- `evalkit/rules/generate-rule.sh` (a convenience script that drafts a rule from an agent config)

If you don't use `kiro-cli`, the declarative-rule path (`evalkit/`) and the deterministic subset of the forensic path (`eval-agent/ --no-llm`) still work fully.

## Documentation

- **5-minute tour**: [`docs/TOUR.md`](docs/TOUR.md) — how the pieces fit together, which one to look at first
- Sub-project docs: [`evalkit/README.md`](evalkit/README.md), [`eval-agent/README.md`](eval-agent/README.md)
- Rule authoring: [`evalkit/rules/AUTHORING.md`](evalkit/rules/AUTHORING.md)
- OpenTelemetry mapping: [`evalkit/OTEL_MAPPING.md`](evalkit/OTEL_MAPPING.md)
- LLM reference: [`evalkit/LLM_GUIDE.md`](evalkit/LLM_GUIDE.md)
- Forensic evaluator design: [`eval-agent/DESIGN.md`](eval-agent/DESIGN.md)

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Security reports: [`SECURITY.md`](SECURITY.md). Release notes: [`CHANGELOG.md`](CHANGELOG.md).

## Privacy notice

If you enable the optional hook collector, it records **user prompts, file contents, and command outputs** to local trace files. This software never uploads that data. See [`SECURITY.md`](SECURITY.md) for what is captured and how to reduce it.

## License

MIT — see [LICENSE](LICENSE).
