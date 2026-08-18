#!/bin/bash
# =============================================================================
# trace-hook.sh - Unified agent behavior trace hook (one script handles all 5 event types)
#
# Kiro passes a JSON payload via STDIN and distinguishes event types via hook_event_name:
#   agentSpawn / userPromptSubmit / preToolUse / postToolUse / stop
# =============================================================================

TRACE_DIR="${KIRO_TRACE_DIR:-$HOME/agent-trace/traces}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
POLICY_FILE="${KIRO_TRACE_POLICY:-$HOME/agent-trace/config/policy.json}"

# Read the event first. The session id must be taken from the STDIN payload's session_id first:
# In practice (dual_probe), when a child agent triggers a hook, payload.session_id is the child's
# real session, while the environment variable KIRO_SESSION_ID is still the parent's stale value.
# Using the environment variable would incorrectly write the child agent's events into the parent
# directory. The environment variable is only a fallback when the payload is missing.
EVENT=$(cat)
HOOK_TYPE=$(echo "$EVENT" | jq -r '.hook_event_name // "unknown"')
CWD=$(echo "$EVENT" | jq -r '.cwd // "unknown"')
PAYLOAD_SID=$(echo "$EVENT" | jq -r '.session_id // ""')
SESSION_ID="${PAYLOAD_SID:-${KIRO_SESSION_ID:-unknown}}"

# ── Session directory layout ──────────────────────────────────────
# KIRO_TRACE_LAYOUT:
#   flat  (default) → $TRACE_DIR/<session>/                 keep original structure
#   daily           → $TRACE_DIR/<YYYY-MM-DD>/<session>/    grouped by day
#
# Key point: all events of a session must land in the same directory; they must not be split
# apart by crossing midnight. Use find-or-create: first check whether the session already has
# a directory under some date, reuse it if so, otherwise create a new one under "today". The
# directory is determined by the session's start date, not by each event's current day.
LAYOUT="${KIRO_TRACE_LAYOUT:-flat}"
if [ "$LAYOUT" = "daily" ]; then
    EXISTING=$(find "$TRACE_DIR" -maxdepth 2 -type d -name "$SESSION_ID" 2>/dev/null | head -1)
    if [ -n "$EXISTING" ]; then
        SESSION_DIR="$EXISTING"
    else
        SESSION_DIR="$TRACE_DIR/$(date -u +%Y-%m-%d)/$SESSION_ID"
    fi
else
    SESSION_DIR="$TRACE_DIR/$SESSION_ID"
fi

mkdir -p "$SESSION_DIR"

# Concurrency-safe append. When the agent issues multiple tool calls in parallel, Kiro spawns
# multiple hook processes concurrently that append to the same trace.jsonl. O_APPEND atomicity
# alone is not enough to prevent lost writes, so flock is used to serialize them.
TRACE_FILE="$SESSION_DIR/trace.jsonl"
LOCK_FILE="$SESSION_DIR/.trace.lock"

append_trace() {
    # stdin → trace.jsonl, written under an exclusive lock
    if command -v flock >/dev/null 2>&1; then
        flock "$LOCK_FILE" -c "cat >> '$TRACE_FILE'"
    else
        cat >> "$TRACE_FILE"
    fi
}

case "$HOOK_TYPE" in

