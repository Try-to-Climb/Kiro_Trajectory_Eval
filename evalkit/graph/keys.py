"""Entity keys extracted from shell command text.

The normalization layer fills `Action.path` for read/write tools, but a shell
tool's parameters are only `command` and `working_dir` -- whatever files the
command touches are buried in its text. Recovering them means parsing, and
parsing means guessing, which is why every edge this module justifies is
`LIKELY` rather than `CERTAIN`: `filter_edges(CERTAIN)` still yields the
subgraph derived purely from structured fields.

How a path is found
-------------------
Two steps, both crude on purpose:

1. A token is a path if it ends in a known extension. `python3.11` is not a
   path because `.11` is not in the whitelist.
2. The direction comes from the first meaningful token to its *left*, looked up
   in three tables (write markers, read verbs, interpreters). A path with
   nothing recognisable to its left is skipped and recorded as a warning --
   guessing would be worse than an isolated node.

Direction is decided per path, not per command, because one command routinely
does both:

    python3 gen.py --out result.yaml
            ^ read          ^ write

Known limits, all deliberate
----------------------------
* Extensionless files (`Makefile`, `/var/log/syslog`) are invisible.
* Complex pipelines can mislead the leftward scan, e.g.
  `cat a.log | python3 x.py > b.log`.
* Unfamiliar option names (`--output-dir`, `-f`) yield no direction.
* Globs and variables (`$LOGFILE`, `$(ls -t ... | head -1)`) are left alone --
  resolving them needs the actual run output, which is Task 5.
* `~` is not expanded. The home directory of the account that ran the agent is
  not in the trace, and guessing it would silently merge or split entities;
  `~/x/y` stays its own key.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# What looks like a path
# ---------------------------------------------------------------------------
# The extension whitelist is what makes a token a path candidate, so its gaps
# are silent: an unlisted extension simply never becomes an entity.
#
# The first version held 18 entries chosen from one Python/YAML project, which
# was overfitting. Measured across six sessions of a C codebase, 1124 tokens
# looked like paths but were rejected on extension alone -- `.c` 583 times and
# `.h` 321. Grouped by what they are, so the next gap is easier to spot.
FILE_EXTENSIONS = (
    # scripts and config
    "py", "sh", "bash", "zsh", "pl", "rb", "yaml", "yml", "json", "jsonl",
    "toml", "cfg", "ini", "conf", "properties", "env",
    # C / C++ / native, including this codebase's interface files
    "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "ifx", "cbc", "cig", "s", "asm",
    # other languages
    "java", "go", "rs", "ts", "tsx", "js", "jsx", "kt", "scala",
    # documents and data
    "md", "rst", "txt", "csv", "tsv", "xml", "html", "htm", "svg", "sql",
    # build and version control artifacts
    "mk", "cmake", "bzl", "bazel", "gradle", "diff", "patch",
    # runtime output
    "log", "out", "err", "trace", "dump", "sig", "head", "pid", "lock",
    # domain-specific
    "mos",
)
_EXT_RE = re.compile(r"\.(" + "|".join(FILE_EXTENSIONS) + r")$", re.IGNORECASE)

# Device and pseudo files are not artifacts; a redirect to /dev/null is noise.
_SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/")


# A token holding a shell variable or command substitution cannot be resolved
# without the run's output (Task 5).
_UNRESOLVED_RE = re.compile(r"[$*?]|\$\(")

# ---------------------------------------------------------------------------
# What the token to the left means
# ---------------------------------------------------------------------------
WRITE_MARKS = frozenset({">", ">>", "tee", "-o", "--out", "--output", "-out"})
READ_VERBS = frozenset({
    "cat", "tail", "head", "less", "more", "grep", "egrep", "zgrep",
    "source", ".", "<", "wc", "diff", "md5sum", "sha256sum", "awk", "sed",
    "py_compile",
})
_INTERPRETER_RE = re.compile(
    r"^(python[\d.]*|python3|bash|sh|zsh|node|perl|ruby|source|\.)$")

# `chmod +x script.sh` alters the file, so the target is a write. Matching the
# mode argument alone is not enough -- `[+-][rwxXstugoa]+` also matches ordinary
# short options such as python's `-u`, which silently turned 60 reads into
# writes. The command must actually contain `chmod`.
_CHMOD_RE = re.compile(r"\bchmod\b")
_CHMOD_MODE_RE = re.compile(r"^[+-][rwxXstugoa]+$|^[0-7]{3,4}$")

# A command substitution wrapping a read verb, e.g. `LOGFILE=$(cat path.txt)`.
# The whole thing arrives as one token because `$(` never gets spaced out.
_SUBST_READ_RE = re.compile(r"\$\((cat|head|tail|less)$")

# `VAR="/abs/path.log"` -- a bare assignment naming a file. The path is
# complete, but an assignment states no direction, so the token is skipped and
# only its *value* is kept (see `shell_vars`), letting the later `$VAR` use site
# decide. It was briefly treated as a write, on the evidence of launches like
# `LOGFILE=... && nohup ... > "$LOGFILE"`; every poll command has the same shape
# with `tail "$LOGFILE"` instead, so that inverted four edges on the gold set and
# suppressed the four real reads with them.
_ASSIGN_PATH_RE = re.compile(r"^(\w+)=[\"']?(/[^\"'\s]+)[\"']?$")

# `sed -i` rewrites in place, so its target is a write even though `sed` is in
# READ_VERBS. Detected on the whole command rather than the neighbouring token,
# because the neighbour is the substitution expression.
_SED_INPLACE_RE = re.compile(r"\bsed\s+(-[a-zA-Z]*i[a-zA-Z]*|--in-place)\b")

# Verbs whose first argument is an expression, not a file: `grep PATTERN file`,
# `awk '{...}' file`, `sed 's/a/b/' file`. The leftward scan stops at the
# expression, which is in no table, so these are matched on the command instead.
# Missing this was hidden for a while by the sed branch wrongly claiming every
# path in such a command -- one bug masking another.
_EXPR_VERB_RE = re.compile(r"\b(grep|egrep|zgrep|fgrep|awk|sed)\b")

READ = "read"
WRITE = "write"


def _flatten(command: str) -> str:
    """Put every token on one line while keeping command boundaries.

    A newline ends a command exactly as `;` does, so outside quotes it becomes
    an explicit separator. Collapsing it to a space instead let a verb reach
    across lines: `chmod +x a.sh` on one line and `python3 -u b.py` on the next
    landed in one segment, the chmod guard saw its keyword, and `-u` was read as
    a file mode.

    Two kinds of newline are *not* boundaries and still collapse to a space:

    * one preceded by a backslash -- a continuation of the same command. Split
      there and `suite.yaml` is stranded in its own segment while the
      interpreter stays behind on the previous line, so the argument is lost.
    * one inside a quoted argument, which is data rather than shell syntax.
      `python3 -c "…multi-line script…"` is the common case, and cutting it into
      segments hid the interpreter from the paths the script opens -- four real
      reads disappeared from one measured session.
    """
    s = re.sub(r"\\\s*\n", " ", command)
    out: list[str] = []
    quote: Optional[str] = None
    for ch in s:
        if ch == "\n":
            out.append(" " if quote else " ; ")
            continue
        if quote is None and ch in "\"'":
            quote = ch
        elif ch == quote:
            quote = None
        out.append(ch)
    return "".join(out)


# A command substitution can contain spaces -- `$(date +%Y%m%d_%H%M%S)` -- so
# splitting on whitespace tears a path like `/a/run_$(date +%s).log` into
# fragments, and the `$` ends up in a different token than the path it belongs
# to. Task 5 relies on spotting that `$`, so substitutions are collapsed to a
# single placeholder token before splitting and restored afterwards.
_SUBST_SPAN_RE = re.compile(r"\$\([^()]*\)")
_SUBST_PLACEHOLDER = "\x00SUBST\x00"


def _protect_substitutions(s: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def take(m: re.Match) -> str:
        saved.append(m.group(0))
        return _SUBST_PLACEHOLDER

    # Innermost-first so nested `$( $( ) )` collapses fully.
    prev = None
    while prev != s:
        prev = s
        s = _SUBST_SPAN_RE.sub(take, s)
    return s, saved


# A quoted argument is one word to the shell, and its contents are data. Left
# alone, `grep -i "a\|ERROR\|no b" /x.log` splits on the `\|` inside the quotes,
# the segment scan starts after a phantom pipe, and `grep` is no longer visible
# to the path that follows -- so a real read went unrecorded.
_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_QUOTE_PLACEHOLDER = "\x00QUOTE\x00"


def _protect_quotes(s: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def take(m: re.Match) -> str:
        saved.append(m.group(0))
        return _QUOTE_PLACEHOLDER

    return _QUOTED_SPAN_RE.sub(take, s), saved


def _restore_substitutions(tokens: list[str], saved: list[str]) -> list[str]:
    it = iter(saved)
    out: list[str] = []
    for t in tokens:
        while _SUBST_PLACEHOLDER in t:
            t = t.replace(_SUBST_PLACEHOLDER, next(it, "$()"), 1)
        out.append(t)
    return out


def _restore_quotes(tokens: list[str], saved: list[str]) -> list[str]:
    it = iter(saved)
    out: list[str] = []
    for t in tokens:
        while _QUOTE_PLACEHOLDER in t:
            t = t.replace(_QUOTE_PLACEHOLDER, next(it, '""'), 1)
        out.append(t)
    return out


# A heredoc body is the *content being written*, not part of the command that
# writes it. Parsing it produces nonsense: a `chmod` inside a generated script
# made the outer command look like a chmod, which in turn let python's `-u` be
# read as a file mode. Everything from the delimiter to the closing marker is
# dropped, keeping only `cat > target.sh` itself.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def strip_heredoc(command: str) -> str:
    """Remove heredoc bodies, leaving the command that introduces them."""
    head, body, tail = _split_heredoc(command)
    if body is None:
        return command
    return head + " " + tail


def _split_heredoc(command: str) -> tuple[str, Optional[str], str]:
    """`(head, body, tail)` around the first heredoc, or `(command, None, "")`."""
    m = _HEREDOC_RE.search(command)
    if not m:
        return command, None, ""
    delim = m.group(1)
    head = command[: m.start()]
    rest = command[m.end():]
    # Resume after the closing delimiter if it is there; otherwise the body runs
    # to the end and there is nothing to resume.
    close = re.search(rf"(?:^|\n)\s*{re.escape(delim)}\s*(?:$|\n)", rest)
    if close is None:
        return head, rest, ""
    return head, rest[: close.start()], rest[close.end():]


def expand_heredoc(command: str) -> str:
    """Command text to scan, keeping a heredoc body only when it is executed.

    A heredoc body is content being written, so its reads and writes have not
    happened -- unless this same command runs the file it just wrote, which is
    the shape of every write-then-run in the reference sessions:

        cat > run.sh << 'EOF' … EOF ; chmod +x run.sh ; nohup run.sh &

    Then the body's effects are this action's, and dropping them is expensive:
    on the gold set it accounted for 20 of 44 missed edges, including the whole
    dependency set of the one launch attempt that finally worked.

    The body is spliced in as its own `;`-separated segments, never merged into
    the outer command, so a verb inside the script cannot claim an outer path.
    Only the first heredoc is handled, as before.
    """
    head, body, tail = _split_heredoc(command)
    if body is None:
        return command
    written = [t for t in head.split() if looks_like_path(t)]
    if written and any(w in set(tail.split()) for w in written):
        return head + " ; " + body + " ; " + tail
    return head + " ; " + tail


def tokenize(command: str) -> list[str]:
    """Split a command into tokens, detaching redirections.

    `>file` and `2>/dev/null` are one token to `str.split`, which would hide
    the redirect from the leftward scan, so they are spaced out first.
    """
    s = _flatten(expand_heredoc(command))
    s, subst = _protect_substitutions(s)
    s, quoted = _protect_quotes(s)
    s = re.sub(r"(\d?)(>>|>|<)", r" \1\2 ", s)     # detach redirects
    s = s.replace("|", " | ").replace(";", " ; ").replace("&&", " && ")
    toks = [t for t in s.split() if t]
    return _restore_substitutions(_restore_quotes(toks, quoted), subst)


def looks_like_path(token: str) -> bool:
    """A token is treated as a path when it ends in a known extension."""
    t = token.strip("\"'`,()")
    if not t or t in {">", ">>", "<"}:
        return False
    return bool(_EXT_RE.search(t))


# A bare variable reference -- `"$LOGFILE"`, `${LOG}` -- has no extension, so
# `looks_like_path` cannot see it, yet it is one of the commonest ways a command
# names a file (`tail -50 "$LOGFILE"`). It carries no information on its own;
# only Task 5, with the recorded output in hand, can say what it pointed at.
_VAR_ONLY_RE = re.compile(r"^\$\{?\w+\}?$")


def is_variable_reference(token: str) -> bool:
    """Whether a token is nothing but a variable reference."""
    return bool(_VAR_ONLY_RE.match(token.strip("\"'`,()")))


def normalize_path(literal: str, working_dir: Optional[str]) -> Optional[str]:
    """Turn a path as written in a command into the form entity keys use.

    Relative paths are resolved against `working_dir` -- 84 of the 188 commands
    in the reference session write paths relative to it, and without this they
    would never match an entity created from `Action.path`.

    Never touches the filesystem: evaluation can run days after the run, so
    `normpath` (pure string folding) is used rather than `realpath` (which
    resolves symlinks and needs the file to exist).

    Returns None for anything that should not become an entity.
    """
    t = literal.strip("\"'`,()")
    if not t:
        return None
    # Reject anything a path cannot plausibly be: newlines or control chars
    # mean we grabbed the body of a heredoc / multi-line quoted argument
    # (seen in `source ... ; echo "GITENV DONE"` style blocks), and letting
    # those through produces entity keys that break DOT's quoted-string
    # parser downstream (`syntax error near '@'`). Length cap guards against
    # multi-KB python heredocs the AST walker occasionally mis-attributes.
    if any(ch == "\n" or ch == "\r" or (ch < " " and ch != "\t") for ch in t):
        return None
    if len(t) > 1024:
        return None
    if _UNRESOLVED_RE.search(t):        # $VAR, $(...), globs -> Task 5
        return None
    if t.startswith(_SKIP_PREFIXES):
        return None
    if t.startswith("~"):
        # Deliberately unexpanded; see the module docstring.
        return t
    if not t.startswith("/"):
        if not working_dir:
            return None                  # a relative path with no anchor
        t = os.path.join(working_dir, t)
    return os.path.normpath(t)


def _is_interpreter(token: str) -> bool:
    """Whether a token runs a script, compared on the basename.

    `_INTERPRETER_RE` anchors on the whole token, so a venv interpreter named by
    absolute path -- `/home/u/python311_env/bin/python` -- never matched. The
    effect was not one lost edge but all of them: the rules that make a script
    and its arguments reads key off recognising the interpreter, so three
    launches in the gold set contributed nothing at all, six edges, purely
    because the interpreter was spelled out in full.
    """
    t = token.strip("\"'`,()")
    return bool(_INTERPRETER_RE.match(t.rsplit("/", 1)[-1]))


def _left_neighbour(tokens: list[str], i: int,
                    is_chmod: bool = False) -> Optional[str]:
    """First meaningful token left of `tokens[i]`.

    Options are skipped so `python3 -u script.py` still sees the interpreter,
    but an option that is itself a marker (`--out`, `+x`) stops the scan.

    Prefixes like `nohup` and `timeout` need no special handling: they always
    sit in front of the interpreter, never in front of the path, so the scan
    reaches the interpreter first. An earlier version passed through them
    explicitly, which changed nothing on 188 real commands.
    """
    j = i - 1
    while j >= 0:
        t = tokens[j]
        if t in WRITE_MARKS or t in READ_VERBS or _is_interpreter(t):
            return t
        if is_chmod and _CHMOD_MODE_RE.match(t):
            return t
        if _SUBST_READ_RE.search(t):
            return t
        if t in {"|", ";", "&&"}:
            return None                  # a new command segment starts here
        if not t.startswith("-"):
            return t
        j -= 1
    return None


def classify(tokens: list[str], i: int, command: str) -> Optional[str]:
    """Direction for `tokens[i]`, or None when nothing recognisable precedes it.

    Every rule is scoped to the current segment. `command` is still accepted so
    callers need not change, but direction no longer depends on text outside
    the segment: asking "does the command contain chmod" let a `chmod` anywhere
    -- including inside a heredoc body being written to a file -- turn a later
    `-u` into a file mode.
    """
    seg = _segment_before(tokens, i)
    is_chmod = bool(_CHMOD_RE.search(seg))
    if _SED_INPLACE_RE.search(seg):
        # `sed -i 's/a/b/' target` -- the neighbour is the substitution
        # expression, not `sed`, so the leftward scan cannot recognise it. Scoped
        # to the current segment so a later `| tail x.log` is not claimed too.
        return WRITE
    prev = _left_neighbour(tokens, i, is_chmod)
    if prev is None:
        return None
    if prev in WRITE_MARKS:
        return WRITE
    if is_chmod and _CHMOD_MODE_RE.match(prev):
        return WRITE                     # chmod alters the file
    if prev in READ_VERBS:
        return READ
    if _SUBST_READ_RE.search(prev):
        return READ                      # VAR=$(cat path)
    if _is_interpreter(prev):
        return READ                      # executing a script reads it
    if _EXPR_VERB_RE.search(seg):
        return READ                      # grep PATTERN file / awk '{...}' file
    if looks_like_path(prev) and _script_is_executed(tokens, i):
        # `python3 run.py suites/case.yaml` -- an argument to a script the
        # command is running. The script consumes it, so from this command's
        # point of view it is read. The largest single gap on real data (41
        # occurrences) before this rule existed.
        return READ
    return None


def _script_is_executed(tokens: list[str], i: int) -> bool:
    """Whether an interpreter appears earlier in this command segment.

    Guards the "argument to a script" rule: without it, two adjacent paths in
    any position would chain into a read, e.g. `diff a.log b.log` has no
    interpreter and its direction is already handled by READ_VERBS.
    """
    j = i
    while j > 0 and tokens[j - 1] not in {"|", ";", "&&"}:
        j -= 1
        if _is_interpreter(tokens[j]):
            return True
    return False


def _segment_before(tokens: list[str], i: int) -> str:
    """Tokens from the start of the current command segment up to `tokens[i]`.

    Segments are split on `|`, `;` and `&&`, so a verb in an earlier segment
    cannot claim a path in a later one.
    """
    j = i
    while j > 0 and tokens[j - 1] not in {"|", ";", "&&"}:
        j -= 1
    return " ".join(tokens[j:i])


def extract(command: str, working_dir: Optional[str] = None,
            session_env: Optional[Mapping[str, str]] = None
            ) -> list[tuple[str, str, str]]:
    """Paths a command touches, as `(absolute_path, direction, evidence)`.

    Deduplicated on (path, direction): the same file mentioned twice the same
    way is one dependency, not two.

    Command substitutions are also scanned on their own. `LOGFILE=$(cat
    /tmp/logpath.txt)` holds a real read inside a token that, taken whole, looks
    unresolvable -- collapsing substitutions to keep `$` attached to its path
    would otherwise hide those.
    """
    if not command:
        return []
    out: dict[tuple[str, str], str] = {}
    _scan(command, working_dir, out, session_env)
    for inner in _SUBST_SPAN_RE.findall(_flatten(strip_heredoc(command))):
        _scan(inner[2:-1], working_dir, out, session_env)     # drop the `$(` and `)`
    return [(p, d, ev) for (p, d), ev in out.items()]


# A shell variable assigned in the same command is not a guess: the value is
# right there in the text. Measured across five sessions, 195 path tokens begin
# with a variable that the very same command assigns -- the dominant extraction
# gap once genuinely dependency-free commands are set aside.
#
#   KIRO_HOME="/work/bb/.kiro" ... cat $KIRO_HOME/settings/info.json
#   └──── assigned here ─────┘          └── used here ──┘
#
# Only literal values are taken. An assignment whose own value needs resolving
# (`D=$(mktemp -d)`, `SB=$BASE/x`) is left alone rather than half-expanded.
_VAR_ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.+)$")
_VAR_USE_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")


def shell_vars(command: str) -> dict[str, str]:
    """Literal variable assignments visible in one command."""
    out: dict[str, str] = {}
    for tok in tokenize(command):
        m = _VAR_ASSIGN_RE.match(tok.strip())
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip("\"'")
        if "$" in value or _SUBST_PLACEHOLDER in value or "`" in value:
            continue                  # not literal; expanding it would be a guess
        out.setdefault(name, value)
    return out


def assigned_names(command: str) -> set[str]:
    """Every variable this command assigns, literal value or not.

    Needed to *forget* values, which matters more than remembering them. A
    launch reassigns `LOGFILE="…$(date …).log"`; the value is unreadable, so
    `shell_vars` rightly skips it -- but a value carried from an earlier turn
    then survived, and `> "$LOGFILE"` was resolved against a log belonging to a
    different test run. Wrong edges to real files are worse than no edges.
    """
    return {m.group(1) for m in
            (_VAR_ASSIGN_RE.match(t.strip()) for t in tokenize(command)) if m}


# `echo $KIRO_HOME` states the value of a variable the command never assigned,
# because it came from the process environment. One session echoes it four
# times; without this the value is unknowable from the record.
_ECHO_VAR_RE = re.compile(r'^\s*echo\s+"?\$\{?([A-Za-z_]\w*)\}?"?\s*$')


def echoed_var(command: str, response: Any) -> Optional[tuple[str, str]]:
    """A variable's value as reported by `echo $VAR`."""
    if not command:
        return None
    m = _ECHO_VAR_RE.match(command.strip())
    if not m:
        return None
    first = response_text(response).strip().split("\n")[0].strip()
    if not first or "$" in first or len(first.split()) > 1:
        return None
    return m.group(1), first


