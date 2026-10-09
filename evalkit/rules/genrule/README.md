# genrule — rule generation by task/policy extraction (experimental)

Adapted from IntellAgent: instead of dumping the whole agent config
into one LLM call, decompose it — task types first, then policies per stage — and add two gates
IntellAgent does not need.

## Differences from generate-rule.sh

| | generate-rule.sh | genrule |
|---|---|---|
| LLM calls | 1, emits checks.json directly | N+2 ACP turns: task types, then policies per stage, then backbone |
| Intermediate artifact | none | `*.policy_ir.json`, reviewable by a human |
| Observability | not distinguished | forced 3-way classification; `judge_only` -> judge, `unobservable` -> dropped |
| severity | guessed from prompt wording | calibrated against real legitimate runs |
| checks.json | written by the LLM | compiled deterministically in Python |
| Multi-purpose agents | one file for everything | one file per task type |

## Steps

The extraction steps drive `kiro-cli acp` through the reusable client at
`~/acp-pipeline/acp_client.py`. Override that location with `ACP_CLIENT_DIR` if it lives
elsewhere. Generated artifacts land in `out/`, which is not tracked.

```bash
cd evalkit

# turn 1 only, to inspect the task split before spending more turns
python3 rules/genrule/extract_tasks.py <agent.json | prompt.md> [more...]

# full extraction (one policy IR per task type)
python3 rules/genrule/generate.py <agent.json | prompt.md>

# compile + calibrate (no LLM)
python3 rules/genrule/compile_ir.py rules/genrule/out/<name>.policy_ir.json \
    --calibrate <session-id> ... --out rules/genrule/out/<name>.checks.json

# self-check and evaluate through the existing engine (unmodified)
python3 -m trajectory.runner rules/genrule/out/<name>.checks.json --compile
python3 -m trajectory.runner rules/genrule/out/<name>.checks.json --session <sid>
```

## Calibration rules

| case | outcome |
|---|---|
| hit rate 1.0 and runs >= `--min-required-runs` (default 5) | required |
| hit rate 1.0 but fewer runs | recommended (4/4 does not establish "always") |
| ordering check (pipeline / before) at 1.0 | capped at recommended — every leave-one-out misjudgement came from `before` |
| 0 < hit rate < 1.0 | recommended |
| hit rate 0 | optional, flagged REVIEW |
| `write` at 0 but the path appears | switched to `touches` (shell redirection / in-script writes are blind spots) |
| `never_*` tripped by a legitimate run | dropped and reported (rules_dsl forces never_* to forbidden, so lowering importance has no effect) |

## Known limits

- Calibration runs must be legitimate **and of the same task type**. Same agent name with a
  different task pollutes the statistics.
- Five runs is the floor. Under leave-one-out (4 training runs) nothing is promoted to required,
  so the generalisation of `required` has not been independently validated yet.
- `judge_only` checks need `--llm`; calibration skips them.
- Task types come from the prompt only. Usage variants that the prompt never describes are not
  discovered at this stage.
- Output lands in `out/` and never overwrites hand-written rules in `rules/`. When `target_agent`
  collides with a hand-written file, `pipeline.py` aborts on multiple matches — pass `--rule`.
