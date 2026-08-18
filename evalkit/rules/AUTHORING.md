# Rule authoring guide (structured intent DSL)

This document is for rule authors and for LLMs. It describes how to author `.checks.json` using **structured intents** to describe "how the subject-under-test should execute", without writing regex or knowing the low-level checkers. When loading rules, the engine **compiles** intents into low-level checkers before evaluating.

> Quick generation: use `rules/generate-rule.sh` (edit the AGENT_JSON path inside, then run); it reads this guide plus your agent config via Kiro in non-interactive mode, auto-produces the rule, and runs `--compile` to self-check.

> For LLM use: pass this document together with the user's natural-language requirements to an LLM,
> which should produce `.checks.json` conforming to the syntax below. The output must be **deterministic
> structured JSON**, not natural language.

---

## 1. File structure

```json
{
  "target_agent": "<subject-agent-name>",
  "description": "<evaluation goal for this rule file>",
  "checks": [ <intent rule>, <intent rule>, ... ]
}
```

`target_agent` lets the one-shot pipeline (`pipeline.py`) auto-select a rule by agent name.

---

## 2. Each rule is an intent

Each `check` is an object containing **one intent keyword** plus a few common fields.

Common fields (all optional):
- `id`: rule identifier. If omitted, auto-generated (e.g., `reads_graph_json`).
- `as`: human-readable one-line description, shown in the reason column of the report.
- `importance`: `required` / `recommended` / `optional`. If omitted, defaults per intent; `never_*` is always `forbidden`.

### Intent-keyword overview

| Intent | Meaning | Compile target | Default importance |
|--------|---------|----------------|--------------------|
| `"reads": "<glob>"` | **The file's content was read** (via the read_file tool, or shell `cat`/`grep`/`sed` etc.; **excludes writes, deletes, and mere mentions**) | Exists (read action / read command verb + target) | recommended |
| `"touches": "<glob>"` | The file **appeared during this run** (reads, writes, mentions all count; used as a landmark) | Exists (pure regex) | recommended |
| `"runs": "<glob>"` | **Some command was executed** (matched at the sub-command start; automatically strips prefixes like timeout/sudo/env/python -m; **excludes "mentioned in a command" and substring mis-matches**) | Exists (action=run_command + program) | recommended |
| `"write": "<name>"` | A file whose path contains this name was produced (add `"at_least": N`); **matched as substring of the path, not as a glob** | Produces | recommended |
| `"dispatches": "<agent-glob>"` | Dispatched to a sub-agent (add `"at_least"` / `"at_most"`) | Count(spawn_subagent) | required |
| `"pipeline": ["<phrase>", ...]` | A set of actions occur **in order** (other actions may appear in between) | Milestone | required |
| `"before": ["<phraseA>", "<phraseB>"]` | A must precede B | Before | required |
| `"never_runs": "<glob>"` | **Forbid** executing some command (add `"except": "<glob>"` for an exemption) | Forbidden | forbidden |
| `"never_reads": "<glob>"` | Forbid reading some file (no action constraint; any action touching it counts) | Forbidden | forbidden |
| `"never_writes": "<glob>"` | Forbid writing to some file (**covers tool writes, shell redirection `>`/`tee`, python `open(w)`/`.write()`**; internal writes by scripts cannot be captured) | Forbidden | forbidden |
| `"never_dispatches": "<glob>"` | Forbid dispatching to some agent | Forbidden | forbidden |
| `"if_claims": "<text>", "then_runs"/"then_dispatches"/"then_write"/"then_reads": "<glob>"` | **Claim requires execution** (detect fakery / conditional implication) | IfThen | recommended |
| `"judge": "<dimension>"` | LLM scores on a dimension (dimensions: efficiency / reasoning_quality / authenticity; add `"pass_threshold"`, default 0.75) | LLMJudge (only actually invoked with `--llm`) | recommended |

---

## 3. glob syntax (replacement for regex)

- `*` matches any number of characters (including zero); `?` matches exactly one character; everything else is a literal and automatically escaped, **no need to write backslashes**.
- Unanchored — this is a "contains" match. Example: `"*.prompt.md"` matches an action whose path contains `xxx.prompt.md`.
- Examples:
  - `"reads": "*_graph.json"` — read some `*_graph.json`
  - `"runs": "timeout *kiro-cli chat*--agent*"` — executed `timeout <any seconds> kiro-cli chat ... --agent ...`
  - `"never_runs": "rm -rf /*"` — forbid `rm -rf /...`

