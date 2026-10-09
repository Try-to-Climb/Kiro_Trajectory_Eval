# rule — Trajectory Evaluation Engine

A general-purpose agent trajectory verification engine. Input is a **normalized action sequence** (from `normalize`); given a set of **checkpoint rules**, it decides whether the run "followed the expected trajectory", outputting PASS / WEAK_PASS / FAIL + a health score + per-check reason.

The engine itself is **agnostic to any specific agent under test**: it only knows about action sequences and rule files. Each subject's (agent's) "expected trajectory" lives in its own rule file (see `../rules/`). The engine is not customized for any single agent.

The design borrows from Strands Evals' trajectory evaluation, and adds what it lacks: check severity levels (required/recommended/forbidden/optional), partial-order / order-preserving subsequence / conditional-implication constraints, and human-readable reasons per check.

---

## Files

| File | Purpose |
|------|---------|
| `checkers.py` | matcher + `CheckResult` + 7 checkers |
| `runner.py` | Reads rules, runs checkers, aggregates verdict |
| `tests/test_trajectory.py` | 24 unit tests |

Rule files are not in this package; they live at `../rules/<subject>.checks.json`.

---

## Usage

```bash
# Run from the project root (evalkit/)
python3 -m rule.runner rules/<subject>.checks.json <normalized.json>

# Evaluate a hook session directly (auto-normalizes)
python3 -m rule.runner rules/<subject>.checks.json --session <session-id>

# JSON output (for downstream consumers)
python3 -m rule.runner rules/<subject>.checks.json <normalized.json> --json
```

Example output (excerpt):

```
⚠️ WEAK_PASS   health=0.789
checkpoint                checker    sev             reason
CP_read_graph             Exists     required    ✓   read agent graph
MS_workflow               Milestone  required    ✓   skeleton in order (3/3)
CPN_no_bypass             Forbidden  forbidden   ✓   no forbidden action occurred
...
```

---

## Verdict Rules

| Verdict | Condition |
|---------|-----------|
| **PASS** | All required hit + no forbidden hit + all recommended hit |
| **WEAK_PASS** | All required hit + no forbidden hit, but some recommended missing |
| **FAIL** | Any required missing / forbidden hit / order or milestone violated |

`optional` does not affect the verdict, only contributes to the **health score** (weighted hit rate).

Verdict logic and health-score weights are all in `rules/scoring.json` and can be customized (including `binarize_checkers`: make checkers like Milestone score 0 when not fully satisfied). See "Scoring and Verdict Configuration" in `rules/README.md`.

---

## The 7 Checkers

| checker | Semantics | Key parameters |
|---------|-----------|----------------|
| `Exists` | Some class of action occurs at least once (unordered) | `match` |
| `Count` | Occurrence count falls within a range | `match`, `min_count`, `max_count` |
| `Forbidden` | Must not occur (any hit → FAIL) | `match`, `exclude` (excludes false positives) |
| `Before` | Action a must precede b (focused on a pair) | `a`, `b` |
| `Milestone` | A series of actions occurs in order (order-preserving subsequence — anything in between is fine — gives a progress score) | `steps: [...]` |
| `IfThen` | If a occurs then b must occur (conditional implication, order not enforced; if a doesn't occur, vacuously passes) | `a`, `b` |
| `Produces` | Write only the artifact name — automatically matches producing actions (create_file/modify_file/...) with matching path | `name`, `min_count` |

- `Exists` vs `IfThen`: the former is an unconditional "must have X"; the latter is "if X, then Y is required"; without X, no requirement.
- `Before` vs `Milestone`: the former focuses on the order of two actions; the latter describes an entire trajectory skeleton.
- `Produces`: fills the "output" layer — the user only writes the artifact name, doesn't need to know which action produced it. ⚠️ Only recognizes tool-level output; files written inside commands (e.g. python scripts batch-writing) are invisible to the hook — must focus on "key artifacts produced with the write tool".

---

## matcher: Mixing Fields + Regex

Within one `match` object (or `a`/`b`/`exclude`/`steps[]`), **whatever keys you write are the keys checked; all must match (AND)**:

| Key | Semantics | Comparison |
|-----|-----------|------------|
| `action` | Action type | Exact equal |
| `tool` / `raw_tool` / `pattern` | Normalized fields | Exact equal |
| `path` / `command` / `root` | Path / command / search root | **Contains** |
| `regex` | Regex search over the action's serialized string | Regex |

- Exact discrete criteria → use fields; command content details → use regex; mixing is most robust.
- ⚠️ For `Forbidden`, `exclude`'s regex **must not anchor with `^`** — the serialized string starts with the action name, so `^cat` won't match `cat` in the command. Use `\b(cat|grep|...)` or add context.

---

## Rule File Format (Declarative)

Each check in `rules/<subject>.checks.json` = one checker instance:

```json
{
  "target_agent": "<subject>",
  "checks": [
    {"id": "CP_graph", "type": "Exists", "severity": "required",
     "match": {"regex": "graph"}, "reason_tmpl": "read graph"},

    {"id": "MS_flow", "type": "Milestone", "severity": "required",
     "steps": [{"regex": "graph"}, {"action": "spawn_subagent"}],
     "reason_tmpl": "skeleton in order"},

    {"id": "OUT_report", "type": "Produces", "severity": "required",
     "name": "_report.md"},

    {"id": "CPN_bypass", "type": "Forbidden", "severity": "forbidden",
     "match": {"action": "run_command", "regex": "..."},
     "exclude": {"regex": "\\b(cat|grep)\\s"}}
  ]
}
```

Fields: `id` / `type` (one of 7) / `severity` (required/recommended/forbidden/optional) / `reason_tmpl` + checker-specific keys. Adding a new rule = adding one object; no code changes.

**One rule file per agent under test.** See existing rules in `../rules/`; checkpoint descriptions per rule live in the corresponding `../rules/<subject>.README.md`.

---

## Data Flow

```
hook trace / official session records
        │  normalize (fan-out / alias / semantics / official enrichment)
        ▼
   normalized.json  (actions[])
        │  rule.runner + rules/<subject>.checks.json
        ▼
   PASS / WEAK_PASS / FAIL + health score + per-check reason
```

The engine only consumes normalized output. It doesn't care whether the data came from hook or official records, or which agent is under test.
