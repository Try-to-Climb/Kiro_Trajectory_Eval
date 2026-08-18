# evalkit vs AgentDiagnose — Comparison, Similarities/Differences, and Lessons (code-verified)

Subject: **AgentDiagnose** (an open-source LLM agent trajectory diagnosis tool). All conclusions below are **verified by reading source code**, with files and evidence cited.

---

## 0. One-Line Positioning

- **evalkit (ours)**: a deterministic **rule engine** — normalizes Kiro agent trajectories into an action sequence and uses declarative checkers to judge "did it follow the expected trajectory, is there any fraud", outputting PASS/WEAK/FAIL + a health score. Targets **compliance auditing of CLI / tool-oriented agents**.
- **AgentDiagnose**: an **LLM-as-judge + exploratory analysis** tool — scores trajectories of **web-navigation agents (NNetNav / browsergym, etc.)** on "quality" (reasoning quality, objective quality, navigation path), with embeddings clustering, word clouds, and a Svelte interactive dashboard. Targets **qualitative diagnosis and visualization of agent quality**.

The two are **complementary** paths: we judge "correct / compliant" (deterministic); it judges "how good" (LLM-subjective) + "what does it look like" (exploratory).

---

## 1. Similarities

| Aspect | In common |
|--------|-----------|
| Core abstraction | Both have a `Trajectory` / action sequence intermediate representation; evaluation consumes it |
| Pluggable scorers | Both are "multiple independent scoring units + weights + aggregation"; subsets can be selected |
| Weighted aggregation | Both use Σ(weight×score)/Σ(weight) for the total (evalkit health / AgentDiagnose weighted_average) |
| Declarative / configurable | evalkit rules are JSON; AgentDiagnose has `TrajectoryJudge.from_config` for dynamic scorer loading |
| Batch + JSON output | Both support batch running of all trajectories and structured result export |
| Multi-source ingest | Both normalize "raw records of different formats" into a unified model |
| Visualization intent | Both want "beyond rule verdicts, also charts / analysis" (we just added viz + OTel; they have the dashboard) |

---

## 2. Differences

| Aspect | evalkit (ours) | AgentDiagnose |
|--------|----------------|---------------|
| Verdict nature | **Deterministic** (rules / regex / order relations); reproducible, zero cost, zero deps | **LLM-subjective** (gemini/gpt scoring, litellm); has cost, needs API key, not fully reproducible |
| Target agent | CLI / tool-oriented (shell/read/write/subagent) | Web-navigation (click/type/goto, URL+AXTree+screenshots) |
| Action model | idx/action/path/command/pattern/completed/blocked... | action/reasoning/**url**/action_type/observation/**image_encoding**/label_pos |
| **reasoning** | ❌ No reasoning at the action level | ✅ Every action has a first-class `reasoning` field, key input to LLM scoring |
| Fraud detection | ✅ Strong (IfThen word-action consistency, Forbidden overreach) | ❌ No such concept |
| Quality dimensions | ❌ Can't judge "is the reasoning good" | ✅ Strong (task_decomposition / self_verification / observation_reading / backtracking) |
| Exploratory analysis | None (only timeline / replay) | ✅ verb-noun word cloud, embeddings clustering |
| Frontend | CLI + matplotlib PNG | Svelte + FastAPI + Plotly interactive dashboard, cross-tab linked filtering |
| schema | dataclass + hand-written validation gateway | pydantic (BaseModel auto validation / serialization) |
| Standard alignment | ✅ Aligned to OTel GenAI + OTLP export | ❌ No standard alignment, custom JSON |

---

## 3. Lessons (each verified by reading source)

> For each: what to learn / evidence (file + basis) / recommended adaptation for us.