def expand_vars(token: str, env: Mapping[str, str]) -> Optional[str]:
    """Substitute known variables, or None if any remain unresolved."""
    if "$" not in token:
        return token
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        val = env.get(m.group(1))
        if val is None:
            missing = True
            return m.group(0)
        return val

    out = _VAR_USE_RE.sub(sub, token)
    return None if missing or "$" in out else out


def referenced_names(token: str) -> set[str]:
    """Every `$VAR` name referenced in `token`.

    Used by `shell_ast` (DESIGN §7.13) to decide if an unresolved variable
    path is backed only by names that are never locally reassigned, i.e. a
    stable process-env global like `$KIRO_HOME`. Does not match special
    variables (`$$`, `$1`, `$?`) -- the regex only accepts `[A-Za-z_]\\w*`.
    """
    return {m.group(1) for m in _VAR_USE_RE.finditer(token)}


def _scan(command: str, working_dir: Optional[str],
          out: dict[tuple[str, str], str],
          session_env: Optional[Mapping[str, str]] = None) -> None:
    tokens = tokenize(command)
    # A local assignment wins over one carried from earlier in the session, and a
    # local assignment whose value cannot be read *invalidates* the carried one.
    env = dict(session_env or {})
    for name in assigned_names(command):
        env.pop(name, None)
    env.update(shell_vars(command))
    for i, tok in enumerate(tokens):
        if _ASSIGN_PATH_RE.match(tok):
            # `VAR="/abs/path.log"` names a file but carries no direction. The
            # identical form is an output in `LOGFILE=… && … > "$LOGFILE"` and an
            # input in `LOGFILE=… && tail "$LOGFILE"`; calling it a write was
            # measured on launches alone and inverted every poll. Worse, because
            # results key on (path, direction), the wrong direction also
            # suppressed the real read that followed. `shell_vars()` records the
            # value, and direction now comes from how `$VAR` is used.
            continue
        # Expansion comes before the path test, not after. A bare `"$LOGFILE"`
        # has no extension, so `looks_like_path` rejected it and the token never
        # reached the expansion below -- which left the poll commands with no
        # edge at all once the assignment stopped being counted as a write.
        resolved, note = tok, ""
        if "$" in tok:
            expanded = expand_vars(tok.strip("\"'"), env)
            if expanded is None:
                continue              # variable unknown; Task 5 may resolve it
            resolved, note = expanded, f" (expanded from {tok!r})"
        if not looks_like_path(resolved):
            continue
        direction = classify(tokens, i, command)
        if direction is None:
            continue
        path = normalize_path(resolved, working_dir)
        if path is None:
            continue
        prev = _left_neighbour(
            tokens, i,
            bool(_CHMOD_RE.search(_segment_before(tokens, i)))) or "?"
        out.setdefault((path, direction),
                       f"command literal {resolved!r} after {prev!r}{note}")


