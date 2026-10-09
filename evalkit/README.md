# evalkit — Agent Trajectory Evaluation Kit

> In one sentence: turn the **raw records** of a Kiro agent run into a **clean action sequence**, then use **a set of rules** to judge whether it "executed along the expected trajectory", emitting PASS / WEAK_PASS / FAIL + a health score.

---

## 1. What problem it solves

Just looking at an agent's final "answer" is not enough to judge how good it is — many issues hide in the **process**. Real pitfalls encountered:

- An agent claimed to have "done a LIVE evaluation", but in fact **never actually invoked the target under test**; the results were all "fabricated after reading the target's prompt".
- An orchestrator **overstepped its role** and personally ran the evaluation script that should have been dispatched to a sub-agent.
- A run **skipped the required collection step** and dispatched directly.

None of this is visible from the final output. You can only catch it by laying out the **execution trajectory** and checking it rule by rule. That is what evalkit does: **trajectory-level automated evaluation**.

---

## 2. Three-layer structure

```
Kiro raw records                       evalkit
─────────────                          ─────────────────────────────────────
hook trace   ┐                         ┌─ normalize/  raw records → unified action sequence (TraceIR)
official session ┘  ─────────────────► │  rule/  validate action sequence against rules; emit verdict + health score
                                       └─ rules/      one rule file per subject-under-test (pure data)
```

| Layer | Directory | Responsibility | Relation to subject-under-test |
|-------|-----------|----------------|--------------------------------|
| Data | `normalize/` | raw records → `actions[]` (fan-out / alias normalization / semantic extraction / official enrichment) | independent, generic |
| Engine | `rule/` | walk through rules against the action sequence, aggregate into a verdict | independent, generic |
| Rules | `rules/` | declare "how this agent should behave" (one `.checks.json` per agent) | **the subject lives here; changing subjects only means changing rules** |

**Key design: the engine is decoupled from the subject-under-test.** `normalize` and `rule` are the "evaluation software"; agent-eval, eval-security-tester, etc. are the "subjects under test", and their expected trajectories are placed as data under `rules/`. Adding a new subject = writing one rule file; the engine is not touched.

---

## 3. Where the data comes from

Two data sources; the action sequences they produce are isomorphic (mutually verifiable):

- **hook trace** — real-time hook records from the collection layer, at `~/agent-trace/traces/<session>/trace.jsonl`. Advantages: captures operations blocked by policy, precise millisecond timing.
- **Kiro official session records** — built into Kiro, at `$KIRO_HOME/sessions/cli/<session>.json{,l}`. Advantages: more complete (includes agent name, tokens, cost, full thinking); parent/child agents naturally live in separate files.

For offline trajectory evaluation, the official records are recommended (more complete); for real-time / interception auditing, use hooks. The normalize layer accepts both.

---

## 4. Quick start

```bash
cd ~/agent-trace/evalkit

# ① View the action sequence of a run (hook source)
python3 -m normalize.cli table <session-id>
python3 -m normalize.cli dump  <session-id>            # full JSONL
python3 -m normalize.cli dump  --official <session-id> # use the official-records source

# ② Evaluate the trajectory against a subject's rules
python3 -m rule.runner rules/agent-eval.checks.json --session <session-id>
#   or against an already-normalized file:
python3 -m rule.runner rules/agent-eval.checks.json path/to/normalized.json

# ③ One-shot export to OpenTelemetry OTLP/JSON (feeds Jaeger/Tempo/otel-collector)
python3 -m normalize.cli export-otel <session-id> --source hook     --out t.json
python3 -m normalize.cli export-otel <session-id> --source official --out t.json
python3 -m normalize.cli export-otel <session-id> --source both     --out t.json  # hook + official enrichment
```

### End-to-end pipeline (pipeline.py)

Chains "(archive) → normalize → auto-pick rule by agent name → evaluate → (optional OTLP / LLM judge)" into a single command:

```bash
python3 pipeline.py <session-id>                 # official source, auto-select rule, evaluate
python3 pipeline.py <session-id> --archive       # archive the whole dispatch tree first (pack_run.sh)
python3 pipeline.py <session-id> --otel out.json # also export OTLP/JSON
python3 pipeline.py <session-id> --llm           # enable LLMJudge (using kiro as the LLM)
python3 pipeline.py path/to/normalized.json --rule rules/x.checks.json
```

Output looks like this:

