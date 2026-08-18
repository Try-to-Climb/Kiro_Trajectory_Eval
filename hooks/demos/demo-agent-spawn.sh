#!/bin/bash
# agentSpawn hook - runs once when the agent starts
# STDOUT is injected into the agent context
EVENT=$(cat)
CWD=$(echo "$EVENT" | jq -r '.cwd')

echo "[agentSpawn inject] Hello! I am the initial context injected by a hook."
echo "[agentSpawn inject] Current host: $(hostname)"
echo "[agentSpawn inject] System: $(uname -s) $(uname -r)"
echo "[agentSpawn inject] File count in working directory: $(ls "$CWD" 2>/dev/null | wc -l)"