# ─── agentSpawn ───────────────────────────────────────────────────────────────
agentSpawn)
    # Kiro's agentSpawn payload only contains {hook_event_name, cwd, prompt} and no agent name,
    # so we can only infer it from the --agent argument in /proc or from an environment variable
    AGENT_NAME="${KIRO_AGENT_NAME:-}"
    if [ -z "$AGENT_NAME" ]; then
        for PID in $PPID $(awk '{print $4}' /proc/$PPID/stat 2>/dev/null); do
            FOUND=$(tr '\0' ' ' < /proc/$PID/cmdline 2>/dev/null | grep -oP '(?<=--agent[ =])\S+' || true)
            if [ -n "$FOUND" ]; then AGENT_NAME="$FOUND"; break; fi
        done
    fi
    [ -z "$AGENT_NAME" ] && AGENT_NAME="unknown"

    jq -nc \
        --arg ts "$TIMESTAMP" --arg event "agent_spawn" \
        --arg session_id "$SESSION_ID" --arg cwd "$CWD" \
        --arg agent_name "$AGENT_NAME" \
        --argjson raw "$EVENT" \
        '{ts:$ts,event:$event,session_id:$session_id,agent_name:$agent_name,cwd:$cwd,raw_event:$raw}' \
        | append_trace

    if [ ! -f "$SESSION_DIR/meta.json" ]; then
        jq -nc \
            --arg sid "$SESSION_ID" --arg ts "$TIMESTAMP" --arg agent "$AGENT_NAME" \
            --arg cwd "$CWD" --arg host "$(hostname)" --arg user "$(whoami)" \
            '{session_id:$sid,started_at:$ts,agent_name:$agent,cwd:$cwd,hostname:$host,user:$user}' \
            > "$SESSION_DIR/meta.json"
    fi

    echo "Agent trace active. Session: $SESSION_ID"
    ;;

# ─── userPromptSubmit ─────────────────────────────────────────────────────────
userPromptSubmit)
    PROMPT=$(echo "$EVENT" | jq -r '.prompt // ""')
    jq -nc \
        --arg ts "$TIMESTAMP" --arg event "user_prompt" \
        --arg session_id "$SESSION_ID" --arg cwd "$CWD" \
        --argjson prompt_length "${#PROMPT}" --arg prompt "$PROMPT" \
        '{ts:$ts,event:$event,session_id:$session_id,cwd:$cwd,prompt_length:$prompt_length,prompt:$prompt}' \
        | append_trace
    ;;

# ─── preToolUse ───────────────────────────────────────────────────────────────
preToolUse)
    TOOL_NAME=$(echo "$EVENT" | jq -r '.tool_name // "unknown"')
    TOOL_INPUT=$(echo "$EVENT" | jq -c '.tool_input // {}')

    # Generate a summary
    SUMMARY=""
    case "$TOOL_NAME" in
        write|fs_write)
            SUMMARY="file=$(echo "$TOOL_INPUT" | jq -r '.path // "?"'), cmd=$(echo "$TOOL_INPUT" | jq -r '.command // "?"')" ;;
        shell|execute_bash)
            SUMMARY="cmd=$(echo "$TOOL_INPUT" | jq -r '.command // "?"' | head -c 200)" ;;
        read|fs_read)
            SUMMARY="file=$(echo "$TOOL_INPUT" | jq -r '.operations[0].path // .path // "?"')" ;;
        use_aws)
            SUMMARY="$(echo "$TOOL_INPUT" | jq -r '"\(.service_name)/\(.operation_name)"')" ;;
        *)
            SUMMARY="keys=$(echo "$TOOL_INPUT" | jq -r 'keys|join(",")')" ;;
    esac

    jq -nc \
        --arg ts "$TIMESTAMP" --arg event "pre_tool_use" \
        --arg session_id "$SESSION_ID" --arg tool "$TOOL_NAME" \
        --arg summary "$SUMMARY" --arg cwd "$CWD" \
        --argjson tool_input "$TOOL_INPUT" \
        '{ts:$ts,event:$event,session_id:$session_id,tool:$tool,summary:$summary,cwd:$cwd,tool_input:$tool_input}' \
        | append_trace

    # Policy check. When blocked we must write a tool_blocked record; otherwise the blocked call
    # would leave only an orphan pre_tool_use entry, which is indistinguishable in the trace from
    # an "execution timeout / interruption".
    write_blocked() {
        jq -nc \
            --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" --arg event "tool_blocked" \
            --arg session_id "$SESSION_ID" --arg tool "$TOOL_NAME" \
            --arg reason "$1" --arg summary "$SUMMARY" --arg cwd "$CWD" \
            --argjson tool_input "$TOOL_INPUT" \
            '{ts:$ts,event:$event,session_id:$session_id,tool:$tool,reason:$reason,summary:$summary,cwd:$cwd,tool_input:$tool_input}' \
            | append_trace
    }

    if [ -f "$POLICY_FILE" ]; then
        # Tool blocklist
        if jq -e --arg t "$TOOL_NAME" '.denied_tools // [] | index($t) != null' "$POLICY_FILE" >/dev/null 2>&1; then
            write_blocked "Tool '$TOOL_NAME' denied by policy"
            echo "Tool '$TOOL_NAME' denied by policy" >&2
            exit 2
        fi
        # Command blocklist
        if [ "$TOOL_NAME" = "shell" ] || [ "$TOOL_NAME" = "execute_bash" ]; then
            CMD=$(echo "$TOOL_INPUT" | jq -r '.command // ""')
            BLOCK=$(jq -r '.denied_commands // [] | .[]' "$POLICY_FILE" | while read -r pat; do
                echo "$CMD" | grep -qE "$pat" && echo "$pat" && break
            done)
            if [ -n "$BLOCK" ]; then
                write_blocked "Command matches denied pattern: $BLOCK"
                echo "Command matches denied pattern: $BLOCK" >&2
                exit 2
            fi
        fi
        # Path blocklist
        TARGET_PATH=$(echo "$TOOL_INPUT" | jq -r '.path // .operations[0].path // ""')
        if [ -n "$TARGET_PATH" ]; then
            PBLOCK=$(jq -r '.denied_paths // [] | .[]' "$POLICY_FILE" | while read -r pat; do
                echo "$TARGET_PATH" | grep -qE "$pat" && echo "$pat" && break
            done)
            if [ -n "$PBLOCK" ]; then
                write_blocked "Path matches denied pattern: $PBLOCK"
                echo "Path matches denied pattern: $PBLOCK" >&2
                exit 2
            fi
        fi
    fi
    ;;