def unresolved(command: str) -> list[str]:
    """Path-shaped tokens skipped because they hold a variable or glob.

    Reported so the gap is visible rather than silent; Task 5 resolves these
    from the recorded output.
    """
    if not command:
        return []
    found: list[str] = []
    for tok in tokenize(command):
        if looks_like_path(tok) and _UNRESOLVED_RE.search(tok.strip("\"'`,()")):
            found.append(tok)
    return found


def extract_from_action(a: Mapping[str, Any],
                        session_env: Optional[Mapping[str, str]] = None
                        ) -> list[tuple[str, str, str]]:
    """`extract` applied to a normalized Action dict.

    `working_dir` lives in `Action.args` -- the normalization layer keeps tool
    parameters verbatim rather than promoting shell-only fields to the top
    level.
    """
    args = a.get("args") or {}
    # `cwd` is the effective working directory the normalization layer resolved
    # (command `cd` > working_dir arg > process cwd). Fall back to raw args so
    # hand-built test dicts without a top-level cwd still work.
    working_dir = (a.get("cwd") or args.get("working_dir") or args.get("cwd"))
    command = a.get("command") or ""
    return extract(command, working_dir, session_env)


# ===========================================================================
# Resolving what the command text could not say (Task 5)
# ===========================================================================
# A command writes `> run_$(date +%s).log` or reads `"$LOGFILE"`; the concrete
# value only exists at run time. It is, however, recorded: the command's own
# output usually reports it, because the agent echoes such values precisely so
# later steps can refer to them.
#
# The response is used *only to fill in the blank*. Direction still comes from
# where the unresolved token sat in the command, using the same rules as Task 4.
# Reading direction off the response instead would be wrong -- three commands in
# the reference session print a concrete log path while writing it, merely
# locating it, and reading it respectively.

