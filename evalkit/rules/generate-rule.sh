#!/bin/bash
# =============================================================================
# generate-rule.sh — Use Kiro in non-interactive mode to automatically generate
#                    an evalkit structured-intent rule file, based on
#                    AUTHORING.md plus the config/prompt of the agent under test.
#
# Usage:
#   1. Run under the evalkit/ directory;
#   2. Only edit AGENT_JSON below to point at your agent config, then execute this script.
# =============================================================================

set -euo pipefail

# ── Only this line needs editing: the config/prompt file of the agent under test ─
AGENT_JSON="/path/to/your_agent.json"          # ← change to your *_agent.json path

# Output rule file (defaults to the agent file's basename; adjust if needed)
OUT="rules/$(basename "$AGENT_JSON" .json | sed 's/_agent$//').checks.json"

kiro-cli chat --no-interactive --trust-tools=read,write "$(cat <<EOF
You are evalkit's rule generator, producing a deterministic structured-intent rule file (.checks.json).

Please follow these steps:
1. Use the read tool to read the rule authoring guide rules/AUTHORING.md and strictly follow
   its intent syntax, glob syntax, and importance values; prefer intent keywords and avoid
   hand-writing type/regex.
2. Use the read tool to read the agent's config and prompt: ${AGENT_JSON},
   understanding its expected workflow, required steps, key outputs, and prohibitions.
3. Generate a structured-intent rule:
   - target_agent should be the agent's name;
   - Each check uses intent keywords (reads/touches/runs/write/dispatches/pipeline/
     before/never_runs/never_reads/never_writes/never_dispatches/if_claims…then_*/judge);
   - Required steps use importance=required, optional ones use recommended, prohibitions use never_*;
     main-line ordering uses pipeline; "claim implies execution" uses if_claims…then_*;
   - Fill in "as" description where possible.
4. Use the write tool to write the result to ${OUT}, as valid JSON, without any explanatory text.
EOF
)"

echo "── Generation complete; running self-check (should print OK) ──"
python3 -m rule.runner "$OUT" --compile
