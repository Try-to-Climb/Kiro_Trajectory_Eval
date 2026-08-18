#!/bin/bash
# preToolUse hook - intercept before a tool executes
# exit 0 = allow, exit 2 = block
EVENT=$(cat)
TOOL=$(echo "$EVENT" | jq -r '.tool_name')
TOOL_INPUT=$(echo "$EVENT" | jq -c '.tool_input')

# Record to a log file (for observation)
LOG="/tmp/kiro-hook-demo.log"
echo "[$(date '+%H:%M:%S')] preToolUse: tool=$TOOL input=$(echo "$TOOL_INPUT" | head -c 200)" >> "$LOG"

# Demo: block writes to the /tmp/forbidden path
if [ "$TOOL" = "write" ] || [ "$TOOL" = "fs_write" ]; then
    FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.path // ""')
    if echo "$FILE_PATH" | grep -q "/tmp/forbidden"; then
        echo "❌ Security policy block: writing to /tmp/forbidden is not allowed" >&2
        exit 2
    fi
fi

# Demo: block dangerous shell commands
if [ "$TOOL" = "shell" ] || [ "$TOOL" = "execute_bash" ]; then
    CMD=$(echo "$TOOL_INPUT" | jq -r '.command // ""')
    if echo "$CMD" | grep -qE "rm\s+-rf\s+/|shutdown|reboot"; then
        echo "❌ Security policy block: dangerous command intercepted → $CMD" >&2
        exit 2
    fi
fi

# Allow everything else
exit 0
