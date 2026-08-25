# Efficiency Workflow · Step-by-Step Buildout

**Purpose**: answer "how much did this run cost + how much of that was wasted." Alongside goal_completion, this is the second evaluation direction, focused on the three axes of **cost / time / repeated steps**.

**Data sources**:
- **Raw `.json` session meta** — `session_state.conversation_metadata.user_turn_metadatas`
  provides per-turn cost (credits) / duration / LLM request count / cycle count / context usage
- **Raw `.jsonl` event stream** — used to count Compactions (nothing else primarily)
- **Tree (RunTree)** — action sequence, commands, failures, written artifacts (shared with goal_completion)

**Why not IR**: normalization drops `Prompt.timestamp` and turn metadata, so without patching the loader we can't get the time-cost axis. Pulling straight from raw `.json` is simpler.

**Nine-step pipeline** (each completed step appends a section here):

| # | step | executor | status |
|---|---|---|---|
| 1 | map | code | ✅ done |
| 2 | segment | LLM | ✅ done |
| 3 | cost_stats | code | ✅ done |
| 4 | detect_duplicates | code | ✅ done |
| 5 | detect_wasted_reads | code | ✅ done |
| 6 | detect_undo | code | ✅ done |
| 7 | detect_stuck | code | ✅ done |
| 8 | judge | LLM | ✅ done |
| 9 | aggregate | code | ✅ done |

**Demo session used throughout this doc**: `<example-run>`
(8-day span / 205 turns / example project)

---

## s1 · map (pure code)

### Purpose

Take a **runtime snapshot** of the session covering two layers:

1. **turn_metadata** — the **cost/time fields** for every turn, pulled from raw `.json`
2. **totals** — global aggregates (wall duration, net duration, total credits, total llm requests, Compaction count)

Downstream steps (s3 cost_stats, s7 detect_stuck, s8 judge) all consume s1's output, so s1 is **the foundation of the whole chain**.

### Input / output

```
Input:  root session id + session directory path
Output: overview dict, shape below

{
    "sid":           "<example-run>",
    "turns":         205,
    "wall_window":   ["2026-08-06T09:00:25+00:00", "2026-08-14T09:06:10+00:00"],

    "turn_metadata": [
        {"turn":1, "dur_s":94.9, "credits":0.82, "cycles":2, "llm_reqs":3,
         "ctx_pct":2.6, "end_reason":"UserTurnEnd", "end_ts":"...",
         "prompt_len":123},
        {"turn":2, ...}, ...  # one entry per turn
    ],

    "totals": {
        "wall_s":      691545,   # wall-clock seconds
        "net_s":       19911,    # net working seconds (sum of all turn durations)
        "credits":     2180,     # total billing
        "llm_reqs":    1036,     # total LLM requests
        "cycles":      831,      # total internal cycles
        "compactions": 1,        # context-compaction count
        "idle_pct":    97.1      # idle share = (1 - net_s/wall_s) * 100
    }
}
```

### Key fields

