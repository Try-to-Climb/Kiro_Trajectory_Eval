# evalkit — LLM Operation Reference

For LLMs/agents working in this repo. Precise, structured, directly actionable. See `README.md` for the human tour.

## 0. One-Line Model

`raw records --normalize--> actions[] --trajectory.runner + rules/*.checks.json--> {verdict, health, results[]}`

Three layers, one-way dependencies: `rules` (pure data) is read by `trajectory`; `trajectory` consumes `normalize` output; `normalize` doesn't depend on the upper layers. Engine and subject are **decoupled**: subjects live only in `rules/`.

## 1. Entry Points (all run under `evalkit/`)

```bash
python3 -m normalize.cli {dump|table|stats|all} <session-id|path> [--official]
python3 -m normalize.cli export-otel <session-id|path> --source {hook|official|both} [--out f] [--compact]
python3 -m trajectory.runner <rules.json> (<normalized.json> | --session <id>) [--json] [--scoring <f>]
```

- `--session <id>`: auto-reads the hook trace from `default_trace_dir()` (default `~/agent-trace/traces`, overridable via `KIRO_TRACE_DIR`) and normalizes.
- `--official`: normalize switches to Kiro official record source (`$KIRO_HOME/sessions/cli/`).
- `export-otel --source`: `hook`=pure hook (enrich=False) / `official`=pure official / `both`=hook + official enrichment (enrich=True). Outputs OTLP/JSON (`ExportTraceServiceRequest`), see OTEL_MAPPING.md.
- On exit, stdout is a human-readable table; `--json` outputs `{"verdict","health","results":[...]}`.

## 2. Action Data Structure (produced by normalize, consumed by trajectory)

Each action is a dict; key fields:

| Field | Type | Meaning | Consumer |
|-------|------|---------|----------|
| `idx` | int | Global order (increments after fan-out) | Order checks (Before/Milestone) |
| `turn` | int | Which conversation turn (split by user_prompt) | Per-turn rules |
| `run` | int | Which agent invocation (split by agent_spawn) | Parent/child separation |
| `action` | str | Semantic action | Matching |
| `tool`/`raw_tool` | str | Normalized / raw tool name | Matching |
| `path` | str? | File path (read/write class) | Matching (contains) |
| `command` | str? | Original shell command | Matching (contains) |
| `pattern` | str? | Search pattern / subagent name / query | Matching (exact equal) |
| `root` | str? | Search root directory | Matching (contains) |
| `completed` | bool | Whether a matching post exists (i.e. did it execute) | Produces filtering |
| `reasoning` | str | Agent thinking before this call (official thinking; empty from hook source) | Quality / reasoning analysis |
| `response` | str? | Full tool reply (**default None**, only collected with `--with-responses`) | Reply content analysis |
| `subcommands` | list | Shell split by &&/;/newline | — |

`action` value set: `read_file` / `list_dir` / `read_image` / `create_file` / `modify_file` / `run_command` / `search_content` (grep) / `search_files` (glob) / `code_<op>` / `aws_call` / `knowledge_<cmd>` / `docs_query` (introspect) / `summarize` / `spawn_subagent` / `list_subagents` / `unknown:<tool>`.

Tool aliases (raw → normalized): `fs_read→read`, `fs_write→write`, `execute_bash→shell`, `use_subagent→subagent`.

## 3. Rule File Schema (`rules/<subject>.checks.json`)

```json
{"target_agent": "<name>", "checks": [ <check>, ... ]}
```

Each `<check>`: `{"id":str, "type":<checker>, "severity":<sev>, ...checker-specific keys, "reason_tmpl":str?}`

- `<sev>` ∈ `required | recommended | forbidden | optional`
- Keys starting with `_` are ignored (usable as comments)

## 4. The Seven Checkers + Required Keys

| type | Required keys | Optional keys | Semantics |
|------|---------------|---------------|-----------|
| `Exists` | `match` | — | ≥1 action matches match |
| `Count` | `match` + (`min_count` or `max_count`) | The other bound | Match count in range; `min_count=0` with no `max_count` is illegal |
| `Forbidden` | `match` | `exclude` | Any match is a violation; exclude hit → exempt |
| `Before` | `a`, `b` | — | min(idx of a) < min(idx of b); if either missing, vacuous pass |
| `Milestone` | `steps` (non-empty list of matcher) | — | Order-preserving subsequence after sorting by idx; progress score = hits / total |
| `IfThen` | `a`, `b` | — | If a exists, b must exist (order not enforced); a missing → vacuous pass |
| `Produces` | `name` (non-empty) | `min_count` (default 1) | ≥min_count producing actions (create_file/modify_file/write/append_file) with path containing name **and completed≠False** |
| `LLMJudge` | `dimension` | `pass_threshold` (default 0.75) | **The 8th, LLM-as-judge**; submits a compact trajectory view to the LLM, scored 1–4 per rubric, normalized to 0–1, ≥threshold = passed. See §5.5 |

## 5.5 LLMJudge (LLM-as-judge checker)

