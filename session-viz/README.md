# kiro-session-viz

Turn a Kiro CLI session (`.json` + `.jsonl` in `$KIRO_HOME/sessions/cli/`)
into a self-contained HTML timeline viewer with semantic zoom levels.

## What it shows

Two zoom levels, click to drill in:

1. **Session overview** — every turn as a bar on a time-scaled axis.
   Bar width = turn duration. Sub-agent turns are colored differently.
   Zoom in/out with the slider or −/+ buttons. A sortable table lists
   every turn with agent name, duration, tool count, tokens, credits.
2. **Turn detail** — one turn expanded as a vertical stream of events:
   user prompt → thinking blocks → assistant text → tool_use (with
   arguments and result summary). Filter by event kind. Click any event
   to expand its full content inline. `←`/`→` to page between turns.

Header shows: agent name, total turns, total duration, tokens (in/out),
total credits, working directory.

## Usage

```bash
cd session-viz

# Build one session (reads from $KIRO_HOME/sessions/cli by default)
python3 build.py <session-id>                        # writes <session-id>.html
python3 build.py <session-id> --out my.html
python3 build.py <session-id> --dir /path/to/cli     # override sessions dir

# List available sessions (with agent + turn count)
python3 build.py --list

# Build a browsable site (index.html + per-session pages)
python3 build.py --all --out-dir site
python3 build.py --all --out-dir site --limit 100    # first 100

# Debug: dump normalized JSON instead of HTML
python3 build.py <session-id> --json | jq .turns[0]

# Open the result
xdg-open site/index.html
# or serve:  python3 -m http.server -d site 8000
```

Default sessions directory resolution: `--dir` > `$KIRO_HOME/sessions/cli` >
`~/.kiro/sessions/cli`.

## What each session's HTML contains

- **No external dependencies.** Data is embedded as JSON in a `<script>` block,
  so the file works offline and can be shared as a single artifact.
- **Vanilla JS + CSS.** No framework, no build step.
- **Dark theme.** Optimized for reading long assistant/tool content.

## Data model

`build.py` normalizes a session into:

```
{ session_id, agent_name, cwd, title, total_turns, total_duration_s,
  total_input_tokens, total_output_tokens, total_credits,
  turns: [
    { turn, agent, parent_agent, start_ts, end_ts, duration_s,
      end_reason, tool_uses, input_tokens, output_tokens, credits,
      context_pct,
      events: [
        { kind: "prompt",         text },
        { kind: "thinking",       text },
        { kind: "assistant_text", text },
        { kind: "tool_use",       name, id, args,
                                  result_status, result_summary },
        ...
      ]
    }, ...
  ]
}
```

`start_ts` is the Unix timestamp from the `Prompt` record's `meta.timestamp`.
`end_ts = start_ts + turn_duration.secs`. Tool results are attached back
onto their originating `tool_use` event via `toolUseId`.

## Notes / limitations

- Only `Prompt` records carry timestamps in Kiro session files. Within
  a turn, events are ordered by their appearance in the `.jsonl` (which
  matches emission order); there are no per-event timestamps.
- `result_summary` is a truncated preview of the first success item
  (stdout / text / json). For the full raw result, look at the
  `.jsonl` file directly.
- Sessions with `session_created_reason: "sub_agent"` have a non-null
  `parent_agent` on their turns and render with a purple tint.
