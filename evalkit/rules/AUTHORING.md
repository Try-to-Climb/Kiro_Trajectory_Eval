# Rule authoring guide (structured intent DSL)

This document is written for rule authors and for LLMs. It describes how to write
`.checks.json` using **structured intents** — describing "how the agent under test
is expected to execute" without hand-writing regexes or knowing the low-level
checker types. On load, the engine **compiles** these intents into low-level
checkers and then evaluates them.

> Quick generation: `rules/generate-rule.sh` (edit the `AGENT_JSON` path and
> run) uses Kiro's non-interactive mode to read this guide and an agent
> config, auto-produce a rule file, and self-check with `--compile`.

> For LLMs: give this document plus the user's natural-language requirements
> to the LLM, and expect it to produce a `.checks.json` conforming to the
> syntax below. The output must be **deterministic structured JSON**, not prose.

---

## 1. File structure

```json
{
  "target_agent": "<name of the agent under test>",
  "description": "<what this rule file evaluates>",
  "checks": [ <intent rule>, <intent rule>, ... ]
}
```

`target_agent` lets the one-shot pipeline (`pipeline.py`) pick the right rule
file by agent name.

---

## 2. Each rule is one intent

Each `check` is an object with **one intent keyword** plus optional common
fields.

Common fields (all optional):
- `id`: rule identifier. Auto-generated if omitted (e.g. `reads_graph_json`).
- `as`: a one-line human-readable description shown in the report's `reason` column.
- `importance`: `required` / `recommended` / `optional`. Falls back to each
  intent's default when omitted; `never_*` is always `forbidden`.

### Intent keywords

| Intent | Meaning | Compiles to | Default importance |
|---|---|---|---|
| `"reads": "<glob>"` | **Read file content** (read_file tool, or shell `cat`/`grep`/`sed` etc.; **excludes writes, deletes, mentions**) | Exists (read action / read verb + target) | recommended |
| `"touches": "<glob>"` | File **appeared in this run** (reads, writes, mentions all count; use as a landmark) | Exists (pure regex) | recommended |
| `"runs": "<glob>"` | **Executed a command** (matched at subcommand head; strips `timeout`/`sudo`/`env`/`python -m` prefixes automatically; **excludes "mentioned in a body" and substring false matches**) | Exists (action=run_command + program) | recommended |
| `"write": "<name>"` | Produced a file whose path contains the name (optional `"at_least": N`); **substring match on path, not glob** | Produces | recommended |
| `"dispatches": "<agent-glob>"` | Dispatch to a sub-agent (optional `"at_least"` / `"at_most"`) | Count (spawn_subagent) | required |
| `"pipeline": ["<phrase>", ...]` | A set of actions appear **in order** (other actions may interleave) | Milestone | required |
| `"before": ["<phrase A>", "<phrase B>"]` | A must appear before B | Before | required |
| `"never_runs": "<glob>"` | **Forbid** executing a command (optional `"except": "<glob>"` exemption) | Forbidden | forbidden |
| `"never_reads": "<glob>"` | Forbid reading a file (no action constraint; any action touching it counts) | Forbidden | forbidden |
| `"never_writes": "<glob>"` | Forbid writing a file (**covers tool writes, shell redirects `>`/`tee`, python `open(w)`/`.write()`**; writes done inside a script cannot be captured) | Forbidden | forbidden |
| `"never_dispatches": "<glob>"` | Forbid dispatching to an agent | Forbidden | forbidden |
| `"if_claims": "<text>", "then_runs"/"then_dispatches"/"then_write"/"then_reads": "<glob>"` | **If claimed, must have executed** (fakery detection / conditional implication) | IfThen | recommended |
| `"judge": "<dimension>"` | LLM-scored on a dimension (dimensions: efficiency / reasoning_quality / authenticity; optional `"pass_threshold"`, default 0.75) | LLMJudge (requires `--llm` to actually invoke) | recommended |

---

## 3. glob syntax (replaces regex)

- `*` matches any number of characters (including zero); `?` matches exactly
  one; everything else is matched literally with automatic escaping, so **no
  backslashes to write**.
- Unanchored, i.e. "contains" matching. Example: `"*.prompt.md"` hits an
  action whose path contains `xxx.prompt.md`.
- Examples:
  - `"reads": "*_graph.json"` — read any `*_graph.json`
  - `"runs": "timeout *kiro-cli chat*--agent*"` — executed
    `timeout <any seconds> kiro-cli chat ... --agent ...`
  - `"never_runs": "rm -rf /*"` — forbid `rm -rf /...`

