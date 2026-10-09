"""Prompt templates for the three LLM sites (s2 requirements / s3 claims / s4 compile / s8 judge).

Centralized here so they can be treated as "data" (like rules/) and diffed / regression-tested.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# s2: extract requirements
# ---------------------------------------------------------------------------
EXTRACT_REQUIREMENTS = """You are working on the first step of agent trajectory evaluation: extracting "verifiable requirements raised by the user" from a multi-turn conversation.

First classify each user turn's intent:
- request      : user asks the agent to do something (produces observable actions or artifacts)
- question     : merely asking / seeking an explanation, no action requested
- clarification: follow-up clarification, does not add a new requirement
- cancellation : cancels or overrides an earlier requirement

Only `request` turns yield requirements. When extracting a requirement you MUST:
1. Split into atomic requirements: one sentence with multiple things becomes multiple entries
   (e.g., "render to html and then serve on a port" = two entries)
2. Set `verifiable_by`:
   action   — some kind of action must appear in the trajectory
   artifact — some file must be produced
   outcome  — you have to inspect command output to know whether it succeeded
3. Set `status`: active (still in force) / superseded (refined-away by a later turn) / cancelled (cancelled)
4. Set `strength`:
   strong — clear criteria; whether it is satisfied carries information
   weak   — vague criteria (e.g., "take a look at this project"); even satisfied it says little
5. `expect`: one sentence describing "what should be observable"; no code, no regex

[IMPORTANT] The [Agent-under-test context] block below (if non-empty) describes this agent's own
positioning and capabilities. Use it to:
- Distinguish "user describing background / suggesting tools" from "user issuing a hard requirement"
  (e.g., if a tool is the agent's typical means, the user merely naming that tool is not a
  requirement to run it)
- When the user's intent falls squarely inside a skill the agent has declared, do not over-split
  (avoid double-counting means and ends)
- If the user's description conflicts with the agent's declared capabilities / workflow, still
  mark that turn as request but flag the requirement as `weak`

{agent_context}

Output exactly one JSON code block:
```json
{{"turn_intents": [{{"turn": 1, "intent": "request"}}],
  "requirements": [{{"id": "R1.1", "text": "...", "origin_turn": 1,
                    "verifiable_by": "action", "status": "active",
                    "strength": "strong", "expect": "..."}}]}}
```

====== Conversation ({turns} turns) ======
{block}
"""

# ---------------------------------------------------------------------------
# s3: extract self-claims
# ---------------------------------------------------------------------------
EXTRACT_CLAIMS = """You are working on agent trajectory evaluation. Task: from the agent's replies, extract the
statements it makes about "what it did", so we can later cross-check those statements against
the trajectory evidence.

Only extract statements about done / produced / verified / not-done. Ignore purely explanatory
content (concepts, principles, advice).

Tag each with `kind`:
- did      : claims to have executed some operation
- produced : claims to have produced some file / service / result
- verified : claims to have verified / tested / gotten it working
- declined : explicitly states "I did NOT do X" (including alternatives-instead and
             capability-limit statements)

`quote` MUST be a **verbatim fragment** from the reply (may be truncated; NO rewriting, NO stitching).

[IMPORTANT] The [Agent-under-test context] block below (if non-empty) describes this agent's
capabilities and workflow. Use it to:
- For things the agent has declared it can do, keep "did X" statements as did / produced (easy to
  align with real actions)
- If a claimed action is outside the agent's capabilities (e.g., the agent claims a review outside
  any of its declared skills), still extract it but keep the raw wording in quote so the downstream
  verdict layer can decide
- Do not mistake the agent's self-introduction of its skills for a claim (e.g., "I can do X" is not
  a `did` claim)

{agent_context}

Output exactly one JSON code block:
```json
{{"claims": [{{"id": "C1.1", "turn": 1, "kind": "produced", "text": "...", "quote": "..."}}]}}
```

====== Agent replies ({n} turns) ======
{block}
"""

# ---------------------------------------------------------------------------
# s4: compile to hard checks + retrieval anchors
# ---------------------------------------------------------------------------
COMPILE = """You are working on agent trajectory evaluation. Task: translate each requirement into two
things — **a hard check** and **retrieval anchors**. You cannot see the trajectory content, and
that is deliberate: the criteria must be writable without knowing the answer, otherwise you would
tailor conditions to the answer.