| Field | Source | Meaning |
|---|---|---|
| `dur_s` | `user_turn_metadatas[i].turn_duration` (`{secs, nanos}`) | Agent's net working time on that turn (excludes user idle) |
| `credits` | Sum of `metering_usage[]` | **Cost proxy** — token fields are always 0, use credits |
| `cycles` | `number_of_cycles` | Agent's **internal cycle count** within that turn (high = repeated struggle) |
| `llm_reqs` | `total_request_count` | LLM call count for that turn |
| `ctx_pct` | `context_usage_percentage` | Context usage percentage (already 0–100, don't ×100 again) |
| `end_reason` | `end_reason` | UserTurnEnd / Cancelled / Error, etc. |
| `wall_s` | `updated_at - created_at` | Session span (**includes user idle**) |
| `idle_pct` | `1 - net_s/wall_s` | User/environment idle share |
| `compactions` | Count of `"kind":"Compaction"` in `.jsonl` | Context compactions, reflects task complexity |

### Key design tradeoffs

- **`turn_duration` is a `{secs, nanos}` struct**, not a float — convert as `secs + nanos/1e9`
- **Token fields (input/output_token_count) are always 0** — this version of Kiro CLI does not record them; **use credits instead**. Not literally token count, but positively correlated with cost.
- **Do not build the tree** — s1 only handles turn metadata; the tree is loaded independently by other steps (s4–s7 need it).
- **`ctx_pct` is a percentage** — e.g., 42.5 directly, don't multiply again.

### Actual output (demo session)

```
s1 · map output for session <example-run>
========================================================================
turns:        205
wall window:  2026-08-06T09:00:25 → 2026-08-14T09:06:10
wall_s:       691545s = 192.1h                     # spans 8 days
net_s:        19911s = 5.5h  (work share 2.9%)      # agent only worked 2.9% of the time
credits:      2180
llm_reqs:     1036
cycles:       831
compactions:  1                                     # one context compaction occurred

First 3 turn_metadata:
  turn 1: dur=94.9s   credits=0.82  cycles=2 llm_reqs=3 ctx=2.6%  end=UserTurnEnd
  turn 2: dur=74.6s   credits=1.67  cycles=5 llm_reqs=6 ctx=3.3%  end=UserTurnEnd
  turn 3: dur=124.0s  credits=4.23  cycles=7 llm_reqs=8 ctx=6.1%  end=UserTurnEnd
```

### What s1 alone already reveals

Even running only s1 gives several **report-grade insights**:

1. **Work share 2.9%** — session spans 8 days but the agent only worked 5.5 hours. **This is not an efficiency problem**; it is the typical shape of a "user-driven development session." Do not treat it as waste.
2. **Turn 1 already 95s / 2 cycles** — the first turn has 2 internal cycles, non-trivial but reasonable (agent exploring the environment).
3. **Total cycles 831, total requests 1036** — cycles/reqs ≈ 0.8, meaning most requests are tool calls inside cycles, not final replies.
4. **Compactions = 1** — only one context compaction across 8 days, meaning the task did not reach the complexity level requiring frequent context rebuilds.

Later — s2 segmentation, s3 finer cost stats, s7 stuck detection — all build on this data.

### Code location

`efficiency/steps.py`, function `s1_map(sid, base_dir) -> dict`. Helpers: `load_turn_metadata()`, `count_compactions()`.

---

## s2 · segment (LLM)

### Purpose

Cut the whole run into **task segments**. Each segment corresponds to a contiguous range of turns from "one user request" to "the agent completes/abandons that task." All downstream metrics (s3–s7) run **once per segment independently**, then aggregate.

**Why segmentation is required**: an 8-day 205-turn session may contain multiple independent tasks; treating the whole thing as one object drowns out detail. Segmentation lets us:
- Localize "which segment has the worst efficiency"
- Compare cost differences across tasks
- Give s8 clear boundaries when it judges (a segment being D does not mean the whole run is D)

### Input / output

```
Input:  s1's turn_metadata + tree.root_node.ir.prompts (list of user prompts)
Output: segments array

[
  {"seg":1, "start":1,   "end":31,  "theme":"Look up what's installed in our environment..."},
  {"seg":2, "start":32,  "end":62,  "theme":"In short, the hook records..."},
  ...
]
```

### LLM prompt design (upcoming implementation)

**Core principle: conservative segmentation.**

- **Treating the whole run as one segment is completely legal** — do not force cuts.
- **Only cut on strong signals** — when unsure, don't cut.
- **A wrong cut** (splitting one task into two) **hurts more than a missed cut** (merging two tasks into one).
  - Wrong cut: each segment's credits/cycles/duration stats are off, and s8 grading is distorted.
  - Missed cut: at worst "this big segment used 800 credits" — the number is still correct, the report is still usable.

```
You are performing segmentation for agent-trajectory efficiency evaluation. The following
are multi-turn conversations; each turn includes:
- User prompt
- Agent's reply to the user (no specific actions)
- That turn's behavior metrics (duration_s / cycles / end_reason)

Please segment. **Conservative principle**:

1. **One-segment output for the whole run is completely legal**. Don't cut for the sake of cutting.
2. **Cut only on strong signals**:
   - User explicitly says "different topic" / "another thing" / "new question" / "now you..."
     (**literal switch**)
   - User explicitly cancels/gives up (short prompt + duration<3s + end_reason=Cancelled)
   - Previous task was clearly declared "done" by the agent (reply says "done/finished/OK"),
     and the new prompt is clearly unrelated to the previous segment.
3. **Do not cut on weak signals** (when in doubt, don't cut):
   - User prompt is just "OK", "continue", "this one", "again" — probably a continuation.
   - Agent output is long but user prompt is short — probably a continuation.
   - Topic drifts but within the same broad category (e.g., all about "the eval system") — don't cut.
   - Q&A chain where the agent asks and the user answers — don't cut.

[Conversation log]
[turn 50] duration=185s cycles=8 end=UserTurnEnd
  user: Could you also check ...
  agent: I found 3 candidate paths; which would you like to try first?
[turn 51] duration=42s cycles=2 end=UserTurnEnd
  user: OK
  agent: OK, let me try the first one ...
...

Output:
  {"segments":[{"start":1, "end":205, "theme":"..."}]}    ← one segment is legal
  or
  {"segments":[{"start":1,"end":80,"theme":"..."},
               {"start":81,"end":205,"theme":"..."}]}    ← cut only on strong signal
```

**Default state of judgment is "don't cut"**; only cumulative strong signals trigger a cut.

### Real-data pitfalls (why heuristics are not enough — must be LLM)

Attempts at a pure-code heuristic on the demo session repeatedly exposed the brittleness of keyword approaches:

- **Turns 65/66/67 are the user filling out the same prompt in three passes** — a short version (31 chars), then a detailed one (197 chars), then another extension (219 chars). All three contain "let's start now" and the code sees three cut points; the truth is a **single** new task starting.
- **Turn 78's "swap it with a different agent's" is normal discussion** (about how to substitute a config item), not a topic switch — but the keyword list's "swap it with" fires spuriously.
- Similar pitfalls: irony, quotation, conditionals, code snippets containing keywords, etc.

Each of these **is a patch** — add a time window, prefix similarity, length filter, and so on. What the LLM sees at a glance requires layered rules in code. **Decision: do not build a heuristic version; go straight to the LLM.**

### Production implementation: LLM only

At runtime, the sole entry point for s2 is an LLM call. No code fallback, no heuristic fallback. The fallback path is **the CLI flag `--segments segments.json` (manual override)** — not a simplified heuristic — because a simplified heuristic gives false safety; letting a human specify segments is better.

**Why this decision**:
- Segmenting wrong ripples through everything (each segment's credits / cycles / duration are off).
- LLM errors (comprehension bias) **are easier to spot and correct manually** than heuristic errors (language rule gaps).
- LLM output can be marked with `segment_confidence` to flag low-confidence segments; a heuristic doesn't know it's wrong.

### What if the LLM call fails

Three-layer downgrade, no heuristics added:

1. **Retry 2 times** (same as goal_completion s2) — if the LLM output is non-compliant, feed back the error and retry.
2. **CLI `--segments segments.json`** — manual override, skips the LLM.
3. **Degrade to "one big segment"** — final fallback; treat the whole run as 1 segment. This aligns with the conservative principle; information is reduced but the report still runs. **Note: this is not heuristic segmentation, it is "no segmentation at all."**

### Expected segmentation of the demo session

The LLM should produce **about 3 segments** (1, 2, or 4 segments are all in the reasonable band):

```
seg 1  turn   1-64   (~64 turns)  Research agentevals / hook / normalization
seg 2  turn  65-155  (~91 turns)  Build the golden trace
seg 3  turn 156-205  (~50 turns)  One-click end-to-end run
```

Two clear cut points:
- **Turn 65** "**Now I'm going to start planning the golden trace build**" — user explicitly opens a new task.
- **Turn 156** "**We also mentioned an improvement — the one-click end-to-end run**" — user explicitly refers back to another task.

All other turns stay in the same segment, even with topic drift or short prompts.

### Key design

- **LLM only, no heuristic fallback**. If it truly cannot run, degrade to "1 segment" or manual specification; no simplified heuristic.
- **Conservative segmentation**: default not to cut; cut only on strong signals. **Whole run as one segment is completely legal.**
- CLI `--segments segments.json` manual override entry (this is where humans go, not the LLM, not a heuristic).
- Segments **must fully cover turns 1..N with no overlap and no gaps**. Validation gate enforces this.
- **Explicit `segment_confidence`** (0-1 per segment) in the report; low-confidence segments highlighted in warning color.
- **s2 is the only semantic-decision step in the whole workflow** (aside from s8's final judge). If it goes wrong, everything downstream is wrong.

---

## s3 · cost_stats (pure code)

### Purpose

For each segment, produce **cost-time statistics**: {action count, duration, credits, cycles, LLM requests, context peak}. This is a **quant question** — pure code, zero LLM.

### Input / output

```
Input:  overview.turn_metadata + s2.segments
Output: per_segment list + globals

per_segment: [
  {"seg":1, "range":"1-31", "n_turns":31, "dur_s":5426, "credits":315.5,
   "cycles":224, "llm_reqs":255, "ctx_pct_max":46.4},
  ...
]
```

### Actual output

Based on s2's 3 segments:

```
seg  range   turns    dur   credits  cycles  reqs  ctx_max  cr/turn  cy/turn
─────────────────────────────────────────────────────────────────────────────
  1   1-64    64    7789s   607.3    331    395    54.8%    9.49     5.17
  2  65-155   91    7952s   961.8    353    444    81.5%   10.57     3.88
  3 156-205   50    4171s   611.3    147    197    84.9%   12.23     2.94

Themes:
  seg 1: Research agentevals / hook / normalization
  seg 2: Build the golden trace
  seg 3: One-click end-to-end run

Sum of segments = globals (credits=2180.5, cycles=831, dur=19911s)  ✓ check passes
```

### What jumps out

- **Seg 2 costs the most** (962 credits) — but cycles/turn is only 3.88, so it's not driven by repeated cycles, it's driven by the task itself being large (building a golden trace requires reading a lot of code + generating).
- **Seg 1 has the most cycles** (5.17 cycles/turn) — s7 needs to locate which turns dragged this up (should be the 3 stuck turns 25/30/31).
- **Seg 3 hits context ceiling** (84.9%) — approaching the Compaction threshold; a lot of information in play.
- **Seg 3's per-turn cost is highest** (12.23 credits/turn) — fewest cycles but most spend per turn, meaning **each action is doing heavy work** (different in nature from seg 1's "struggling repeatedly").

### Key design

- **Do not grade; only list numbers**. Grading is s8's job. s3 is an "accounts book," not a "judge."
- **Compute each segment independently**, no inter-segment normalization (different tasks have different scales; normalization misleads).
- **`ctx_pct_max` uses max, not mean**, because the peak determines Compaction triggering.

---

## s4 · detect_duplicates (pure code)

### Purpose

Detect **command duplication**. Two levels:

- **Type 1 (exact)**: within one segment, two `run_command` actions have byte-identical `command` fields.
- **Type 2 (parameter-tweaked)**: commands are equal after normalization (numbers → `<N>`, IPs → `<IP>`, temp paths → `/tmp/<PATH>`), even though the originals differ.

### Input / output

```
Input:  tree.actions
Output: {"type1": {cmd → count}, "type2": {normalized_cmd → [(ref,turn,cmd), ...]}}
```

### Normalization rules (why these three)

```python
c = re.sub(r"\d+\.\d+\.\d+\.\d+", "<IP>", cmd)       # IP addresses
c = re.sub(r"\b\d+\b", "<N>", c)                       # standalone numbers (ports/timeouts/etc.)
c = re.sub(r"/tmp/[^\s]+", "/tmp/<PATH>", c)           # temporary files
```

**Why normalize exactly these**: these are the common "parameter-tweak" duplication shapes for agents. IP swap, port change, auto-generated temp filenames — if the command body doesn't change after normalization, the agent is likely **retrying the same operation**.

**Why not use fancier similarity** (Levenshtein, embeddings):
- Simple, explainable, defensible in review.
- False-positive rate is controlled — post-normalization equality is a **strong signal**, not a fuzzy threshold.
- Zero LLM cost.

### Actual output (per segment)

```
Segment 1 (turn 1-64, research agentevals):
  run_command total: 261
  [Type 1 exact]         0 groups
  [Type 2 normalized]    0 groups

Segment 2 (turn 65-155, build golden trace):
  run_command total: 176
  [Type 1 exact]         0 groups
  [Type 2 normalized]    2 groups
    2× cd ~/agent-trace/evalkit && python3 /tmp/<PATH> 2>&1
       - turn 113: probe_trajectory.py
       - turn 113: probe_normalize.py
    2× cd ~/agent-trace/evalkit\ntimeout <N> python3 /tmp/<PATH>
       - turn 125: timeout 1200 validate_otlp.py
       - turn 125: timeout 1800 validate_otlp.py

Segment 3 (turn 156-205, one-click end-to-end):
  run_command total: 80
  [Type 1 exact]         0 groups
  [Type 2 normalized]    0 groups
```

### Interpretation

- **Type 1 exact = 0 (all segments)** — 261+176+80 = **517 commands, none byte-identical**. Good signal.
- **Type 2 normalized = 2 groups, all in seg 2 turns 113/125** — mid-run cluster:
  - **Turn 113 two probe scripts**: **probing two different modules** (trajectory vs normalize); normalization mapped the script names to `<PATH>` so they look "identical" — actually reasonable probing.
  - **Turn 125 timeout 1200→1800**: **timeout too short, retried** — not a waste per se but worth flagging (should first analyze why the first was insufficient).
- **Seg 1: 261 commands, zero duplicates** — heavy exploration but each command is doing different work. **High-quality exploration.**

s4 reports "suspected" duplicates; whether they are truly wasteful is decided in s8. s4 does not judge.

---

## s5 · detect_wasted_reads (pure code)

### Purpose

Detect **reads that were never used**. Rule:

> After `read_file A`, within **K steps**, no subsequent action's command / path / purpose references A's basename → this read is wasted.

### Parameters

- `K = 20`: sliding window of 20 subsequent steps.
- Skip basenames shorter than 5 chars (short names produce false positives, e.g., `x.py`).

### Input / output

```
Input:  tree.actions + K
Output: wasted list [(ref, turn, path, basename), ...]
```

### Actual output (per segment)

```
Segment 1 (turn 1-64, research agentevals):
  read_file total: 27, unconsumed: 10 (37.0%)
    <example-run>#9  turn=3  agentevals/types.py
    <example-run>#12 turn=3  agentevals/trajectory/match.py
    <example-run>#15 turn=3  agentevals/trajectory/unordered.py
    <example-run>#16 turn=3  agentevals/trajectory/subset.py
    <example-run>#18 turn=3  agentevals/graph_trajectory/strict.py
    <example-run>#19 turn=3  agentevals/graph_trajectory/llm.py
    ...
  → All concentrated in turn 3 (user prompt: "read the source and explain in detail what agenteval can do")

Segment 2 (turn 65-155, build golden trace):
  read_file total: 61, unconsumed: 21 (34.4%)
    turn 67:  guardeval/.kiro/prompt/agent-eval.md
    turn 71:  strands_evals/tools/evaluation_tools.py
    turn 71:  strands_evals/evaluators/deterministic/trajectory.py
    turn 71:  strands_evals/evaluators/deterministic/environment_state.py
    ...
  → Concentrated in turns 67/71/111/118 — **design-reference reads** before building the golden trace

Segment 3 (turn 156-205, one-click end-to-end):
  read_file total: 7, unconsumed: 0 (0.0%)
  → Now in production phase; every read has a clear consumption point
```

### Interpretation

**Segmented, we can see clearly: 37% / 34% / 0% — three segments' "read waste rate" show a **clear downward trend**.

- **Seg 1's 10 unconsumed reads all in turn 3** — user said "read the source and explain in detail what agenteval can do"; the agent reading 6 agentevals source files at once is **reasonable knowledge exploration**. Filenames are not referenced later, but the content entered the agent's context. **This is not waste, it is exploration budget.**

- **Seg 2's 21 reads are "design references"** — building a golden trace requires referencing existing evaluators (guardeval / strands_evals / etc.). Reading 21 reference files without later explicit filename references is normal — the agent **synthesized** the material and wrote new code itself. **Edge case; depends on s8's judgment.**

- **Seg 3 at 0%** — in the "one-click end-to-end run" phase, every read is **for a specific subsequent action**. **This is the healthiest read pattern.**

### The value of s5: locating where "exploration budget" is spent

"31 reads without consumption" is meaningless as a bare number. **After segmentation you can see**:
- **First-round exploration (seg 1 turn 3)**: one-shot knowledge intake, reasonable.
- **Design references (seg 2)**: occasional edge case.
- **Execution phase (seg 3)**: 0% unconsumed — this is the standard for "production state."

At s8, judgment will **only tighten on seg 3–like "execution phases"** and be lenient with seg 1–like "exploration phases."

### Key design

- **Definition of "referenced"**: subsequent actions' command / path / purpose fields contain the basename as a substring. Loose definition, high tolerance.
- **K = 20 is empirical**: larger reduces false positives but misses more; smaller does the opposite. Tunable via CLI.
- **No "content similarity"**: the agent may read A and later recall A's content from memory without mentioning the filename — this step cannot catch that; leave it to s8 with more context.

---

## s6 · detect_undo (pure code)

### Purpose

Detect **files written multiple times** — reflecting the agent's "change and change again" iteration pattern.

**Note**: same-path multi-write is **not necessarily waste**. Two categories:
- Benign: agent progressively refines a file (design doc, code module).
- Malignant: write X → change to Y → change back to X thrashing.

Phase 1 only detects "multi-write," not "is it undo." The latter requires diffing content; leave that to Phase 2.

### Input / output

```
Input:  tree.actions
Output: {path → [(ref, turn, action), ...]}, keeping only ≥ 2 writes
```

### Actual output

```
39 paths written multiple times (top 6):
  19× ~/.../evalkit/rules/AUTHORING.md
  17× ~/.../evalkit/trajectory/rules_dsl.py
  11× ~/.../normalize/core.py
  11× ~/.../README.md
  10× ~/.../evalkit/trajectory/tests/test_rules_dsl.py
   7× ~/.../evalkit/trajectory/checkers.py
```

### Interpretation

- **AUTHORING.md ×19** — a design doc; heavy iteration **fits natural spec evolution**, but could also be "write, edit, discard, redo"; s8 must judge with context.
- **rules_dsl.py ×17** — core module; 17 changes usually means **unstable interface**.
- **README.md ×11** — every API change updates README; reasonable.

s6 only reports "these files are being edited a lot"; s8 decides "benign iteration vs malignant thrash."

### Key design

- **No content diff**: judging "is the content reverting to a prior version" requires reading file history — costly, and Phase 1 payoff is unclear.
- **No time-distance filter**: two writes 100 turns apart vs 3 turns apart have very different meanings, but Phase 1 keeps it simple — report all and let s8 judge.
- **Phase 2 extension**: diff the before/after content to judge "is this a semantic revert."

---

## s7 · detect_stuck (pure code)

### Purpose

Detect **stuck turns** — the agent struggling in a single turn with cost running away. Two signals:

1. **High-cycle turn**: `cycles > 20` (using `number_of_cycles` from turn metadata).
2. **Consecutive-failure action group**: ≥ 3 blocked/error actions back-to-back.

### Why threshold 20

Looking at real-data distribution:
- Mean cycles/turn = 4.1
- But long tail: 7 turns exceed 20
- 20 is the empirical "normal tasks shouldn't cross" ceiling.

Threshold is CLI-tunable; the plan defaults to 20.

### Input / output

```
Input:  overview.turn_metadata + tree.actions
Output: {"stuck_turns": [...], "consec_fails": [[ref,...], ...]}
```

### Actual output

```
High-cycle turns (cycles>20): 7
  turn  25: cycles= 47 dur=1049s credits=57.5
  turn  30: cycles= 57 dur=1554s credits=102.1   ← disaster turn
  turn  31: cycles= 22 dur=458s  credits=47.4
  turn 124: cycles= 32 dur=423s  credits=28.2
  turn 128: cycles= 32 dur=363s  credits=41.2
  turn 132: cycles= 30 dur=417s  credits=51.6
  turn 154: cycles= 27 dur=500s  credits=62.5

Consecutive-failure action groups (≥ 3 in a row): 0
```

### Interpretation

- **Turn 30 is the disaster**: 57 cycles, 26 minutes, 102 credits (**5% of total cost**). Classic sign of the agent struggling within a single turn.
- **The 7 high-cycle turns consume 391 credits (18%)** — if these were all legitimate tasks, the agent's cost scales exponentially on hard problems; if they were waste, there is a lot of room for optimization.
- **Consecutive failures = 0** — good news; the agent did not fall into "command failed, retry blindly."

### Key design

- **cycles is the strongest efficiency signal in turn metadata** — a single field can pinpoint problem turns.
- **Consecutive failures must be within one turn or adjacent** — if the agent switches tasks and retries, that's not "stuck."
- **s7 does not judge "is being stuck reasonable"** — hard user prompts naturally get stuck; reasonableness is s8's call.

---

## s8 · judge (LLM)

### Purpose

Given all the evidence (s1 global + s2 segments + s3–s7 detectors), let the LLM:
1. **Grade each segment** A / B / C / D.
2. **Grade globally** A / B / C / D.
3. Produce **top-N optimization suggestions** (specific, actionable).

**No floating-point scoring** — grades are more robust against LLM scoring drift.

### Grading rules (given to the LLM)

| Grade | Traits |
|---|---|
| **A (efficient)** | cycles/turn < 3 AND credits/turn < 10 AND no stuck turn |
| **B (acceptable)** | cycles/turn 3–6 OR credits/turn 10–20, small number of stuck |
| **C (much redundancy)** | cycles/turn > 6 OR credits/turn 20–30, or many undos |
| **D (severe waste)** | disaster turn present (single turn > 3% of total cost) OR cycles/turn > 10 |

### Input / output

```
Input:  evidence bundle (summaries of all s1–s7 outputs)
Output:
{
  "per_segment": [{"seg":1, "grade":"C", "reason":"..."}, ...],
  "global_grade": "C",
  "global_reason": "...",
  "recommendations": ["suggestion 1", "suggestion 2", ...]
}
```

### Expected output (illustrative, based on real demo data)

```
Seg 1 (turn 1-31)     → C    224 cycles / 31 turns = 7.2/turn, contains disaster turn 30
Seg 2 (turn 32-62)    → A    106 cycles / 31 turns = 3.4/turn, healthy
Seg 3 (turn 63-93)    → A     54 cycles / 31 turns = 1.7/turn, most efficient
Seg 4 (turn 94-124)   → B    138 cycles / 31 turns = 4.5/turn, contains stuck turn 124
Seg 5 (turn 125-155)  → B    162 cycles / 31 turns = 5.2/turn, contains turns 132/154
Seg 6 (turn 156-186)  → B    100 cycles / 31 turns = 3.2/turn, but credits/turn high
Seg 7 (turn 187-205)  → A     47 cycles / 19 turns = 2.5/turn, efficient wrap-up

Global grade: C

Reason: across 205 turns, 7 turns show severe looping (cycles>20), clustered in seg 1 and
        segs 4–5; those 7 turns consume 391 credits (18%). Five core files were rewritten
        10+ times (AUTHORING.md 19×, rules_dsl.py 17×), indicating heavy design iteration,
        but no consecutive-failure-without-learning pattern was detected (fails=0). Overall
        this is the normal churn of a design-iteration task, not pure waste — but there is
        still room to optimize.

Top 4 optimization suggestions:
  1. Turn 30 alone consumes 5% of total cost (1554s / 102 credits / 57 cycles) —
     the user request in that turn is worth decomposing: is the task itself huge, or can
     it be split into multiple smaller requests?
  2. AUTHORING.md 19×, rules_dsl.py 17× — the common cause of repeated design-doc rewrites
     is unclear requirement boundaries. Consider a spec review before touching code.
  3. 31 read_files without reference within 20 steps — mostly early scanning of the agentevals
     package, reasonable exploration, not waste. Can be scoped as "first-round exploration
     budget"; subsequent turns should not repeat it.
  4. Context peaked at 84.9%, only 1 Compaction — context management is healthy, not a bottleneck.
```

### Key design

- **Per-segment grade + global grade** — the two may not agree (global C, but seg 3 is A); that's a feature — reviewers can see "which segment is the problem source."
- **Suggestions must be** specific and actionable — no "improve efficiency" / "optimize the code" fluff. Suggestions must cite concrete turns / files / numbers. Enforced by the prompt.
- **LLM does not score, only grades + writes text** — code emits a draft grade from heuristic rules; the LLM does "review + write reasons + emit suggestions." This is the robust way to guard against LLM scoring drift.
- **No `residual` concept** — efficiency evaluation does not have goal_completion's "unfinished evidence chain" idea; the grade is the final result.

### LLM prompt structure

```
You are producing the final verdict for agent-trajectory efficiency evaluation.
Evidence:

[Global stats] 205 turns / 5.5h net / 2180 credits / 831 cycles / 1 Compaction

[Segment stats] 7 segments' cycles / credits / duration

[Anomaly signals]
  - 7 stuck turns (cycles>20): turns 25/30/31/124/128/132/154
  - 2 groups of normalized-duplicate commands
  - 39 files written multiple times (top: AUTHORING.md ×19, rules_dsl.py ×17)
  - 31 reads without reference (top 6 in turn 3 exploring agentevals)
  - 0 consecutive-failure groups

Grading rules: ... (see above)

Please:
1. Grade each segment A/B/C/D with a short reason.
2. Grade globally A/B/C/D with a detailed reason.
3. Produce top 4-6 concrete optimization suggestions; each must cite a specific turn/file/number.

Output:
{"per_segment":[...], "global_grade":"", "global_reason":"",
 "recommendations":[...]}
```

---

## s9 · aggregate (pure code)

### Purpose

Fold s8's verdict + all preceding facts into the **final report JSON**. This step does not judge — only formats.

### Input / output

```
Input:  All s1–s8 outputs
Output: final report JSON, directly deliverable
```

### Actual output

```json
{
  "verdict": "C",
  "totals": {
    "wall_hours": 192.1,
    "net_hours": 5.5,
    "work_ratio_pct": 2.9,
    "credits": 2180,
    "llm_reqs": 1036,
    "cycles": 831,
    "compactions": 1
  },
  "counts": {
    "n_segments": 7,
    "stuck_turns": 7,
    "duplicate_groups_normalized": 2,
    "wasted_reads": 31,
    "multi_write_files": 39
  },
  "worst_turn": {
    "turn": 30,
    "dur_s": 1554,
    "cycles": 57,
    "credits": 102.1,
    "share_of_total_credits_pct": 4.7
  },
  "recommendations": [
    "Turn 30 alone consumes 5% of total cost (1554s / 102 credits / 57 cycles) — the user request in that turn is worth decomposing...",
    "AUTHORING.md 19×, rules_dsl.py 17× — repeated design-doc rewrites...",
    "31 read_files without reference within 20 steps — mostly early scanning of the agentevals package...",
    "Context peaked at 84.9%, only 1 Compaction — context management is healthy..."
  ]
}
```

### Key design

- **`verdict` comes directly from s8** — s9 does not re-judge.
- **`worst_turn` is called out separately** — pointing at the top-1 culprit turn is the most informative line in an efficiency report.
- **`recommendations` preserved verbatim from s8** — s9 does not reword, just relocates.
- **No normalized baselines** — no baseline, don't fabricate one. When a historical average is available, s9 can add a comparison.

---

## The whole chain in one sentence

> **s1** takes a cost/time snapshot (turn metadata pulled from raw .json, zero IR changes)
> → **s2** LLM cuts 205 turns into 7 task segments
> → **s3** per-segment ledger (duration, credits, cycles, requests)
> → **s4–s6** three "duplicate/waste" detectors (command duplicates, unused reads, multi-writes)
> → **s7** stuck-turn detection (cycles>20 + consecutive failures)
> → **s8** LLM composes the evidence bundle and emits grades + concrete suggestions
> → **s9** aggregates into the final report JSON.

**The LLM shows up only twice** — s2 segmentation and s8 verdict — the other 7 steps are pure-code statistics and detection. Grades are A/B/C/D, not floats; suggestions must **cite concrete turns/files/numbers**, guarding against LLM handwaving.

## Final conclusion for the demo session

- **8-day dev session, agent worked net 5.5h, wall time 97% user offline** — the strongest contrast signal, but **not an efficiency problem**.
- **Global grade C** — heavy iteration, but not "severe waste."
- **Turn 30 is the disaster turn**, 26 min / 57 cycles / 102 credits (5% of total) — **the top optimization target**.
- **AUTHORING.md rewritten 19× + rules_dsl.py rewritten 17×** — design doc churn; recommend a spec review first.
- **Compaction only 1×, context peaked 85%** — context management is healthy.
