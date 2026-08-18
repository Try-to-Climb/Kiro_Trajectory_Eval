#!/bin/bash
# userPromptSubmit hook - runs each time the user sends a message
# STDOUT is injected into the context of the current turn
EVENT=$(cat)
CWD=$(echo "$EVENT" | jq -r '.cwd')
PROMPT=$(echo "$EVENT" | jq -r '.prompt')

echo "[userPrompt inject] Timestamp: $(date '+%H:%M:%S')"
echo "[userPrompt inject] User input length: ${#PROMPT} chars"
echo "[userPrompt inject] Current memory usage: $(free -h 2>/dev/null | awk '/Mem:/{print $3"/"$2}' || echo 'N/A')"
