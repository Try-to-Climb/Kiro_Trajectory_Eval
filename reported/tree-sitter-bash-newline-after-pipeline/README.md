# A newline after a two-stage pipeline is not treated as a command separator

<!-- LOCAL NOTES — everything above the `---` is ours and is not part of the issue.

Status:   filed 2026-09-18, open, no response yet
Filed as: https://github.com/tree-sitter/tree-sitter-bash/issues/347
Searched: #345 and #316 look adjacent; see "Possibly related" below for why this
          was filed separately anyway. If a maintainer closes 347 as a duplicate,
          record which one and why here.

To produce the issue body:

    sed -n '/^---$/,$p' README.md | tail -n +2

Title to use: the heading of this file.
-->

---

`tree-sitter-bash` parses the first command of a line into the argument list of
the previous line's last command, so that command disappears from the tree. Two
things are needed: the previous line ends in a pipeline of two or more stages, and
the new line continues past its first command with `&&` or `||`. A `;` there parses
correctly, as does a single-stage pipeline on the line before. No redirection is
involved. `has_error` is false, so nothing signals that anything was lost.

## Versions

```
tree-sitter        0.26.0
tree-sitter-bash   0.25.1      (the newest on PyPI at the time of writing)
python             3.12.6
```

## Input

```bash
a | grep x | head -30
echo B && cd s
```

Two lines, two statements. `bash -n` accepts the file and running it prints `B`,
so `echo B` is a command in its own right and not an argument of anything.

## Expected

Five `command` nodes, because `head -30` ends the first line and `echo B` is the
first command of the second:

```
a
grep       ['x']
head       ['-30']
echo       ['B']
cd         ['s']
```

## Actual

Four, with `echo` and `B` absorbed into `head`:

```
a
grep       ['x']
head       ['-30', 'echo', 'B']
cd         ['s']
```

The `list` on the right of the pipeline is allowed to continue past the newline:

```
program                a | grep x | head -30 echo B && cd s
  pipeline               a | grep x | head -30 echo B && cd s
    command                a
      command_name           a
        word                   a
    list                   grep x | head -30 echo B && cd s
      pipeline               grep x | head -30 echo B
        command                grep x
          command_name           grep
            word                   grep
          word                   x
        command                head -30 echo B
          command_name           head
            word                   head
          number                 -30
          word                   echo
          word                   B
      command                cd s
        command_name           cd
          word                   cd
        word                   s
```

`root_node.has_error` is `False`. There is no `redirected_statement` and no
`file_redirect` anywhere in this tree.

## Boundary

Each of these differs from the failing input by one token.

| line 1 | line 2 | parses correctly |
|---|---|---|
| `a \| grep x \| head -30` | `echo B && cd s` | **no** — the failing input |
| `a \| grep x \| grep y \| head -30` | `echo B && cd s` | no — three stages behave as two |
| `a \| grep x \| head` | `echo B && cd s` | no — no argument on `head` |
| `a 2>&1 \| grep x \| head -30` | `echo B && cd s` | no — a redirection makes no difference |
| `true && a \| grep x \| head -30` | `echo B && cd s` | no — an `&&` before it makes no difference |
| `a \| grep x \| head -30` | `cat f && cd s` | no — which command is on line 2 makes no difference |
| `a \| grep x \| head -30` | `echo B \|\| cd s` | no — `||` behaves as `&&` |
| `a \| head -30` | `echo B && cd s` | **yes** — one pipeline stage |
| `a \| grep x \| head -30;` | `echo B && cd s` | **yes** — an explicit `;` ends it |
| `a \| grep x \| head -30` | `echo B` | **yes** — nothing after line 2's first command |
| `a \| grep x \| head -30` | `echo B; cd s` | **yes** — `;` on line 2, not `&&` |

So two things are needed, and two things that look relevant are not.

Needed:

* the line before ends in a pipeline of **two or more** stages -- with one stage
  the newline ends the statement correctly;
* the new line continues past its first command with `&&` or `||`. With `;`, or
  with nothing after it, the parse is correct.

Not needed:

* a redirection -- `a | grep x | head -30` fails identically without `2>&1`;
* any particular commands -- `cat f && cd s` on the second line fails the same
  way as `echo B && cd s`.

## Possibly related, we are not sure

Two open issues look adjacent, and this may be a duplicate of one of them. We
cannot tell from the outside, so we are filing it separately rather than assuming.

**#345 — "Redirect after a list or pipeline attaches to the whole list instead of
the last command."** If a `redirected_statement` spans a whole list, a newline
inside that span would be swallowed with it, which would explain this. But the
failure here needs no redirection at all:

```bash
a | grep x | head -30
echo B && cd s
```

parses the same wrong way, and the tree contains no `redirected_statement` and no
`file_redirect`. So either #345 is a separate fault with a similar shape, or the
cause is something both rest on -- where a `list` inside a `pipeline` is allowed
to end.

**#316 — "Single-bracket test commands are merged across redirected statements."**
The symptom is the same: a newline is absorbed and the next line's first command
becomes an argument. The trigger is different (`[ ]` commands there, a two-stage
pipeline here).

If a maintainer judges this the same underlying bug as either, closing it as a
duplicate is fine -- the boundary table above may still be useful there.

## Reproduce

```
pip install tree-sitter tree-sitter-bash
python3 repro.py
```

<details>
<summary><code>repro.py</code></summary>

```python
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
```

</details>

It prints the parse of the failing input and of three controls, and exits 0 when
the fault reproduces.

## How we ran into it

Parsing shell commands out of recorded terminal sessions. Found by reviewing every
parse: 3 of 269 commands in one session, 0 of 188 in another. Both were shaped like
this, which is what a script that prints progress between steps looks like:

```bash
cd /some/dir &&
echo "=== status ===" && git submodule status 2>&1 | grep -v "^-" | head -30
echo "=== next check ===" && cd sub && git log --oneline -5
```

The swallowed command was `echo`, which was harmless for us. Had the next line
begun with `cat somefile`, that read would have been attributed to `head`.

## Notes

* We are not asking for the `bash` grammar to accept anything new — `bash -n`
  already accepts this text. The two disagree about where a statement ends.
* `has_error` being false is the part that makes this expensive for a consumer:
  there is no signal to fall back on.