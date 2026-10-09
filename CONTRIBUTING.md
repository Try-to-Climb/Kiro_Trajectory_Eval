# Contributing

Thanks for considering a contribution. This project has two evaluator components (`evalkit/`, `evalkit/goal/`) that share a normalization layer, plus an optional hook collector.

## Development setup

```bash
git clone <repo>
cd Kiro_Trajectory_Eval

# Core is zero third-party deps. Optional viz dep:
pip install matplotlib
```

Python 3.10+.

## Running tests

```bash
# evalkit (normalize + rule)
cd evalkit
python3 -m unittest discover -s normalize/tests -t .
python3 -m unittest discover -s rule/tests -t .

# goal
cd ../goal
python3 -m unittest discover -s tests -t .
```

All tests must pass before opening a PR.

## Adding a new evaluation subject to evalkit

1. Run the subject a few times and capture the sessions.
2. Normalize them and inspect the action sequence: `python3 -m normalize.cli dump --official <session-id>`.
3. Extract the pattern: what must always happen (`required`), what's optional (`recommended`), what must never happen (`forbidden`).
4. Write `evalkit/rules/<subject>.checks.json`. See [`evalkit/rules/AUTHORING.md`](evalkit/rules/AUTHORING.md) for intent-driven syntax.
5. Validate: `python3 -m rule.runner rules/<subject>.checks.json --compile`.
6. Test with a real good run and a real bad run.

The engine (`normalize/`, `rule/`) should not need changes.

## Adding a new direction to goal

1. Create `evalkit/goal/directions/<name>.json` describing steps, budgets, degrade behavior, validators.
2. Implement the step functions in `evalkit/goal/steps.py`.
3. Add tests in `evalkit/goal/tests/`.

See `evalkit/goal/DESIGN.md` for the design contract.

## Coding style

- 4-space Python indentation; 2-space YAML/JSON.
- Prefer standard library over third-party dependencies.
- LLM-calling code paths must go through a validation gate (schema → structural → factual check). See `evalkit/goal/steps.py` for the pattern.
- Every LLM conclusion must cite evidence (`action_id` / file path) that exists in the trajectory.

## Pull requests

- Small, focused PRs preferred.
- Include tests.
- Update `CHANGELOG.md` under `[Unreleased]`.
- Do not include real user data. Trace fixtures should use synthetic session IDs and paths.

## Reporting bugs

See [`SECURITY.md`](SECURITY.md) for security issues. For regular bugs, open a GitHub issue with:
- A minimal reproduction (session dump if applicable).
- Expected vs actual verdict/output.
- Python / OS version.
