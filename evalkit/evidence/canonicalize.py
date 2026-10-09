"""Command canonicalization for the efficiency workflow.

The problem this solves:

`s4_detect_duplicates` compares ``run_command`` actions via cosine similarity
on the raw command text. That works when commands differ meaningfully, but
returns false positives when many commands share the same shell scaffold
and only differ in an inner python heredoc or a nested CLI argument list.
In one real session, five diagnostic probes had command_strong=1.00 across
the board despite each doing a different step, because the outer
``cd && source && export && timeout 900 python -u -c "..."`` wrapper was
byte-identical and the meaningful inner args= list was buried inside the
quoted argument.

Approach: peel the shell scaffold (regex, no bashlex) to extract the inner
payload, then if it parses as Python, walk the AST to produce a
`structure_hash` (invariant modulo literal values) and a `literals` list
(the actual varying content). Two commands with the same scaffold but
different `cmds=[...]` lists get different structure_hashes on their AST
constants, so downstream similarity code can distinguish "template replay"
from "template parameterized diagnosis".

No IO, no persistence -- called on demand, per action, per run. The whole
pipeline for 188 commands takes ~12 ms.
"""
from __future__ import annotations

import ast as pyast
import hashlib
import re
from typing import Any


# ---------------------------------------------------------------------------
# Shell prefix / suffix patterns (regex only, deliberately no bashlex).
#
# Applied in a loop until nothing matches: each command may layer several of
# these, e.g. `cd X && source venv && export PYTHONPATH=... && timeout 900 ...`.
# ---------------------------------------------------------------------------
_RE_LINE_CONT = re.compile(r"\\\r?\n\s*")

_RE_CD       = re.compile(r"^\s*cd\s+\S+(?:\s+2>\S+)?\s*(?:&&\s*|\n\s*|;\s*)")
_RE_SOURCE   = re.compile(r"^\s*source\s+\S+(?:/activate)?\s*(?:2>/dev/null\s*)?(?:&&\s*|\n\s*|;\s*)")
_RE_EXPORT   = re.compile(r"^\s*export\s+\w+=(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&\s*|\n\s*|;\s*)")
_RE_ENV_UNSET = re.compile(r"^\s*env(?:\s+-u\s+\w+)+\s*")
_RE_ENV_KV   = re.compile(r"^\s*(?:\w+='[^']*'\s+)+")
_RE_NOHUP    = re.compile(r"^\s*nohup\s+")
_RE_TIMEOUT  = re.compile(r"^\s*timeout(?:\s+-\w+(?:\s+\S+)?)*\s+\d+[smhd]?\s+")

# `python -c "..."` payload extraction.
#
# Escape-aware content: `(?:[^"\\]|\\.)*` matches every char except
# unescaped `"` (a `"` preceded by `\` is fine, e.g. `\"`).
# After the closing `"`, we require a shell-argument boundary: end of
# string, whitespace, pipe/redirect, or `&&`/`;` — this prevents the
# regex from swallowing shell code that follows the python invocation
# (`... python -c "..." 2>&1 | tail -3 && echo "..."` used to fail).
_RE_PYTHON_C = re.compile(
    r"""^\s*python3?\s+(?:-u\s+)?-c\s+"((?:[^"\\]|\\.)*)"(?=\s|$|[|&;])"""
    r"""\s*(?:2>&1)?"""
    r"""(?:.*)?$""",
    re.DOTALL)

# `bash -c '...'` payload extraction (single-quoted, no escape handling
# needed since `'` inside `'...'` requires closing the quote in shell).
_RE_BASH_C = re.compile(
    r"""^\s*(?:bash|sh)\s+-c\s+'([^']*)'(?=\s|$|[|&;])\s*.*$""",
    re.DOTALL)

# `python - <<'EOF' [2>&1 | head -N] ... EOF` heredoc payload.
#
# `[^\n]*` between the tag and the newline allows an optional shell redirect
# / pipe on the same line as `<<EOF` before the body starts.
# After the body's closing tag we optionally strip a `2>&1 | tail -N` style
# trailer but only within the same line (`[^\n|]*`, not `[^|]*`) so we
# don't accidentally swallow subsequent commands.
_RE_HEREDOC = re.compile(
    r"""python3?\s+(?:-u\s+)?-\s*<<\s*['"]?(?P<tag>\w+)['"]?"""
    r"""[^\n]*\n"""
    r"""(?P<body>[\s\S]*?)"""
    r"""\n(?P=tag)"""
    r"""(?:\s*(?:2>&1[^\n|]*)?(?:\|[^\n]*)?)?$""",
    re.MULTILINE)

# Tail pipeline (`2>&1 | tail -N | ...`).
#
# CRUCIAL: `[^|\n]*` NOT `[^|]*`. The old version was unbounded on newlines
# so once it started matching `2>&1 | head -80` at the top of a heredoc,
# it would greedily consume EVERY newline afterwards including the body
# and the closing `EOF`, collapsing many-line commands to a 17-char shell
# skeleton and creating a rash of false-positive duplicates. Anchoring to
# same-line-only makes the pipeline trailer local.
_RE_PIPE_TRAIL = re.compile(
    r"""\s*2>&1(?:\s*\|\s*(?:tail|head|sed|grep|awk|cat|wc)\b[^|\n]*)*\s*$""")


_PREFIX_RULES = (
    ("cd",       _RE_CD),
    ("source",   _RE_SOURCE),
    ("export",   _RE_EXPORT),
    ("env-unset", _RE_ENV_UNSET),
    ("env-kv",   _RE_ENV_KV),
    ("nohup",    _RE_NOHUP),
    ("timeout",  _RE_TIMEOUT),
)