# `{"Success": {"items": [{"Json": {"exit_status": .., "stdout": .., "stderr": ..}}]}}`
def response_text(response: Optional[str]) -> str:
    """stdout and stderr out of a recorded tool result.

    Falls back to the raw string when the envelope is not the expected shape,
    so an unrecognised format degrades instead of raising.
    """
    if not response:
        return ""
    try:
        import json
        d = json.loads(response)
    except Exception:
        return response
    if not isinstance(d, dict):
        return response
    out: list[str] = []
    for wrapper in ("Success", "Error"):
        items = (d.get(wrapper) or {}).get("items") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            payload = it.get("Json")
            if isinstance(payload, dict):
                for field in ("stdout", "stderr"):
                    v = payload.get(field)
                    if isinstance(v, str) and v:
                        out.append(v)
            elif isinstance(it.get("Text"), str):
                out.append(it["Text"])
    return "\n".join(out) if out else response


# Only three line shapes are trusted. Everything else in stdout is content the
# command happened to print -- a `tail` of a log file emits whatever the log
# contains, and a traceback in there names source files the command never
# touched. Scanning the whole blob would treat those as dependencies.
_RESP_ASSIGN_RE = re.compile(r"^\s*(\w+)=(/[^\s\"']+)\s*$")     # LOG=/abs/path
_RESP_BARE_PATH_RE = re.compile(r"^\s*(/[^\s\"']+)\s*$")        # ls output
_RESP_PID_RE = re.compile(r"^\s*(\w*PID\w*)=(\d{3,})\s*$", re.IGNORECASE)
_RESP_CREATED_RE = re.compile(
    r"Successfully (?:created|replaced \d+ occurrence\(s\) in) (\S+)")