```
⚠️ WEAK_PASS   health=0.921
checkpoint            checker    sev            reason
collect_graph         Exists     required    ✓  read target agent graph
reachability_probe    Exists     required    ✓  reachability probe before dispatch
dispatch              Count      required    ✓  dispatched sub-agents (≥1)
MS_pipeline           Milestone  required    ✓  main flow: collect→generate→probe→dispatch in order (4/4)
produce_cases         Produces   recommended ✗  no case summary produced
CPN1_no_eval_script   Forbidden  forbidden   ✓  orchestrator forbidden from running eval script itself
```

---

## 4.5 How to run it after download (external users)

**Environment**: Python 3.10+. The core (evaluation + normalization + OTLP export) has **zero third-party dependencies**; only rendering PNG timelines needs `pip install matplotlib`. Commands must be run from the `evalkit/` directory (`normalize`/`rule` are imported as top-level packages).

**Try it right now** (the repo ships with anonymized samples; no data required):
```bash
cd evalkit
python3 -m rule.runner rules/example-minimal.checks.json examples/sample.normalized.json
# Expected: ✅ PASS health=1.0
```

**Using your own agent's data**, three options:
1. **Kiro official session source (easiest)**: as long as you use Kiro CLI, the records live in `$KIRO_HOME/sessions/cli/`; use them directly:
   ```bash
   python3 -m rule.runner rules/<your-rule>.checks.json --session <session-id> --official  # runner side
   python3 -m normalize.cli dump --official <session-id>                                          # look at actions first
   ```
2. **hook trace source**: requires the companion **hook collection component** (see `hooks/` + `install.sh` at the repo root); once installed, agent runs write traces to `~/agent-trace/traces` (override with `KIRO_TRACE_DIR`), then read with `--session <id>`.
3. **Bring-your-own normalized JSON**: shape your records as `{"actions": [...]}` (fields in `LLM_GUIDE.md`) and feed the path directly.

**Writing rules for your agent**: follow `rules/README.md`; copy and adapt `rules/example-minimal.checks.json` / `example-all-checkers.checks.json` into your own `.checks.json`.

**Recommended: structured intents + one-shot generation.** Rules can be written using **intent keywords** (reads/runs/write/dispatches/pipeline/before/never_*/if_claims…/judge), with no hand-written regex needed. See [`rules/AUTHORING.md`](rules/AUTHORING.md) for the syntax. Full workflow:

```bash
cd evalkit

# ① Generate: use Kiro to produce rules automatically from AUTHORING.md + your agent config
#    (edit the AGENT_JSON path at the top of rules/generate-rule.sh, then run)
bash rules/generate-rule.sh

# ② Self-check: verify the rules compile correctly (any issues are listed; non-zero exit)
python3 -m rule.runner rules/<your-agent>.checks.json --compile

# ③ Evaluate: run against your session
python3 -m rule.runner rules/<your-agent>.checks.json --session <id> --official
#   or one-shot: python3 pipeline.py <session-id>
```

You may also hand-write intent rules (see `rules/example-intent.checks.json`); low-level checker rules with `type` are still supported as-is.

> The hook collector is a **companion independent component** (at the repo root); evaluation itself does not depend on it — you can use only the official source or your own JSON.

---

Taking `agent-eval` as an example:

1. **Normalize**: hook trace (48 raw events) → 48 actions; each action has `action` (read_file/run_command/spawn_subagent...), `path`/`command`/`pattern`, `idx` (order), `turn` (which round), etc.
2. **Check one by one**: the rule has 11 checkpoints; each uses a checker against those 48 actions, getting ✓/✗ + a one-line reason.
3. **Aggregate verdict**: per `rules/scoring.json` — all required pass and no forbidden hits → not FAIL; missing recommended → drop to WEAK_PASS; else PASS. A 0–1 health score is also computed.

---

## 6. Seven checkers (the building blocks of rules)