- `dimension` ∈ `efficiency` / `reasoning_quality` / `authenticity` (rubrics in `trajectory/llm_judge.py::RUBRICS`).
- **Not run by default**: without `--llm`, runner **gracefully skips** these rules (passed=True, confidence=0, excluded from verdict and health score). Backend is only called when `--llm` is passed.
- Backend = `kiro-cli chat --no-interactive --trust-tools= --agent kiro-judge` (uses Kiro as the LLM; the judge agent has no tools). `--judge-agent` / `--effort` are tunable.
- Three-part prompt = RUBRIC (per dimension) + TASK (objective + dimension) + TRAJECTORY (`normalize/judge_view.py` Plan B view: per-step action + target + purpose + think).
- Advisory: results carry `confidence` (running=0.9); **errors / no backend → non-punitive pass**, avoiding misjudgment from LLM jitter.
- objective is taken by runner from `--session`'s ir.prompts[0] or normalized.json's `prompts[0]`, passed via context.
- Usage: `python3 -m trajectory.runner rules/agent-eval.checks.json <norm.json> --llm`

## 5. matcher (values of `match`/`a`/`b`/`exclude`/`steps[]`)

Only the keys you write are checked; **all must match (AND)**:
- Exact-equal fields: `action` `tool` `raw_tool` `pattern`
- Contains fields: `path` `command` `root`
- `regex`: `re.search` on the serialized action string (fields joined by **newlines**)

Key points:
- Fields and regex can be mixed (narrow first with action, then dig details with regex) to reduce false positives.
- In JSON, regex needs double backslashes: `\\s` `\\.`.
- `exclude` regex **must not anchor with `^`** (the serialized string starts with the action name and won't match command content); use `\\b(cat|grep)\\s`.
- Serialization uses newlines as separators, so `.` in regex doesn't cross fields — avoids cross-field false matches.

## 6. Verdict & Scoring (`rules/scoring.json`)

```json
{"weights":{"required":1.0,"forbidden":1.0,"recommended":0.5,"optional":0.25},
 "binarize_checkers":["Milestone"],
 "verdict":{"fail_on_required_miss":true,"fail_on_forbidden_hit":true,"weak_on_recommended_miss":true}}
```

- Verdict: any fail condition → FAIL; otherwise any weak condition → WEAK_PASS; otherwise PASS. `optional` doesn't affect verdict.
- Health = Σ(weight×score)/Σ(weight); types in `binarize_checkers` take score ∈ {1.0 if passed else 0.0} (ignoring progress score). No checks → health=1.0.
- Loading: explicit `--scoring` broken → fall back to built-in default + stderr warning (does not silently read rules/scoring.json); unspecified → find rules/scoring.json → built-in default.

## 7. Hard Invariants (do not break when modifying code)

- `run_check` is the **validation gateway**: illegal rules (missing keys / invalid regex / empty steps / empty name / illegal severity / Count without bounds) → returns `passed=False` with reason starting `rule error:`. **Doesn't crash, doesn't silently pass.**
- Illegal / unknown severity → normalized to `required` (both scored and FAIL-eligible); never silently dropped.
- Exception in a single checker → caught to a fail result; doesn't take down the entire run.
- `match` with illegal regex → returns False (doesn't throw).
- Before and Milestone **both order by idx** (not list position).
- Produces **does not count** outputs with `completed=False`.

## 8. Key Pitfalls (must know when writing rules)

1. **Script outputs are invisible**: files an agent writes via `python3` / `cat >` in bulk produce no create_file. Produces should only target key artifacts written by the write tool (`test_cases_*.json`, `report_*.md`), not the shards written by scripts.
2. **Single-turn vs multi-turn output patterns differ**: multi-turn subagents `create_file` their own result → judge via Produces; single-turn result is written by `example_target_runner.py` while the agent only reads → judge via `Exists {"regex":"verification"}`, not Produces.
3. **Fraud-detection pattern**: `IfThen(a="summary contains LIVE", b="run_command executes *_acp.py")` — if you claimed you did it, there must be a corresponding execution action. Exists can't do this (Exists would blanket-require every run to execute).
4. **Only put "must-do for every valid run" actions in required**; anything that might be skipped due to context reuse should be recommended, otherwise a FAIL is misjudged.

## 9. Common Task Recipes

- **Write a rule for a new agent**: `normalize.cli dump --official <qualified run>` to see actions → distill → write `rules/<agent>.checks.json` → verify no misjudgment on real run + negative sample catches → done.
- **Add a new checker type**: write `check_xxx(cp,actions)->CheckResult` in `trajectory/checkers.py`, register in `CHECKERS`, declare required keys in `_REQUIRED_KEYS`, add `_validate` branch, add tests.
- **Change verdict/weights**: only edit `rules/scoring.json`, no code changes.
- **Archive a run (parent + children)**: `bash ~/agent-trace/archive/pack_run.sh <parent-session-id>` (recursively pulls the full tree by `parent_session_id`, stores official + hook + normalized).

## 10. Tests

```bash
python3 -m unittest trajectory.tests.test_trajectory      # 24 items
python3 -m unittest discover -s normalize/tests -t .      # 93 items
```

Both must remain green after changes; verdicts on real runs shouldn't change (see README §5 baseline: agent-eval qualified run = PASS/WEAK_PASS, faked sample = FAIL).
