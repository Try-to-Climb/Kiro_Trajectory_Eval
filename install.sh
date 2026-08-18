#!/bin/bash
# =============================================================================
# install.sh - Install the Kiro Agent Trace tracking system
#
# Features:
#   1. Grant executable permission to all hook scripts
#   2. Create the trace output directory
#   3. Install the agent configuration to ~/.kiro/agents/
#   4. Add the kiro-trace command to PATH
# =============================================================================

set -euo pipefail

# ---- Preflight checks ----
if ((BASH_VERSINFO[0] < 4)); then
    echo "❌ bash 4+ required (currently $BASH_VERSION)." >&2
    echo "   macOS default /bin/bash is 3.2: brew install bash, then run this script with /opt/homebrew/bin/bash." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIRO_AGENTS_DIR="$HOME/.kiro/agents"
TRACE_DIR="$HOME/agent-trace/traces"

echo "═══════════════════════════════════════════════════"
echo "  Kiro Agent Trace - Installer"
echo "═══════════════════════════════════════════════════"
echo ""

# 1. Create required directories
echo "📁 Creating directories..."
mkdir -p "$TRACE_DIR"
mkdir -p "$KIRO_AGENTS_DIR"
echo "   ✅ $TRACE_DIR"
echo "   ✅ $KIRO_AGENTS_DIR"

# 2. Grant execute permission
echo ""
echo "🔑 Setting script permissions..."
chmod +x "$SCRIPT_DIR/hooks/"*.sh
chmod +x "$SCRIPT_DIR/bin/kiro-trace"
echo "   ✅ hooks/*.sh"
echo "   ✅ bin/kiro-trace"

# 3. Install the agent configuration
echo ""
echo "⚙️  Installing agent configuration..."

# Back up existing configuration (if present)
if [ -e "$KIRO_AGENTS_DIR/traced-agent.json" ]; then
    BACKUP="$KIRO_AGENTS_DIR/traced-agent.json.bak.$(date +%s)"
    cp "$KIRO_AGENTS_DIR/traced-agent.json" "$BACKUP"
    echo "   💾 Backed up original configuration to $BACKUP"
fi

# Substitute the $HOME variable with an absolute path
HOOKS_DIR="$SCRIPT_DIR/hooks"
HOOK="$HOOKS_DIR/trace-hook.sh"
cat > "$KIRO_AGENTS_DIR/traced-agent.json" << EOF
{
  "name": "traced-agent",
  "description": "Kiro Agent with full behavior tracing - records all actions to a JSONL log",
  "prompt": "You are a helpful assistant with full behavior tracing enabled. All your tool calls and responses are being logged for audit purposes.",
  "tools": ["read", "write", "shell", "grep", "glob", "code", "use_aws", "subagent", "knowledge"],
  "hooks": {
    "agentSpawn": [
      {
        "command": "$HOOK",
        "timeout_ms": 5000
      }
    ],
    "userPromptSubmit": [
      {
        "command": "$HOOK",
        "timeout_ms": 5000
      }
    ],
    "preToolUse": [
      {
        "matcher": "*",
        "command": "$HOOK",
        "timeout_ms": 5000
      }
    ],
    "postToolUse": [
      {
        "matcher": "*",
        "command": "$HOOK",
        "timeout_ms": 5000
      }
    ],
    "stop": [
      {
        "command": "$HOOK",
        "timeout_ms": 5000
      }
    ]
  }
}
EOF
echo "   ✅ ~/.kiro/agents/traced-agent.json"

# 4. Create a symlink into bin
echo ""
echo "🔗 Creating command link..."
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$SCRIPT_DIR/bin/kiro-trace" "$LOCAL_BIN/kiro-trace"
echo "   ✅ $LOCAL_BIN/kiro-trace"

# 5. Check PATH
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    echo ""
    echo "⚠️  $LOCAL_BIN is not on PATH, please add:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 6. Verify jq dependency
echo ""
echo "🔍 Checking dependencies..."
if command -v jq &>/dev/null; then
    echo "   ✅ jq $(jq --version)"
else
    echo "   ❌ jq not installed - please install first: sudo apt install jq"
    echo "      hook scripts depend on jq to parse JSON"
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✅ Installation complete!"
echo "═══════════════════════════════════════════════════"
echo ""
echo "Usage:"
echo "  1. Switch to traced-agent:  /agent traced-agent"
echo "  2. Use the agent normally; all actions are recorded automatically"
echo "  3. View traces:  kiro-trace list"
echo "  4. View details: kiro-trace show <session-id>"
echo ""
echo "Or add the hooks to an existing agent configuration - see README.md"
