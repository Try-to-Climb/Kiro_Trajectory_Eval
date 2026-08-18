# eval-agent — Forensic Trajectory Evaluation

The second evaluation path, sitting alongside `evalkit/`. **evalkit** performs full static verdicts with declarative rules; **eval-agent** does targeted forensics along evaluation dimensions: first break the user's request into individually verifiable requirements, then dig through the trajectory for evidence for each one, then deliver a conclusion backed by that evidence.

Design and empirical notes live in [`DESIGN.md`](DESIGN.md). The `goal_completion` direction is currently implemented.

## What it answers

**Did the agent actually accomplish what the user asked for?** Output: a per-requirement verdict + which actions/files were cited + confidence + any residuals that could not be verified.

Compared to a one-shot LLM judge: the LLM is only invoked as a function at 4 specific points (extracting requirements / extracting claims / compiling criteria / rendering the verdict); everything else is deterministic code; every LLM output passes a code-side validation gate; every conclusion must cite action indices that actually exist in the evidence pack — wrong references are treated as hallucination and bounced.

## Shortest getting-started

```bash
cd eval-agent

# Full pipeline (requires kiro-cli available)
python3 runner.py <session-id>

# Only the deterministic parts; no LLM spend
python3 runner.py <session-id> --no-llm

# Inject a human-confirmed requirements list; skip s2 (requirement extraction is the foundation of the whole chain)
python3 runner.py <session-id> --requirements my_reqs.json

# Machine-readable output
python3 runner.py <session-id> --json out.json

# Only the root session (by default, child sessions are loaded too)
python3 runner.py <session-id> --no-children
```

Environment: Python 3.10+, zero third-party dependencies. Depends on `evalkit/` from the same repo (normalization layer); use `EVALKIT_DIR` to point elsewhere.

## What the output looks like

```
PASS   completion=1.0   (strong/active 7/7 satisfied, 0 unverifiable, 0 unmet, overclaim 0)
Built-in control: 1 item, passed (all judged false)

Requirement Verdict  Evidence level     conf   Reason
R1.1        ✓       direct             0.9    Hard criterion hit: aaaa1111#2 cat example-dev-agent.json; child session bbbb2222#8 read…
            Evidence: aaaa1111#2, bbbb2222#8, cccc3333#0
R1.2        ✓       artifact           0.855  Filesystem hit: 5 strong artifacts (08:15:42–08:18:53 within the run's time window); artifacts produced by script…
R1.3        ✓       cross_session      0.63   Multiple child sessions ran example_target_runner.py --target example-dev-agent for the live evaluation…
R1.5        ?       derived            0.765  has_response=False; cannot verify final output → unverifiable rather than false
CTRL1       ✗       none               0.0    [control] no send-to-network action in the trajectory vocabulary sample; confirmed not done
```