**Note: do not hard-code variable values.** Cover variable segments like timeout seconds or path fragments with `*` (mechanism differences such as read_file vs cat are unified by the engine; authors only need to describe "what was read / what was executed").

### Choosing between reads (precise) and touches (loose)

- Use **`reads`** when you need to confirm "**the content was actually read, not written or deleted**" (e.g., "must have read the config"). It only recognizes read actions (read_file/list_dir/search) and shell read verbs (cat/grep/sed etc.); it will **not** misjudge create/modify/rm/echo as reads.
- Use **`touches`** when you only need to confirm "**this file was involved in this run**" (as a landmark, without distinguishing read from write; a path occurrence is enough).
- `reads` recognizes read signals including: read actions (read_file/list_dir/search); shell read verbs (cat/grep/sed etc.); and **reading idioms** (`json.load(`, `.read()`, `open(single-arg)`, covering python reads). It will **not** classify writes (create/modify/`open(...,'w')`), deletes, or mere mentions as reads. For a rare reading form that is not covered, either add the verb/idiom to the map (see below), fall back to `touches`, or use low-level regex.

### The map is editable: `intent_map.json`

The intent → concrete action/command-verb map lives at **`rules/intent_map.json`** and can be edited:
```json
{
  "read_actions": ["read_file", "list_dir", "read_image", "search_content", "search_files"],
  "read_shell_verbs": ["cat", "grep", "sed", "awk", "head", "tail", "less", "wc", "jq", "..."],
  "write_actions": ["create_file", "modify_file", "append_file"],
  "dispatch_action": "spawn_subagent"
}
```
For example, if the subject habitually reads files with `python3`, add `python3` to `read_shell_verbs` and `reads` will recognize it. If the file is missing or corrupt, the built-in defaults are used as fallback.

### Known blind spots of write

`write` only recognizes **tool writes** (create_file / modify_file / append_file), and only counts writes whose `completed` is success. The following cases **cannot be captured**:
- **Shell redirection writes**: `echo x > f`, `cat > f <<EOF`, `tee f`, `>> f` (the action is run_command, not a write action);
- **Writes inside scripts**: files produced inside `python3 gen.py` (no corresponding action at normalization; this is an inherent blind spot of the hook/official records).

For this reason, artifact-class checks are best set to `recommended` (e.g., `produce_cases`). If you must judge such outputs, combine with `touches` (path occurrence), `runs` (did the generator script run?), or low-level regex. The `write` name is matched as **substring**, not glob.

### The retrieval scope of if_claims

For `if_claims: C`, claim text C is searched over **the serialized string of each action**, which only contains 6 fields:
`action / tool / command / path / root / pattern`. Therefore:
- Detectable: **summarize (the pattern of summarize)**, **command text**, and claims appearing in **paths / artifact names**;
- Not detectable: the action's **`reasoning` (thinking)**, and the agent's **final reply body to the user** (which is not an action).

That is, `if_claims` detects "**claims manifested at the action level**". Choose **highly discriminative** claim text (avoid `LIVE.md`-like false positives), and pair it with `then_runs`/`then_dispatches`/`then_write` pointing to the required action. Verdict rule: if the claim exists, the corresponding action must exist, else FAIL; **when the claim never occurs, this passes** (the rule does not fire).

---

## 4. "Phrases" in pipeline / before

Each item in `pipeline` and `before` is a `"<verb> <target>"`:
- Verbs: `reads` / `runs` / `write` / `dispatches` (`dispatches *` means any dispatch);
- Target: a glob.

Example:
```json
{"pipeline": ["reads *_graph.json", "reads *baseline.json",
              "runs timeout *kiro-cli chat*", "dispatches *"],
 "importance": "required", "as": "main flow: collect → generate → probe → dispatch in order"}
```

### Ordering semantics (shared by pipeline and before)

- **Only relative order is constrained; other actions may appear in between, and there is no distance requirement.** pipeline is "ordered subsequence" (each step appears in order; any actions may be interleaved); before is "the earliest occurrence of a precedes the earliest occurrence of b". Neither requires adjacency.
- If you only care that "these actions all happened, regardless of order", **do not use pipeline**; use multiple independent `reads`/`runs`/`dispatches`.
- **Two notes on before**: ① **Missing either side passes** — if a or b never occurs, the check is PASS; so `before:[read config, deploy]` cannot detect "deploy without reading". If you must enforce read-first, add a separate required `reads`. ② **Only the earliest occurrences are compared** — when either side occurs multiple times, only the earliest idx is compared, not per-instance pairs.

