# normalize — Trajectory Normalization Data Layer

Converts Kiro's raw records into a **unified, clean action sequence** (`TraceIR`) for upper-layer evaluation. Agnostic to any specific agent under test.

Supports two data sources with isomorphic output:
- **hook trace** (`traces/<sid>/trace.jsonl`) — hook records from the collection layer
- **Kiro official session records** (`~/.kiro/sessions/cli/<sid>.jsonl`) — shipped with Kiro, more complete

## Problem It Solves

Raw records can't be evaluated directly. Normalization does the following (the unit of evaluation is "action", not "tool call"):

| Processing | Description |
|------------|-------------|
| Fan-out | A single batch call (e.g. read multiple files, subagent multi-stage) is expanded into multiple actions |
| Alias normalization | `fs_read→read`, `execute_bash→shell`, etc. — unifies naming across versions |
| Semantic extraction | `write+create`→create_file, `write+strReplace`→modify_file; paths mapped to unified fields |
| Derived fields | `completed` (pre/post pairing), `turn` (split by user_prompt), `run` (split by agent_spawn) |
| Official enrichment | Use Kiro official records to fill in agent name / usage / cost, cross-validate, and correct hook orphan misjudgments |

## Usage

```bash
python3 -m normalize.cli dump    <session-id>        # Output action JSONL (hook source)
python3 -m normalize.cli dump    --official <sid>    # Official record source
python3 -m normalize.cli table   <session-id>        # Human-readable timeline
python3 -m normalize.cli stats   <session-id>        # Stats for a single session
python3 -m normalize.cli all                         # Batch stats
```

Code:

```python
from normalize import normalize_file, load_trace_from_official
ir = normalize_file("traces/<sid>/trace.jsonl")   # → TraceIR; ir.actions is the action sequence
```

## Known Limitations

- **File operations inside commands are invisible to the hook** (e.g. shell `cat > f`, python scripts batch-writing files) — only one `run_command` is recorded, no create_file is produced. Be aware when evaluating outputs.
- **Sub-agents inherit the parent session id**: after the hook fix, directories are split by payload session_id; older data is separated via the `run` dimension + prompt attribution.
- `--no-interactive` runs are not recorded in Kiro official records — only the hook source is available.

## Modules

| File | Purpose |
|------|---------|
| `schema.py` | `Action` / `TraceIR` data structures |
| `mapping.py` | Alias table + action mapping + shell command splitting |
| `core.py` | hook source normalization (scan / pairing / fan-out / semantics / enrichment) |
| `official.py` / `official_loader.py` | Kiro official record parsing and enrichment |
| `attribution.py` | Attribute sub-agent activity to the real session in multi-run scenarios |
| `cli.py` | Command line |
| `tests/` | Unit tests |

## What to run after changing normalize/

**All three suites must run.** The `Action` fields the normalization layer
produces are shared by all three upstream features, and a field change usually
only breaks downstream:

```bash
cd evalkit
python3 -m unittest discover                 # all 241 tests across normalize, rule, goal, efficiency
```

The flags: `-s` is where to look for tests, `-t` is the project root (decides
whether `from normalize import ...` resolves; must be `evalkit/`).

### Which fields each consumer reads

Check this table before changing a field:

| Feature | `Action` fields consumed | Breakage if the field stops being filled |
|------|---------------------|-----------|
| rule | `action` `tool` `command` `path` `root` `pattern` `idx` `completed` `subcommands` | rules **silently miss**, verdicts drift loose |
| goal | the six serialized fields above + `args.__tool_use_purpose` + `reasoning` | anchor-retrieval recall drops, false MISS appears |
| efficiency | all of the above + `response` `error` `blocked` + `TraceIR.thinkings` + official turn metadata | s4/s5/s7 directly break |

`response` is only filled when `include_responses=True`, which only efficiency
sets; `thinkings` is currently consumed only by efficiency (s4's sibling-filter
depends on it).

### Limitation of the existing tests

The records under `tests/` are all **hand-built in code**
(`normalize_events([prompt(), pre("execute_bash", {...})])`): they exercise our
own mapping logic. They do **not catch Kiro's own record-format drift**: if Kiro
renames a field, `load_trace_from_official` records a warning and returns an
empty IR (see `test_missing_jsonl_warns`), and all 241 tests still pass.

So after a Kiro upgrade, besides running the tests, also sanity-check a real
session recorded by the new build:

```bash
python3 -m normalize.cli table <sid> --official --official-dir <dir of that session>
```

If the action count is 0, or stderr prints `[warn]`, the format has drifted.
