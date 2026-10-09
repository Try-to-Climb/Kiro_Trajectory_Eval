"""Show what the extractor makes of one shell command.

The edges in the graph rest on this: a command is split by a syntax tree, each
`command` node is looked at, and a rule decides whether the paths in it are being
read or written. When an edge looks wrong the question is always which of those
three steps produced it, and until now the only way to find out was to add a print
statement.

    python3 -m graph.tests.inspect_command 'cat a.txt > b.txt'
    python3 -m graph.tests.inspect_command --file commands.txt
    echo 'tail -f /var/log/x' | python3 -m graph.tests.inspect_command

    python3 -m graph.tests.inspect_command --tree 'nohup python3 x.py &'
    python3 -m graph.tests.inspect_command --session <id> --official-dir <dir>

The last form runs over every command in a real session, which is how the corpus
numbers in DESIGN.md were measured.

Sections, in the order the extractor works in:

  tree        the syntax tree, as `tree-sitter-bash` sees it
  split       what the extractor decided the command was made of, read from its
              own record -- not from a second walk of the tree. The second walk
              is what this used to do, and it disagreed with the real one on 20
              of 188 commands in the reference session
  paths       what was claimed, with the direction and the evidence for it
  skipped     paths deliberately not claimed, and why -- usually a variable
              whose value the run never states
  regex       the same command through the fallback extractor, and the
              difference, since the two are meant to agree
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import keys                                              # noqa: E402
from graph import shell_ast                                         # noqa: E402
from graph.shell_ast import ParseFailure                            # noqa: E402

BAR = "─" * 78


def _text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def print_tree(command: str, max_depth: int = 40) -> None:
    """The syntax tree, indented. Unnamed nodes -- the punctuation -- are kept:
    a missing `>` is exactly the kind of thing that turns a write into a read."""
    parser = shell_ast._PARSER
    if parser is None:
        print("  tree-sitter-bash is not installed; the regex fallback is all "
              "there is")
        return
    data = command.encode()
    tree = parser.parse(data)
    if tree.root_node.has_error:
        print("  ⚠ tree-sitter reported a syntax error in this command.")
        print("    `extract` raises ParseFailure here and the caller falls back "
              "to the regexes.")

    def walk(node, depth: int) -> None:
        if depth > max_depth:
            return
        body = " ".join(_text(node, data).split())
        if len(body) > 58:
            body = body[:55] + "…"
        name = node.type if node.is_named else repr(node.type)
        mark = "  " if node.is_named else "· "
        print(f"  {'  ' * depth}{mark}{name:<26} {body}")
        for child in node.children:
            walk(child, depth + 1)

    walk(tree.root_node, 0)


# ---- structure only -------------------------------------------------------
# The split, with no opinion about it. `shell_ast` does two separable things: the
# tree decides the structure, the rules decide the meaning. Mixing them in one
# report meant a wrong edge could not be attributed -- was the command split
# badly, or split fine and then read wrongly? This half answers the first
# question and deliberately says nothing about read or write.

# Wrappers, the interpreter test and the peeling all come from `shell_ast`, not
# from a copy here. The copy existed for one session and was four names short --
# `exec`, `unbuffer`, `setsid`, `command` -- so this tool described a split the
# extractor does not perform, which is the one thing it must never do.

def structure(command: str, working_dir: Optional[str] = None,
              env: Optional[dict] = None) -> dict:
    """What the extractor decided this command was made of.

    Read from `shell_ast.last_split()` -- the record the extraction itself keeps --
    not from a second walk of the tree. The second walk is what this used to do,
    and it disagreed with the real one on 20 of 188 commands in the reference
    session: it had no `env`, so `$LOG` stayed a variable where the extraction had
    resolved it, and it did not recurse into heredoc bodies, so eighteen
    `source …/bin/activate` calls were invisible here while the graph had edges
    for them.

    `working_dir` and `env` are the same arguments `build._command_paths` passes.
    Omitting them was worth 44 of 140 commands in one session: a relative path
    cannot be resolved without a cwd, so the record showed nothing where the graph
    had an edge.
    """
    if shell_ast._PARSER is None:
        return {"ok": False, "why": "tree-sitter-bash is not installed",
                "commands": [], "assignments": [], "heredocs": []}
    try:
        shell_ast.extract(command, working_dir, env)
    except ParseFailure as exc:
        return {"ok": False, "why": str(exc), "commands": [], "assignments": [],
                "heredocs": []}

    out = {"ok": True, "commands": shell_ast.last_split(),
           "assignments": [], "heredocs": []}

    # Assignments and heredoc sizes are read off the tree: they are facts about the
    # text rather than decisions the extractor made, and it keeps no record of
    # them. Whether a value is knowable is the single biggest reason a path never
    # becomes a node, so it is worth stating even though nothing claims it.
    data = command.encode()
    tree = shell_ast._PARSER.parse(data)

    def walk(node) -> None:
        if node.type == "variable_assignment":
            name, value = "", ""
            for child in node.children:
                if child.type == "variable_name":
                    name = _text(child, data)
                elif child.is_named:
                    value = " ".join(_text(child, data).split())
            out["assignments"].append({
                "name": name, "value": value,
                "literal": not any(m in value for m in ("$", "`")),
            })
        elif node.type == "heredoc_body":
            body = _text(node, data)
            out["heredocs"].append({
                "lines": len(body.splitlines()),
                "first": (body.splitlines() or [""])[0][:70],
            })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return out


def print_structure(command: str, as_json: bool = False) -> None:
    st = structure(command)
    if as_json:
        import json
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return
    if not st["ok"]:
        print(f"  ⚠ {st.get('why', 'unparsable')}")
    if st["assignments"]:
        print("  ASSIGNMENTS")
        for a in st["assignments"]:
            mark = "literal" if a["literal"] else "computed at run time"
            print(f"    {a['name']}={a['value'][:56]}")
            print(f"       value: {mark}")
    if st["heredocs"]:
        print("  HEREDOC BODIES")
        for h in st["heredocs"]:
            print(f"    {h['lines']} line(s), starting {h['first']!r}")
    print("  COMMANDS")
    if not st["commands"]:
        print("    none")
    for i, c in enumerate(st["commands"], 1):
        where = f"   ({', '.join(c['context'])})" if c.get("context") else ""
        wrap = (f"   [through {' -> '.join(c['wrappers'])}]"
                if c.get("wrappers") else "")
        # Said out loud: a command the extractor decided to skip is exactly the one
        # a reader comes here to ask about.
        skip = "   [ignored by the rules]" if c.get("ignored") else ""
        print(f"    [{i}] {c['runs'] or c['name']}{wrap}{where}{skip}")
        for a in c["positional"]:
            print(f"        positional  {a[:66]}")
            if a in c.get("resolved", {}):
                print(f"                    \u2192 {c['resolved'][a][:60]}")
        for a in c["options"]:
            print(f"        option      {a[:66]}")
        for r in c.get("redirects", ()):
            print(f"        redirect    {r[:66]}")


def report(command: str, working_dir: Optional[str] = None,
           env: Optional[dict] = None, show_tree: bool = True,
           show_regex: bool = True) -> int:
    """Everything about one command. Returns the number of paths claimed."""
    print(BAR)
    print(f"COMMAND  {command if len(command) < 300 else command[:300] + '…'}")
    if working_dir:
        print(f"CWD      {working_dir}")
    if env:
        print(f"ENV      {dict(list(env.items())[:6])}"
              + (" …" if len(env) > 6 else ""))
    print(BAR)

    if show_tree:
        print("\nTREE")
        print_tree(command)

    print("\nSPLIT  (the extractor's own record)")
    print_structure(command)

    print("\nPATHS CLAIMED  (path, direction, evidence)")
    try:
        claims = shell_ast.extract(command, working_dir, env)
    except ParseFailure as exc:
        claims = None
        print(f"  ParseFailure: {exc}")
        print("  the caller falls back to graph/keys.py here")
    if claims is not None:
        if not claims:
            print("  none")
        for path, direction, why in sorted(claims):
            arrow = "→ writes" if direction == "write" else "← reads "
            print(f"  {arrow}  {path}")
            print(f"            because: {why}")

        skipped = shell_ast.skipped_paths()
        print("\nSKIPPED  (seen, deliberately not claimed)")
        if not skipped:
            print("  none")
        for item in skipped:
            if isinstance(item, tuple) and len(item) == 2:
                what, why = item
            else:
                what, why = item, ""
            print(f"  {str(what)[:60]}")
            if why:
                print(f"            because: {why}")

    if show_regex:
        print("\nREGEX FALLBACK  (graph/keys.py, for comparison)")
        try:
            fb = keys.extract(command, working_dir, env)
        except Exception as exc:                      # noqa: BLE001
            fb = []
            print(f"  raised {type(exc).__name__}: {exc}")
        for path, direction, why in sorted(fb):
            arrow = "→ writes" if direction == "write" else "← reads "
            print(f"  {arrow}  {path}   ({why})")
        if not fb:
            print("  none")
        if claims is not None:
            a = {(p, d) for p, d, _ in claims}
            b = {(p, d) for p, d, _ in fb}
            if a != b:
                print("\n  DIFFERENCE  (the two are meant to agree)")
                for p, d in sorted(a - b):
                    print(f"    only the tree: {d:5} {p}")
                for p, d in sorted(b - a):
                    print(f"    only the regex: {d:5} {p}")
            else:
                print("\n  the two agree")
    print()
    return len(claims or ())


def _from_session(session: str, official_dir: str, limit: int,
                  only_unlinked: bool) -> int:
    """Every command in a real session. `--only-unlinked` narrows it to the ones
    that produced no path at all, which is the list worth reading."""
    import warnings
    warnings.filterwarnings("ignore")
    from normalize import load_trace_from_official

    ir = load_trace_from_official(session, official_dir or None,
                                 include_responses=True)
    shown = 0
    for a in ir.actions:
        if not a.command:
            continue
        wd = (a.args or {}).get("working_dir") if hasattr(a, "args") else None
        try:
            claims = shell_ast.extract(a.command, wd)
        except ParseFailure:
            claims = None
        if only_unlinked and claims:
            continue
        print(f"\n### a[{a.idx}]  turn {a.turn}  {a.action}")
        report(a.command, wd, None, show_tree=False)
        shown += 1
        if limit and shown >= limit:
            break
    print(f"{BAR}\nshown {shown} command(s)")
    return shown


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m graph.tests.inspect_command",
        description="Show how one shell command is split and what is claimed "
                    "from it.")
    p.add_argument("command", nargs="?", help="the command; omit to read stdin")
    p.add_argument("--file", help="a file of commands, separated by "
                                  "blank lines")
    p.add_argument("--cwd", default="", help="working directory, for relative "
                                             "paths")
    p.add_argument("--env", action="append", default=[], metavar="K=V",
                   help="a variable the run had already set; repeatable")
    p.add_argument("--no-tree", action="store_true",
                   help="skip the syntax tree, print only what was claimed")
    p.add_argument("--no-regex", action="store_true",
                   help="skip the fallback comparison")
    p.add_argument("--split-only", action="store_true",
                   help="stop at the split: what the tree says a command is made "
                        "of, with no read/write judgement and no path resolution")
    p.add_argument("--json", action="store_true",
                   help="with --split-only, emit the structure as json")
    p.add_argument("--session", help="every command of a real session")
    p.add_argument("--official-dir", default="",
                   help="where the official session records live")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after N commands (with --session)")
    p.add_argument("--only-unlinked", action="store_true",
                   help="with --session, only commands that claimed nothing")
    args = p.parse_args(argv)

    if not shell_ast.available():
        print("[warn] tree-sitter-bash is not installed: no tree to show, and "
              "the extractor is running on regexes alone.", file=sys.stderr)

    env = {}
    for pair in args.env:
        k, _, v = pair.partition("=")
        env[k] = v

    if args.session:
        _from_session(args.session, args.official_dir, args.limit,
                      args.only_unlinked)
        return 0

    if args.file:
        # Blocks separated by a blank line, not one command per line: a heredoc is
        # several lines and one of the commands worth inspecting most.
        with open(args.file, encoding="utf-8") as fh:
            blocks, cur = [], []
            for line in fh:
                if line.startswith("#"):
                    continue
                if line.strip():
                    cur.append(line.rstrip("\n"))
                elif cur:
                    blocks.append("\n".join(cur))
                    cur = []
            if cur:
                blocks.append("\n".join(cur))
        for block in blocks:
            report(block, args.cwd or None, env or None,
                   not args.no_tree, not args.no_regex)
        print(f"{BAR}\ninspected {len(blocks)} command(s) from {args.file}")
        return 0

    command = args.command
    if command is None:
        command = sys.stdin.read().rstrip("\n")
    if not command.strip():
        p.error("no command given")
    if args.split_only:
        if not args.json:
            print(BAR)
            print(f"COMMAND  {command if len(command) < 300 else command[:300]}")
            print(BAR)
        print_structure(command, args.json)
        return 0
    report(command, args.cwd or None, env or None,
           not args.no_tree, not args.no_regex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
