"""Tool alias table + semantic action mapping.

This file is the sole coupling point between the "Kiro tool protocol" and the
"evaluation rules". When tools are renamed, parameters added, or schemas
changed, only this file changes; rule code stays untouched.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Alias normalization: two generations of names for the same action across
#    different Kiro versions
# ---------------------------------------------------------------------------
TOOL_ALIASES: dict[str, str] = {
    "execute_bash": "shell",
    "fs_read": "read",
    "fs_write": "write",
    "use_subagent": "subagent",
}


def canonical_tool(raw_tool: str) -> str:
    return TOOL_ALIASES.get(raw_tool, raw_tool)


# ---------------------------------------------------------------------------
# 2. Semantic actions: split one tool name into different actions by parameters
# ---------------------------------------------------------------------------

# write's command → action. create is new (no prior read needed); strReplace/insert
# is modify (requires prior read).
# Note: the same semantics has camelCase and snake_case forms across Kiro versions
#   kiro-cli 2.11.0 (measured): create / str_replace / insert
#   old traces (measured):      create / strReplace
# Before lookup we fold case and underscore via _norm_param()
WRITE_ACTIONS: dict[str, str] = {
    "create": "create_file",
    "strreplace": "modify_file",
    "insert": "modify_file",
    "append": "modify_file",
    "delete": "delete_file",
}

# read's operations[].mode → action. Directory lists a directory, not a file read
READ_MODE_ACTIONS: dict[str, str] = {
    "line": "read_file",
    "directory": "list_dir",
    "image": "read_image",
    "search": "search_content",
}


def norm_param(value: str | None) -> str:
    """Fold naming styles of a parameter value: strReplace / str_replace / STR_REPLACE → strreplace"""
    if not value:
        return ""
    return value.replace("_", "").replace("-", "").lower()


# Tools whose action is determined without inspecting parameters
SIMPLE_ACTIONS: dict[str, str] = {
    "shell": "run_command",
    "grep": "search_content",
    "glob": "search_files",
    "summary": "summarize",
    "subagent": "spawn_subagent",
    "use_aws": "aws_call",
    "introspect": "docs_query",
    "code": "code_query",
}

UNKNOWN_ACTION_PREFIX = "unknown:"


def unknown_action(tool: str) -> str:
    return f"{UNKNOWN_ACTION_PREFIX}{tool}"


# ---------------------------------------------------------------------------
# 3. shell command splitting
# ---------------------------------------------------------------------------

_SEPARATORS = ("&&", "||", ";", "\n")


def split_subcommands(command: str) -> list[str]:
    """Split a compound command by && || ; and newlines, skipping separators inside quotes.

    Note: this is heuristic splitting; it does not parse heredoc internals.
    Newlines inside a heredoc body are treated as separators, so commands
    containing <<EOF will produce more parts than expected. Callers must be
    aware of this.
    """
    if not command:
        return []
    if not isinstance(command, str):
        command = str(command)

    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        # Quote state machine
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        # Separators
        matched = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            continue

        buf.append(ch)
        i += 1

    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]
