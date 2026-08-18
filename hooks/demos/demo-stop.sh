#!/bin/bash
# stop hook - triggered when the agent's reply finishes (observation only, cannot intervene)
EVENT=$(cat)
RESPONSE=$(echo "$EVENT" | jq -r '.assistant_response // ""')
RESPONSE_LEN=${#RESPONSE}

# Record to log
LOG="/tmp/kiro-hook-demo.log"
echo "[$(date '+%H:%M:%S')] stop: response_length=$RESPONSE_LEN chars" >> "$LOG"

exit 0
