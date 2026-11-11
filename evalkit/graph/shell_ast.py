"""Entity keys parsed from a shell command's *syntax tree*.

`keys.py` finds paths by flattening a command into one token stream and reading
each path's left-hand neighbour. That works, but every shell construct that
groups tokens has to be re-discovered with a regex, and the regexes interfere:
a `chmod` inside a heredoc body once made an outer `python3 -u` look like a file
mode, and the fix -- dropping heredoc bodies wholesale -- cost 20 of 44 edges on
the gold set. Measured on 1751 real commands, 22.6% contain a heredoc and 86% of
those feed a script to an interpreter's stdin, which the regex path cannot see at
all.

tree-sitter-bash answers the structural questions directly:

    redirected_statement
      command          'python3'        <- who receives the heredoc
      heredoc_redirect
        heredoc_body   'import json…'   <- exact bounds, no delimiter matching

Division of labour, which this module keeps strict:

* the tree decides *structure* -- which command a token belongs to, what
  position it holds, where a heredoc ends and who it feeds, what is a comment;
* this module decides *semantics* -- `tail` reads, `chmod` writes, `python3`
  executes its stdin. That table cannot come from a parser and is shared with
  `keys.py` rather than duplicated.

Parsing failed on 1.5% of those commands (mostly >2000 chars of Python embedded
in a heredoc), so `extract()` reports failure and the caller falls back to
`keys.extract`.
"""
from __future__ import annotations

import ast
from typing import Any, Iterable, Mapping, Optional

from normalize import mapping

from . import keys

__all__ = ["available", "extract", "skipped_paths", "weak_paths",
           "hosts_touched",
           "ParseFailure"]


class ParseFailure(RuntimeError):
    """Raised when the command is not valid enough shell to build a tree."""


def _load_parser():
    """Import tree-sitter lazily so the package still imports without it."""
    try:
        import tree_sitter_bash
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    return Parser(Language(tree_sitter_bash.language()))


_PARSER = _load_parser()


def available() -> bool:
    """Whether the AST path can run at all."""
    return _PARSER is not None


# Commands that wrap another command rather than doing anything themselves. The
# real verb is further right: `nohup env PYTHONPATH=… python3 -u run.py` has to
# resolve to `python3`, which is exactly the shape of every launch in the
# reference session.
WRAPPERS = frozenset({
    "nohup", "env", "timeout", "sudo", "time", "exec", "stdbuf", "unbuffer",
    "ionice", "nice", "setsid", "xargs", "command",
})

# Commands whose argument is a file no matter what it is called. Position proves
# it, so the extension whitelist does not get a vote -- this is what recovers
# `source …/bin/activate`, which has no extension at all.
NAMES_TAKING_A_FILE = frozenset({"source", ".", "bash", "sh", "zsh"})

# Deletion is a real effect but the model has no edge kind for it, so `rm` is
# recorded as touching nothing rather than guessed at.
IGNORED_NAMES = frozenset({"rm", "rmdir", "mkdir", "touch", "ls", "stat", "test"})

# `git diff x.md` reads, `git add x.md` writes: the verb is the subcommand.
VCS_NAMES = frozenset({"git", "hg", "svn"})

# `timeout 240 python3 …`, `timeout 1.5s …` -- a wrapper's own argument, not the
# command it wraps.
import re as _re                                            # noqa: E402
_DURATION_RE = _re.compile(r"^\d+(\.\d+)?[smhd]?$")


def _is_glob(value: str) -> bool:
    """Whether a resolved value is a shell pattern rather than a name.

    Asked of the whole value at first, which reported `python3 -c "<700 characters
    of python>"` as a glob: the script indexes dictionaries, so it is full of `[`.
    A pattern is one path-like word: anything carrying a newline, a tab or a space
    is not one, whatever characters it contains. That alone is enough -- the case
    that mattered was a 700-character python program, and every real glob we have
    seen is a single word.
    """
    if not value or len(value) > 400:
        return False
    if any(ch in value for ch in "\n\t "):
        return False
    return any(ch in value for ch in "*?[")


def _redirects_of(stmt):
    """Every redirect belonging to a statement, in source order.

    A `file_redirect` written after a heredoc is a child of the `heredoc_redirect`
    rather than of the statement, so the statement's own children are not the whole
    list. `python3 <<'PY' 2>&1` lost its `2>&1` to that; the same command with the
    order reversed never did.
    """
    out = []
    for child in stmt.children:
        if child.type == "file_redirect":
            out.append(child)
        elif child.type == "heredoc_redirect":
            out.append(child)
            out.extend(x for x in child.children if x.type == "file_redirect")
    return sorted(out, key=lambda n: n.start_byte)


def _owner_of_redirect(stmt, redirect):
    """The command a redirect applies to: the last one starting before it.

    Position, not "the last command in the statement". Three shapes defeat the
    latter, all of them real:

      * `a | b > c` -- the redirect is the pipeline's, and belongs to `b`;
      * `cd x && LOG=$(date) && nohup … > f` -- a breadth-first scan reached the
        `date` inside the assignment last, and filed `> f` against a command that
        leaves no record, so the redirect vanished;
      * `mkdir && nohup … > f & echo …; echo …; ls … | head -1` -- tree-sitter
        makes this one statement, so the last command in it is `head`, three
        commands after the one the shell redirects.

    Four of the first eight commands a judge looked at were reported for the
    second and third of those.
    """
    cut = redirect.start_byte
    best = None
    stack = [(stmt, True)]
    while stack:
        cur, top = stack.pop()
        if cur.type == "command":
            if cur.start_byte < cut and (best is None
                                         or cur.start_byte > best.start_byte):
                best = cur
            continue
        if cur.type == "redirected_statement" and not top:
            # An inner `a > b` owns its own redirect; its commands are not
            # candidates for the outer one.
            continue
        if cur.type in ("command_substitution", "process_substitution"):
            # `> …_$(date +%s).log 2>&1`: the `date` inside the filename starts
            # before the `2>&1` and would otherwise be the nearest candidate, so
            # `2>&1` was filed against a command that only computes a name.
            continue
        stack.extend((c, False) for c in cur.children)
    return best


# ---- a script fed to an interpreter ---------------------------------------
#
# The body of `python3 <<PY` is a program, and the paths it touches are edges of
# the run like any other. Read with `ast` rather than with a regex over quoted
# strings, because the question is structural -- *is this literal the first
# argument of `open`* -- and a regex can only ask whether the word `open` happens
# to be nearby. Measured on three sessions, the regex answered wrong 7 times out
# of 15: `f.endswith('.jsonl')` became a file called `/.jsonl`, a Chinese sentence
# describing the script became a path, and `open(p + '/s9_final_report.json')`
# became a file at the root of the filesystem.

_WRITE_METHODS = frozenset({
    "write_text", "write_bytes", "writelines", "to_csv", "to_json",
    "to_parquet", "savefig", "write",
})
_READ_METHODS = frozenset({"read_text", "read_bytes", "readlines", "read"})
_READ_FUNCS = frozenset({"read_csv", "read_json", "read_parquet", "read_excel"})