| checker | Judges | Typical use |
|---------|--------|-------------|
| `Exists` | An action occurs at least once (unordered) | "must have read the graph" |
| `Count` | Occurrence count falls in an interval | "the same file is read at most 3 times" |
| `Forbidden` | Forbidden to appear (any hit is a violation) | "must not run the eval script itself" |
| `Before` | Action a must precede b | "must read before modifying a file" |
| `Milestone` | A sequence of actions occurs in order (anything may appear in between; yields progress score) | "collect → probe → dispatch skeleton" |
| `IfThen` | If a appears, b must appear (conditional implication) | "**claiming LIVE means it must actually call the target**" (catches fakery) |
| `Produces` | A file was produced (only the artifact name; automatically matches write-type actions) | "produced a report" |
| `LLMJudge` | (Optional 8th) Feed a trimmed trajectory to the LLM for scoring on dimensions: efficiency / reasoning_quality / authenticity | "let the LLM rate how well this run went" (requires `--llm`; skipped by default) |

Matching mixes **fields + regex**: use fields for precise criteria (`{"action":"spawn_subagent"}`), use regex for command-content details (`{"regex":"python3 .*_acp\\.py"}`). See `rules/README.md`.

---

## 7. Scoring and verdicts (customizable)

The verdict logic and health-score weights are not hard-coded; they live in `rules/scoring.json` and can be edited:

- **weights** — per-severity weights (required 1.0 / recommended 0.5 / optional 0.25 / forbidden 1.0)
- **binarize_checkers** — listed checkers are scored all-or-nothing (default `Milestone`: anything less than fully satisfied is 0, no progress credit)
- **verdict** — three switches control "what counts as FAIL and what counts as WEAK"

`--scoring my.json` swaps in a different set on the fly. See "Scoring and verdict configuration" in `rules/README.md`.

---

## 8. Directory guide

```
evalkit/
├── README.md                    ← this file (for humans)
├── LLM_GUIDE.md                 ← precise reference for LLMs
├── BUGS.md                      ← known bug log (all fixed)
├── normalize/                   data layer
│   ├── README.md                  module description
│   ├── core.py                    hook-source normalization
│   ├── official.py / official_loader.py   official record parsing/enrichment
│   ├── attribution.py             attribute sub-agents to the real session in multi-run scenarios
│   ├── schema.py mapping.py cli.py viz.py
│   └── tests/                     93 unit tests
├── rule/                  engine layer
│   ├── README.md                  engine + rule authoring
│   ├── checkers.py                7 checkers + matcher + validation gate
│   ├── runner.py                  run rules, aggregate verdict, load scoring
│   └── tests/                     24 unit tests
└── rules/                       rules layer (subjects under test)
    ├── README.md                  how to write rules (tutorial)
    ├── scoring.json               scoring/verdict configuration
    ├── agent-eval.checks.json     subject: evaluation orchestrator (example)
    ├── eval-security-tester-single.checks.json   subject: security-testing sub-agent (single-turn)
    ├── eval-security-tester-multi.checks.json    subject: security-testing sub-agent (multi-turn)
    ├── example-minimal.checks.json         example: minimal starter
    └── example-all-checkers.checks.json    example: demo of all seven checkers (with comments)
```

---

## 9. Adding a new subject-under-test

1. Run several real, legitimate runs of it; archive + normalize; use `normalize.cli dump` to see the actual actions
2. Distill the key nodes: things done every time → required; sometimes skipped → recommended; must never appear → forbidden; ordered → Milestone/Before; word-must-match-deed → IfThen; produced artifacts → Produces
3. Write `rules/<subject>.checks.json` (format in `rules/README.md`)
4. Validate on real runs that it does not misjudge; then check with negative samples that it can catch FAIL
5. `python3 -m rule.runner rules/<subject>.checks.json --session <id>`

The engine and normalization layers do not need to change.

---

## 10. Known limitations (not bugs, but capability limits)

- **File operations inside commands are invisible to hooks**: when the agent uses a `python3` script or `cat > f` to batch-write files, only one `run_command` is recorded, with no `create_file`. So `Produces` can only track "key artifacts produced by write tools" (e.g., `test_cases_*.json`, `report_*.md`), not the granular files produced inside scripts.
- **`--no-interactive` runs do not enter Kiro official records**; that scenario has only the hook source.
- **Sub-agent attribution**: after the hook fix, sub-agents live in directories keyed by real session id; older data is reconstructed via `parent_session_id` + prompt matching (~69%).

---

## 11. Development / testing

```bash
cd ~/agent-trace/evalkit
python3 -m unittest rule.tests.test_trajectory          # 24 engine tests
python3 -m unittest discover -s normalize/tests -t .          # 93 data-layer tests
```

The data-source collection layer (hook scripts, traces directory) lives at `~/agent-trace/README.md` and is not part of evalkit.
