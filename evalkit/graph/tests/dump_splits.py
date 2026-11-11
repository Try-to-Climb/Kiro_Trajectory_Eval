"""Dump every command in a session with the split the extractor made of it.

Written to be read by a language model. The graph's edges rest on this split, and
the only judge available for "is this split right" is someone who can read shell:
there is no gold set for it, and a wrong split is invisible in the totals -- an
edge that should exist simply does not appear.

    python3 -m graph.tests.dump_splits /proj/…/kexin_folder            # a folder
    python3 -m graph.tests.dump_splits /proj/…/<session-id>.jsonl      # one file
    python3 -m graph.tests.dump_splits <session-id> --official-dir /proj/…

    python3 -m graph.tests.dump_splits <path> --out audit.md
    python3 -m graph.tests.dump_splits <path> --format jsonl --out audit.jsonl
    python3 -m graph.tests.dump_splits <path> --only-unsplit    # the suspicious ones

What is in a record, and nothing else: the command as it ran, and what the
extractor decided it was made of. No direction, no path resolution, no edges --
those are the next stage and mixing them in makes a wrong answer impossible to
attribute. The split comes from `shell_ast.last_split()`, the extraction's own
record, so what is shown is what the graph was built from.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import shell_ast                                         # noqa: E402
from graph.shell_ast import ParseFailure                            # noqa: E402
from graph.build import command_cwd
from graph.tests import inspect_command as ic                       # noqa: E402

# Stated in the file itself so the reader does not need this module to know what
# they are looking at, or what they are being asked.
PREAMBLE = """\
# split audit

Each section below is one shell command from an agent session, together with the
way our extractor broke it apart. The split is what every provenance edge is
derived from: a command is parsed into a syntax tree, each `command` node is
found, and only then does a separate set of rules decide which of its paths were
read and which were written. This file covers the first half only.

## What each field means

- `command` — the command as the agent ran it, verbatim, including newlines.
- `split` — one line per command node the extractor found, in the order it
  reached them:
  - `runs` — the program, after wrappers were stepped past.
  - `through` — wrappers that were stepped past to get there (`nohup`,
    `timeout 240`, `env FOO=1` and so on).
  - `positional` / `options` — its arguments, as written.
  - `redirects` — `> f`, `>> f`, `< f`, `<<'EOF'`, `2>&1`.
  - `resolved` — where a `$VAR` was substituted, and with what. The value comes
    from an assignment earlier in the same command.
  - `in` — nesting the extractor tracks, currently only a heredoc body: a
    command written into a script and then executed is found by re-parsing that
    body.
  - `ignored by the rules` — found, and deliberately not turned into an edge
    (`rm`, `ls`, `mkdir`, `touch`, `stat`, `test`).
  - `claims` — paths read out of a script fed to an interpreter's stdin, the body
    of `python3 <<PY`. Not arguments of the interpreter: literals found inside the
    program, with a guess at direction.
- `assignments` — variable assignments in the command, and whether the value can
  be known from the text alone. A value built by `$(…)` cannot, which is the
  commonest reason a path never becomes a node.
- `note` — present only when something is worth saying out loud.

## What we are asking you to check

1. **Missing commands.** Is there a program being run that the split does not
   list? Nesting is the usual cause: `$( )`, a pipeline, a subshell, a heredoc,
   a `for` body, an `if` branch.
2. **Wrong boundaries.** Does `runs` name the program that does the work, or a
   wrapper? Are its arguments its own, or the wrapper's?
3. **Lost redirects.** Every `>`/`>>`/`<`/heredoc in the command should appear
   against exactly one command, the one the shell attaches it to.
4. **Wrong substitution.** Where `resolved` is shown, is that the value the
   variable actually had at that point?
5. **Anything a reader of shell would say the split gets wrong.**

Please answer per section id (`a[12]`), and say which of the five it is. Where a
command is genuinely ambiguous, say so rather than guessing -- we would rather
record an ambiguity than a wrong fix.

## Known gaps, so you need not report them

- A `$(…)` inside an assignment (`LOG=$(ls -t *.log | head -1)`) is not entered
  at all: the walk stops at the assignment. 63 occurrences in the reference
  session. Recorded as a known defect.