# `np.load("/w/x.npy")`, `torch.save(model, "/w/m.pt")`: the path can be in either
# position, so any argument that resolves to something path-shaped is taken. The
# extension is required here and nowhere else, because `yaml.safe_load(text)` is
# the same shape with a document rather than a filename in it.
_LOAD_FUNCS = frozenset({"load", "safe_load", "full_load", "loads_file"})
_SAVE_FUNCS = frozenset({"save", "savez", "savetxt", "dump_file"})
_WRITE_MODES = ("w", "a", "x", "+")


def _py_env_lookup(node, shell_env: Mapping[str, str]) -> Optional[str]:
    """`os.environ['KIRO_HOME']`, `os.environ.get('X')`, `os.getenv('X')`.

    Resolved against the shell's own ledger, which is the same value the script
    will see: the variable was exported by an earlier command in the run. Four
    edges in one session hung on this -- `os.environ['KIRO_HOME'] + "/.state/..."`
    where the regex kept the suffix and dropped the prefix, naming a file at the
    root of the filesystem.
    """
    if isinstance(node, ast.Subscript):                 # os.environ['X']
        if isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            key = _py_value(node.slice, {}, {})
            return shell_env.get(key) if key else None
        return None
    if isinstance(node, ast.Call):
        name = _py_callee(node)
        if name in ("getenv", "get") and node.args:
            recv = node.func.value if isinstance(node.func, ast.Attribute) else None
            ok = name == "getenv" or (
                isinstance(recv, ast.Attribute) and recv.attr == "environ")
            if ok:
                key = _py_value(node.args[0], {}, {})
                return shell_env.get(key) if key else None
    return None


