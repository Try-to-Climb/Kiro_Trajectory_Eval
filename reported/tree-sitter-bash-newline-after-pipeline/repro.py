#!/usr/bin/env python3
"""Minimal reproduction: a newline after a two-stage pipeline is not a separator.

Run it:

    pip install tree-sitter tree-sitter-bash
    python3 repro.py

It prints the parse of four inputs -- the failing one, and three that differ from
it by one token and parse correctly -- so the boundary of the problem is visible
rather than asserted. Nothing here is specific to our project: the only imports
are `tree_sitter` and `tree_sitter_bash`.
"""
import sys

try:
    import tree_sitter_bash
    from tree_sitter import Language, Parser
except ImportError:
    sys.exit("pip install tree-sitter tree-sitter-bash")

PARSER = Parser(Language(tree_sitter_bash.language()))

# Two commands on two lines. The newline ends the first one: `bash -n` accepts
# this, and running it prints `B`, so `echo B` is a command in its own right.
FAILING = 'a | grep x | head -30\necho B && cd s'

# One token different from FAILING in each case, and each parses correctly.
CONTROLS = [
    ('a | head -30\necho B && cd s',
     "one pipeline stage instead of two"),
    ('a | grep x | head -30;\necho B && cd s',
     "a semicolon before the newline"),
    ('a | grep x | head -30\necho B',
     "nothing after the second line's first command"),
    ('a 2>&1 | grep x | head -30\necho B && cd s',
     "a redirection added -- fails the same way, so it is not about redirects"),
]


def commands(source):
    """Every `command` node, as (name, [argument texts]), in source order."""
    data = source.encode()
    tree = PARSER.parse(data)
    text = lambda node: data[node.start_byte:node.end_byte].decode()
    found, stack = [], [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "command":
            name = next((text(c) for c in node.children
                         if c.type == "command_name"), "")
            args = [text(c) for c in node.children
                    if c.type in ("word", "string", "number", "concatenation")]
            found.append((node.start_byte, name, args))
        stack.extend(node.children)
    return [(n, a) for _, n, a in sorted(found)], tree.root_node.has_error


def has_own_node(source):
    """Whether the second line's first word became a `command_name` of its own.

    The one test used throughout: it does not depend on which commands an example
    happens to use.
    """
    want = source.split("\n")[1].split()[0]
    data = source.encode()
    stack = [PARSER.parse(data).root_node]
    while stack:
        node = stack.pop()
        if (node.type == "command_name"
                and data[node.start_byte:node.end_byte].decode() == want):
            return True
        stack.extend(node.children)
    return False


def show(source, label):
    got, has_error = commands(source)
    print("-" * 72)
    print(label)
    print("-" * 72)
    print("input:")
    for line in source.split("\n"):
        print("    " + line)
    print(f"has_error: {has_error}")
    print("commands found:")
    for name, args in got:
        print(f"    {name:10} {args}")
    return got


def main():
    print(__doc__.strip().split("\n")[0])
    print()
    got = show(FAILING, "FAILING INPUT")

    print("""
expected: five commands, because `head -30` ends the first line and `echo B` is
          the first command of the second.

    a
    grep       ['x']
    head       ['-30']
    echo       ['B']
    cd         ['s']

actual:   `echo` and `B` are arguments of `head`, so the second line's first
          command is gone. `has_error` is false, so nothing signals it. There is
          no redirection anywhere in this input.
""")

    swallowed = not has_own_node(FAILING)
    if swallowed:
        print("    -> reproduced: `echo` is an argument, not a command")
    else:
        print("    -> NOT reproduced on this version; please say which one")

    print("\nControls. Each differs from the failing input by one token.\n")
    for source, label in CONTROLS:
        show(source, "CONTROL: " + label)
        print(f"    -> the second line's first command is its own node: "
              f"{has_own_node(source)}")
        print()

    print("-" * 72)
    print("what bash does with the same text")
    print("-" * 72)
    print("    $ printf 'a | grep x | head -30\\necho B && cd s\\n' > t.sh")
    print("    $ bash -n t.sh      # accepted")
    print("    $ bash t.sh         # prints B, so `echo B` did run")
    return 0 if swallowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