def response_paths(response: Optional[str]) -> list[str]:
    """Absolute paths the output states, line by line.

    Line-anchored on purpose: `a[16]` prints `LOG=<path>` and then dumps a
    traceback naming other files, and only the first line is a statement about
    what this command touched.
    """
    seen: dict[str, None] = {}
    for line in response_text(response).split("\n"):
        for rx, group in ((_RESP_ASSIGN_RE, 2), (_RESP_BARE_PATH_RE, 1)):
            m = rx.match(line)
            if m and _EXT_RE.search(m.group(group)):
                seen.setdefault(os.path.normpath(m.group(group)), None)
        m = _RESP_CREATED_RE.search(line)
        if m:
            cand = m.group(1).rstrip(".,")
            if cand.startswith("/"):
                seen.setdefault(os.path.normpath(cand), None)
    return list(seen)


def response_pids(response: Optional[str]) -> list[str]:
    """Process ids the output reports, e.g. `PID=3301766`."""
    out: dict[str, None] = {}
    for line in response_text(response).split("\n"):
        m = _RESP_PID_RE.match(line)
        if m:
            out.setdefault(m.group(2), None)
    return list(out)


def _resolvable_prefix(token: str) -> str:
    """The literal head of a token, up to the first variable or wildcard.

    `/a/b/run_$(date).log` -> `/a/b/run_`;  `"$LOGFILE"` -> `""`.
    """
    t = token.strip("\"'`,()")
    cut = len(t)
    for ch in ("$", "*", "?"):
        i = t.find(ch)
        if i != -1:
            cut = min(cut, i)
    return t[:cut]