# ─── postToolUse ──────────────────────────────────────────────────────────────
postToolUse)
    TOOL_NAME=$(echo "$EVENT" | jq -r '.tool_name // "unknown"')
    TOOL_INPUT=$(echo "$EVENT" | jq -c '.tool_input // {}')
    TOOL_RESPONSE=$(echo "$EVENT" | jq -c '.tool_response // {}')
    SUCCESS=$(echo "$TOOL_RESPONSE" | jq -r '.success // true')
    RESPONSE_SIZE=$(echo "$TOOL_RESPONSE" | wc -c)

    jq -nc \
        --arg ts "$TIMESTAMP" --arg event "post_tool_use" \
        --arg session_id "$SESSION_ID" --arg tool "$TOOL_NAME" \
        --argjson success "$SUCCESS" --argjson response_size "$RESPONSE_SIZE" \
        --arg cwd "$CWD" --argjson tool_input "$TOOL_INPUT" \
        --argjson tool_response "$TOOL_RESPONSE" \
        '{ts:$ts,event:$event,session_id:$session_id,tool:$tool,success:$success,response_size:$response_size,cwd:$cwd,tool_input:$tool_input,tool_response:$tool_response}' \
        | append_trace
    ;;

# ─── stop ─────────────────────────────────────────────────────────────────────
stop)
    RESPONSE=$(echo "$EVENT" | jq -r '.assistant_response // ""')
    RESPONSE_LEN=${#RESPONSE}
    TURN_NUM=$(grep -c '"event":"stop"' "$SESSION_DIR/trace.jsonl" 2>/dev/null)
    TURN_NUM=$((TURN_NUM + 1))

    PREVIEW=$(echo "$RESPONSE" | head -c 2000)
    jq -nc \
        --arg ts "$TIMESTAMP" --arg event "stop" \
        --arg session_id "$SESSION_ID" --argjson turn "$TURN_NUM" \
        --argjson response_length "$RESPONSE_LEN" \
        --arg response_preview "$PREVIEW" --arg cwd "$CWD" \
        '{ts:$ts,event:$event,session_id:$session_id,turn:$turn,response_length:$response_length,response_preview:$response_preview,cwd:$cwd}' \
        | append_trace

    echo "$RESPONSE" > "$SESSION_DIR/turn_${TURN_NUM}_response.txt"
    ;;

*)
    echo "Unknown hook type: $HOOK_TYPE" >&2
    ;;
esac

exit 0