def _py_value(node, env: Mapping[str, str],
              shell_env: Mapping[str, str]) -> Optional[str]:
    """A string this expression is known to evaluate to, or None.

    Two tables, deliberately not one. `env` holds names the script itself assigns;
    `shell_env` holds the run's exported variables and is reachable only through
    `os.environ`. Merging them let a Python name resolve to a shell value that
    happened to share its spelling -- a shell `p` holding the text
    `glob.glob('traces/f8ba129a*/trace.jsonl')[0]` became the argument of the
    script's own `open(p, ...)`.

    None is the answer for anything built at run time, and saying None is the
    point: `open(p + '/s9_final_report.json')` with `p` a parameter used to yield
    `/s9_final_report.json`, a file that never existed.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):                 # f"{D}/x.json"
        parts = []
        for v in node.values:
            got = _py_value(v, env, shell_env)
            if got is None:
                return None
            parts.append(got)
        return "".join(parts)
    if isinstance(node, ast.FormattedValue):
        return _py_value(node.value, env, shell_env)
    if isinstance(node, ast.BinOp):
        left, right = _py_value(node.left, env, shell_env), _py_value(node.right, env, shell_env)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):                # "a" + "b"
            return left + right
        if isinstance(node.op, ast.Div):                # Path("a") / "b"
            return left.rstrip("/") + "/" + right
        if isinstance(node.op, ast.Mod):                # "%s/x" % d
            try:
                return left % right
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        got = _py_value(node.value, env, shell_env)     # `p.parent / "x.json"`
        return got.rstrip("/").rsplit("/", 1)[0] if got and "/" in got else None
    if isinstance(node, ast.Subscript):
        return _py_env_lookup(node, shell_env)
    if isinstance(node, ast.Call):
        got = _py_env_lookup(node, shell_env)
        if got is not None:
            return got
        name = _py_callee(node)
        if name in ("Path", "PosixPath", "PurePath"):
            parts = [_py_value(a, env, shell_env) for a in node.args]
            if not parts or any(x is None for x in parts):
                return None
            return "/".join(x.rstrip("/") for x in parts)
        if name in ("join", "path.join", "os.path.join"):
            parts = [_py_value(a, env, shell_env) for a in node.args]
            if not parts or any(x is None for x in parts):
                return None
            out = parts[0]
            for x in parts[1:]:
                out = x if x.startswith("/") else out.rstrip("/") + "/" + x
            return out
        if name in ("expanduser", "resolve", "absolute", "strip", "rstrip"):
            return _py_value(node.func.value, env, shell_env) if isinstance(
                node.func, ast.Attribute) else None
        if name == "format":                            # "{}/x".format(d)
            base = _py_value(node.func.value, env, shell_env) if isinstance(
                node.func, ast.Attribute) else None
            args = [_py_value(a, env, shell_env) for a in node.args]
            if base is None or any(x is None for x in args):
                return None
            try:
                return base.format(*args)
            except (IndexError, KeyError, ValueError):
                return None
    return None


def _py_callee(node) -> str:
    """`open`, `json.dump`, `p.write_text` -> the name a rule can match on."""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _py_mode_is_write(node, env: Mapping[str, str],
                      shell_env: Mapping[str, str]) -> bool:
    """`open(p, 'w')`, `open(p, mode='ab')`."""
    mode = None
    if len(node.args) > 1:
        mode = _py_value(node.args[1], env, shell_env)
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = _py_value(kw.value, env, shell_env)
    return bool(mode) and any(c in mode for c in _WRITE_MODES)


def _py_in_order(node):
    """Assignments and calls, in the order the program runs them.

    `ast.walk` is breadth-first, which loses the ordering the values depend on.
    Evaluation order here means a call's arguments come before the call, and an
    assignment's right-hand side before the assignment: `s = p.read_text()` reads
    with the value `p` holds now, and only then binds `s`.
    """
    if isinstance(node, ast.Assign):
        yield from _py_in_order(node.value)
        yield node
        return
    if isinstance(node, ast.Call):
        for child in ast.iter_child_nodes(node):
            yield from _py_in_order(child)
        yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from _py_in_order(child)


def python_file_ops(text: str, shell_env: Optional[Mapping[str, str]] = None
                    ) -> list[tuple[str, str, str]]:
    """`(path, direction, why)` for the files a Python program opens.

    Raises `SyntaxError` when the body is not Python, which is the caller's signal
    to fall back to the regex scan. 3.1% of bodies in the corpus (65 of 2096) --
    a `<<PY` fed to something else, or a script cut short.
    """
    tree = ast.parse(text)
    shell_env = shell_env or {}

    env: dict = {}
    out: list[tuple[str, str, str]] = []

    def add(path: Optional[str], direction: str, why: str) -> None:
        if path and "\n" not in path and len(path) < 400:
            out.append((path, direction, why))

    # In source order, assignments and calls interleaved. Collecting every
    # assignment first was wrong on the commonest shape an agent writes:
    #
    #   p = Path("normalize/schema.py"); p.write_text(s)
    #   p = Path("normalize/core.py");   p.write_text(s)
    #
    # with one table built in advance, both writes resolve to whichever value came
    # last, so two real files vanished and the third was claimed twice. Walking in
    # order gives each call the value `p` held when it ran. A name reassigned
    # inside a loop still resolves to the last value seen, which is the price of
    # not executing the program.
    for node in _py_in_order(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                got = _py_value(node.value, env, shell_env)
                if got is not None:
                    env[t.id] = got
                else:
                    env.pop(t.id, None)      # stale is worse than unknown
            continue

        if not isinstance(node, ast.Call):
            continue
        name = _py_callee(node)

        if name == "open" and node.args:
            write = _py_mode_is_write(node, env, shell_env)
            add(_py_value(node.args[0], env, shell_env),
                keys.WRITE if write else keys.READ,
                f"open(..., {'write' if write else 'read'}) in the script")
            continue

        if name in _READ_FUNCS and node.args:           # pd.read_csv("x.csv")
            add(_py_value(node.args[0], env, shell_env), keys.READ, f"{name}() in the script")
            continue

        if name in _LOAD_FUNCS or name in _SAVE_FUNCS:
            direction = keys.READ if name in _LOAD_FUNCS else keys.WRITE
            for arg in node.args:
                got = _py_value(arg, env, shell_env)
                if got and keys.looks_like_path(got):
                    add(got, direction, f"{name}() in the script")
            continue

        if name in ("copy", "copy2", "move", "copyfile") and len(node.args) > 1:
            add(_py_value(node.args[0], env, shell_env), keys.READ, f"{name}() in the script")
            add(_py_value(node.args[1], env, shell_env), keys.WRITE, f"{name}() in the script")
            continue

        if not isinstance(node.func, ast.Attribute):
            continue
        recv = node.func.value

        if name in _WRITE_METHODS:
            # `p.write_text(...)`, and `df.to_csv("x.csv")` where the path is the
            # argument rather than the receiver.
            if name in ("to_csv", "to_json", "to_parquet", "savefig") and node.args:
                add(_py_value(node.args[0], env, shell_env), keys.WRITE,
                    f"{name}() in the script")
            else:
                add(_py_value(recv, env, shell_env), keys.WRITE, f"{name}() in the script")
            continue

        if name in _READ_METHODS:
            add(_py_value(recv, env, shell_env), keys.READ, f"{name}() in the script")
            continue

        if name == "open":                              # `p.open('w')`
            write = bool(node.args) and any(
                c in (_py_value(node.args[0], env, shell_env) or "") for c in _WRITE_MODES)
            add(_py_value(recv, env, shell_env),
                keys.WRITE if write else keys.READ,
                f"open(..., {'write' if write else 'read'}) in the script")

    return out


def peel_wrappers(name: Optional[str], args: list[str]) -> tuple[Optional[str], list[str]]:
    """Step past wrappers to the command that does the work.

    Module level rather than a method, because the inspector in
    `graph/tests/inspect_command.py` reports the same peeling. It kept its
    own list of wrappers for a while, four names short of this one, so it
    described a split the extractor did not perform.

    `timeout 240 python3 run.py` puts a duration between the two, so a plain
    "first non-option argument" rule picks `240` and the real interpreter is
    never seen -- 94 edges on the corpus.
    """
    seen = 0
    while name and name.rsplit("/", 1)[-1] in WRAPPERS and seen < 4:
        seen += 1
        rest = [a for a in args
                if not a.startswith("-")
                and "=" not in a.split("/")[0]
                and not _DURATION_RE.match(a)]
        if not rest:
            return None, args
        name, args = rest[0], args[args.index(rest[0]) + 1:]
    return name, args


def _text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


class _Walker:
    """One pass over a command's tree, collecting `(path, direction)` claims.

    State is deliberately small: `env` for variable values and `executed` for
    file names the command runs, which is what decides whether a heredoc body
    written to a script is data or code.

    `split` is the one exception, and it is there for a reader rather than for the
    extraction: a record of what this pass decided a command was made of, before
    any rule was applied. `graph/tests/inspect_command.py` used to walk the tree a
    second time to show that, and the second walk disagreed with this one on 20 of
    188 real commands -- it had no `env`, so `$LOG` stayed a variable, and it did
    not recurse into heredoc bodies, so eighteen `source …/activate` calls were
    invisible. A debugging tool that describes a different split than the one being
    debugged is worse than no tool.
    """

    def __init__(self, data: bytes, working_dir: Optional[str],
                 session_env: Optional[Mapping[str, str]],
                 local_names: Optional[set[str]] = None):
        self.data = data
        self.wd = working_dir
        self.env: dict[str, str] = dict(session_env or {})
        # Variable names ever locally assigned anywhere in this run. Needed
        # by DESIGN §7.13: a `$VAR` whose name is in this set may be reassigned
        # at any time, so keeping it as a placeholder entity would merge
        # unrelated files (`stale value is worse than none`, §7.10). A name
        # NOT in this set is treated as a stable process-env global, and the
        # unexpanded literal becomes a WEAK-confidence entity so shell-bridge
        # agents (ft-fixup's `$KIRO_HOME/scripts/x.sh`) still produce a graph.
        self.local_names: Optional[frozenset[str]] = (
            None if local_names is None else frozenset(local_names))
        self.out: dict[tuple[str, str], str] = {}
        # Paths kept as their variable literal (`$KIRO_HOME/x.sh`) because the
        # value could not be resolved but the name is a stable global. Carried
        # separately from `out` so `build.py` can attach them at WEAK confidence
        # without changing the shape of the main return. See DESIGN §7.13.
        self.weak: dict[tuple[str, str], str] = {}
        self.executed: set[str] = set()
        # Hosts a command reached: (host_key, evidence). A set-like dict keeps the
        # first evidence a host was named for and drops the duplicate when the
        # same action lists the same host twice (`scp a user@h:b user@h:c`).
        self.hosts: dict[str, str] = {}
        # Paths deliberately not claimed, so the gap is visible rather than
        # looking like an action that touched nothing.
        self.skipped: list[str] = []
        self.loop_vars: dict[str, list[str]] = {}
        # What each command node turned out to be, in the order they were reached.
        # Written but never read by the extraction: nothing below may branch on it.
        self.split: list[dict[str, Any]] = []
        self.context: list[str] = []
        # Redirect text noted by `redirected` for the command it belongs to, so the
        # record can be complete without anything reading `split` back.
        self._pending_redirects: dict[int, list[str]] = {}

    # ---- recording ----------------------------------------------------
    def claim(self, literal: str, direction: str, why: str) -> None:
        """Record one path, resolving variables and relative paths first."""
        lit = _unquote(literal).strip()
        if not lit:
            return
        if lit.startswith("~"):
            # `~` needs HOME, which the command text does not carry. Emitting the
            # tilde form would split one file into two entity keys, so this is
            # left for the response-resolution pass, which often sees the
            # expanded path in the output.
            self.skipped.append((lit, "needs HOME; the command text has no value"))
            return
        if "$" in lit:
            expanded = keys.expand_vars(lit, self.env)
            if expanded is None:
                # DESIGN §7.13: before giving up, see if this is a *stable
                # global* (no name is ever locally reassigned in this run). If
                # so, keep the variable literal as a WEAK-confidence placeholder
                # entity so shell-bridge agents (`$KIRO_HOME/scripts/x.sh`) do
                # not vanish from the graph. Local variables (`LOGFILE=$(...)`)
                # continue to skip -- a stale value is worse than none.
                #
                # `local_names=None` is the opt-out contract for callers that
                # cannot or do not want to classify names; they get the old
                # conservative behaviour (always skip). The build pipeline opts
                # in by passing the pre-scanned set.
                if self.local_names is not None:
                    refs = keys.referenced_names(lit)
                    if refs and refs.isdisjoint(self.local_names):
                        self.weak.setdefault((lit, direction),
                            f"{why} (variable path; value unresolved, treated as global)")
                        return
                self.skipped.append(
                    (lit, "variable value never stated in the run"))
                return
            why, lit = f"{why} (expanded from {literal!r})", expanded
        path = keys.normalize_path(lit, self.wd)
        if path is not None:
            self.out.setdefault((path, direction), why)

    def claim_if_pathlike(self, literal: str, direction: str, why: str) -> None:
        """Same, but only when the token actually looks like a file.

        A bare `"$LOG"` has no extension, so it cannot pass the path test until
        it is expanded -- and if the value is unknown it never will. That case is
        recorded here rather than dropped, because it is the commonest reason an
        activity ends up isolated.
        """
        probe = _unquote(literal)
        if "$" in probe:
            expanded = keys.expand_vars(probe, self.env)
            if expanded is None:
                if keys.is_variable_reference(probe) or keys.looks_like_path(probe):
                    # DESIGN §7.13: promote to WEAK placeholder if the names
                    # are all stable globals; otherwise record the gap. The
                    # `local_names=None` opt-out keeps the old conservative
                    # behaviour for callers that did not classify names.
                    if self.local_names is not None:
                        refs = keys.referenced_names(probe)
                        if refs and refs.isdisjoint(self.local_names) \
                                and keys.looks_like_path(probe):
                            self.weak.setdefault((probe, direction),
                                f"{why} (variable path; value unresolved, treated as global)")
                            return
                    self.skipped.append(
                        (probe, "variable value never stated in the run"))
                return
            probe = expanded
        if keys.looks_like_path(probe):
            self.claim(literal, direction, why)

    # ---- host contact ------------------------------------------------
    #
    # These verbs reach out to a remote endpoint. The payload (what runs on the
    # other side) is not parsed -- the remote shell is often not bash, and more
    # than one real session used Windows CMD syntax inside the quotes. The
    # endpoint itself is still worth a node: a reader of the graph can see
    # which run touched which host, which docker container, which pod.

    def _host_from_rsync_scp(self, tok: str) -> Optional[str]:
        """Extract `user@host` from an scp/rsync argument.

        A positional is a remote endpoint when it contains a `:` that is not
        inside a path (`/etc:x` is a file, so the `:` has to come before the
        first `/`). Options are already excluded by the caller.
        """
        probe = _unquote(tok)
        if "$" in probe:                 # `scp $DST:...` -- host runtime-only
            return None
        colon = probe.find(":")
        slash = probe.find("/")
        if colon <= 0 or (0 <= slash < colon):
            return None
        return probe[:colon]

    def claim_host(self, key: str, why: str) -> None:
        """Record contact with an external endpoint.

        `key` has the shape `<scheme>://<endpoint>`. Keys with unresolved
        `$VAR` segments are refused (recorded in `skipped`) rather than
        emitted: an edge keyed on a literal variable name would conflate
        every ssh under that variable into one node.
        """
        if not key or "://" not in key:
            return
        scheme, _, ep = key.partition("://")
        if not ep or "$" in ep:
            self.skipped.append((key, "endpoint uses a value the command text "
                                      "does not carry"))
            return
        self.hosts.setdefault(key, why)

    # ---- tree traversal ----------------------------------------------
    def walk(self, node) -> None:
        t = node.type
        if t in self._BRANCH_SCOPES:
            # Each branch gets its own copy of `self.wd`: a `cd` on the left of
            # `|` must not be seen on the right. Also save-and-restore around
            # the pipeline as a whole, because the left branch's `cd` lives in
            # a subshell at runtime and does not change the caller's directory.
            saved = self.wd
            for child in node.children:
                before = self.wd
                self.walk(child)
                self.wd = before
            self.wd = saved
            return
        if t in self._BODY_SCOPES:
            # The whole body is one isolated scope: a `cd` inside `(…)`, a
            # function body or any looping/branching construct must not change
            # the directory the caller sees afterwards. Within the body a `cd`
            # does still affect subsequent siblings -- that is what `_Walker`
            # already does, since we recurse normally into the children here.
            saved = self.wd
            for child in node.children:
                self.walk(child)
            self.wd = saved
            return
        if t == "comment":
            # Belt and braces: tree-sitter already models a comment as a leaf
            # with no children, so nothing inside it is reachable. Removing this
            # line changes no behaviour -- the regex path had no such luxury and
            # linked a file named in a comment.
            return
        if t == "variable_assignment":
            self.assignment(node)
            # And then into the value. `LOG=$(cat /tmp/logpath.txt)` reads that
            # file, and returning here meant the `cat` was never visited: 28 real
            # reads absent from the reference session, all of them the same habit
            # -- the agent writes a path into a small file and reads it back.
            #
            # Not a judgement that was made and is now reversed. The regex path
            # had the same fault and had already been fixed (DESIGN.md: "extract
            # from 193 down to 153 ... scan inside the substitution separately"),
            # and `command` below descends for exactly this reason, with a comment
            # saying so. The lesson simply did not travel to this branch when the
            # tree replaced the regexes. Measured: +30 edges, 0 lost, precision
            # still 1.000 on the gold set.
            self._walk_substitutions(node)
            return
        if t == "for_statement":
            # `cd` inside the loop body may be walked once but at runtime runs
            # per iteration, and the loop body is not where the caller's
            # directory changes. Save and restore around the whole statement
            # so a `cd` in the body does not leak into whatever follows.
            saved_wd = self.wd
            self.for_statement(node)
            # The word list, and only the word list. `for i in $(seq 1 40)` runs
            # `seq`, which no edge comes of but which a split that claims to name
            # every command has to mention -- and the next one may be
            # `$(ls /data/*.log)`.
            #
            # Not the whole node: `for_statement` has already walked the body once
            # per value, so descending again from here went through the body's
            # substitutions a second time. `for s in A B; do D=$(ls …| head -1);
            # echo $D; done` recorded eight commands where six ran, and a reader
            # counting iterations could not reconcile that -- eight of the nine
            # `unsure` verdicts across three corpora were this.
            for c in node.children:
                if c.type == "do_group":
                    break
                self._walk_substitutions(c)
            self.wd = saved_wd
            return
        if t == "redirected_statement":
            self.redirected(node)
            return
        if t == "command":
            self.command(node)
            return
        for child in node.children:
            self.walk(child)

    def assignment(self, node) -> None:
        """`LOG=/x.log` records a value; it is not itself a read or a write."""
        name = value = None
        for c in node.children:
            if c.type == "variable_name":
                name = _text(c, self.data)
            elif c.type not in {"="}:
                value = _text(c, self.data)
        if not name:
            return
        # A value that needs resolving itself invalidates any carried value:
        # a stale one resolves `$LOG` to an earlier run's file.
        self.env.pop(name, None)
        if value and "$" not in value and "`" not in value:
            self.env[name] = _unquote(value)

    # ---- commands ----------------------------------------------------
    def _args(self, node) -> tuple[Optional[str], list[str]]:
        """`(command_name, arguments)` as source text, assignments applied.

        Substitutions are left out: `<(sed … a.sh)` holds a whole command, and
        taking its text as an argument produced the path `<(sed … a.sh` -- a
        plausible-looking key for a file that does not exist.
        """
        name, args = None, []
        for c in node.children:
            if c.type == "variable_assignment":
                self.assignment(c)
            elif c.type == "command_name":
                name = _text(c, self.data)
            elif c.type in {"file_redirect", "heredoc_redirect", "subshell",
                            "command_substitution", "process_substitution"}:
                continue                     # handled elsewhere, not an argument
            else:
                args.append(_text(c, self.data))
        return name, args

    def _walk_substitutions(self, node) -> None:
        """Descend into `$(…)` and `<(…)`, wherever they are nested.

        These hold whole commands, and skipping them lost real reads: the file in
        `echo "n: $(wc -l < /tmp/x.txt)"` and the one inside
        `diff <(sed … a.sh) b.sh`.
        """
        if node.type in {"command_substitution", "process_substitution"}:
            for c in node.children:
                self.walk(c)
            return
        for c in node.children:
            self._walk_substitutions(c)


    # Within one pipeline *branch*, `cd` does affect the next command -- bash
    # runs each side of `|` in a subshell, but within that subshell the cd is
    # live. So `pipeline` counts as sequential for the ancestor check; the leak
    # from one branch into a sibling branch is prevented in `walk()` by saving
    # and restoring `self.wd` around each pipeline child.
    #
    # A subshell `( cd X; cmd )`, a loop body, an `if` branch and a function
    # body still end without changing the caller's directory. The loop body
    # matters most here -- walked once per value, a `cd` inside would be
    # applied as many times as the list is long. Those scopes are handled with
    # a save/restore in `walk()` and left out of this set.
    _SEQUENTIAL = frozenset({"program", "list", "redirected_statement",
                             "negated_command", "pipeline"})

    # Nodes that split the walk into sibling branches that do not share a
    # directory. Each child is walked under its own saved `self.wd` so a `cd`
    # on one side cannot be seen on the other. For bodies rather than
    # branches (`subshell`, loops, `if`), the whole body is isolated instead.
    _BRANCH_SCOPES = frozenset({"pipeline"})
    _BODY_SCOPES = frozenset({"subshell", "function_definition",
                              "while_statement",
                              "c_style_for_statement", "if_statement",
                              "case_statement", "compound_statement"})

    def _is_sequential(self, node) -> bool:
        """Whether this command runs in the directory the next one will see."""
        n = node.parent
        while n is not None:
            if n.type not in self._SEQUENTIAL:
                return False
            n = n.parent
        return True

    def command(self, node) -> None:
        raw_name, raw_args = self._args(node)
        redirects = []
        for c in node.children:              # redirects attached to the command
            if c.type == "file_redirect":
                redirects.append(" ".join(_text(c, self.data).split()))
                self.file_redirect(c)
            elif c.type == "heredoc_redirect":
                # The delimiter only. The body is a child of this node, so the
                # whole text would put an entire script on one line.
                start = next((_text(x, self.data).strip() for x in c.children
                              if x.type == "heredoc_start"), "")
                op = next((_text(x, self.data) for x in c.children
                           if not x.is_named), "<<")
                redirects.append((op + start).strip())
                self.heredoc(c, raw_name)
            elif c.type == "subshell":
                self.walk(c)                 # `time ( … )`
            else:
                self._walk_substitutions(c)
        name, args = peel_wrappers(raw_name, raw_args)
        base = name.rsplit("/", 1)[-1] if name else ""
        positional = [a for a in args if not a.startswith("-")]

        # Recorded before the early returns below: a command this pass decided to
        # ignore is exactly the one a reader comes here to ask about.
        wrappers = []
        if name and raw_name and name != raw_name:
            for tok in [raw_name] + list(raw_args):
                if tok == name:
                    break
                if tok.rsplit("/", 1)[-1] in WRAPPERS:
                    wrappers.append(tok.rsplit("/", 1)[-1])
        # Variables resolved with the same function and the same `env` the rules
        # below will use, at the same point in the walk -- so the record cannot
        # claim a value the extraction did not see. Without this the record showed
        # `"$LOG"` while the edge showed the file it stood for.
        resolved = {}
        for tok in [raw_name or ""] + list(args):
            if tok and "$" in tok:
                got = keys.expand_vars(_unquote(tok), self.env)
                if got is None or got == tok:
                    continue
                if _is_glob(got):
                    # `for p in /dir/*.patch` binds the pattern, not the files it
                    # matches: the shell expands it, and the run's text does not
                    # say to what. Reporting the pattern as the value claimed a
                    # resolution that had not happened, and a judge caught it on
                    # two commands. No edge was ever built from it -- the glob is
                    # refused further down -- but the record was saying otherwise.
                    resolved[tok] = got + "   (a glob; the files it matched are " \
                                          "not in the command text)"
                    continue
                resolved[tok] = got
        self.split.append({
            "name": raw_name or "",
            "runs": base,
            "wrappers": wrappers,
            "args": list(args),
            "options": [a for a in args if a.startswith("-")],
            "positional": positional,
            "redirects": redirects + self._pending_redirects.pop(node.id, []),
            "resolved": resolved,
            "context": list(self.context),
            "ignored": bool(base and base in IGNORED_NAMES) or not name,
        })

        if not name:
            return

        if base == "cd":
            # The rule for what `cd X` does lives in `normalize.apply_cd`, so the
            # coarse fold in `command_working_dir` and this one cannot drift on
            # what a target means -- only on which targets count, which is the one
            # question that needs the tree.
            if self._is_sequential(node):
                target = positional[0] if positional else ""
                if "$" in target:
                    got = keys.expand_vars(_unquote(target), self.env)
                    target = got if got is not None else target
                was = self.wd
                self.wd = mapping.apply_cd(self.wd, _unquote(target))
                if self.wd is None and was is not None:
                    # Said out loud: every relative path after this point is now
                    # unresolvable, and `(isolated)` without a reason is not
                    # information.
                    self.skipped.append(
                        (target or "cd", "the directory after this `cd` cannot be "
                                         "read from the command text"))
            return
        if base in IGNORED_NAMES:
            return

        # Reaching out to a remote endpoint. The command on the other side is
        # not parsed -- the remote shell may not be bash, and in one real
        # session it was Windows CMD -- but the endpoint itself is a node, so a
        # reader can see which turn touched which host / container / pod.
        if base == "sshpass":
            # `sshpass -p PASS ssh ...`. Not a `WRAPPERS` entry because the
            # password after `-p` is an arbitrary string that `peel_wrappers`
            # would pick as the next verb (`-p 'pass' ssh host` → verb=pass).
            # Instead, find the inner verb and fall through as if it had been
            # the base.
            inner = next((a for a in args
                          if _unquote(a).rsplit("/", 1)[-1]
                             in {"ssh", "scp", "rsync"}), None)
            if inner is None:
                return
            inner_name = _unquote(inner).rsplit("/", 1)[-1]
            inner_args = args[args.index(inner) + 1:]
            base, name, args = inner_name, inner, inner_args
            positional = [a for a in args if not a.startswith("-")]

        if base == "ssh":
            # The first positional that isn't an option value is the host.
            # Two shapes to skip:
            #   `-o KEY=VAL`   leaves `KEY=VAL` as a positional; the `=`
            #                  tells it apart from an endpoint.
            #   `-p 29418`     leaves `29418` as a positional with nothing
            #                  on it to tell it apart. Walk `args` instead,
            #                  consuming one value after each option that
            #                  takes one.
            if "-G" in args:                        # config query, no connection
                return
            VALUE_OPTS = {"-o", "-p", "-i", "-l", "-F", "-E", "-c", "-b",
                          "-m", "-O", "-Q", "-S", "-J", "-D", "-L", "-R",
                          "-W", "-w"}
            host = None
            port = ""
            skip_next = False
            for i, a in enumerate(args):
                if skip_next:
                    if args[i - 1] == "-p":
                        port = f":{a}"
                    skip_next = False
                    continue
                if a in VALUE_OPTS:
                    skip_next = True
                    continue
                if a.startswith("-"):               # bundled flag like `-vv`
                    continue
                if "=" in a and host is None:       # a stray KEY=VAL value
                    continue
                host = a
                break
            if host:
                self.claim_host(f"ssh://{_unquote(host)}{port}",
                                f"argument of {base}")
            return
        if base in {"scp", "rsync"}:
            for a in positional:
                h = self._host_from_rsync_scp(a)
                if h:
                    self.claim_host(f"ssh://{h}", f"argument of {base}")
            return
        if base == "docker" and positional:
            sub, rest = positional[0], positional[1:]
            if sub == "exec" and rest:
                self.claim_host(f"docker://{_unquote(rest[0])}",
                                f"argument of docker {sub}")
            elif sub == "cp" and rest:
                # `docker cp <container>:/src /dst`  or the reverse.
                for a in rest:
                    probe = _unquote(a)
                    if ":" in probe and "/" not in probe.split(":")[0]:
                        self.claim_host(f"docker://{probe.split(':')[0]}",
                                        f"argument of docker {sub}")
                        break
            return
        if base == "kubectl" and positional:
            sub, rest = positional[0], positional[1:]
            if sub == "exec" and rest:
                pod = next((a for a in rest if not a.startswith("-")
                            and a != "--"), None)
                if pod:
                    self.claim_host(f"k8s://{_unquote(pod)}",
                                    f"argument of kubectl {sub}")
            elif sub == "cp" and rest:
                for a in rest:
                    probe = _unquote(a)
                    if ":" in probe and "/" not in probe.split(":")[0]:
                        self.claim_host(f"k8s://{probe.split(':')[0]}",
                                        f"argument of kubectl {sub}")
                        break
            return

        # `git diff x.md`, `git show y`: the subcommand carries the meaning.
        if base in VCS_NAMES and positional:
            sub, rest = positional[0], positional[1:]
            if sub in {"diff", "show", "log", "blame", "cat-file", "grep"}:
                for a in rest:
                    self.claim_if_pathlike(a, keys.READ, f"argument of {base} {sub}")
            elif sub in {"add", "checkout", "restore", "apply", "rm", "mv"}:
                for a in rest:
                    self.claim_if_pathlike(a, keys.WRITE, f"argument of {base} {sub}")
            return

        if base == "chmod":
            for a in positional:
                if not keys._CHMOD_MODE_RE.match(a):
                    self.claim_if_pathlike(a, keys.WRITE, f"argument of {base}")
            return

        if base in {"sed", "gsed"} and any(
                a.startswith("-") and "i" in a for a in args):
            for a in positional[1:]:         # first positional is the script
                self.claim_if_pathlike(a, keys.WRITE, "sed -i rewrites in place")
            return

        if base == "tee":
            for a in positional:
                self.claim_if_pathlike(a, keys.WRITE, f"argument of {base}")
            return

        # `-o out.json`, `--output out.json`
        wrote_by_flag = set()
        for i, a in enumerate(args[:-1]):
            if a in keys.WRITE_MARKS:
                self.claim_if_pathlike(args[i + 1], keys.WRITE, f"after {a}")
                wrote_by_flag.add(args[i + 1])

        # The value of `-o` is also a positional as far as the tree is concerned,
        # so the read rules below reached it too and every generated file arrived
        # as both read and written by the command that only wrote it: 19 paths
        # across the corpus, and a reviewer called out 16 of them unprompted.
        #
        # Per command, not per statement. Some of those 19 really are read, by a
        # later command in the same line -- `generate.py --output s.yaml && cat
        # s.yaml`, `… && python run.py s.yaml` -- and that read has to survive.
        # It does more than survive: `claim` keeps the first evidence it is given
        # for a (path, direction), so the spurious read was also taking the slot
        # and the edge read `argument of the script` where `argument of cat` was
        # the truth.
        positional = [a for a in positional if a not in wrote_by_flag]

        if base in NAMES_TAKING_A_FILE and positional:
            # Position proves it is a file; `bin/activate` has no extension.
            self.claim(positional[0], keys.READ, f"argument of {base}")
            for a in positional[1:]:
                self.claim_if_pathlike(a, keys.READ, f"argument of {base}")
            return

        if keys._is_interpreter(name):
            # The interpreter itself is not recorded. It is the engine, not data:
            # `source activate` reads a file whose contents change the shell,
            # while `venv/bin/python` is executed and contributes nothing to
            # where a result came from. Claiming it also contradicted the rule
            # below, which demands an extension precisely so that `/usr/bin/jq`
            # stays out -- the same binary was admitted or refused depending on
            # which branch reached it, and `/usr/bin/python3` did get in.
            script = next((a for a in positional if a != "-"), None)
            if script is not None:
                self.claim_if_pathlike(script, keys.READ, f"script run by {base}")
                for a in positional[positional.index(script) + 1:]:
                    self.claim_if_pathlike(a, keys.READ, "argument of the script")
            return

        if ("/" in name or name.startswith("./")) and "." in base:
            # A script run as the command itself: `/repo/hooks/trace-stop.sh`,
            # `./build.sh`. Executing it reads it, and nothing else in the table
            # notices -- the script-argument rules all key off an interpreter
            # *name*, which is absent here. 40 edges across 20 corpus sessions,
            # plus 10 more that the same action wrote and so cannot consume.
            # Requiring an extension keeps ordinary binaries (`/usr/bin/jq`) out:
            # they are tools, not artifacts of the run.
            self.claim(name, keys.READ, "script executed as the command itself")
            for a in positional:
                self.claim_if_pathlike(a, keys.READ, "argument of the script")
            return

        if base in keys.READ_VERBS:
            # For pattern-taking verbs the first positional is the expression.
            skip = 1 if keys._EXPR_VERB_RE.search(base) else 0
            for a in positional[skip:]:
                self.claim_if_pathlike(a, keys.READ, f"argument of {base}")
            return

        if base == "diff":
            for a in positional:
                self.claim_if_pathlike(a, keys.READ, "argument of diff")

    # The tree names the operator, and the name is the whole answer: `>&` and `<&`
    # duplicate a descriptor, `&>` merges output into a file. Spelled as text they
    # share characters; as node types they do not.
    _REDIRECT_OPS = frozenset({">", ">>", "<", "&>", ">&", "<&", "<<<", ">|"})
    _DESCRIPTOR_OPS = frozenset({">&", "<&"})
    _READ_OPS = frozenset({"<", "<<<"})

    # Target node kinds that name a file. An allowlist rather than a list of things
    # to refuse, because the extension test used to refuse everything unfamiliar
    # and dropping it moves that burden here: `< <(losetup -a)` is a process
    # substitution, and claiming it put `/app/<(losetup -a` in the graph. A kind
    # not listed is recorded instead, so the next unfamiliar form is visible rather
    # than either invented or silently lost.
    _TARGET_KINDS = frozenset({
        "word", "string", "raw_string", "number", "concatenation",
        "simple_expansion", "expansion",
    })

    def file_redirect(self, node) -> None:
        """`> f`, `2> f`, `&> f`, `< f` -- and `2>&1`, which names no file.

        The target is claimed by position rather than by the look of it: shell
        syntax leaves `> X` nothing to be but a file, so an extension is not
        required and asking for one lost 19.1% of every redirect target in 1524
        human-written scripts -- `> Singularity`, `> .gitignore`,
        `> /etc/bind/named.conf.local`. Our own traces never showed it, because an
        agent writes `.log` and `.json` where a person writes `Dockerfile`.

        Which puts the burden here: the extension test had been refusing `2>&1`
        all along, since `1` has no extension. Nothing said so, and dropping the
        test without replacing that refusal would have added a file called
        `/app/1` to the graph, once for each of 1125 such redirects in that corpus.
        """
        op_type, op, target, kind = None, None, None, None
        for c in node.children:
            if c.type in self._REDIRECT_OPS:
                if op_type is None:
                    op_type, op = c.type, _text(c, self.data)
            elif c.type != "file_descriptor":
                # `2` in `2> f` is the descriptor being redirected, not the target.
                target, kind = _text(c, self.data), c.type
        if not target or op_type in self._DESCRIPTOR_OPS:
            return
        if kind not in self._TARGET_KINDS:
            self.skipped.append((target, f"a {kind} is not a file name"))
            return
        # From the operator, not from the text: `3< f` reads, and asking whether the
        # text starts with `<` said it wrote, because the text starts with `3`.
        direction = keys.READ if op_type in self._READ_OPS else keys.WRITE
        self.claim(target, direction,
                   "input redirect" if direction == keys.READ
                   else f"redirect {op or '>'}")

    def redirected(self, node) -> None:
        """`cmd > f << EOF …`: the inner command, then its redirects."""
        receiver = None
        for c in node.children:
            if c.type == "command":
                name, _ = self._args(c)
                receiver = receiver or name
        # `a > b` puts the redirect beside the command rather than inside it. The
        # text is noted before walking, so `command` can put it in its own record
        # and `split` stays append-only -- nothing in the extraction may read it.
        # In `a | b > c` it belongs to `b`: that is where the shell sends output.
        # A `file_redirect` nested inside a `heredoc_redirect` counts too. Written
        # after the heredoc -- `python3 <<'PY' 2>&1` -- tree-sitter puts the `2>&1`
        # inside the heredoc node, and looking only at this statement's own
        # children missed it. Written before it, the same redirect is a direct
        # child and was always found.
        for c in _redirects_of(node):
            owner = _owner_of_redirect(node, c)
            if owner is None:
                continue
            if c.type == "file_redirect":
                text = " ".join(_text(c, self.data).split())
            else:
                start = next((_text(x, self.data).strip() for x in c.children
                              if x.type == "heredoc_start"), "")
                op = next((_text(x, self.data) for x in c.children
                           if not x.is_named), "<<")
                text = (op + start).strip()
            self._pending_redirects.setdefault(owner.id, []).append(text)
        for c in node.children:
            if c.type not in {"file_redirect", "heredoc_redirect"}:
                self.walk(c)
        for c in _redirects_of(node):
            if c.type == "file_redirect":
                self.file_redirect(c)
        for c in node.children:
            if c.type == "heredoc_redirect":
                self.heredoc(c, receiver, node)

    # ---- heredocs ----------------------------------------------------
    def heredoc(self, node, receiver: Optional[str], stmt=None) -> None:
        """Parse the body only when it is being executed.

        The same syntax carries two opposite meanings, and the tree settles which
        by naming the command that receives it: fed to an interpreter's stdin the
        body *is* the program running now, while `cat > f.sh << EOF` is data that
        nothing has run yet.
        """
        body = next((c for c in node.children if c.type == "heredoc_body"), None)
        if body is None:
            return
        text = _text(body, self.data)
        base = (receiver or "").rsplit("/", 1)[-1]

        if base.startswith("python") or base in {"node", "perl", "ruby"}:
            self._scan_script_literals(text, base)
            return
        if base in {"bash", "sh", "zsh"}:
            self._recurse_shell(text)
            return
        # Written to a file: code only if this same command then runs the file.
        if stmt is not None:
            for c in stmt.children:
                if c.type != "file_redirect":
                    continue
                target = _text(c, self.data).lstrip("><& ").strip()
                resolved = keys.normalize_path(_unquote(target), self.wd)
                if resolved and resolved in self.executed:
                    self._recurse_shell(text)
                    return

    def _recurse_shell(self, text: str) -> None:
        sub = _PARSER.parse(text.encode())
        inner = _Walker(text.encode(), self.wd, self.env)
        inner.executed = self.executed
        inner.context = self.context + ["inside a heredoc body"]
        inner.walk(sub.root_node)
        for k, v in inner.out.items():
            self.out.setdefault(k, f"{v} (inside a heredoc body)")
        # The commands found in there are part of this command's split: eighteen
        # `source …/bin/activate` calls in the reference session live only here.
        self.split.extend(inner.split)
        self.skipped.extend(inner.skipped)

    def _scan_script_literals(self, text: str, lang: str) -> None:
        """The files a script fed to stdin opens.

        Parsed with `ast` when the body is Python, which is 88% of them in the
        corpus, because the question is structural: *is this literal the first
        argument of `open`*. The regex below can only ask whether the word `open`
        is within 60 characters, and it answered wrong 7 times in 15 across three
        sessions -- `f.endswith('.jsonl')` became a file called `/.jsonl`, and
        `open(p + '/s9_final_report.json')` became one at the root of the
        filesystem.
        """
        claims = []
        if lang.startswith("python"):
            try:
                for path, direction, why in python_file_ops(text, self.env):
                    claims.append(f"{direction} {path}")
                    self.claim(path, direction, why)
            except SyntaxError:
                pass                      # not Python after all; regex below
            else:
                self._record_script_claims(lang, claims)
                return

        claims = self._scan_script_literals_regex(text, lang)
        self._record_script_claims(lang, claims)

    def _scan_script_literals_regex(self, text: str, lang: str) -> list:
        """The shallow scan, for a body `ast` cannot read.

        65 of 2096 python bodies in the corpus, plus every node/perl/ruby one.
        Kept rather than dropped: the alternative on record was to lose the body
        entirely, and a guessed direction on a real path is worth more than no
        edge at all.
        """
        import re
        claims = []
        for m in re.finditer(r"""['"]([^'"\n]+)['"]""", text):
            lit = m.group(1)
            if not keys.looks_like_path(lit):
                continue
            window = text[max(0, m.start() - 60):m.end() + 40]
            writes = re.search(
                r"""(?:open\([^)]*['"][wa]b?\+?['"]|write_text|writelines|"""
                r"""to_csv|to_json|savefig|\bdump\(|>\s*$)""", window)
            direction = keys.WRITE if writes else keys.READ
            claims.append(f"{direction} {lit}")
            self.claim(lit, direction,
                       f"literal in the {lang} script fed to stdin")
        return claims

    def _record_script_claims(self, lang: str, claims: list) -> None:
        """Recorded, not only claimed.

        `split` is what the inspector shows and what a reviewer is asked about, so
        an edge produced without an entry there is a region nobody can audit -- and
        this was one for three sessions: 306 edges across 21 sessions came from
        here and no review could have questioned them.

        A separate field rather than `positional`: these are not arguments of
        `python3`, and calling them that would invite the opposite error.
        """
        if claims:
            self.split.append({
                "name": lang, "runs": lang, "wrappers": [], "args": [],
                "options": [], "positional": [], "redirects": [], "resolved": {},
                "context": [f"inside the {lang} script fed to stdin"],
                "ignored": False, "claims": claims,
            })

    # ---- loops -------------------------------------------------------
    def for_statement(self, node) -> None:
        """`for f in a.yaml b.yaml; do grep -q x "$f"; done`.

        The tree names the loop variable and lists its values, so the body can be
        walked once per value with `$f` bound. The regex path could not connect
        the two and lost both the list items and the body's use of them.
        """
        var, values, body = None, [], None
        seen_in = False
        for c in node.children:
            if c.type == "variable_name" and var is None:
                var = _text(c, self.data)
            elif c.type == "in":
                seen_in = True
            elif c.type == "do_group":
                body = c
            elif seen_in and c.type in {"word", "string", "raw_string",
                                        "concatenation", "expansion",
                                        "simple_expansion"}:
                values.append(_text(c, self.data))
        if body is None:
            return
        if not var or not values:
            self.walk(body)
            return
        outer = self.env.get(var)
        for v in values:
            resolved = _unquote(v)
            if "$" in resolved:
                got = keys.expand_vars(resolved, self.env)
                if got is None:
                    continue
                resolved = got
            self.env[var] = resolved
            self.walk(body)
        if outer is None:
            self.env.pop(var, None)
        else:
            self.env[var] = outer


def _collect_executed(node, data: bytes, working_dir: Optional[str],
                      into: set[str]) -> None:
    """File names this command runs, needed before any heredoc is judged.

    `cat > run.sh << EOF … EOF` followed by `bash run.sh` only counts as code
    because of the second statement, which the walk has not reached yet.
    """
    if node.type == "command":
        name, args = None, []
        for c in node.children:
            if c.type == "command_name":
                name = _text(c, data)
            elif c.type in {"word", "string", "raw_string", "concatenation"}:
                args.append(_text(c, data))
        # `nohup /tmp/run.sh &` runs the script; the command *name* is `nohup`,
        # so without peeling the wrapper the heredoc that wrote it stays data.
        name, args = peel_wrappers(name, args)
        if name:
            for candidate in ([name] if "/" in name or name.startswith(".") else []):
                p = keys.normalize_path(_unquote(candidate), working_dir)
                if p:
                    into.add(p)
            if name.rsplit("/", 1)[-1] in {"bash", "sh", "zsh", "source", "."}:
                for a in args:
                    if a.startswith("-"):
                        continue
                    p = keys.normalize_path(_unquote(a), working_dir)
                    if p:
                        into.add(p)
                    break
    for c in node.children:
        _collect_executed(c, data, working_dir, into)


def extract(command: str, working_dir: Optional[str] = None,
            session_env: Optional[Mapping[str, str]] = None,
            local_names: Optional[set[str]] = None
            ) -> list[tuple[str, str, str]]:
    """Paths a command touches, as `(absolute_path, direction, evidence)`.

    Same contract as `keys.extract`, so the two can be compared on the gold set
    and swapped behind one flag.

    `local_names` lists every variable name reassigned anywhere in the run;
    used by DESIGN §7.13 to decide which unresolved `$VAR` references stay as
    WEAK placeholder entities (globals) and which get skipped (locals). The
    weak set is returned via `weak_paths()`, parallel to `skipped_paths()`.

    Raises `ParseFailure` when the text is not valid enough shell to build a
    tree -- 1.5% of 1751 measured commands, mostly very long Python heredocs --
    leaving the caller to fall back rather than silently returning less.
    """
    if _PARSER is None:
        raise ParseFailure("tree-sitter-bash is not installed")
    if not command:
        return []
    data = command.encode()
    tree = _PARSER.parse(data)
    if tree.root_node.has_error:
        raise ParseFailure("tree-sitter reported a syntax error")

    walker = _Walker(data, working_dir, session_env, local_names)
    _collect_executed(tree.root_node, data, working_dir, walker.executed)
    walker.walk(tree.root_node)
    _LAST_SKIPPED[:] = walker.skipped
    _LAST_WEAK[:] = [(p, d, why) for (p, d), why in walker.weak.items()]
    _LAST_SPLIT[:] = walker.split
    _LAST_HOSTS[:] = [(k, why) for k, why in walker.hosts.items()]
    return [(p, d, why) for (p, d), why in walker.out.items()]


# Set by the most recent `extract`, for callers that want to report the gaps.
# A return value would be cleaner, but `keys.extract` defines the contract both
# implementations share and changing it would ripple through every caller.
_LAST_SKIPPED: list[str] = []
_LAST_WEAK: list[tuple[str, str, str]] = []
_LAST_SPLIT: list[dict[str, Any]] = []
_LAST_HOSTS: list[tuple[str, str]] = []


def weak_paths() -> list[tuple[str, str, str]]:
    """Variable-form paths kept as WEAK placeholder entities (DESIGN §7.13).

    Each item is `(literal, direction, evidence)` with the literal still
    containing `$VAR`. Build-side consumers should attach them at WEAK
    confidence so a reader can distinguish them from edges backed by a
    resolved absolute path.
    """
    return list(_LAST_WEAK)


def last_split() -> list[dict[str, Any]]:
    """What the most recent `extract` decided each command was made of.

    The same traversal that produced the edges, so a reader looking at this is
    looking at the split the graph was built from -- variables already resolved
    through `env`, heredoc bodies already recursed into. A second walk of the tree
    disagreed with this one on 20 of 188 real commands.

    Same shape of accessor as `skipped_paths` for the same reason: `keys.extract`
    defines the contract both implementations share, and widening the return value
    would ripple through every caller.
    """
    return [{k: v for k, v in r.items() if not k.startswith("_")}
            for r in _LAST_SPLIT]


def skipped_paths() -> list[str]:
    """Paths the last `extract` saw and deliberately refused to resolve."""
    return list(_LAST_SKIPPED)


def hosts_touched() -> list[tuple[str, str]]:
    """`(host_key, evidence)` for every remote endpoint the last `extract` saw.

    `host_key` is `<scheme>://<endpoint>` -- `ssh://user@h`, `docker://<cid>`,
    `k8s://<pod>`. The payload that would run on the far side is not parsed;
    the node answers "which turn touched which host" and nothing more.
    """
    return list(_LAST_HOSTS)





