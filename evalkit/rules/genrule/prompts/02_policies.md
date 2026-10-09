Continuing from the previous turn. This turn handles exactly one stage group: `{scope_name}`.

Extract the requirements (policies) from the agent material above that belong **directly** to
this scope. Each policy must be self-contained.

## Accuracy beats coverage

You are not required to produce anything. An empty `policies` array is a perfectly good answer.
A wrong check is worse than a missing check, because a wrong check either fires on every run or
on none, and in both cases it destroys trust in the whole rule file.

Whenever you are unsure — about the classification, about the right intent, or about the exact
target string — **leave the policy out, or classify it one level weaker**. Never guess a target.

## Step 1: classify observability

evalkit judges a run against its **normalized action sequence**. Every action carries only these
six fields: `action` (one of read_file / list_dir / create_file / modify_file / run_command /
spawn_subagent / summarize), `tool`, `command`, `path`, `root`, `pattern`.
There is **no** final reply text, **no** reasoning trace, and **no** content of produced files.

- `deterministic` — decidable from those fields, and you can name a concrete token (script name,
  file name, path fragment, sub-agent name) that must appear.
- `judge_only` — needs the reply text or a produced file's content. Examples: "keep responses
  under 200 words"; "never fabricate results"; "every case must cite its source"; "difficulty
  split 30/40/30".
- `unobservable` — leaves no trace at all. Examples: "ask the user for confirmation";
  "the score formula is X"; "retry once then mark ERROR".

Requirements about *truthfulness*, *quality*, *wording*, *proportions*, or *the content of a
file* are `judge_only`, never `deterministic`.

## Step 2: pick the intent, knowing how each one actually compiles

| intent | compiles to | what the target must be |
|---|---|---|
| `reads` | read actions (read_file / list_dir / search) OR a shell read verb (cat, grep, sed, head, tail, jq, ...) OR a read idiom (`json.load`, `.read()`) together with the target | a path or path fragment. Writes, deletes and bare mentions do NOT count |
| `touches` | a plain substring search over the six fields | a path fragment. Use it when you cannot tell whether the agent reads or writes the file |
| `runs` | `run_command`, matched **from the start of a sub-command** after stripping `timeout N`, `sudo`, `env`, `VAR=x`, `nice`, `exec`, `stdbuf`, `python -m` | a program or script name. Directly invoked programs: write them at the start, e.g. `tmux new-session*`, `git cherry-pick*`. Scripts run through an interpreter (`python3 foo.py`, `bash x.sh`): put a leading `*`, e.g. `*foo.py*` |
| `write` | `Produces`: a **substring of the path** of a create_file / modify_file / append_file action | a literal file name fragment, e.g. `report_`, `context.md`. **No globs here** — `*` is matched literally and will never hit. Shell redirection (`> f`, `tee f`) and files written inside a script are invisible to this check; if the file is likely produced that way, use `touches` instead |
| `dispatches` | `spawn_subagent` matching the name | a name from the "Sub-agents that may be dispatched to" list above, and nothing else |
| `never_runs` | forbidden `run_command` whose text contains the target | note this also matches merely *mentioning* the command, e.g. `cat x.py` trips `never_runs *x.py*`. Only use it when that is acceptable |
| `never_reads` | forbidden, with **no action constraint** — any action mentioning the path trips it | very broad; prefer to omit unless the path must never appear at all |
| `never_writes` | forbidden write: tool writes, shell redirection, and write idioms | a path fragment |
| `never_dispatches` | forbidden `spawn_subagent` | a name from the sub-agent list above |

Two hard rules that follow from the table:

1. **A skill is not a sub-agent.** Invoking a skill produces no `spawn_subagent` action, so a
   `dispatches` check on a skill name can never fire. If the requirement is about using a skill,
   express what that leaves in the trace instead — `reads *<skill>/SKILL.md*`, or `runs` on the
   script the skill actually executes — or classify the policy as `judge_only`. Only names in the
   sub-agent list above are valid `dispatches` targets. If that list is empty, never emit
   `dispatches` or `never_dispatches`.
2. **A glob must be discriminating.** `target` is a glob (`*` = any run of characters, `?` = one
   character, unanchored, no regex and no backslashes). Cover variable path segments with `*`,
   e.g. `*/.kiro/agents/*.json`. But `target` must never be `*` alone, or consist only of `*`
   and `?`: such a check asserts merely "something ran" or "something was read", which is not
   what any policy says. If you cannot name a discriminating target, the policy is not
   deterministic — make it `judge_only`, or leave it out.

Also note the six fields are matched separately, so a glob cannot span two of them: a target
that mixes a command fragment and a path fragment will not match.

## Step 3: fields

For `deterministic`: `intent` and `target` per the table above.
For `judge_only`: `intent` is exactly `judge`, `target` is one of
`efficiency` / `reasoning_quality` / `authenticity`.
For `unobservable`: both `intent` and `target` are null.

`severity_hint` is used **as the final severity** — there is no later calibration step, so a
wrong `required` will make legitimate runs fail. Grade it from what a run must actually do, not
from how forcefully the prompt is worded. Imperative wording ("must", "always", "NEVER") is not
evidence of `required`.

- `required` — **every** legitimate run of this task type performs it, with no exception.
  Before writing `required`, ask: *could a correct, legitimate run finish without doing this?*
  If the answer is anything other than a confident no, it is not `required`.
- `recommended` — normally done, but a legitimate run may skip it.
- `optional` — explicitly optional, user-gated, or you are simply not sure.

Any one of these disqualifies `required` — use `optional` instead:

1. the stage or step is marked optional in the prompt (`(Optional)`, "if the user wants");
2. it happens only after an explicit user confirmation or user choice;
3. it is conditional — "if X then do Y", "for install requests", "when no direct flight exists";
4. it applies only to a subset of scopes, dimensions, engines, or branches;
5. it is an auxiliary artifact such as a log file, an audit trail, or a summary that a run could
   reasonably omit;
6. it depends on prior state (a cached file already exists, a session is being resumed);
7. you are not certain.

Prefer omitting a policy entirely over recording it with a severity you cannot defend.

When the requirement is conditional, state the condition inside `policy` — do not emit a positive
rule and a `never_*` rule for the same target.

Output JSON only. No prose, no markdown code fences.

{"scope": "{scope_id}", "policies": [
  {"policy": "<self-contained requirement>",
   "observability": "deterministic|judge_only|unobservable",
   "intent": "<see table>|null", "target": "<glob>|null",
   "severity_hint": "required|recommended|optional",
   "why": "<one sentence: why this classification and this target>"}
]}