Four verdict states: `PASS` / `WEAK_PASS` (has unverifiables) / `FAIL` (has unmet items or overclaims) / `INVALID` (the built-in control group failed → the judgment is too lenient; this run's result is untrustworthy).

## Nine-step pipeline

| # | step | Executor | What it does |
|---|---|---|---|
| 1 | map | code | Take a snapshot: define the search boundary (idx range per turn) + capability detection (does response / timestamp exist) |
| 2 | extract | **LLM** | Multi-turn conversation → verifiable atomic requirements (intent classification / status / strength) |
| 2b | control | code | Inject a fabricated requirement that certainly does not appear in the trajectory, as a built-in control |
| 3 | extract | **LLM** | Agent replies → list of self-claims for overclaim reconciliation |
| 4 | compile | **LLM** | Requirements → hard criteria + retrieval anchors + residuals; scope is filled in by code |
| 5 | search | code | Two lanes: precise hard-criterion lookup + top-K anchor retrieval. Emits three states, no verdicts |
| 6 | probe | code | Supplemental catch-up for misses: same-family write actions + trajectory-vocabulary samples |
| 7 | crosscheck | code | Filesystem reconciliation, covering trajectory blind spots (files written inside scripts do not appear in the trajectory) |
| 8 | judge | **LLM** | Assemble the evidence pack → per-requirement verdict + reason |
| 9 | aggregate | code | Aggregate; if the control fails, mark the entire result INVALID |

s1/s2/s3 are independent and can run in parallel; the two chains `s4→s5→s6` and `s7` can run in parallel; s8 joins them.

## Three key design choices

**Only s8 has verdict authority.** An earlier design let the LLM generate "verdict-form matchers", and s5 emitted hit/miss directly — in practice, out of 13 criteria 6 MISS-ed and **not a single one was a real "did not do"**; all of them were mismatches between the criterion form and the actual implementation form. Now s4 only produces hard criteria (structured fields + filesystem; no command-text peeking) and retrieval anchors; s5 only produces candidates. The LLM's error mode is downgraded from "producing wrong verdicts" to "insufficient recall", and insufficient recall is detectable (`suspect_anchors`) and recoverable (s6 catch-up).

**Confidence is derived from the evidence tier, not given by the LLM.** `direct 1.0 > artifact 0.95 > derived 0.85 > retrieved 0.75 > cross_session 0.70 > testimonial 0.30`; unverifiable residuals apply a 0.9× multiplier.

**Built-in control.** Every run injects one fabricated requirement that certainly does not appear in the trajectory (e.g., "push to Slack"); it is only injected when its anchors have zero hits across the whole tree. A correct system must judge it `false`; if it is judged satisfied, the judgment is too lenient and the entire result is marked `INVALID`. This solves the "without negative samples we cannot verify verdict trustworthiness" problem.

## Relationship to evalkit

**Facts shared, verdicts separate.** Normalization (action sequences, idx numbering, fan-out, alias normalization) fully reuses `evalkit/normalize`, no changes; matchers reuse `evalkit/trajectory/checkers.py`. The two paths must produce the same action sequence for the same run, otherwise they cannot cross-check each other.

The verdict layer does not shoehorn itself into `CheckResult` — goal_completion is naturally three-state + evidence-tiered + residual + overclaim. When merging reports, use `schema.Finding.to_check_result()` as an adapter.

An empirical complementarity example (session `aaaa1111`): evalkit reports `produce_cases ✗` (a capability boundary it acknowledges in its docs: files generated by scripts are invisible); eval-agent's s7 crosscheck finds 5 `test_cases_*.json` files, all strong, filling in that blind spot.

## Directory

```
eval-agent/
├── README.md / DESIGN.md
├── runner.py                 orchestrator + ledger + budget + CLI
├── steps.py                  nine steps + validators
├── schema.py                 Requirement / Claim / Criterion / SearchResult / Finding + evidence tiers
├── prompts.py                prompt templates for the four LLM sites
├── llm.py                    call + ANSI stripping + JSON extraction + validation retry + backend retry
├── evidence/
│   ├── loader.py               run-tree loading (parent + recursive child sessions), time-window reconstruction
│   └── api.py                  overview / query / retrieve / get / window / hard_check / crosscheck
├── directions/
│   └── goal_completion.json    workflow definition (pure data)
└── tests/                      56 unit tests (fully deterministic; no live LLM backend)
```

## Development

```bash
cd eval-agent
python3 -m unittest discover -s tests -t .      # 56 tests
```

All unit tests inject stub callers and never touch a real backend. Coverage includes: retrieval scoring and idf weighting; scope semantics (root bounded by idx, child sessions unbounded); the three kinds of hard criteria; crosscheck strength tiers; three-state classification; conditions for synthetic control-group injection; verdict aggregation and INVALID; confidence ceiling; JSON extraction (fences / ANSI / nesting / braces inside strings); the four validation gates; the citable set for the evidence pack.

## Known boundaries

- **s2 extraction results are unstable across runs**: the same session yields 13 or 14 requirements across two runs; strong/active drops from 10 to 7. Requirement extraction is the foundation of the whole chain; for important evaluations use `--requirements` with human confirmation.
- **`response` is not collected by default**: `outcome`-class requirements (where you need to see command output to know whether it succeeded) can only be judged `unverifiable`. Collecting via `--with-responses` resolves this.
- **Official source has no millisecond timestamps**: the time window is reconstructed via `meta.created_at` → session-record file mtime, at second granularity. `duration_s` cannot be summed (that is net working time; measured 13.7min vs wall-clock 40min).
- **Shared state does not count as evidence**: a listening port cannot be attributed to this run; always tagged `info`.
- **No semantic parsing inside commands**: `run_command` is one big text string (measured up to 6933 chars); files written inside scripts are caught by s7 crosscheck, not by action fields.