### L1. Every action carries reasoning — the door to "quality evaluation" ⭐⭐⭐
- **Evidence**: `evaluator/trajectory.py` `Action.reasoning` is a first-class field; `reasoning_quality.py::_format_steps` feeds each `action.reasoning` to the LLM to score "task decomposition / self-verification / reading observations / backtracking".
- **Our gap**: `normalize/schema.py::Action` **has no reasoning field**. Kiro official records actually have `thinking` (`official_loader` only collects `thinking_text` at the turn level, doesn't attach to actions).
- **Adaptation**: add `reasoning`/`thinking` to `Action` (attribute AssistantMessage thinking from official records to the toolUse of the same message). This is the prerequisite for any "quality / reasoning" evaluation we do — **highest ROI**.

### L2. Pluggable multi-format ingest adapters ⭐⭐
- **Evidence**: `trajectory.py` has 5 classmethods `from_json` / `from_browsergym_pickle` / `from_cuga_record` / `from_synatra_training_file` / `from_agentTrek_training_file`, each parsing one agent-record format into an isomorphic `Trajectory`.
- **Us**: already have hook / official sources, but scattered across `core.normalize_file` and `official_loader`, no unified "adapter" convention.
- **Adaptation**: define an `IngestAdapter` convention (`can_handle(path)` + `load(path)->TraceIR`) + registry; adding a third-party / other-agent format later (even OTLP reverse import) only needs one adapter. Echoes the earlier "make evalkit more open" direction.

### L3. LLM-as-judge scorer, as a complement to deterministic rules ⭐⭐
- **Evidence**: `scorers/base.py::LLMScorer` (generate_prompt / parse_response / count_tokens / dry_run); `objective_quality.py` / `reasoning_quality.py` use rubric system prompts + require ```json``` responses, score /4.0 normalization, with `justification`.
- **Us**: purely deterministic; can't judge subjective quality.
- **Adaptation**: add a class of `LLMChecker` in the trajectory engine (peer to existing checkers, severity=recommended), enabled only on demand; **verdict backbone remains deterministic**, LLM scores as a reference dimension only. Keep "deterministic-first, LLM optional" to preserve reproducibility.

### L4. Dry-run cost estimation ⭐⭐
- **Evidence**: `base.py::LLMScorer.dry_run` counts tokens with `tiktoken.cl100k_base` without sending requests; `evaluate_trajectories.py`'s dry-run branch sums per-scorer tokens. **Read code confirms this path is correct** (reads `details['token_count']`; base does write that key).
- **Adaptation**: if L3's LLMChecker is introduced, copy dry-run: estimate token / cost first, then decide to run.

### L5. Incremental save + resume for long batches ⭐⭐
- **Evidence**: `evaluate_trajectories.py`: on start, if `output_json` exists, load it and remove completed items from the pending list; `save_interval=50` for periodic atomic writes (write `.temp`, then rename).
- **Us**: `cli.py::cmd_all` runs in one pass; a crash means restart.
- **Adaptation**: add `--resume` + periodic atomic dump to `cmd_all` / batch eval. For our rule evaluation this is quick, medium value; but for L3 LLM evaluation (slow + costly) this is essential.

### L6. Parallel batching ⭐
- **Evidence**: `evaluate_trajectories.py` uses `concurrent.futures.ThreadPoolExecutor(max_workers)` + `tqdm` as_completed.
- **Adaptation**: our `cmd_all` running 885 official sessions is sequential; could parallelize with a process pool (ProcessPool is more suitable for pure-CPU normalization).

### L7. Every result carries confidence ⭐
- **Evidence**: `scorers/base.py::ScorerResult.confidence` (navigation 0.8, LLM 0.9, fail 0.0).
- **Us**: `CheckResult` has no confidence (deterministic checks are always 1.0, but LLMChecker will need it).
- **Adaptation**: add confidence to `CheckResult` when L3 is introduced.

### L8. "Scorer as pure metric emitter" (score=0, only produces details) ⭐
- **Evidence**: `navigation_path_scorer.py` — both returns have `score` constantly 0.0; the real value is in `details` (backtrack_count / domain_transitions / actions_per_url…), consumed by the dashboard.
- **Adaptation**: we could add "stats-only, non-judgmental" metric checkers (e.g. "repeat-read count", "subcommand distribution"), feed viz without affecting verdict.

### L9. verb-noun exploratory analysis + embeddings clustering ⭐ (web/NLP-leaning, optional)
- **Evidence**: `trajectory.py::label_verb_noun` uses spacy+benepar to extract (VERB, dobj-NOUN) pairs from reasoning; `generate_embeddings.py` uses SentenceTransformer (Qwen3-Embedding-0.6B) + FAISS (flat/ivf) to index verb/noun/pair for semantic clustering / retrieval.
- **Adaptation**: for Kiro this could translate to "extract verb-object from thinking to see what the agent is doing" / "cluster embeddings of command / action sequences to find anomalous trajectories". Heavy deps (spacy/faiss/torch), **on demand**.

### L10. pydantic for result schema ⭐ (tradeoff)
- **Evidence**: `ScorerResult` / `EvaluationResult` are pydantic BaseModels, auto validate + `model_dump`.
- **Tradeoff**: we just did `schema_version` + a hand-written validation gateway, and deliberately kept **zero deps** (easy to embed anywhere). Introducing pydantic saves validation code but adds a dep. **Recommendation: don't introduce for now**, keep the zero-dep advantage; unless schema complexity grows significantly.

---

## 4. What NOT to Copy (also code-verified, avoid pitfalls)

### N1. Token accounting on the parallel path is broken ❌
- **Evidence**: `objective_quality.py::score` stores `token_usage['prompt_tokens']` / `['completion_tokens']` / `['total_tokens']`; but `evaluate_trajectories.py::process_parallel_result` reads `token_usage.get('input_tokens')` / `get('output_tokens')`. **Keys don't match**, so "TOKEN USAGE SUMMARY" is always 0 on parallel. If adopting L4/L5, unify key names.

### N2. `TrajectoryJudge` / `from_config` is dead code in the CLI main path ❌
- **Evidence**: `evaluate_trajectories.py::main` uses its own `evaluate_trajectories` + `aggregate_results`, never instantiates `evaluator/evaluator.py::TrajectoryJudge`. Two aggregation logics coexist; `min/max` aggregation lives only in the unused one. Lesson: **one aggregation path, don't split**.

### N3. Hardcoded `models[name]` dict KeyErrors on unknown scorers ❌
- **Evidence**: in `evaluate_trajectories.py::evaluate_trajectories`, `scorer_kwargs['model'] = models[name]`; `models` only has 3 built-in names — pass a custom scorer name and it crashes (no `.get` fallback). This is exactly the kind of "missing input validation" we fixed last round — don't repeat it.

### N4. navigation scorer score=0 but still enters weighted denominator ❌
- **Evidence**: in `aggregate_results`, navigation's weighted_score=0 but weight is counted in the denominator, **diluting** overall_score. Be careful mixing "metric emitter" and "scorer" in one aggregation (echoes L8: metric types should be excluded from verdict / total score).

---

## 5. Conclusion: Priority Recommendations

1. **L1 (action carries reasoning)** — highest priority, the foundation for all quality evaluation; the data source (official thinking) already exists, only attribution / attachment is missing.
2. **L2 (ingest adapter registry)** — aligns with the "more open" direction; small refactor, long-term benefit.
3. **L8 (metric checker) + N4 lesson** — low-cost extension for "stats-only, non-judgmental" dimensions feeding viz.
4. **L3 + L4 + L5 + L7 (LLM-as-judge family)** — mid-term; introduces the subjective quality dimension; must be "deterministic first, LLM optional + dry-run cost control + unified token keys (avoid N1)".
5. **L9 / L10** — on-demand / defer (heavy deps / conflicts with the zero-dep principle).

All similarities/differences and lessons have been verified against the source. Section 4's pitfalls are empirically grounded counter-examples from their code; adopt lessons while avoiding them.
