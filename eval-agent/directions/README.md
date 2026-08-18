# eval-agent/directions/ — Evaluation direction definitions

`eval-agent` uses the concept of a **direction** to separate different evaluation perspectives. A direction defines **what question to ask** (e.g. "did the user's requests get done?") plus **how the 9 steps run** (per-step input/output/validation), a **budget** (LLM call count, timeout), and **degrade behavior** (what to do when data is incomplete).

Currently one direction is implemented: `goal_completion`. Future directions could include `correctness`, `efficiency`, `safety` — one JSON per direction.

## Existing directions

### `goal_completion.json`

**Question**: did the user's requests get done?

- **Mode** `scripted` — the 9-step flow is fixed
- **Contributes to verdict** `contributes_to_verdict=true` — this direction's conclusion enters the final verdict
- **Data source** must be Kiro's official session records (hook traces don't preserve full agent responses, so self-claims can't be extracted)
- **Degrade** if the official source is missing the `response` field, judge `unverifiable` instead of `false` (see `requires.degrade`)
- **Budget** `llm_calls=8` / `seconds=3600`
- **Control group** `controls.n=1` — inject one synthetic requirement per run to test whether the judgment chain is too lenient

## direction JSON field conventions

Top-level fields:
| Field | Type | Required | Meaning |
|---|---|---|---|
| `direction` | str | ✓ | Direction name, must match the filename |
| `version` | int | ✓ | Schema version |
| `mode` | enum | ✓ | `scripted` (fixed 9 steps) or `agentic` (future) |
| `contributes_to_verdict` | bool | ✓ | Whether this feeds the final verdict |
| `question` | str | ✓ | One-sentence description of what's being asked |
| `requires` | obj |   | Data source requirements + degrade behavior for missing fields |
| `budget` | obj |   | LLM call / seconds caps |
| `retrieve` | obj |   | Retrieval `k` and scoring mode |
| `controls` | obj |   | Control group count and description |
| `steps` | list |   | Per-step `id` / `type` / `impl` (function in `steps.py`) / `in` / `out` / `purpose` / `validate` |

## Adding a new direction

1. Copy `goal_completion.json` → `<your-direction>.json`, change `direction` and `question`
2. Implement the new/modified step functions in `steps.py` (naming: `<id>_<purpose>`)
3. Add corresponding LLM prompt templates in `prompts.py`
4. Add unit tests in `tests/` (at minimum: happy path + one key degrade path)
5. Register the direction entry point in `runner.py`
6. Update `DESIGN.md`

## Current state

`steps.py` currently **hardcodes** the 9 steps of goal_completion; it doesn't dispatch dynamically from JSON. This file is therefore closer to a "spec document + future integration point" than a runtime driver. When a second direction is added, this will be refactored to data-driven dispatch.
