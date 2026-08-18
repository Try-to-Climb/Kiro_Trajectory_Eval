#!/bin/bash
# Test userPromptSubmit hook - inject context
EVENT=$(cat)
CWD=$(echo "$EVENT" | jq -r '.cwd')

echo "=== Injected context info ==="
echo "Current time: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Working directory: $CWD"
echo "Current user: $(whoami)"
echo "========================"