---

## 5. Example: from intent to rule

```json
{
  "target_agent": "my-agent",
  "checks": [
    {"reads": "*README*", "importance": "required", "as": "must have read README"},
    {"runs": "pytest*", "importance": "recommended", "as": "ran tests"},
    {"write": "report", "importance": "optional", "as": "produced a report"},
    {"dispatches": "reviewer", "at_least": 1, "as": "dispatched to reviewer"},
    {"never_runs": "rm -rf /*", "as": "forbid dangerous delete"},
    {"if_claims": "LIVE", "then_runs": "*_acp.py", "as": "claiming LIVE requires executing the eval script"},
    {"pipeline": ["reads *README*", "runs pytest*"], "as": "read before test"},
    {"judge": "reasoning_quality"}
  ]
}
```

---

## 6. Low-level checkers (escape hatch)

In a few cases the glob is not expressive enough (e.g., exclude requires a multi-tool regex `(cat|grep|sed)`). Here you can **write a low-level checker directly** — a rule that carries `type` is passed through as-is, without compilation:

```json
{"id": "no_eval_script", "type": "Forbidden", "severity": "forbidden",
 "match": {"action": "run_command", "regex": "python3\\s+\\S*_acp\\.py"},
 "exclude": {"regex": "\\b(cat|grep|sed)\\s+\\S*_acp"}}
```
Full semantics of low-level checkers live in `../LLM_GUIDE.md` (§4 seven checkers, §5 matcher).

---

## 7. Inspecting the compiled result and self-check

Compile intent rules to low-level checkers and print (without evaluating), to verify that the compilation matches expectation:

```bash
python3 -m trajectory.runner rules/<your>.checks.json --compile
```

The compiled result goes to stdout; `--compile` also **self-checks** and writes issues to stderr:
- Unknown intents, or compiled checks that fail validation (illegal judge dimension, Count without an interval, etc.) → listed one by one, exit code **1**;
- Rule-file parse failure (invalid JSON) → error, exit code **2**;
- All good → prints `OK: N rules compiled and validated`, exit code **0**.

Evaluate:
```bash
python3 -m trajectory.runner rules/<your>.checks.json <normalized.json | --session <id> --official>
# or one-shot: python3 pipeline.py <session-id>
```

---

## 8. Compilation cheat sheet (intent → low-level)

| Intent | Compilation result |
|--------|--------------------|
| `{"reads":"*.json"}` | `{"type":"Exists","match":{"regex":"(?s:^(read_file\|list_dir\|…)\\b.*.*\\.json)\|(\\b(cat\|grep\|sed\|…)\\b[^\\n]*.*\\.json)\|<read idiom + target>"}}` (any of read action / read verb / read idiom; the action type is baked into the regex, no separate action key) |
| `{"runs":"ls*"}` | `{"type":"Exists","match":{"action":"run_command","program":"(?:<prefix>)*ls.*"}}` (matched at the sub-command start) |
| `{"write":"cases_","at_least":2}` | `{"type":"Produces","name":"cases_","min_count":2}` |
| `{"dispatches":"eval-*","at_least":1}` | `{"type":"Count","match":{"action":"spawn_subagent","regex":"eval\\-.*"},"min_count":1}` |
| `{"pipeline":["reads a","runs b"]}` | `{"type":"Milestone","steps":[<the reads-a read regex>, {"action":"run_command","program":"(?:<prefix>)*b"}]}` (phrases compile identically to standalone reads/runs) |
| `{"before":["reads a","runs b"]}` | `{"type":"Before","a":<the reads-a read regex>, "b":{"action":"run_command","program":"(?:<prefix>)*b"}}` |
| `{"never_runs":"x","except":"y"}` | `{"type":"Forbidden","severity":"forbidden","match":{"action":"run_command","regex":"x"},"exclude":{"regex":"y"}}` |
| `{"if_claims":"LIVE","then_runs":"*_acp.py"}` | `{"type":"IfThen","a":{"regex":"LIVE"},"b":{"action":"run_command","regex":".*_acp\\.py"}}` |
| `{"judge":"efficiency"}` | `{"type":"LLMJudge","dimension":"efficiency","pass_threshold":0.75}` |

Reference implementation: `../trajectory/rules_dsl.py`. Runnable examples: `example-intent.checks.json`, `agent-eval.intent.checks.json`.
