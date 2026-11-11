"""Tool alias table + semantic action mapping.

This file is the sole coupling point between the "Kiro tool protocol" and the
"evaluation rules". When tools are renamed, parameters added, or schemas
changed, only this file changes; rule code stays untouched.
"""

from __future__ import annotations

import os

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


def apply_cd(cwd: "str | None", target: str) -> "str | None":
    """Working directory after `cd target`; returns None when it cannot be
    computed.

    The rule lives here, the traversal lives in the caller -- because the two
    sides need different things. The rule is pure string arithmetic: given a
    directory and a `cd` target, what is the new directory. Traversal needs
    the command's structure: which `cd`s are in sequence, which ones live in
    a subshell or pipeline branch that ends before the next command -- and
    structure requires an AST, which only `graph` has.

    So `command_working_dir()` here calls it with coarse string splitting
    (producing one final value, enough for rule/efficiency to decide
    "roughly where a command operates"), while `graph` walks the AST and
    calls the same function (per-position tracking, which the provenance
    graph needs to be precise). The rule exists in only one place, so the
    two sides cannot disagree -- this repo has eaten that lesson three
    times (DESIGN.md 7.5 / 7.7 / 7.9 in the graph module).

    **Unknown returns None, do not keep the old value.** A stale base
    resolves a relative path into a same-named file in a different
    directory, which is worse than having no base: a reader has no way to
    tell that the resulting node was fabricated.
    """
    target = (target or "").strip().strip("\"'")
    if not target or target == "-":
        return None               # bare `cd` goes to $HOME, `cd -` to the previous: both need external state
    if any(c in target for c in "$`*?"):
        return None               # value is only known at runtime
    if target.startswith("~"):
        return None               # command text does not carry $HOME
    if target.startswith("/"):
        return os.path.normpath(target)         # absolute: overrides, no base needed
    if not cwd:
        return None               # relative, but nothing to stack onto
    return os.path.normpath(os.path.join(cwd, target))


def command_working_dir(command: str) -> "str | None":
    """Working directory a command ends up in, as decided by its own `cd`s.

    Records often omit the shell tool's `working_dir` parameter (it is an
    optional kwarg the LLM frequently skips, embedding the directory in the
    command instead). Commands commonly carry `cd /project/x && grep ...
    sub/file.c`. That `cd` target is the correct base for relative paths
    inside the command -- not the agent-process cwd, and not the
    `working_dir` kwarg.

    **Fold every `cd`, not just the first absolute literal.** The original
    reason to only take the first absolute cd was "the dominant pattern is
    one leading cd" -- which held on our own agent's traces but not on
    human-written scripts: among 1524 reference solutions, 130 have more
    than one `cd`, and `cd /app` followed by `cd sublib` is common; taking
    only the first resolves later relative paths against the wrong parent
    and fabricates a node that never existed.

    **This is a scalar, enough for rule/efficiency, not enough for graph.**
    One command's cwd varies with position (`cd a; > x; cd b; > y` has `x`
    under a), and this field can only carry one value. The provenance graph
    wants "cwd at position N", which requires an AST -- `graph` walks it
    using the same `apply_cd`, starting from `working_dir` rather than
    this folded value (stacking again would double-count).

    Splitting uses `split_subcommands` (quote-aware, not an AST), so a
    `cd` inside a heredoc body may be mistaken for a subcommand (see its
    docstring), and `cd`s inside subshells and pipelines will also be
    treated as taking effect. The graph module's AST does not make either
    mistake -- the disagreement between the two sides is about **when**
    to apply, not about what `cd X` means.
    """
    if not command:
        return None
    wd = None
    for sub in split_subcommands(command):
        sub = sub.strip()
        if not (sub == "cd" or sub.startswith("cd ")):
            continue
        rest = sub[3:].strip()
        # A quoted target may hold spaces, so read to the closing quote rather
        # than splitting on whitespace.
        if rest[:1] in "\"'":
            end = rest.find(rest[0], 1)
            target = rest[1:end] if end > 0 else rest[1:]
        else:
            target = rest.split()[0] if rest else ""
        # A `cd` whose target cannot be read leaves the directory unknown
        # rather than where it was: `cd /app && cd $D && > out.txt` has no
        # base for `out.txt`, and using `/app` would name a file that does
        # not exist.
        wd = apply_cd(wd, target)
    return wd
