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
