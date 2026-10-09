"""LLM prompt templates for the efficiency workflow."""


NARRATIVE_PROMPT = """You are writing the opening of an efficiency audit report
that a human will read. Below is an OBJECTIVE facts pack -- every number and
identifier in it is exact. Do NOT invent numbers, refs, turn ids, file names,
or percentages that are not in the pack. Do NOT include grade / recommendation /
fix advice -- that comes later in the report from a different step.

Write TWO paragraphs in the SAME language as the user's prompts in this trace
(zh-CN in this case unless facts suggest otherwise):

1. overview: total turns, wall / net wall-clock, credits, cycles, compactions,
   agent name if given, idle_pct if given. Prose, not bullets. ~120-250 chars.
2. hotspots: which turns dominated cost AND WHAT each of those turns was
   trying to do. Cite the top-2 or top-3 turns by credits or cycles (turn
   number, credits, cycles, duration, share of total), AND for each, name
   the purpose from the `user_prompt` field in the facts pack -- rephrase
   it in natural language, don't just quote it verbatim (e.g. "turn 3
   (checking STP topology) took 668s..."). If the facts pack mentions duplicate
   clusters / multi-write files / stuck turns / wasted reads, mention
   which segment they concentrate in. Do not editorialize about causes
   beyond what the numbers allow. ~250-600 chars.

Facts pack:

{facts}

Output exactly one JSON code block:

```json
{{"overview": "<paragraph 1>", "hotspots": "<paragraph 2>"}}
```

Hard constraints:
  - Both fields non-empty.
  - "overview" between 80 and 400 chars.
  - "hotspots" between 250 and 900 chars, MUST contain at least three digits
    (turn numbers and cost numbers).
  - No mention of grade letters (A/B/C/D), no fix advice ("should", "could",
    "consider"), no suspicious/culprit judgments -- that belongs to s8.
"""


SEGMENT_PROMPT = """You are segmenting an agent execution trace for efficiency evaluation.

Below is a multi-turn dialogue. Each turn contains:
- the user prompt for that turn
- the agent's final reply to the user (may be empty)
- runtime metrics for the turn (duration_s / cycles / end_reason)

Produce a segmentation. **Conservative principle**:

1. **Returning the entire run as a single segment is a valid output.** Do not
   split just to split.
2. **Split only on strong signals**:
   - User explicitly switches topic: "let's move on", "another thing",
     "now, ...", "new question", "switch topic", or any explicit
     topic-change phrase in the session's own language.
   - User explicitly cancels/abandons (short prompt + duration<3s +
     end_reason=Cancelled).
   - Agent has clearly declared the prior task complete in its reply
     ("done", "finished", "OK", or equivalents) AND the new user prompt is
     unrelated to the prior segment.
3. **Weak signals do NOT justify a split** (default to "don't split"):
   - Short acknowledgements like "ok", "continue", "this", "again".
   - Long agent output followed by a short user prompt (usually continuation).
   - Topic drift within the same broad theme (e.g., all discussing the same
     evaluation system) — keep in one segment.
   - Agent asks a question and user answers — that is a continuation chain.

Default state: DO NOT SPLIT. Only split when strong signals accumulate.

--- Dialogue ---
{block}

Output exactly one JSON code block:

```json
{{"segments":[
    {{"start": 1, "end": 205, "theme": "short description, <=60 chars"}}
]}}
```

Hard constraints:
- Segments must be contiguous (no gaps, no overlaps), covering turns 1..{n_turns}.
- Each segment: start <= end.
- At least 1 segment (a single all-encompassing segment is fine).
- theme must be non-empty and <= 60 characters.
"""