def _peel_prefix(s: str, peeled: list[str]) -> str:
    changed = True
    while changed:
        changed = False
        for tag, rx in _PREFIX_RULES:
            m = rx.match(s)
            if m:
                s = s[m.end():]
                peeled.append(tag)
                changed = True
                break
    return s


def _peel_suffix(s: str, peeled: list[str]) -> str:
    m = _RE_PIPE_TRAIL.search(s)
    if m:
        peeled.append("pipe-trail")
        return s[:m.start()]
    return s


# In a bash **double-quoted** context, only these backslash escapes are
# meaningful:  \"  \\  \$  \`  \<newline>  ->  ", \, $, `, (nothing).
# Every other `\X` is left literally as `\X`.
# When we extract the argument of `python -c "..."` we've captured the
# already-shell-quoted string; before feeding it to ast.parse we have to
# reverse those five escapes, otherwise the leaked backslashes look like
# Python line-continuations and every non-trivial payload fails to parse.
_BASH_DQ_ESCAPES = {'"': '"', "\\": "\\", "$": "$", "`": "`"}

def _shell_dq_unescape(s: str) -> str:
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _BASH_DQ_ESCAPES:
                out.append(_BASH_DQ_ESCAPES[nxt])
                i += 2; continue
            if nxt == "\n":
                # `\<newline>` inside double quotes = line continuation,
                # bash drops both.
                i += 2; continue
        out.append(c); i += 1
    return "".join(out)


# In a bash **single-quoted** context there is no escape processing at all;
# single quote itself cannot appear inside `'...'`. So single-quoted
# `bash -c '...'` bodies need no unescaping.
def _shell_sq_unescape(s: str) -> str:
    return s


# ---------------------------------------------------------------------------
# AST walk: structural signature + literal collection
# ---------------------------------------------------------------------------
def _ast_signature(tree: pyast.AST) -> tuple[str, list[str]]:
    """Walk a Python AST; return (structure_signature, string_literals).

    Structure signature is invariant under variable renaming and literal
    value changes -- two programs that differ only in variable names or
    string content produce the same signature. String literals themselves
    are collected separately so the caller can compare *content* on top of
    *structure*.
    """
    struct_parts: list[str] = []
    literals: list[str] = []
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Constant):
            if isinstance(node.value, str):
                literals.append(node.value)
                struct_parts.append("C:str")
            elif isinstance(node.value, bool):
                struct_parts.append("C:bool")
            elif isinstance(node.value, (int, float)):
                struct_parts.append(f"C:{type(node.value).__name__}")
            elif node.value is None:
                struct_parts.append("C:none")
            else:
                struct_parts.append(f"C:{type(node.value).__name__}")
        elif isinstance(node, pyast.Name):
            # variable identity dropped
            struct_parts.append("N")
        elif isinstance(node, pyast.arg):
            struct_parts.append("arg")
        elif isinstance(node, pyast.Attribute):
            # keep the attribute name since `.run_raw` vs `.run_cmd` is
            # semantically meaningful even if the receiver differs
            struct_parts.append(f"A:{node.attr}")
        else:
            struct_parts.append(type(node).__name__)
    sig = "|".join(struct_parts)
    return sig, literals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def canonicalize(command: str) -> dict[str, Any]:
    """Extract a canonical representation of a shell command.

    Returns a dict with:

    - ``core``          — the innermost payload after peeling shell scaffold
    - ``payload_lang``  — one of ``python`` / ``heredoc`` / ``bash`` /
                          ``raw`` (last means nothing was recognized)
    - ``structure_hash``— 16-hex hash over the Python AST signature; empty
                          when payload wasn't parseable as Python
    - ``literals``      — list of string literals from the Python AST
    - ``literals_text`` — literals joined by ``\\n`` (convenient input for
                          a text-embedding model)
    - ``peeled``        — list of layer tags that were stripped, in order
                          (for auditing / debugging)
    - ``raw_len``, ``core_len`` — byte lengths before/after
    """
    raw = command or ""

    # Normalize shell line continuations first so subsequent prefix regex
    # matches don't trip over `&& \<newline>` sequences.
    normalized = _RE_LINE_CONT.sub(" ", raw)
    peeled: list[str] = []
    if normalized != raw:
        peeled.append("line-continuations")

    s = _peel_prefix(normalized, peeled)
    s = _peel_suffix(s, peeled)

    payload_lang = "raw"
    core = s

    m = _RE_PYTHON_C.match(s)
    if m:
        core = _shell_dq_unescape(m.group(1))
        payload_lang = "python"
        peeled.append("python-c")
    else:
        mh = _RE_HEREDOC.search(s)
        if mh:
            core = mh.group("body")
            payload_lang = "heredoc"
            peeled.append("heredoc")
        else:
            mb = _RE_BASH_C.match(s)
            if mb:
                core = _shell_sq_unescape(mb.group(1))
                payload_lang = "bash"
                peeled.append("bash-c")

    structure_hash = ""
    literals: list[str] = []
    if payload_lang in ("python", "heredoc"):
        try:
            tree = pyast.parse(core)
            sig, literals = _ast_signature(tree)
            structure_hash = hashlib.sha1(sig.encode()).hexdigest()[:16]
        except SyntaxError:
            # payload isn't syntactically valid Python -- keep the core but
            # emit no structure hash
            pass

    return {
        "core": core,
        "payload_lang": payload_lang,
        "structure_hash": structure_hash,
        "literals": literals,
        "literals_text": "\n".join(literals),
        "peeled": peeled,
        "raw_len": len(raw),
        "core_len": len(core),
    }
