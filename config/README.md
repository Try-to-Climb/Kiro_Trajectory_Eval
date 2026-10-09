# config/ — Sample configuration files

Three drop-in JSON configs. Copy them into your own project and edit. None of them are hard dependencies of the evaluators — the only one read directly by shipped code is `traced-agent.json` (by `install.sh`).

## `traced-agent.json` — Generic agent template with tracing

`install.sh` uses this as a template to write `~/.kiro/agents/traced-agent.json`, wiring 5 hook events to `hooks/trace-hook.sh`. Switching to that agent (`/agent traced-agent`) records every tool call.

**Fields**:
- `name` / `description` / `prompt` — a minimal generic agent; you'll want to replace these
- `tools` — allowed tool list
- `hooks` — 5 events; `$HOME/agent-trace/...` inside `command` is replaced with your machine's absolute path by `install.sh`

**Recommended customization**:
- Replace `prompt` with your business agent's system prompt
- Narrow or widen `tools`
- If you already have an agent config, don't switch to `traced-agent`. Instead, copy the whole `hooks` block into your own agent JSON.

## `policy.json` — preToolUse policy engine

Read by `hooks/trace-hook.sh` during `preToolUse`. Matching a `denied_*` rule triggers `exit 2` (blocks the call, reason returned to the LLM); matching `alert_on` only records without blocking.

**Fields**:
| Field | Type | Meaning |
|---|---|---|
| `denied_tools` | list[str] | Tool names to block outright (exact match) |
| `denied_commands` | list[regex] | Shell command regex blocklist (for the `shell` tool) |
| `denied_paths` | list[regex] | Path regex blocklist (for `read` / `write` / `edit` tools) |
| `alert_on.tools` | list[str] | Tools that trigger an alert (not blocked) |
| `alert_on.patterns` | list[regex] | Command-content regex that triggers an alert |

The **default policy** shipped here blocks 5 destructive shell patterns (rm -rf /, fork bomb, mkfs, dd to a block device, ...), 5 sensitive path categories (`/etc/`, `/boot/`, `.ssh/`, `.pem`, `.key`), and raises alerts on the `use_aws` / `shell` tools and on `DELETE` / `DROP TABLE` / `force-push` patterns.

**Customization**: adjust to your trust boundary. Regexes use double backslashes (`\\s`, not `\s`) because JSON.

## `kiro-judge.json` — Judge agent for evalkit's LLMJudge

`evalkit/rule/llm_judge.py` needs an LLM that "returns only rubric-scored JSON and never calls a tool". This config defines exactly that:

- `tools: []` + `allowedTools: []` — refuses every tool call
- `prompt` insists on "output only the requested JSON code block"

**Usage**: copy to `~/.kiro/agents/kiro-judge.json` (or let `pipeline.py --llm` pick it up automatically). `evalkit` then dispatches through `kiro-cli chat --agent kiro-judge` when a rule contains an `LLMJudge` checker.

**Customization**: if your kiro-cli version uses different field names (e.g. no `allowedTools`), rename to match your schema. Do not modify the `prompt`.

---

## Quick index

| File | Who reads it | When |
|---|---|---|
| `traced-agent.json` | `install.sh` | Installer time |
| `policy.json` | `hooks/trace-hook.sh` | Every `preToolUse` |
| `kiro-judge.json` | `evalkit/rule/llm_judge.py` (via kiro-cli) | When a rule has `LLMJudge` and the user passed `--llm` |
