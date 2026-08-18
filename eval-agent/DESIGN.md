# eval-agent —— Forensic Trajectory Evaluation (Design Draft)

> The second evaluation path, parallel to `evalkit/`. evalkit is **full static scan + deterministic rules**; eval-agent is **directed forensics per evaluation dimension** — the agent, holding a set of restricted read-only tools, retrieves needed evidence from the trace step by step, rather than dumping a truncated trajectory to the LLM in one shot.

Status: three directions are fixed (`goal_completion` / `efficiency` / `compliance`). **`goal_completion` is implemented and runs end-to-end on two real sessions** (56 unit tests all green); 6 more bugs were fixed during implementation (see §5.4.1). `efficiency` / `compliance` not yet implemented. Usage in [`README.md`](README.md).

---

## 1. Why not the current LLMJudge

The existing `trajectory/llm_judge.py` is one-shot: `judge_view.py` compresses the trajectory into a compact view → sends it, together with the rubric, in one shot to `kiro-judge` → gets back a 1–4 score. Problems:

- Overly long trajectories must be truncated; the LLM doesn't know what it didn't see
- No follow-up questions, no way to verify hypotheses, verdict not reviewable (only a score and a paragraph)
- Evidence and conclusion are not bound, hallucination cannot be detected

**Reference: agent-as-a-judge (ICML 2025) and its limits**: it runs an evidence-collection workflow per requirement (`workspace → locate → read → search → history → trajectory`), tiered by `--setting`/`--planning`. But its main evidence arena is the **workspace** (`DevGraph` builds the code graph, `DevLocate` locates files, `DevRead` reads files); the trajectory step `DevTextRetrieve.llm_summary` is very coarse — concatenate all steps into a big string, truncate to 10k tokens, feed to LLM once. **Same problem as our LLMJudge.**

So eval-agent is not a copy of it, but does the half it left undone (step-by-step forensics on the trajectory side).

---

## 2. Positioning: not "static vs dynamic", but "verdict vs explanation"

| | evalkit | eval-agent |
|---|---|---|
| What is judged | Whether it matches the **predefined** trajectory | How well it ran / **why** it failed |
| Premise | Known how the agent should run (must write rules first) | Don't know how it should run, or subjective dimensions rules can't judge |
| Mechanism | Deterministic, full-scan, zero cost, reproducible | LLM-driven, on-demand deep-dive, costly |
| Output | verdict + health score | findings (with evidence) + score |
| Cold start | Requires human-written `.checks.json` first | Can run without rules |

eval-agent should not repeat what evalkit can do. Its exclusive value is two things: **give explanations backed by evidence**, and **cold-start on a new agent without rules**.

---

## 3. Four-layer architecture

### 3.1 Evidence layer: restricted read-only trace query API