- `~` is not expanded, because the command text does not carry HOME.
- A variable whose value is produced at run time is never resolved; the split
  shows the token as written.
- A `cd` part-way through a command does not move the directory that later paths
  resolve against. The effective directory is decided once, before the walk, so
  after `cd /app && cd sub` a `> out.txt` is recorded under `/app`. 8.5% of 1524
  reference solutions in an external corpus contain more than one `cd`.
- `cp` and `mv` produce no edge in either direction, though the first argument is
  read and the second written. 56 occurrences in that same corpus.
- A URL is never a node. `curl http://host/x.txt -o x.txt` records the local file
  and not where it came from.
- `mkdir`, `rm`, `ls`, `touch`, `stat` and `test` are found and deliberately
  dropped: they move no content.
"""


def _actions_from_jsonl(path: str):
    """`normalize.Action`s from a file of `{id, command, cwd}` objects.

    So a corpus that never came from a session can be dumped by the same
    `record`: external shell -- another benchmark's reference solutions, a
    Dockerfile's `RUN` lines -- is worth auditing precisely because it was not
    written by the agent whose traces shaped these rules. On 1524 reference
    solutions from one such corpus the parse failure rate was 0.07% against 2.48%
    on our own, which says the forms we fail on are the model's habits rather than
    shell's.

    A real `Action` rather than a stand-in, and no session variables carried
    between entries: each is independent, which a session's commands are not.
    """
    from normalize.schema import Action
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            command = d.get("command") or d.get("solve_sh") or ""
            # The corpus stamps every file with a canary comment against training
            # contamination. It is not shell, and repeating it 1524 times to a
            # model is the one thing the stamp asks us not to do.
            command = "\n".join(
                l for l in command.splitlines()
                if "BENCHMARK DATA SHOULD NEVER APPEAR" not in l
                and "harbor-canary" not in l)
            if not command.strip():
                continue
            out.append(Action(
                idx=n, call_idx=n, op_idx=0, ts="", turn=1, run=1,
                raw_tool="execute_bash", tool="shell", action="run_command",
                purpose=d.get("domain") or "",
                command=command, cwd=d.get("cwd") or d.get("workdir"),
                args={}, response=None,
                ref=d.get("id") or d.get("task_id") or f"line {n}"))
    return out


def _find_session(target: str, official_dir: str,
                  named: str = "") -> tuple[str, str]:
    """`(session_id, official_dir)` from a folder, a file, or an id.

    `named` wins over anything found by looking: an explicit `--session` was
    ignored here at first, because the folder branch exited on ambiguity before
    the caller had a chance to apply it.
    """
    if named and os.path.isdir(target):
        return named, target
    if os.path.isfile(target):
        base = os.path.basename(target)
        for suffix in (".jsonl", ".json", ".history"):
            if base.endswith(suffix):
                return base[:-len(suffix)], os.path.dirname(target) or "."
        return base, os.path.dirname(target) or "."
    if os.path.isdir(target):
        found = sorted(glob.glob(os.path.join(target, "*.jsonl")))
        ids = sorted({os.path.basename(f)[:-len(".jsonl")] for f in found})
        if len(ids) == 1:
            return ids[0], target
        if not ids:
            sys.exit(f"no *.jsonl session record under {target}")
        sys.exit(f"{len(ids)} sessions under {target}; name one with "
                 f"--session:\n  " + "\n  ".join(ids[:20]))
    return target, official_dir


def record(action, env: Optional[dict] = None) -> dict:
    """One command, its split, and the edges the graph was built from.

    The edges come from `build._command_paths` -- the same call the graph makes,
    with the same `working_dir` and carried `env` -- rather than from
    `shell_ast.extract` directly. Three things differed before, and all three made
    the dump describe something the graph was not built from:

      * no `working_dir`, so a relative path resolved to nothing: 44 of 140
        commands in one session;
      * no carried `env`, so a variable set by an earlier command stayed a
        variable;
      * no fallback, so the 1.5% of commands the tree cannot parse were reported
        as empty when the graph had used the regex path for them.
    """
    command = action.command or ""
    # From the action, by the graph's own rule. Taking it as a parameter meant two
    # sources that could disagree, and they did: the caller passed
    # `args.working_dir` while the graph resolves against `cwd`.
    d = action.to_dict() if hasattr(action, "to_dict") else dict(action)
    wd = command_cwd(d)
    out = {
        # `ref` when the corpus has its own name for the entry, so a report can be
        # taken back to the file it came from.
        "id": getattr(action, "ref", None) or f"a[{action.idx}]",
        "turn": action.turn,
        "action": action.action,
        "purpose": action.purpose or "",
        "command": command,
        "cwd": wd or "",
    }
    try:
        st = ic.structure(command, wd, env)   # wd from build.command_cwd
    except ParseFailure as exc:                       # structure() catches, belt
        st = {"ok": False, "why": str(exc), "commands": [], "assignments": []}

    # The real entry point, handed the real action rather than a dict assembled
    # here: assembling one meant choosing which fields to copy, and the choice was
    # wrong -- `args.working_dir` where the graph uses `cwd`, which put a[109]'s
    # relative paths under a tree that does not exist.
    from graph.build import _command_paths
    claims, skipped, weak = _command_paths(d, env or {})
    out["edges"] = sorted((p, d, why) for p, d, why in claims)
    out["skipped"] = [list(x) if isinstance(x, tuple) else [str(x), ""]
                      for x in (skipped or ())]
    if weak:
        out["weak"] = sorted((p, d, why) for p, d, why in weak)
    out["via"] = "syntax tree" if st.get("ok") else "regex fallback"
    out["parsed"] = bool(st.get("ok"))
    if not st.get("ok"):
        out["note"] = ("the syntax tree could not be built: "
                       + str(st.get("why", "unknown"))
                       + ". The extractor falls back to regexes for this one, so "
                         "the split below is empty rather than wrong.")
    out["split"] = [{
        "runs": c.get("runs") or c.get("name") or "",
        "through": c.get("wrappers") or [],
        "positional": c.get("positional") or [],
        "options": c.get("options") or [],
        "redirects": c.get("redirects") or [],
        "resolved": c.get("resolved") or {},
        "in": c.get("context") or [],
        "ignored": bool(c.get("ignored")),
        # Paths read out of a script fed to an interpreter's stdin. Not arguments
        # of the interpreter, so they are their own field: 306 edges across 21
        # sessions came from here with no record at all, and no review of the
        # split could have questioned them.
        "claims": c.get("claims") or [],
    } for c in st.get("commands", ())]
    out["assignments"] = [{
        "name": a["name"], "value": a["value"],
        "knowable": bool(a["literal"]),
    } for a in st.get("assignments", ())]
    if st.get("ok") and not out["split"]:
        out["note"] = ("the extractor found no command node here. If the command "
                       "clearly runs something, this is a defect worth "
                       "reporting.")
    return out


def as_markdown(rec: dict) -> str:
    lines = [f"## {rec['id']}  ·  turn {rec['turn']}  ·  {rec['action']}"]
    if rec["purpose"]:
        lines.append(f"_{rec['purpose']}_")
    if rec["cwd"]:
        lines.append(f"cwd: `{rec['cwd']}`")
    lines.append("")
    lines.append("### command")
    # A fence long enough to survive a command that contains one.
    fence = "```"
    while fence in rec["command"]:
        fence += "`"
    lines.append(fence + "sh")
    lines.append(rec["command"])
    lines.append(fence)
    lines.append("")

    if rec["assignments"]:
        lines.append("### assignments")
        for a in rec["assignments"]:
            mark = "knowable from the text" if a["knowable"] else \
                   "built at run time — not knowable"
            lines.append(f"- `{a['name']}={a['value']}` — {mark}")
        lines.append("")

    lines.append("### split")
    if not rec["split"]:
        lines.append("- (none)")
    for i, c in enumerate(rec["split"], 1):
        bits = [f"runs `{c['runs']}`"]
        if c["through"]:
            bits.append("through " + " → ".join(f"`{w}`" for w in c["through"]))
        if c["positional"]:
            bits.append("positional " + ", ".join(f"`{a}`" for a in c["positional"]))
        if c["options"]:
            bits.append("options " + ", ".join(f"`{a}`" for a in c["options"]))
        if c["redirects"]:
            bits.append("redirects " + ", ".join(f"`{r}`" for r in c["redirects"]))
        if c["in"]:
            bits.append("in " + ", ".join(c["in"]))
        if c["ignored"]:
            bits.append("**ignored by the rules**")
        if c.get("claims"):
            bits.append("paths read out of the script body: "
                        + ", ".join(f"`{x}`" for x in c["claims"]))
        lines.append(f"{i}. " + "  ·  ".join(bits))
        for tok, val in c["resolved"].items():
            lines.append(f"   - `{tok}` resolved to `{val}`")
    lines.append("### edges the graph was built from")
    lines.append(f"via: {rec.get('via', '?')}")
    if not rec.get("edges"):
        lines.append("- (none)")
    for path, direction, why in rec.get("edges", ()):
        arrow = "writes" if direction == "write" else "reads "
        lines.append(f"- {arrow} `{path}` — {why}")
    if rec.get("skipped"):
        lines.append("")
        lines.append("seen and deliberately not claimed:")
        for what, why in rec["skipped"]:
            lines.append(f"- `{what}`" + (f" — {why}" if why else ""))
    lines.append("")
    if rec.get("note"):
        lines.append(f"> note: {rec['note']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m graph.tests.dump_splits",
        description="Every command in a session, with the split the extractor "
                    "made of it, in a form a language model can audit.")
    p.add_argument("target", help="a session folder, a session record, an id, or "
                                  "-- with --jsonl -- a file of commands")
    p.add_argument("--jsonl", action="store_true",
                   help="`target` is a file of {id, command, cwd} objects rather "
                        "than a session: for auditing shell that came from "
                        "somewhere else")
    p.add_argument("--session", default="", help="the id, when the folder holds "
                                                 "several")
    p.add_argument("--official-dir", default="",
                   help="where the records live, when `target` is an id")
    p.add_argument("--out", default="", help="write here instead of stdout")
    p.add_argument("--format", default="md", choices=("md", "jsonl"),
                   help="md is for reading; jsonl is one record per line")
    p.add_argument("--limit", type=int, default=0, help="stop after N commands")
    p.add_argument("--only-unsplit", action="store_true",
                   help="only commands the extractor found nothing in -- the "
                        "shortest list worth auditing first")
    p.add_argument("--no-preamble", action="store_true",
                   help="omit the explanation and the questions")
    args = p.parse_args(argv)

    if not shell_ast.available():
        sys.exit("tree-sitter-bash is not installed: there would be no split to "
                 "dump. `pip install tree-sitter tree-sitter-bash`")

    import warnings
    warnings.filterwarnings("ignore")

    if args.jsonl:
        actions = _actions_from_jsonl(args.target)
        session, total = args.target, len(actions)
        if not actions:
            sys.exit(f"no commands in {args.target!r}")
    else:
        session, official = _find_session(args.target, args.official_dir,
                                          args.session)
        from normalize import load_trace_from_official
        ir = load_trace_from_official(session, official or None,
                                      include_responses=True)
        if not ir.actions:
            sys.exit(f"no actions for session {session!r} under {official!r}")
        actions = ir.actions
        total = len(actions)

    # Carried across commands by the graph's own function, not a copy of it: a
    # value set by one command is what the next one's `$VAR` resolves against.
    # Not carried between entries of a `--jsonl` corpus: those are independent
    # commands, and a value leaking from one into the next would invent an edge.
    from graph.build import carry_env
    session_env: dict = {}

    recs = []
    for a in actions:
        if not a.command:
            continue
        rec = record(a, session_env)
        if not args.jsonl:
            carry_env(session_env, a.command, a.to_dict().get("response"))
        if args.only_unsplit and rec["split"]:
            continue
        recs.append(rec)
        if args.limit and len(recs) >= args.limit:
            break

    chunks = []
    if args.format == "md":
        if not args.no_preamble:
            chunks.append(PREAMBLE)
            chunks.append(f"session: `{session}`\n\n"
                          f"{len(recs)} command(s) of "
                          f"{sum(1 for a in actions if a.command)}; "
                          f"{total} entries in total.\n")
        chunks.extend(as_markdown(r) for r in recs)
        body = "\n---\n\n".join(chunks)
    else:
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        print(f"{len(recs)} command(s) written to {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
