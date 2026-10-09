# 5-minute tour

> **Audience**: first-time reader of this repo who wants to understand how the pieces connect and where to start.
> **Assumes**: you know what Kiro CLI is and have used it once.

## One picture

```
       ┌─────────────────────────────────────────────────────────┐
       │                    Kiro CLI runs an agent                 │
       │  (Kiro writes session records to $KIRO_HOME/sessions/cli/) │
       └─────────────────────────────────────────────────────────┘
                    │                             ▲
                    │                             │(optional) preToolUse block returns
                    ▼                             │
        ┌─────────────────────┐                   │
        │  hooks/  (optional)  │  writes trace.jsonl
        │  trace-hook.sh       │──────────────────►│
        └─────────────────────┘   (~/agent-trace/traces/)
                    │
        ┌───────────┴──────────────────────┐
        ▼                                  ▼
  ~/agent-trace/traces/          $KIRO_HOME/sessions/cli/
   (only if hook installed)         (always on, Kiro built-in)
                    │                                                 │
                    │                                                 │
                    └────────┐               ┌────────────────────────┘
                             ▼               ▼
                    ┌────────────────────────────┐
                    │  evalkit/normalize/          │
                    │  either source → same TraceIR│
                    └────────────────────────────┘
                             │
                ┌────────────┼────────────┐
                ▼                         ▼
       ┌─────────────────┐       ┌─────────────────┐
       │ evalkit/         │       │ evalkit/goal/     │
       │ rule/      │       │ 9-step pipeline │
       │ rule matching    │       │ user reqs → evidence │
       └────────┬────────┘       └────────┬────────┘
                │                         │
                ▼                         ▼
        PASS / WEAK / FAIL         PASS / WEAK / FAIL / INVALID
        + health score             + per-req evidence + confidence
        + OTLP/JSON export
```

## Which component do I want?

- **Batch-check whether Kiro sessions followed the expected trajectory** (automated verdicts): `evalkit/`
- **Deep-dive one run: did the agent actually accomplish what the user asked?** (need cited evidence): `evalkit/goal/`
- **Audit every tool call, block dangerous ones, get millisecond timing**: install `hooks/`
- **View agent behavior as a timeline in Jaeger / Tempo**: `evalkit/normalize/ export-otel`

## Suggested reading paths

**Just want to run it and see output**:
1. Top-level `README.md` (1 min)
2. `evalkit/README.md` §4 "quick start" (2 min) — copy-paste the commands
3. Open `evalkit/rules/example-minimal.checks.json` to see what a rule looks like (30 sec)

**Want to write rules for your own agent**:
1. `evalkit/rules/README.md` — every field of a rule file
2. `evalkit/rules/AUTHORING.md` — intent-DSL tutorial (`reads` / `runs` / `pipeline` keywords)
3. `evalkit/rules/example-all-checkers.checks.json` — annotated example of all 7 checkers

**Want to understand how goal does "evidence gathering"**:
1. `evalkit/goal/README.md` — table of the 9-step pipeline
2. `evalkit/goal/docs/R10.1_walkthrough.md` — one real requirement traced through all 9 steps
3. `evalkit/goal/DESIGN.md` — design rationale and rejected alternatives

**Want to add a new checker or a new direction**:
1. `CONTRIBUTING.md`
2. `evalkit/rule/README.md` (implementation of the current 7 checkers)
3. `evalkit/goal/directions/README.md` (direction concept + how to add one)

**Want to install the hook collector**:
1. `hooks/README.md` — capabilities, format, overhead, uninstall
2. `config/README.md` — purpose and fields of the 3 sample configs
3. Root `./install.sh` — run

## Common misconceptions

- **"I have to install hooks to use this"** ❌ — no. By default we read Kiro's own session records (`--official`). Hooks are an optional plugin.
- **"evalkit and goal are either/or"** ❌ — they share the same normalization layer. Both can evaluate the same session; their verdicts are complementary perspectives.
- **"Adding a new evaluation subject requires patching the engine"** ❌ — add a subject by writing one `rules/<x>.checks.json`. The engine stays untouched.
- **"LLMJudge is required"** ❌ — it's the optional 8th checker, active only with `--llm`. The core 7 checkers make zero LLM calls.

## Actually run it in 5 steps

```bash
# 1. clone
git clone <this-repo> Kiro_Trajectory_Eval && cd Kiro_Trajectory_Eval

# 2. Python 3.10+, zero deps — run the built-in sample immediately
cd evalkit
python3 -m rule.runner rules/example-minimal.checks.json examples/sample.normalized.json
# expected: PASS  health=1.0

# 3. Run the unit tests to confirm your environment is OK
python3 -m unittest discover -s normalize/tests -t .            # 117 tests
python3 -m unittest discover -s rule/tests -t .           # 65 tests
cd ../goal && python3 -m unittest discover -s tests -t .  # 59 tests

# 4. Grab one of your own Kiro session IDs and evaluate it
# First, inspect its action sequence:
cd ../evalkit
python3 -m normalize.cli table <session-id> --source official
# Pick a rule (or copy rules/example-minimal and edit)
python3 -m rule.runner rules/example-minimal.checks.json --session <session-id> --official

# 5. (optional) Install the hook collector to trace future Kiro runs:
cd ..
./install.sh
```