**Do not hard-code variable values.** Timeout seconds and variable path
segments should be covered by `*` (the engine already unifies mechanism
differences such as read_file vs cat; the author only needs to describe
"what was read / executed").

### Choosing between `reads` (precise) and `touches` (loose)

- Use **`reads`** when you need to confirm "**the content was actually read, not
  written or deleted**" (e.g. "must have read the config"). It only recognises
  read actions (read_file / list_dir / search) and shell read verbs (cat /
  grep / sed / ...), and will **not** mistake create / modify / rm / echo for
  reads.
- Use **`touches`** (any path appearance counts) when you only need to confirm
  "**this file was involved in the run**" as a landmark, regardless of
  read/write.
- The read signals that `reads` recognises are: read actions
  (read_file / list_dir / search), shell read verbs (cat / grep / sed / ...),
  and **read idioms** (`json.load(`, `.read()`, `open(single arg)` — covering
  python reads). It does **not** classify writes
  (create / modify / `open(..., 'w')`), deletes, or plain mentions as reads.
  If a less common read form is missed, add the corresponding verb / idiom to
  the mapping (see below), fall back to `touches`, or use a low-level regex.

### The mapping is editable: `intent_map.json`

The intent-to-action-and-verb mapping lives in **`rules/intent_map.json`**
and can be edited by rule authors:
```json
{
  "read_actions": ["read_file", "list_dir", "read_image", "search_content", "search_files"],
  "read_shell_verbs": ["cat", "grep", "sed", "awk", "head", "tail", "less", "wc", "jq", "..."],
  "write_actions": ["create_file", "modify_file", "append_file"],
  "dispatch_action": "spawn_subagent"
}
```
For example, if the agent under test habitually uses `python3` to read files,
adding `python3` to `read_shell_verbs` makes `reads` recognise it. The file
falls back to built-in defaults if missing or malformed.

### Known blind spots of `write`

`write` only recognises **tool-level writes** (create_file / modify_file /
append_file), and only counts writes where `completed` is successful. The
following forms **cannot be captured**:
- **Shell redirect writes**: `echo x > f`, `cat > f <<EOF`, `tee f`, `>> f`
  (the action is `run_command`, not a write action);
- **Writes inside a script**: files produced by `python3 gen.py` from inside
  the script (there is no corresponding action in the normalization layer; this
  is an inherent blind spot of both hook and official records).

So artefact-type checks are best set to `recommended` (e.g. `produce_cases`).
If such outputs must be judged, combine `touches` (any path appearance),
`runs` (whether the generating script was executed), or a low-level regex.
The name in `write` is a **substring**, not a glob.

### Search scope of `if_claims`

The claim text C in `if_claims: C` is searched over **each action's serialised
form**, which only contains 6 fields: `action / tool / command / path / root /
pattern`. Therefore:
- Detectable: claims appearing in the **summarize `pattern`**, in **command
  text**, or in **paths / artefact names**;
- Not detectable: the action's **`reasoning` (thinking)**, and the agent's
  **final reply prose to the user** (which is not an action).

In other words, `if_claims` detects "**claims that surface at the action
layer**". When authoring, pick **highly distinctive** claim text (avoid
accidental triggers like `LIVE.md`) and pair it with
`then_runs`/`then_dispatches`/`then_write` pointing at the action that must
have happened. Decision rule: if the claim exists, the required action must
also exist, otherwise FAIL; **if the claim never appears, the rule passes**
(the rule does not fire).

---

## 4. "Phrases" in `pipeline` / `before`

Each item in `pipeline` or `before` is a sentence `"<verb> <target>"`:
- Verb: `reads` / `runs` / `write` / `dispatches` (`dispatches *` means any
  dispatch);
- Target: glob.

Example:
```json
{"pipeline": ["reads *_graph.json", "reads *baseline.json",
              "runs timeout *kiro-cli chat*", "dispatches *"],
 "importance": "required", "as": "backbone: collect -> generate -> probe -> dispatch, in order"}
```

### Ordering semantics (shared by `pipeline` and `before`)

- **Only relative order is constrained**; no restriction on interleaving
  actions or distance between them. `pipeline` is "ordered subsequence"
  (steps appear in order, anything may interleave); `before` is "earliest
  occurrence of a precedes earliest occurrence of b". Neither requires
  adjacency.
- If you only care that "these actions all happened, in any order", **do not
  use `pipeline`** — use several independent `reads` / `runs` / `dispatches`
  rules instead.