def resolve_with_response(command: str, working_dir: Optional[str],
                          response: Optional[str]
                          ) -> list[tuple[str, str, str]]:
    """Paths that only the recorded output can pin down.

    For each token skipped by `extract` (variable or glob), a concrete path from
    the output is matched to it and given the direction the token's position
    implies. Two matching strategies, tried in order:

    1. Prefix -- `/a/run_$(date).log` against `/a/run_162941.log`. Handles
       `$(...)` and globs, and stays specific enough to be safe.
    2. Sole candidate -- a token with no literal head at all (`"$LOGFILE"`)
       resolves only when the output states exactly one path with a matching
       extension. Anything more ambiguous is left alone.
    """
    if not command:
        return []
    tokens = tokenize(command)
    candidates = response_paths(response)
    if not candidates:
        return []

    is_chmod = bool(_CHMOD_RE.search(strip_heredoc(command)))
    out: dict[tuple[str, str], str] = {}
    for i, tok in enumerate(tokens):
        bare = tok.strip("\"'`,()")
        var_only = is_variable_reference(tok)
        if not var_only:
            if not looks_like_path(tok):
                continue
            if not _UNRESOLVED_RE.search(bare):
                continue                   # `extract` already handled it
        direction = classify(tokens, i, command)
        if direction is None:
            continue

        prefix = "" if var_only else _resolvable_prefix(tok)
        if prefix and prefix.startswith("/"):
            matched = [c for c in candidates if c.startswith(prefix)]
            how = "prefix"
        elif var_only:
            # No literal head at all: only safe when the output names exactly
            # one path.
            matched = candidates
            how = "sole candidate"
        else:
            ext = _EXT_RE.search(bare)
            matched = [c for c in candidates
                       if ext and c.lower().endswith(ext.group(0).lower())]
            how = "sole candidate"
        if len(matched) != 1:
            continue                        # ambiguous; leave it unresolved
        prev = _left_neighbour(tokens, i, is_chmod) or "?"
        out.setdefault((matched[0], direction),
                       f"response resolved {tok!r} after {prev!r} ({how})")
    return [(p, d, ev) for (p, d), ev in out.items()]


def resolve_from_action(a: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """`resolve_with_response` applied to a normalized Action dict."""
    args = a.get("args") or {}
    return resolve_with_response(a.get("command") or "",
                                 args.get("working_dir") or args.get("cwd"),
                                 a.get("response"))
