# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`evalkit/graph/`** — trajectory provenance graph. Actions become activities, outputs become versioned entities, edges capture producer/consumer relations. Produces a self-contained interactive HTML viewer (one page per turn + overview). Not a verdict path; a reader-facing fact-layer derivative. See `evalkit/graph/DESIGN.md`.
- **`evalkit/evidence/canonicalize.py`** — shell command canonicalization (peel scaffold + Python AST walk) producing a `structure_hash` so that commands sharing a template but differing only in inline args no longer collide in efficiency's duplicate detection.
- **`evalkit/rules/genrule/`** — stepwise rule generation: decompose the agent config into task types and ordered stages, extract policies stage by stage with explicit observability classification, compile the policy IR deterministically to `.checks.json`. Complements the one-shot `generate-rule.sh`. See `evalkit/rules/GENERATION.md`.
- **`session-viz/`** — standalone single-file HTML viewer for a Kiro session, with two semantic zoom levels (session overview + per-turn event stream). No external dependencies, offline-usable.
- **DSL tightening**: new `touches` (loose landmark) and `never_writes` (write ban covering tool / shell redirect / python idiom); `reads` now recognises python read idioms (`json.load`, `.read()`, `open()` single-arg); `runs` matches at subcommand head after stripping prefixes (`timeout`, `sudo`, `env`, `python -m`, ...). Intent→action/verb map is externalised to `rules/intent_map.json`.
- **Normalization contract documented**: `evalkit/normalize/__init__.py` now carries a full field-by-field contract of the two input sources, the `TraceIR` / `Action` output shape, fan-out semantics, and the warn-vs-raise caveat.
- `Action` dataclass gains `sid` / `ref` (`<sid8>#<idx>`, globally unique across sessions) and `cwd` (effective working directory). `TurnMeta` gains `cycles` (number of inner loops, drives efficiency's stuck-turn detection); `OfficialRecord` gains `parent_session_id`, `updated_at`, `compactions`, and `unknown_kinds`.
- Rule evaluation now defaults to reading the Kiro official session record (`rule.runner --session <sid>` no longer requires `--official`); hook trace is a fallback via `--hook`. `--official-dir` can override the resolved directory on `rule.runner`, `normalize.cli`, and `pipeline.py`.
- `normalize.cli` echoes loader warnings and a "0 actions" line to stderr so a mis-pointed `--official-dir` no longer looks like a clean run.
- `goal` (ex `eval-agent/`) is now a sibling of `rule`, `efficiency`, and `graph` under `evalkit/`. The `--agent-dir` flag now falls back to `KIRO_EVAL_AGENT_DIR` env var.
- Two upstream `tree-sitter-bash` defects discovered while building the graph's command extractor are filed under `reported/`.

### Changed

- `evalkit/trajectory/` renamed to `evalkit/rule/`. Entry point is now `python3 -m rule.runner`.
- `eval-agent/` top-level directory merged into `evalkit/goal/`. `_bootstrap.py` is gone; `evidence/`, `llm.py`, and `kiro_acp.py` sit at the `evalkit/` root. Entry point is now `python3 -m goal.runner`.
- One `unittest discover` from `evalkit/` runs the full suite (705 tests across normalize, rule, goal, efficiency, graph; 16 skipped — the graph page-harness tests that require `node` + `npm install`).
- `efficiency.s1_map(sid, official_dir)` → `s1_map(tree)`; drops redundant raw-JSON reads now that `normalize/official.py` parses the same fields. The IR pickle cache now embeds a schema fingerprint and auto-expires when the pickled dataclasses change.
- `dispatches` with only `at_most` given lowers the implicit lower bound to 0 (`<=N` allows 0 occurrences); both unspecified still requires at least one dispatch.

## [0.1.0] — 2026-08-18

Initial public release.

### Added

**evalkit** (declarative-rule evaluator)
- Normalization layer supporting two data sources (hook trace + Kiro official session records), producing an isomorphic `TraceIR` action sequence per source.
- Trajectory engine with 7 built-in checkers: `Exists`, `Count`, `Forbidden`, `Before`, `Milestone`, `IfThen`, `Produces`; plus optional `LLMJudge`.
- Intent-driven rule DSL (`reads` / `runs` / `write` / `dispatches` / `pipeline` / `before` / `never_*` / `if_claims…then_runs` / `judge`) that compiles to the low-level checker syntax. See `rules/AUTHORING.md`.
- Rule self-check via `python3 -m rule.runner <rule>.checks.json --compile`; distinct exit codes for unknown intents, validation errors, and parse failures.
- OpenTelemetry OTLP/JSON export aligned with OTel GenAI semantic conventions. See `OTEL_MAPPING.md`.
- One-shot pipeline (`pipeline.py`): archive → normalize → auto-select rule → evaluate → optional OTLP / LLM judge.
- Rule generator scaffold (`rules/generate-rule.sh`) that produces a starter rule from an agent config (requires `kiro-cli`).
- Example rules for the sample agent-eval orchestrator and eval-security-tester (single-turn / multi-turn variants), plus fully commented starter rules (`rules/example-*.checks.json`).
- Configurable scoring (`rules/scoring.json`) — weights per severity, verdict thresholds, and per-checker binarization.

**goal** (forensic evaluator)
- Nine-step pipeline: `map` → `extract requirements (LLM)` → `inject control` → `extract claims (LLM)` → `compile criteria (LLM)` → `search evidence` → `judge (LLM)` → `overclaim check` → `finalize`.
- Every LLM output passes a code validation gate; every conclusion must cite evidence action IDs that exist in the trajectory.
- Built-in synthetic control requirement invalidates the run if the model incorrectly accepts it.
- One direction implemented: `goal_completion` (did the user's requests get done?).

**Hook collector** (optional)
- Bash `trace-hook.sh` captures all 5 Kiro CLI hook events into per-session JSONL.
- `flock`-serialized concurrent writes.
- `preToolUse` policy engine (deny by tool name / command regex / path regex).
- `kiro-trace` CLI to list, show, summarize, timeline, tail, export, and clean traces.
- `flat` and `daily` trace layouts (`KIRO_TRACE_LAYOUT`).

### Fixed in initial release

Bugs found during adversarial testing of the trajectory engine, all fixed before this release:

| # | Class | Bug | Fix |
|---|---|---|---|
| S1 | Silent pass | Misspelled severity (e.g. `critical`) had weight 0 → failures silently ignored → PASS. | `run_check` normalizes unknown severities to `required`; `verdict` treats unknown severities as `required`. |
| B4 | Silent pass | `Forbidden` had a hardcoded severity, disabling the documented "soft-forbidden" downgrade. | Uses `cp.get("severity", "forbidden")`. |
| B5 | Silent pass | `Produces` with empty `name` matched every `create_file`. | `_validate` rejects empty `name`. |
| B6 | Silent pass | Empty-`steps` `Milestone` always scored 1.0. | `_validate` rejects empty `steps`. |
| N7 | Silent pass | Non-completed writes (`completed=False`) counted as produced. | `Produces` skips `completed=False`. |
| B10 | Silent pass | `Count` with lower=0 and no upper always passed. | `_validate` rejects meaningless configuration. |
| B1 | Crash | Invalid regex raised uncaught `re.error`. | `match` catches `re.error`; `_validate` pre-compiles patterns. |
| B2 | Crash | Actions without `idx` raised `KeyError`. | `_find` / `Produces` / `Milestone` fall back to position. |
| B3 | Crash | Missing required rule keys raised `KeyError`. | `_validate` enforces `_REQUIRED_KEYS`. |
| N5 | Crash | Non-string shell command raised `AttributeError`. | `split_subcommands` coerces to `str`. |
| B7 | Semantics | Field-joined-by-space serialization caused cross-field regex matches. | Join by newline (default `.` doesn't cross lines). |
| B8 | Semantics | `Milestone` scanned in list order, `Before` in `idx` order — inconsistent under out-of-order input. | `Milestone` sorts by `idx` before scanning. |
| B9 | Semantics | Empty ruleset yielded `PASS` but `health=0.0`. | Vacuous ruleset → `health=1.0`. |
| S6 | Semantics | Corrupt explicit `--scoring` file silently fell back to defaults. | Corrupt explicit file → fall back with `stderr` warning. |

Hardening: `run_check` is now the single validation gate (rule integrity / severity / regex / numeric sanity), plus per-check `try/except` — a single checker failure no longer aborts the run. These invariants are documented in `LLM_GUIDE.md §7`.

[Unreleased]: https://github.com/Try-to-Climb/Kiro_Trajectory_Eval/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Try-to-Climb/Kiro_Trajectory_Eval/releases/tag/v0.1.0