The agent is not allowed to read `trace.jsonl` directly — that only trades one-shot context explosion for staged explosion, and is unauditable. Give it a bounded, read-only, deterministic toolset (corresponding to AaaJ's `DevGraph/DevLocate/DevRead`, with the object switched from workspace to TraceIR):

| Tool | Purpose | AaaJ counterpart |
|---|---|---|
| `overview()` | session tree metadata + action histogram + turn/run structure + dispatch tree | `display_tree()` |
| `query(filter, page)` | Filter by action/tool/path/command/regex/turn/run; return **idx + one-line summary**, with `total`/`has_more` | `locate` |
| `retrieve(anchors, k)` | Score-rank actions by full-text anchor matching, return top-K candidates + scores (reuses `checkers._serialize()`) | `DevTextRetrieve` (BM25/embedding) |
| `get(idx, fields)` | Fetch detailed fields for a single action (full command / reasoning / response) | `read` |
| `window(idx, ±n)` | Fetch a slice around a given idx, for temporal context | none (trace-specific) |
| `stats(group_by)` | Aggregate: reads per file, command frequency, latency distribution, failed retries | `statistics` |
| `verify(matcher)` | Call evalkit's matcher/checker for deterministic comparison (**only on structured fields, not on command content**) | none (our own) |
| `crosscheck(path)` | Verify "the file it claims to have produced actually exists" on the filesystem/git | AaaJ's main arena |

Three hard constraints:

1. **Bounded return.** Default gives summary lines + `total`/`has_more`; full text requires an explicit second request. Let the agent decide whether to dig deeper or stop.
2. **`verify()` is the killer feature.** The LLM proposes a hypothesis ("looks like it never actually ran that script"), and instead of reading a pile of actions itself to confirm, it compiles the hypothesis into a matcher and hands it to the deterministic engine. Effectively giving the LLM a magnifying glass that cannot hallucinate, while naturally reusing `checkers.py`.
3. **evidence ledger.** All tool call records (tool name + args + returned idx set), **stores only idx, not full text**. This is the foundation for anti-hallucination and reviewability, and the means to control context.

Known constraint: the `response` field is not collected by default (`--with-responses` needed); `reasoning` exists only in the official source (hook source is empty). Each workflow must explicitly declare which fields it depends on.

### 3.2 Workflow layer: evaluation direction → forensic playbook

Parallel to `rules/`, add a `directions/` where each evaluation direction is a pure-data playbook declaring:

- `steps[]` — which tools to use, purpose, intermediate conclusions to produce
- `budget` — max tool calls / LLM calls / tokens
- `requires` — data source and field dependencies (official? response? hook?)
- `evidence_required` — what class of evidence must be obtained before a conclusion is allowed
- `contributes_to_verdict` — is it a verdict item or a pure metric/diagnostic (echoing AgentDiagnose's N4 lesson: metric-type items should not enter the weighted denominator)

Two execution tiers (following AaaJ's planning tiers): `scripted` (fixed steps, cheap and reproducible) and `planned` (LLM plans the forensic path itself, expensive but flexible). AaaJ's `black_box`/`gray_box` corresponds here to "hook source available or not / response collected or not."

**See section 5 for details (TBD).**

### 3.3 Verdict layer: no evidence, no finding

Every finding must carry `evidence: [idx...]` or a tool_call_id from the ledger, **otherwise rejected**. This rule is more useful than any prompt trick, because it can be enforced by code:

- References a non-existent idx → hallucination, discard
- Referenced action doesn't support the conclusion (e.g., claims "didn't run the script" but cites a run_command) → suspicious, lower confidence

Output schema is isomorphic to `CheckResult` (`id/passed/severity/reason/confidence` + `evidence`), so it can be merged with evalkit results into the same report and health score. Confidence uses AaaJ's approach: k independent judgments → `satisfied_ratio` → `confidence = max(r, 1-r)`.

### 3.4 Orchestration layer

The lowest-effort deployment: the evidence layer is a CLI (`python3 -m evidence.cli query --action run_command --regex ...`), eval-agent invokes it via shell, and the agent definition restricts tools to this one command + read-only. Once it works, consider wrapping as an MCP server. **Do not jump to MCP on day one.**

---

## 4. Two interfaces with evalkit

**Interface 1: hotspot-guided (check-up then follow-up).** Run evalkit first, feed the FAIL/WEAK checkpoints and their hit idxs as entry-point clues to eval-agent: "`CPN1_no_eval_script` hit at idx 214, find out why." Much more efficient than letting the agent roam from zero; the two go from "parallel" to "pipeline."

The reverse also works: stable patterns eval-agent surfaces on rule-less agents, after human confirmation, **crystallize into `.checks.json`**, becoming zero-cost static checks next time. Positive feedback: dynamic discovery → static solidification.

**Interface 2: dogfooding loop.** eval-agent itself generates a trace when it runs; write a `rules/eval-agent.checks.json` in evalkit to evaluate it:

- `Exists` — must have called `query`/`verify`
- `IfThen` — outputs an authenticity conclusion → must have called `verify` or `crosscheck` (**catches "concluded without collecting evidence"**, same paradigm as "claimed LIVE but never actually called the target")
- `Forbidden` — no bypassing the API to read raw `trace.jsonl`
- `Count` — tool calls must not exceed budget

An evaluator's evaluator, itself governed by the same evaluator; also incidentally mitigates the "LLM verdict not reproducible" trust issue — the process is auditable.

---

## 5. Workflow directions and playbooks

### 5.1 Fixed three directions

| Priority | Direction | Answers what | Enters verdict |
|---|---|---|---|
| 1 | `goal_completion` | Whether what the user asked for was done | Yes |
| 2 | `efficiency` | How many calls were wasted | No (metrics only) |
| 3 | `compliance` | Any out-of-scope behavior, any attempt to bypass limits | Yes |

Later candidates (not now): `reasoning_quality` / `orchestration` / `failure_diagnosis`. `authenticity` (catching fakery) **is not a separate one** — it is absorbed by `goal_completion`'s self-claim reconciliation, and because there is a requirement as anchor, it is more precise than running standalone.

### 5.2 Step type registry

A workflow is a permutation + parameters of these steps, pure data. The LLM appears in only 3 kinds of steps; every output passes a code validation gate.

| step type | Executor | Input → Output | Validation |
|---|---|---|---|
| `map` | code | session tree → snapshot (boundaries + capability probe) | — |
| `extract` | **LLM** | text (prompt / response) → structured list | enum values + turn range + quote must be found in original text |
| `compile` | **LLM** | requirement `expect` → `hard_check` + `anchors` + `residual` | action whitelist, glob syntax, non-empty anchors |
| `hard_check` | code | structured fields + filesystem → deterministic yes/no | — |
| `retrieve` | code | anchors → candidate actions top-K + scores | — |
| `probe` | code | low-recall items → same-family action set + temporal window | bounded + sorted truncation |
| `crosscheck` | code | glob + time window → file hits + strength grading | path whitelist |
| `judge` | **LLM** | evidence bundle → verdict + reason | evidence must be a subset of refs that appeared in the bundle |
| `aggregate` | code | findings → verdict / score | — |
| `detect` | code | deterministic detector registry (H1..H7 / C1..C7) | used by efficiency and compliance |

**Key change (vs first draft): drop the `verify` step.** The first draft let the LLM generate a "verdict matcher" and step 5 directly emitted hit/miss — measured, this path's false-MISS rate is about half (see 5.4), because requirements are semantic and matchers are string-based, and the most-needed check "what the command did internally" is precisely the least structured. Now split into `hard_check` (deterministic verdict, using only structured fields and filesystem) + `retrieve` (deterministic retrieval, only surfacing candidates). **Verdict authority is centralized into the `judge` step.** The LLM's failure mode is downgraded from "produce wrong verdict" to "insufficient recall," and insufficient recall is detectable and can have a fallback.

### 5.3 goal_completion playbook

```
        ┌─ s1 map ────────┐
session ┼─ s2 extract reqs (LLM) ─┼─┬─→ s4 compile (LLM) → s5 hard+retrieve → s6 probe ─┐
  tree  └─ s3 extract claims (LLM) ─┘ │                                        ├→ s8 judge (LLM) → s9 aggregate
                             └─→ s7 filesystem crosscheck ───────────────────┘
```

s1/s2/s3 can run in parallel; the s4→s5→s6 chain and s7 can run in parallel; s8 merges them.

| # | step | Input | Output |
|---|---|---|---|
| 1 | `map` | session id (**including recursive child sessions**) | `turns` / `idx_range_per_turn` / `written_dirs` / `time_window` / `has_response` / `child_sessions[]` |
| 2 | `extract` | all user prompts (+ s1 `turns` for validation) | intent classification + requirement list: `id / origin_turn / verifiable_by(action\|artifact\|outcome) / status(active\|superseded\|cancelled) / strength(strong\|weak) / expect` |
| 3 | `extract` | all agent replies | claim list: `id / turn / kind(did\|produced\|verified\|declined) / text / quote` |
| 4 | `compile` | requirement `expect` + static action table (+ s1 `idx_range` to fill scope) | `hard_check` (`read_path` / `artifact_glob` / `invoke_agent`) + `anchors[]` + `residual` + `residual_needs` |
| 5 | `hard_check` + `retrieve` | s4 outputs + action sequence | three states: `hard` (definitely satisfied) / `candidates` (top-K + scores) / `absent` |
| 6 | `probe` | `absent` and low-score items | same-family action set + in-scope action summary (bounded) |
| 7 | `crosscheck` | `artifact_glob` + s1 `written_dirs`/`time_window` | file hits + `strength: strong/stale`; shared state uniformly `info` |
| 8 | `judge` | evidence bundle composed of all s2~s7 outputs | `satisfied(true/false/unverifiable)` + `evidence_actions` (with session ident) + `evidence_files` + `confidence` + `overclaim` + `reason` |
| 9 | `aggregate` | all verdicts | satisfaction ratio + unmet list + overclaim list → verdict |

**s4's three outputs**

```json
{"id": "R1.2",
 "hard_check": {"artifact_glob": "test_cases_*.json", "root": "written_dirs"},
 "anchors": ["test_cases", ".json", "cases", "gen_"],
 "residual": "the filename cannot prove that the content is a structurally valid, non-empty case set",
 "residual_needs": ["artifact_content"]}
```

`anchors` come from two parts: deterministic extraction (regex-pull paths, extensions, identifiers, agent names out of the requirement text) + LLM supplement of implementation-form synonyms (e.g., "start a port service" → `http.server` / `serve` / `uvicorn` / `nohup`). **Enumerating possible forms is what LLMs are good at; writing regex that must match exactly is not.**

**s5's two tracks**

- `hard_check`: no retrieval, directly compare structured fields (`path` contains / `pattern` equals) or glob on the filesystem. Only handles three classes — read a file, produced a file, invoked an agent. These three have clean fields; verdict cannot be wrong. Measured: two sessions with 12 requirements, 8 belong to these three classes.
- `retrieve`: reuses `checkers._serialize()` to concatenate an action's 6 fields into one string, counts anchor substring hits, sorts, takes top-K. Granularity = single action (post-fanout), aligned with the evidence citation unit.

**`absent` must be re-split**, this is the key to preventing false MISSes:

```
Total anchor hits across the whole trajectory == 0 → suspect_anchors (anchors may be wrong, cannot judge unmet)
Anchors have hits but none constitutes evidence   → likely_not_done (may actually be undone)
```

### 5.4 Measured records (two real sessions)

**session `4b143da9`** (agent-eval, 11 turns / 76 actions, interactive exploration):
- s2 extracted 12 requirements (11 active + 1 **superseded auto-detected**), intent classified as 7 request / 2 question / 2 clarification
- s3 extracted 40+ claims, including 1 `declined`
- s8 judged 5/5 satisfied, confidence 0.72~0.85

**session `aaaa1111`** (agent-eval orchestrator, single-turn / 73 actions / 19 dispatches):
- A 77-char prompt yielded 7 requirements, including R1.3 "live mode must actually call the subject agent"
- s8 judged 5/5 satisfied, but **only 1 has direct trajectory evidence**; the rest rely on filesystem supplements, cross-session supplements, or overrides
- Compared with evalkit rule verdict: `WEAK_PASS 0.944`, the only deduction `produce_cases ✗` is exactly the capability boundary acknowledged in evalkit docs (files generated by scripts are invisible), and s7 crosscheck fills it in (5 `test_cases_*.json` all strong). **Complementarity empirically confirmed.**

**Problems found in practice and fixes (merged into the playbook above)**

| # | Problem | Fix |
|---|---|---|
| P1 | Verdict-matcher false-MISS rate ~half: 13 criteria 6 MISS, **none is "actually not done"** | Drop `verify`, use `hard_check` + `retrieve` (5.2) |
| P2 | LLM-generated `all_of:[{action:X},{path:Y}]` has wrong semantics — the two conditions can be satisfied by **different actions**, silently letting things pass | Conditions that must be satisfied by the same action must be written in one spec; in the new plan, borne by `hard_check` |
| P3 | Scope hard-lower-bound causes false MISS (requirement was completed before it was raised) | Double query: in-scope + full trajectory, mark the latter `evidence_before_request`, s8 decides per requirement's nature |
| P4 | Criterion references an action type that appears 0 times in the trajectory → certain MISS | `suspect_anchors` three-state split (5.3) |
| P5 | Time window cannot use `created_at + Σduration_s` (net work 13.7min vs wall 40min), all artifacts get misjudged `stale` | Use `created_at` → official-record file mtime |
| P6 | Official source `Action.ts` / `duration_ms` all empty (hook source has millisecond timing) | Time window rebuilt per P5; `duration_s` only for cost/efficiency, not for timing |
| P7 | **The orchestrator agent's core requirement cannot be judged in the parent session**: all 19 dispatches are its own eval-* sub-agents, the only direct-target action is a ping probe; real live calls are in `*_acp.py` across 6+ child sessions | s1's input changed from single session to **dispatch tree** (`archive/pack_run.sh` already recursively grabs the full tree by `parent_session_id`) |
| P8 | Filesystem evidence has no idx to cite, `evidence` field name misleading | Split into `evidence_actions` / `evidence_files` |
| P9 | After cross-session, idx collides; `[7]` exists in multiple sessions | `evidence_actions` must carry session ident |
| P10 | Weak requirements with criteria so broad as to be meaningless ("read this project") | s2 adds `strength` field; weak ones list-only, not in verdict |
| P11 | LLM reply carries terminal ANSI color codes | Strip before parsing JSON |
| P12 | Retrieval has length bias: a 6933-char action gets more anchor hits than a 61-char one | Now score by "number of **distinct** anchors hit"; measured, generic anchors still crowd out real evidence, so default changed to idf (see P15) |

### 5.4.1 New issues found during coding (code fixed, all with regression tests)

| # | Problem | Fix |
|---|---|---|
| P13 | **Hard criterion also needs double query**: R3.1's only action reading `agent.py` is in turn 1, but the requirement is raised in turn 3, `scope=idx_gte 22` filters it out → hard_check reports MISS. P3 recurs on hard_check; initially only retrieve was double-queried | s5 also runs scope+full-tree twice for hard_check; hits only in full tree get `before_request` marker |
| P14 | **`evidence_before_request` refs not added to citable set**: they only enter the evidence-bundle text, not `refs`; s8 sees them but can't cite them — validation treats citations as hallucination and rejects; the LLM in retry withdraws the citation, ultimately mis-judging what should be satisfied as `unverifiable` | `build_pack` also adds these refs to `refs` |
| P15 | **Generic anchors crowd real evidence out of top-K**: R3.1's anchors contain `read` (df=62)/`cat` (df=14)/`.py`; count mode ranks a heap of actions that only touch generic words above | Default `score_mode=idf`, weight by anchor rarity |
| P16 | **One bad quote taints the whole batch of claims**: a quote's punctuation differs from source, all 40 claims are rejected, overclaim detection completely broken | Only structural errors reject the whole batch; quote mismatch is now per-item discard-and-log. Comparison keeps only word characters, drops all punctuation |
| P17 | kiro-cli occasionally panics (exit 101); same prompt succeeds shortly after | `llm.ask` separates "validation failure" from "backend exception" for retry; backend exception retries twice with backoff by default |
| P18 | **s2 extraction unstable across runs**: same session, two runs, 13/14 requirements, strong+active from 10 to 7 | No auto-fix yet; this is the biggest weakness of the chain — for important evaluations, use `--requirements` with human confirmation (echoes §5.5 open item 2) |

### 5.4.2 Post-implementation measured results

Two real sessions run end-to-end, 56 unit tests all green:

| session | Shape | Result |
|---|---|---|
| `aaaa1111` | Orchestrator, single-turn / root 73 actions / full tree 261 actions / 16 child sessions | `PASS` completion 1.0 (4/4 strong satisfied), control passed. R1.3 judged via `cross_session` evidence (`--target` calls in 6+ child sessions); R1.5 judged `unverifiable` because `has_response=False` |
| `4b143da9` | Interactive, 17 turns / 104 actions / no child sessions | `PASS` completion 1.0 (7/7 strong satisfied), control passed; before fixing P13~P15 it was `FAIL`, and R3.1's `unverifiable` was exactly caused by P14 |

The before/after fix comparison validates the value of P13~P15: hard-layer hits went from 4 to 6, one misjudged `unverifiable` was corrected. The synthetic control group is correctly judged `false` on both sessions, and the LLM's reason shows it did use the trajectory vocabulary sample from s6 to distinguish "anchor wrong" from "actually not done" — the mechanism works as designed.

### 5.5 Open items (TBD)

1. `outcome`-type requirements when `response` is missing: mark `unverifiable`, or downgrade to artifact-based judgment (s8 at runtime chose downgrade itself)
2. Whether requirement extraction (s2) needs a human-confirmation step — it is the foundation of the whole direction; wrong extraction means wrong everything, and correctness cannot be auto-evaluated
3. Where do negative samples come from: both sessions are 5/5 satisfied, so verdict has not been tested against "requested but actually not done"
4. Whether `hard_check`'s `invoke_agent` should be extended to cross-session lookup of sub-agent `agent_name` — then P7's R1.3 would enter the hard layer without relying on retrieval

---

## 6. Landing order (MVP)

1. **Five evidence-layer tools** (`overview` / `query` / `retrieve` / `get` / `crosscheck`), pure Python, unit-testable, no LLM. Once done, we have a usable trace query and retrieval CLI, so even without the agent part it's not a loss. `retrieve` and `crosscheck` are the two pillars of the new plan.
2. **All nine steps of `goal_completion`'s scripted workflow.** Already run through manually on two real sessions (see 5.4); code up the manual steps.
3. **Add evidence ledger + reject findings without evidence**, plus `suspect_anchors` three-state and cross-session idx ident.
4. **Find negative samples for validation** (runs where something was requested but really not done); this is the first hard indicator of verdict credibility.
5. Then roll out `efficiency` (via `detect`'s H1~H7) and `compliance` (C1~C7), finally add `planned` mode.

---

## 7. Risks and mitigations

- **Context still accumulates.** Step-by-step forensics doesn't equal saving context; accumulating 20 tool returns still explodes. Mitigation: each step must produce "intermediate conclusion + evidence idx" and then **discard the raw returns** — this is the real reason the ledger stores only idx.
- **Cost.** Borrow AaaJ's dry-run: estimate tool calls and tokens first, then decide whether to run; over-budget downgrades to scripted, or falls back to evalkit-only.
- **Meta-evaluation from day one.** At least three: ① items evalkit can judge deterministically, eval-agent should agree with (agreement rate = sanity score); ② existing fakery-negative samples must be caught; ③ evidence idxs can be replayed and verified by a script. Without this layer we can't answer "is it itself accurate?" — the single area where AaaJ beats us (it has 365 human annotations).
