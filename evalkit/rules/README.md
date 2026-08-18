# How to write rules (rule files)

> **Want to get started quickly?** Use the **structured intent** syntax (reads/runs/write/dispatches/pipeline/never_*/if_claims…/judge) — no regex required. See **[AUTHORING.md](AUTHORING.md)**. This document describes the low-level checkers (which intents compile down to).

A rule file = one subject-under-test's "expected trajectory", named `<subject>.checks.json`. The engine (`../trajectory/`) reads it and decides whether a run is PASS / WEAK_PASS / FAIL.

## Examples in this directory

| File | Purpose |
|------|---------|
| `example-minimal.checks.json` | Minimal starter: 3 checks; copy and adapt |
| `example-all-checkers.checks.json` | Demo of all 7 checkers; each has a `_comment` annotation |
| `agent-eval.checks.json` + `agent-eval.README.md` | A real subject (the GuardEval orchestrator) |

> JSON does not support comments, but the runner only reads `checks[]`; any key beginning with `_` (`_doc`/`_comment`) is ignored and can be used as a comment.

## File skeleton

```json
{
  "target_agent": "<subject-name>",
  "checks": [
    { "id": "...", "type": "...", "severity": "...", ...checker-specific keys }
  ]
}
```

Each check has three required fields:
- `id` — the checkpoint name (pick your own; be descriptive)
- `type` — one of the 7 checkers (see below)
- `severity` — `required` / `recommended` / `forbidden` / `optional`

Optional: `reason_tmpl` — the human-readable reason shown in the verdict output.

## Choosing severity

| severity | Effect of miss / hit | Used for |
|----------|----------------------|----------|
| `required` | Miss → overall FAIL | Core steps that must be done |
| `recommended` | Miss → WEAK_PASS | Should do, but a legitimate run may skip (e.g., reusing a cache) |
| `forbidden` | **Hit** → FAIL | Forbidden actions (out-of-bounds, dangerous ops) |
| `optional` | Does not affect the verdict; only contributes to the health score | Completeness bonus |

Principle: **required is only for actions that all legitimate runs will necessarily do**. When unsure, start with recommended so as not to misjudge a legitimate run as FAIL.

## Quick reference for the 7 checkers

| type | Judges | Own keys |
|------|--------|----------|
| `Exists` | An action occurs at least once | `match` |
| `Count` | Count within an interval | `match`, `min_count`, `max_count` |
| `Forbidden` | Must not occur | `match`, `exclude` |
| `Before` | a precedes b | `a`, `b` |
| `Milestone` | A sequence of actions occurs in order | `steps: [matcher...]` |
| `IfThen` | If a occurs, b must occur | `a`, `b` |
| `Produces` | A file was produced | `name`, `min_count` |

Selection guide:
- "Must do X" → `Exists`; "conditional requirement" → `IfThen`
- "step A before step B" → `Before`; "whole ordered flow" → `Milestone`
- "produced a file" → `Produces` (easier than counting create_file)
- "must not do" → `Forbidden`

## matcher (objects inside match / a / b / exclude / steps[])

Whatever key you write is what is checked; all keys must match (AND):

| Key | Comparison | Example |
|-----|------------|---------|
| `action` | exact | `"action": "read_file"` |
| `tool` / `pattern` | exact | `"pattern": "eval-security-tester"` |
| `path` / `command` / `root` | **contains** | `"path": "config.json"` |
| `regex` | regex search over the serialized string | `"regex": "rm\\s+-rf"` |

- Use fields for precise discrete criteria; use regex for command-content details; mix freely (narrow first with `action`, then drill in with `regex`).
- In JSON, regex needs doubled backslashes: `\\s`, `\\.`.
- ⚠️ Do **not** anchor `exclude` regex with `^` — the serialized action string starts with the action name, so `^cat` will not match a `cat` in the command. Use `\\b(cat|grep)\\s`.

## Which fields can I match on?

After normalization (see `../normalize/`), each action commonly has:
- `action`: `read_file` / `run_command` / `create_file` / `modify_file` / `list_dir` / `spawn_subagent` / `summarize` ...
- `path`: file path (for read/write-class actions)
- `command`: raw shell command
- `pattern`: search pattern, sub-agent name, query, etc.
- `root`: search root directory

Not sure which actions appear in a given run? First run `python3 -m normalize.cli dump <session>` to see the actual actions, then write your rules to match.

## Recommended workflow for writing rules

1. Do several **real legitimate runs** of the agent; run `normalize.cli dump` to inspect their action sequences
2. Find actions **done in every run** → required; **should do but occasionally skipped** → recommended
3. Find actions that **must never appear** → forbidden
4. Add ordering as needed → Before / Milestone; add word-must-match-deed → IfThen
5. Validate on real runs: legitimate runs should be PASS/WEAK_PASS, not misjudged FAIL; then use negative samples to confirm it catches FAIL

## Running

```bash
cd ~/agent-trace/evalkit
python3 -m trajectory.runner rules/<subject>.checks.json --session <session-id>
# or against an already-normalized file:
python3 -m trajectory.runner rules/<subject>.checks.json path/to/normalized.json
```

## Scoring and verdict configuration (scoring.json)

Verdict logic (PASS/WEAK_PASS/FAIL) and the health score (0–1) are not hard-coded; they live in `scoring.json` and can be customized. Rule files only say "what to check"; scoring says "how to score".

Loading priority: `--scoring <path>` > `rules/scoring.json` > built-in defaults (works even if the file is deleted).

```json
{
  "weights": {
    "required": 1.0, "forbidden": 1.0, "recommended": 0.5, "optional": 0.25
  },
  "binarize_checkers": ["Milestone"],
  "verdict": {
    "fail_on_required_miss": true,
    "fail_on_forbidden_hit": true,
    "weak_on_recommended_miss": true
  }
}
```

**weights** — the weight of each severity in the health score.
health = Σ(weight × score) / Σ(weight). One required check counts as two recommended or four optional.

**binarize_checkers** — checker types listed here are scored **all-or-nothing** in the health score: pass = 1.0, fail = 0.0, **progress/partial credit is ignored**.
- Typical use: `Milestone` normally awards progress credit (2/3 done → 0.67); once listed here, **anything short of full satisfaction is 0**.
- To binarize `Count` etc., add them to the list; to restore progress credit, remove them.
- Note: binarize only affects how the checker contributes to the **health score**; it does not change the verdict (which reads the `passed` boolean) and does not change the real progress shown in the report.

**verdict** — three switches control the verdict:
- `fail_on_required_miss` — any missed required → FAIL
- `fail_on_forbidden_hit` — any forbidden hit → FAIL
- `weak_on_recommended_miss` — any missed recommended → downgrade to WEAK_PASS

Verdict flow: if any FAIL condition holds → FAIL; otherwise, if any WEAK condition holds → WEAK_PASS; otherwise → PASS. `optional` never affects the verdict; it only feeds the health score.

Usage:
```bash
# Use the default rules/scoring.json
python3 -m trajectory.runner rules/<subject>.checks.json --session <id>
# Use custom scoring
python3 -m trajectory.runner rules/<subject>.checks.json --session <id> --scoring my-scoring.json
```