JUDGE_PROMPT = """You are an INVESTIGATOR auditing an agent execution trace for efficiency.

Your role is NOT to rubber-stamp the summary you are given. It is to:
  1. Read the evidence pack to spot anomalies (clusters, pairs, wasted
     reads, high-cycle turns, thrashed files).
  2. INVESTIGATE at least the anomalies whose severity is uncertain from
     the pack alone -- pull full command text with `get_action`, list a
     high-credit turn with `list_turn`, read a churned file with
     `read_file`, run a targeted `search_actions` to see whether a pattern
     is really pervasive. The pack shows the top few pairs/clusters
     truncated to 300 chars -- if the truncation hides the decisive
     signal, actively fetch it.
  3. Only after investigating do you emit the final grade. A grade written
     without any tool calls IS acceptable ONLY when the summary alone is
     conclusive (grade A with no flagged clusters, or single-metric-driven
     D like turn_credits_share > 20%). Any B/C grade in a segment with
     duplicate clusters or multi-write files SHOULD trigger at least one
     tool call before you conclude.

You have {budget} tool calls in total. Typical investigation budget spends:
  - 1-2 `list_turn` for the highest-credit turns (understand WHAT was
    happening)
  - 1-2 `get_action` for the largest cluster's members (was it real
    duplication or refinement?)
  - 0-1 `read_file` for the file with the highest multi-write density
    (churn vs planned iteration?)
  - 0-1 `search_actions` if you suspect a pattern the pack missed
Reserve 1 call for surprises.

Grading rubric (evidence-based, NOT a formula on cycles/turn or credits/turn):

Do NOT grade off aggregate metrics like `cycles/turn` or `credits/turn` --
those are noisy proxies. Grade off the concrete evidence you can see and
verify. The four evidence axes that carry weight:

  E1  Duplicate action clusters and pairs (s4)
      -- count of clusters with size >= 3
      -- count of high-similarity pairs (command_strong >= 0.9)
      -- whether the duplication is real (same intent + same output) vs
         parametric refinement (same scaffold, different args) -- confirm
         via get_action / read_file before scoring.

  E2  Same-file multi-write / churn (s6)
      -- files written multiple times in a single turn (density >= 3
         within one turn is a strong D signal)
      -- files rewritten 5+ times across turns (span-normalized density)

  E3  Stuck turns and consecutive failures (s7)
      -- turns with high cycles that made no forward progress
      -- consecutive_failures groups indicating retry loops

  E4  Cost concentration
      -- any single turn taking > 15% of total credits: strong signal
      -- any single turn > 25%: even stronger, likely D unless investigation
         shows it was a legitimate multi-file feature landing
      -- cumulative share of top-3 turns (informational)

Grade rules (holistic, allow judgment calls after tool investigation):

  A  Zero or negligible evidence on all four axes. No clusters of size >= 3,
     no multi-write thrashing, no stuck turns, no single-turn > 10%. Or the
     evidence exists but investigation shows it is entirely legitimate
     (e.g. exploratory reads at session start).

  B  Some evidence but each item, on inspection, is defensible. Small
     clusters that are parametric refinement, one multi-write file that
     is orderly iteration, one hot turn between 10-15% that landed real
     work. Nothing points to structural waste.

  C  Multiple evidence items each of which is at least mildly suspicious,
     or one strong item plus supporting minor items. Examples: a size-5+
     cluster whose members show real duplication, OR a file thrashed 4+
     times within a single turn, OR a hot turn 15-25% coupled with a
     duplicate cluster in the same turn.

  D  Severe waste that a diligent operator would call out immediately.
     Any single-turn >= 25% AND investigation shows retry/thrash;
     OR multi-write density >= 4 within a single turn without a good
     reason on inspection;
     OR unresolved stuck-turn group with consecutive failures.

Judgment notes:
  - Exploratory reads (early in a session, learning a codebase) are healthy;
    high wasted_reads% at the start does not count against grade.
  - Multiple writes to the same design doc (e.g. AUTHORING.md 19x) may be
    healthy iteration OR churn -- decide from context by actually looking
    at the file's before/after content or the surrounding turns.
  - stuck turns concentrated in one region signal being blocked; stuck turns
    scattered evenly can be normal for hard problems.
  - When in doubt between B and C, investigate more; when in doubt between
    C and D, ALSO investigate more (the D grade should be well-supported
    because it reads as a strong claim to the user).

Evidence pack:

{pack}

Note: the [2. Per-segment cost] table lists each segment's human-readable
`theme`. When writing `reason` and `suspicious_ops`, mention the theme
(e.g. "moshell UE probe" segment) rather than only the numeric seg id --
the seg id alone is not meaningful to end-user readers.

============================================================
TOOL PROTOCOL (read carefully; you are inside a tool-use loop)
============================================================

The evidence pack above is a SUMMARY. Fields like command text are truncated
to ~300 chars, only the top pairs/clusters/wasted-reads are listed, and files
that get written multiple times are not shown by content. TREAT THE PACK AS A
STARTING POINT, NOT AS THE FINAL EVIDENCE.

Before writing `suspicious_ops` for a segment, ask yourself:
  - Is the truncated command in the pack enough to know what actually
    happened? If it ends with `…`, probably not -- `get_action(ref)` first.
  - Does this cluster look like duplication or like an intentional retry
    with different parameters? Fetch two members and compare.
  - Is the highest-credit turn in this segment already justified by the
    turn table, or is there hidden waste inside it? `list_turn(N)` shows
    every action so you can spot the actual culprit.

Emit a `tool_call` when you would need the extra info to write a good
`suspicious_ops` item. Emit the `final` only when you can back each item
with evidence you have actually inspected. A judgment with no tool calls on
a run that has multiple duplicate clusters or multi-write files WILL be
flagged as under-investigated.

You have {budget} tool calls in total. Plan them. If evidence is still
insufficient after {budget} calls, output the final judgment with your best
inference; do not stall.

Available tools (each call: emit exactly ONE JSON code block containing
either "tool_call" OR "final", never both):

1. get_action(ref)
   Full record for a single action ref (e.g. "aaaa1111#243"). Use when a
   duplicate cluster / pair / wasted-read looks suspicious and the truncated
   preview isn't enough to tell.
   Args: {{"ref": "<sid_prefix>#<idx>"}}

2. list_turn(turn)
   All root-session actions in a given turn (turn=1..N). Use to diagnose
   stuck/high-credit turns: what did the agent actually do in turn 31 that
   burned 60 credits?
   Args: {{"turn": 31}}

3. search_actions(query, k=8, turn_gte=None, turn_lte=None)
   Substring search across serialized action text (command + purpose +
   reasoning). Anchors are lowercased and OR'd. Use to check whether a
   pattern the summary hinted at is really pervasive.
   Args: {{"query": "iperf3 -u", "k": 8, "turn_gte": 20, "turn_lte": 30}}

4. read_file(path, max_bytes=4000)
   Read a file from disk (only paths under this run's written_dirs are
   allowed; otherwise returns error). Use to tell "churn vs refinement" on
   files written multiple times.
   Args: {{"path": "/proj/.../realue.py", "max_bytes": 2000}}

Call shape:

```json
{{"tool_call": {{"name": "get_action", "args": {{"ref": "aaaa1111#243"}}}}}}
```

After each tool call the runner appends "TOOL_RESULT ..." to your context.
Then output the next tool_call OR the final judgment.

Final judgment shape:

```json
{{"final": {{
  "per_segment": [
    {{"seg": 1, "grade": "A|B|C|D", "reason": "20-80 chars",
      "suspicious_ops": [
        "0..6 items. Each item is a DETAILED, self-contained audit note (target length 200-500 characters, in the same language as the user's prompts in the trace) that the end user will read WITHOUT access to the raw trace. REQUIRED CONTENT per item, in this order:",
        "  (1) WHERE: name the segment by its human theme (e.g. 'In the moshell UE-probe segment' / 'During the login-Control-PC UE-status-check phase'). DO NOT write 'seg 1' or 'segment 3' in the content — the seg id is metadata, not user-facing text. Also mention the turn number(s).",
        "  (2) WHAT: describe the operation in plain language -- name the command / tool / file / API / endpoint in words, not just refs. Example good phrasings: 'moshell ue print probing, rewrote the script as /tmp/ue.mos -> ue2.mos -> ue3.mos to try 3 flag combinations', 'ERIS REST API /params endpoint queried via inline python3 heredoc'. Refs go in parentheses ONLY for traceability, never as the item's headline.",
        "  (3) WHY IT'S SUSPICIOUS: cite concrete evidence -- similarity score, repeat count, cycle count, credits, error strings from response text, single-turn density, or share-of-total. Include at least two numeric data points per item where possible.",
        "  (4) DECISIVE DETAIL FROM INVESTIGATION: whenever you inspected an action's full body / response / turn / file via tool calls, quote or paraphrase the specific detail that made it suspicious (e.g. 'response body was JSONDecodeError for /params calls #14/#21 — retries were papering over an endpoint typo', 'trace.py grew from 24 to 189 lines across 4 in-turn writes with the last diff only fixing an indent'). Do NOT restate what is already in the pack unless you added detail from a tool call.",
        "May be empty [] if the segment has nothing worth flagging (typical for A-grade segments)."
      ]}}
  ],
  "global_grade":  "A|B|C|D",
  "global_reason": "80-300 chars; must mention stuck turns, duplicate clusters, and multi-write findings",
  "recommendations": [
    "40-160 chars each; MUST cite specific turn numbers, file names, or numbers. Do not output vague advice like 'improve efficiency'."
  ]
}}}}
```

Hard constraints on the final judgment:
  - grade must be one of A, B, C, D
  - per_segment must give a grade for every segment in the pack
  - per_segment[].suspicious_ops: 0..6 items (empty list allowed).
    Non-empty items MUST:
      * be at least 120 characters and up to ~500 characters -- short
        blurbs are rejected. Include enough context that a user with no
        access to the trace understands what happened and why it matters.
      * name the segment by its human theme (from the pack's per-segment
        table). "seg N" as a standalone marker in the content is rejected;
        seg id belongs to the JSON metadata only.
      * describe WHAT in plain language (name the actual command / tool /
        file / API / endpoint), not just refs.
      * cite at least one ref (e.g. b7f51167#48), turn number, or file
        name in parentheses for traceability.
      * contain at least two numeric data points (repeat count, similarity,
        credits, cycles, share-of-total, error count, etc.).
      * where you called a tool to investigate, include the decisive
        finding you extracted (an error string from response, a diff you
        observed in the file, a specific action from list_turn, ...).
      * describe WHY it looks wrong, not how to fix it.
      Rejected patterns:
        - items shorter than 120 chars
        - items whose content contains the substring "seg 1", "seg 2", ...
          (case-insensitive; the same string in ref ids like "b7f51167#12"
          is fine because it's inside a `#N` ref token, not a bare
          "seg N" phrase)
        - items that are just refs + similarity numbers without any
          semantic description
        - items proposing fixes ("should batch these", "consolidate ...");
          fixes go into recommendations at the run level.
  - recommendations: 3 to 8 items, cross-cutting; the fix advice belongs
    here, not in suspicious_ops.
  - every recommendation must contain at least one digit or specific identifier.
"""