[hard_check] Only the following three forms are allowed. Use one whenever you can (they are
deterministic verdicts that bypass the LLM):
  {{"read_path": "<path fragment>"}}       when the requirement is "read/inspect some file"
  {{"artifact_glob": "<filename glob>"}}   when the requirement is "produce some file"; must be
                                           Python glob syntax — do NOT use the {{a,b}} brace form,
                                           if you need two patterns write them as an array
  {{"invoke_agent": "<agent name>"}}       when the requirement is "call / evaluate some agent"
If none applies, return an empty object {{}}.

[anchors] 3~8 short words used for substring search over the action text (NOT regex — no `.*`).
Points:
  - Include proper nouns from the requirement text: path fragments, filenames, extensions,
    agent names, command names
  - Add keywords for **common implementation shapes** of that goal
    (e.g., "start a port service" -> http.server / serve / uvicorn / nohup)
  - Prefer recall over precision: a few extra hits are fine — the LLM filters them later; missing
    is the fatal case

[residual] The portion of `expect` that cannot be verified via action/file — record it verbatim,
and in `residual_needs` say what additional evidence is required (allowed values:
response / artifact_content / cross_session / human).

Output exactly one JSON code block:
```json
{{"compiled": [{{"req_id": "R1.2",
                "hard_check": {{"artifact_glob": "test_cases_*.json"}},
                "anchors": ["test_cases", "cases", ".json"],
                "residual": "filename alone cannot prove the content is a valid, non-empty case set",
                "residual_needs": ["artifact_content"]}}]}}
```

====== Requirements to translate ======
{block}
"""

# ---------------------------------------------------------------------------
# s8: judge
# ---------------------------------------------------------------------------
JUDGE = """You are on the last step of agent trajectory evaluation: given the collected evidence, decide
whether each requirement is satisfied.

Rules (follow each strictly):
1. `satisfied` values: "true" / "false" / "unverifiable" (evidence type absent, cannot decide)
2. `evidence_actions` may only cite refs that appear in the evidence pack (of the form
   "aaaa1111#37"); do NOT cite refs not present. File evidence goes in `evidence_files`, not
   `evidence_actions`.
3. Pick one `tier` reflecting the strongest evidence you relied on:
   direct (structured action field directly proves it) / artifact (artifact on disk within the
   run's time window) / derived (inferred from command text) / retrieved (retrieval candidate you
   accept as evidence) / cross_session (evidence in a child session, attributed via dispatch) /
   testimonial (self-claim only) / none
4. For artifact-type requirements: if the trajectory query MISSes but the filesystem is strong ->
   judge "true", and note in `reason` that the artifact was produced by a script (hooks /
   normalization cannot record file operations internal to a command).
5. Shared state with strength=info (ports/processes) MUST NOT be used as verdict evidence.
6. Whether `evidence_before_request` (completed before the requirement was raised) counts as
   satisfied depends on the requirement: pure read/inspect types do; "redo it my new way" types
   do not.
7. When search status is `absent`, DO NOT jump to "false". First look at the [trajectory vocabulary
   sample] at the top of the evidence pack (action distribution / command heads / files written):
   - If the sample shows activity of this kind exists in the trajectory, just with different
     words -> judge "unverifiable" and note the anchors are wrong (especially watch out when
     absent_reason == suspect_anchors)
   - If the sample shows no such activity at all -> only then judge "false"
8. Portions of `residual` that could not be verified must be called out in `reason`.
9. Self-claim contradicting the verdict (claimed done but not satisfied) -> overclaim=true.
10. Requirements labeled "synthetic control group" are deliberately-injected requests that
    definitely do NOT exist in the trajectory, used to test whether the verdict is too lax.
    Judge them normally from the evidence — do not give them special treatment.

Output exactly one JSON code block:
```json
{{"judgments": [{{"req_id": "R1.2", "satisfied": "true", "tier": "artifact",
                 "evidence_actions": ["aaaa1111#32"],
                 "evidence_files": ["/abs/path/test_cases_boundary.json"],
                 "overclaim": false, "reason": "..."}}]}}
```

====== Evidence pack ======
{pack}
"""