- **Two gotchas with `before`**: (1) **missing-either-side passes** — if a or
  b never appears, the rule PASSes; so `before: [reads config, deploy]` does
  not catch "deployed without reading"; use an additional required `reads` if
  "must read first" matters. (2) **Only earliest occurrences are
  compared** — when either side appears multiple times, only the earliest idx
  of each side is compared, not pairwise.

---

## 5. Example: from intents to a rule file

```json
{
  "target_agent": "my-agent",
  "checks": [
    {"reads": "*README*", "importance": "required", "as": "read README at least once"},
    {"runs": "pytest*", "importance": "recommended", "as": "ran tests"},
    {"write": "report", "importance": "optional", "as": "produced a report"},
    {"dispatches": "reviewer", "at_least": 1, "as": "dispatched to reviewer"},
    {"never_runs": "rm -rf /*", "as": "forbid dangerous removal"},
    {"if_claims": "LIVE", "then_runs": "*_acp.py", "as": "if claimed LIVE then must run eval script"},
    {"pipeline": ["reads *README*", "runs pytest*"], "as": "read then test"},
    {"judge": "reasoning_quality"}
  ]
}
```

---

## 6. Low-level checkers (escape hatch)

In rare cases glob is not expressive enough (e.g. `exclude` needs a multi-tool
regex `(cat|grep|sed)`). You can **write a low-level checker directly** — a
rule carrying `type` is passed through verbatim, bypassing the compiler:

```json
{"id": "no_eval_script", "type": "Forbidden", "severity": "forbidden",
 "match": {"action": "run_command", "regex": "python3\\s+\\S*_acp\\.py"},
 "exclude": {"regex": "\\b(cat|grep|sed)\\s+\\S*_acp"}}
```

The full semantics of low-level checkers are in `../LLM_GUIDE.md` (§4 the
seven checkers, §5 matcher).

---

## 7. Inspect the compiled result and self-check

Compile intent rules to low-level checkers and print them (no evaluation);
useful for inspecting that compilation matches your expectations:

```bash
python3 -m rule.runner rules/<yours>.checks.json --compile
```

The compiled result is written to stdout; `--compile` also **self-checks** and
writes problems to stderr:
- Unrecognised intents, or rules that fail validation after compilation
  (illegal judge dimension, Count with no range, etc.) are listed, exit code
  **1**;
- Rule file fails to parse (invalid JSON) — reports and exits with code
  **2**;
- All good — prints `OK: N rules compiled and validated`, exit code **0**.

Evaluate:
```bash
python3 -m rule.runner rules/<yours>.checks.json <normalized.json | --session <id> --official>
# Or one-shot:  python3 pipeline.py <session-id>
```

---

## 8. Quick lookup: intent -> low-level compilation

| Intent | Compiles to |
|---|---|
| `{"reads":"*.json"}` | `{"type":"Exists","match":{"regex":"(?s:^(read_file\|list_dir\|...)\\b.*.*\\.json)\|(\\b(cat\|grep\|sed\|...)\\b[^\\n]*.*\\.json)\|<read-idiom + target>"}}` (any of read action / read verb / read idiom; the action types are baked into the regex, no separate `action` key) |
| `{"runs":"ls*"}` | `{"type":"Exists","match":{"action":"run_command","program":"(?:<prefixes>)*ls.*"}}` (matched at subcommand head) |
| `{"write":"cases_","at_least":2}` | `{"type":"Produces","name":"cases_","min_count":2}` |
| `{"dispatches":"eval-*","at_least":1}` | `{"type":"Count","match":{"action":"spawn_subagent","regex":"eval\\-.*"},"min_count":1}` |
| `{"pipeline":["reads a","runs b"]}` | `{"type":"Milestone","steps":[<reads-a read regex>, {"action":"run_command","program":"(?:<prefixes>)*b"}]}` (phrases compile exactly as standalone `reads`/`runs` do) |
| `{"before":["reads a","runs b"]}` | `{"type":"Before","a":<reads-a read regex>, "b":{"action":"run_command","program":"(?:<prefixes>)*b"}}` |
| `{"never_runs":"x","except":"y"}` | `{"type":"Forbidden","severity":"forbidden","match":{"action":"run_command","regex":"x"},"exclude":{"regex":"y"}}` |
| `{"if_claims":"LIVE","then_runs":"*_acp.py"}` | `{"type":"IfThen","a":{"regex":"LIVE"},"b":{"action":"run_command","regex":".*_acp\\.py"}}` |
| `{"judge":"efficiency"}` | `{"type":"LLMJudge","dimension":"efficiency","pass_threshold":0.75}` |

Reference implementation: `../rule/rules_dsl.py`. Runnable examples:
`example-intent.checks.json`, `agent-eval.intent.checks.json`.
