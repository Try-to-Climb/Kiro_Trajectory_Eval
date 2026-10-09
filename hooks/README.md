# hooks/ — Optional Kiro CLI hook collector

**You don't need this to evaluate.** `evalkit` and `goal` read Kiro's built-in session records from `$KIRO_HOME/sessions/cli/` by default. What's in this directory is an **optional plugin** — install it only when you need signals the built-in records don't capture.

## When you want hooks

Install:
- To audit **tool calls that were blocked** by policy (built-in records only keep the ones that went through)
- To get **precise millisecond timing** (built-in records only have turn-level timestamps)
- To **watch live** what an agent is doing (`kiro-trace tail`)
- To **actively block** offending commands at `preToolUse` (see `../config/policy.json`)

Skip:
- If you only do offline post-hoc evaluation — the built-in records are enough.

## Layout

```
hooks/
├── trace-hook.sh     one script that dispatches on hook_event_name (handles all 5 events)
└── demos/            standalone examples showing what each hook can do (not required for collection)
    ├── demo-agent-spawn.sh     what an agentSpawn event carries
    ├── demo-user-prompt.sh     userPromptSubmit
    ├── demo-pre-tool.sh        preToolUse: how to exit 2 to block
    ├── demo-stop.sh            stop event
    ├── demo-secret-inject.sh   inject context back into the agent's output
    └── test-inject-context.sh  round-trip test of context injection
```

## Install

Run `./install.sh` from the repo root. It will:
1. `chmod +x` the hook scripts and `bin/kiro-trace`
2. Create `~/agent-trace/traces/` (override via `KIRO_TRACE_DIR`)
3. Write `~/.kiro/agents/traced-agent.json` with 5 hook events pointing to `trace-hook.sh`; if the file exists, it's backed up first
4. Symlink `bin/kiro-trace` to `~/.local/bin/kiro-trace`

Dependencies: `bash 4+`, `jq`, Kiro CLI itself. `install.sh` checks the first two.

## Enable tracing

**Option 1**: switch to the shipped `traced-agent` in Kiro CLI
```
/agent traced-agent
```

**Option 2**: add the 5 hooks to your **existing** agent config (better — tracing overlays your business agent, instead of running your work under a stranger):

```json
{
  "hooks": {
    "agentSpawn":       [{"command": "$HOME/agent-trace/hooks/trace-hook.sh", "timeout_ms": 5000}],
    "userPromptSubmit": [{"command": "$HOME/agent-trace/hooks/trace-hook.sh", "timeout_ms": 5000}],
    "preToolUse":       [{"matcher": "*", "command": "$HOME/agent-trace/hooks/trace-hook.sh", "timeout_ms": 5000}],
    "postToolUse":      [{"matcher": "*", "command": "$HOME/agent-trace/hooks/trace-hook.sh", "timeout_ms": 5000}],
    "stop":             [{"command": "$HOME/agent-trace/hooks/trace-hook.sh", "timeout_ms": 5000}]
  }
}
```

## Trace storage format

One directory per session:
```
~/agent-trace/traces/<session-id>/
├── trace.jsonl     one event per line, appended in time order
├── meta.json       session metadata (cwd, start time, total action count, ...)
└── stats.json      aggregated stats (tool call counts, duration distribution)
```

Each line of `trace.jsonl` is a JSON event, keyed by `event` (`user_prompt` / `pre_tool` / `post_tool` / `agent_spawn` / `stop`) plus the payload. `evalkit`'s normalizer turns this into a `TraceIR` action sequence.

**Concurrent writes**: the hook uses `flock` to serialize, so parallel sub-agent calls don't drop events.

## View

After install:
```bash
kiro-trace list                  # all traced sessions
kiro-trace show <session-id>     # full event stream
kiro-trace summary <session-id>  # aggregate stats
kiro-trace tools <session-id>    # tool call details
kiro-trace timeline <session-id> # timeline view
kiro-trace tail                  # follow the latest session live
kiro-trace export <session-id>   # export as Markdown report
kiro-trace clean --days 7        # remove traces older than N days
```

## preToolUse policy engine

The hook installed on `preToolUse` can return `exit 2` to abort the tool call, sending the reason back to the LLM. **This is the unique capability of this plugin vs the built-in records** — blocked actions still leave a trace of the attempt.

Policy lives in `../config/policy.json`:
```json
{
  "denied_tools":    ["use_aws"],
  "denied_commands": ["rm\\s+-rf\\s+/"],
  "denied_paths":    ["^/etc/", "^.*\\.pem$"],
  "alert_on":  { "tools": ["shell"], "patterns": ["DROP TABLE"] }
}
```

See `../config/README.md`.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `KIRO_TRACE_DIR` | Where traces are stored | `~/agent-trace/traces` |
| `KIRO_TRACE_POLICY` | Policy file path | `~/agent-trace/config/policy.json` |
| `KIRO_TRACE_LAYOUT` | Directory layout: `flat` or `daily` | `flat` |

## Privacy

Traces record **user prompts, file contents, and command outputs**. See the "Data handling" section of the top-level [`SECURITY.md`](../SECURITY.md).

## Overhead

Each tool call fires one extra hook: ~50–100 ms per call (within the 5 s timeout).

## Uninstall

```bash
rm ~/.kiro/agents/traced-agent.json     # or restore the .bak.<ts> that install.sh saved
rm ~/.local/bin/kiro-trace              # remove the CLI symlink
rm -rf ~/agent-trace/traces/            # optional: purge history
```
