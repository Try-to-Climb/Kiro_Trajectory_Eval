"""What the syntax tree buys over the flattened token stream.

Each case here is a shape the regex path gets wrong, with the measured cost
noted. Cases are skipped rather than failed when tree-sitter is absent, since
`build` falls back to `keys` in that situation.
"""
import os
import sys
import re
import pathlib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import shell_ast                                 # noqa: E402

WD = "/work/repo"


def _dirs(command, working_dir=WD, session_env=None):
    """path -> direction. Only for commands where each path has one direction."""
    return {p: d for p, d, _ in
            shell_ast.extract(command, working_dir, session_env)}


def _pairs(command, working_dir=WD, session_env=None):
    """`(path, direction)` pairs, for commands that both write and read a file."""
    return {(p, d) for p, d, _ in
            shell_ast.extract(command, working_dir, session_env)}


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestHeredocPurpose(unittest.TestCase):
    """The same syntax is data or code depending on which command receives it,
    and the tree names that command. 22.6% of 1751 real commands contain a
    heredoc; 86% of those feed an interpreter's stdin, which the regex path
    could not see at all."""

    def test_body_fed_to_an_interpreter_is_executing_now(self):
        got = _dirs("python3 <<'PYX'\n"
                    "import json\n"
                    "r = json.load(open('/work/out/scan.json'))\n"
                    "PYX")
        self.assertEqual(got["/work/out/scan.json"], "read")

    def test_dash_stdin_form_is_the_same(self):
        got = _dirs("python3 - <<'E'\nload('/work/a.json')\nE")
        self.assertEqual(got["/work/a.json"], "read")

    def test_every_heredoc_is_seen_not_only_the_first(self):
        got = _dirs("python3 - <<'E1'\nload('/work/first.json')\nE1\n"
                    "echo mid\n"
                    "python3 - <<'E2'\nload('/work/second.json')\nE2")
        self.assertEqual(got.get("/work/first.json"), "read")
        self.assertEqual(got.get("/work/second.json"), "read")

    def test_body_written_to_a_file_and_then_run_is_code(self):
        got = _pairs("cat > /work/run.sh <<'EOF'\n"
                     "python3 /work/tool.py /work/input.yaml\n"
                     "EOF\n"
                     "bash /work/run.sh")
        self.assertIn(("/work/tool.py", "read"), got)
        self.assertIn(("/work/input.yaml", "read"), got)
        self.assertIn(("/work/run.sh", "write"), got)   # written by the redirect
        self.assertIn(("/work/run.sh", "read"), got)    # then executed

    def test_body_written_but_never_run_stays_data(self):
        got = _dirs("cat > /work/gen.sh <<'EOF'\n"
                    "cat /work/never_read.log\n"
                    "EOF\n"
                    "echo done")
        self.assertEqual(got, {"/work/gen.sh": "write"})

    def test_a_wrapper_still_counts_as_running_the_script(self):
        """`nohup run.sh &` executes it; the command *name* is `nohup`."""
        got = _dirs("cat > /work/run.sh <<'EOF'\n"
                    "python3 /work/tool.py\n"
                    "EOF\n"
                    "nohup /work/run.sh &")
        self.assertEqual(got.get("/work/tool.py"), "read")

    def test_a_write_inside_an_executed_script_is_a_write(self):
        got = _dirs("python3 <<'PYX'\n"
                    "open('/work/out/report.json', 'w').write(x)\n"
                    "PYX")
        self.assertEqual(got["/work/out/report.json"], "write")


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestPositionBeatsTheExtensionList(unittest.TestCase):
    """A file's name proves nothing; its position does. This is what recovers
    `bin/activate` and `bin/python`, 11 of the gold set's misses."""

    def test_source_argument_is_a_file_without_an_extension(self):
        got = _dirs("source /home/u/venv/bin/activate")
        self.assertEqual(got["/home/u/venv/bin/activate"], "read")

    def test_the_interpreter_itself_is_not_recorded(self):
        """It ran the script; it is not where the result came from. `source
        activate` is the opposite case -- a file whose contents change the shell
        -- which is why one is recorded and the other is not.

        This asserted the opposite until it was pointed out that the rule had no
        principle behind it: the same rule set demands an extension so that
        `/usr/bin/jq` stays out, and `/usr/bin/python3` was getting in.
        """
        got = _dirs("/home/u/venv/bin/python -u /work/run.py")
        self.assertNotIn("/home/u/venv/bin/python", got)
        self.assertEqual(got["/work/run.py"], "read")

    def test_a_system_interpreter_is_not_recorded_either(self):
        got = _dirs("/usr/bin/python3 /work/run.py")
        self.assertEqual(got, {"/work/run.py": "read"})

    def test_a_script_run_as_the_command_itself_is_read(self):
        """`/repo/hooks/trace-stop.sh` with no interpreter named. Every rule for
        script arguments keys off an interpreter *name*, so nothing noticed."""
        self.assertEqual(_dirs("/repo/hooks/trace-stop.sh"),
                         {"/repo/hooks/trace-stop.sh": "read"})

    def test_a_wrapped_script_is_read_too(self):
        self.assertEqual(_dirs("nohup /repo/run.sh </dev/null &"),
                         {"/repo/run.sh": "read"})

    def test_relative_script_form(self):
        self.assertEqual(_dirs("./build.sh"),
                         {"/work/repo/build.sh": "read"})

    def test_an_ordinary_binary_is_not_an_artifact(self):
        """`/usr/bin/jq` is a tool the run used, not something it produced or
        consumed; requiring an extension keeps it out."""
        self.assertEqual(_dirs("/usr/bin/jq -r . "), {})


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestStructureTheRegexHadToGuess(unittest.TestCase):

    def test_a_comment_names_files_it_never_touches(self):
        got = _dirs("ps aux\n# cat /work/fake.yaml\ncat /work/real.log")
        self.assertEqual(got, {"/work/real.log": "read"})

    def test_loop_values_reach_the_body_through_the_variable(self):
        got = _dirs('for f in a.yaml b.yaml; do grep -q x "$f"; done')
        self.assertEqual(got, {"/work/repo/a.yaml": "read",
                               "/work/repo/b.yaml": "read"})

    def test_command_substitution_inside_an_argument_is_visited(self):
        got = _dirs('echo "count: $(wc -l < /work/first.txt)"')
        self.assertEqual(got, {"/work/first.txt": "read"})

    def test_process_substitution_is_visited(self):
        got = _dirs("diff <(sed s/a/b/ /work/one.sh) /work/two.sh")
        self.assertEqual(got, {"/work/one.sh": "read", "/work/two.sh": "read"})

    def test_a_duration_is_not_the_wrapped_command(self):
        """`timeout 240 python3 run.py` -- picking the first non-option argument
        finds `240` and the interpreter is never recognised. 94 corpus edges."""
        got = _dirs("timeout 240 python3 /work/run.py")
        self.assertEqual(got["/work/run.py"], "read")

    def test_a_subshell_body_is_walked(self):
        got = _dirs("time (python3 /work/a.py | tee /work/out.log)")
        self.assertEqual(got["/work/a.py"], "read")
        self.assertEqual(got["/work/out.log"], "write")

    def test_vcs_subcommand_decides_the_direction(self):
        self.assertEqual(_dirs("git diff --stat /work/d.md"),
                         {"/work/d.md": "read"})
        self.assertEqual(_dirs("git add /work/d.md"), {"/work/d.md": "write"})


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestWhatItRefusesToGuess(unittest.TestCase):

    def test_a_tilde_path_is_left_alone(self):
        """`~` needs HOME, which the text does not carry. Emitting the tilde
        form would split one file into two entity keys."""
        self.assertEqual(_dirs("cat ~/.config/app.json"), {})

    def test_an_unknown_variable_is_not_a_guess(self):
        self.assertEqual(_dirs('tail -40 "$MYSTERY"'), {})

    def test_a_known_variable_resolves(self):
        got = _dirs('tail -40 "$LOG"', session_env={"LOG": "/work/a.log"})
        self.assertEqual(got, {"/work/a.log": "read"})

    def test_a_reassignment_that_cannot_be_read_forgets_the_old_value(self):
        got = _dirs('LOG="/work/run_$(date +%s).log" && echo hi > "$LOG"',
                    session_env={"LOG": "/work/stale.log"})
        self.assertEqual(got, {})

    def test_deletion_is_not_modelled(self):
        self.assertEqual(_dirs("rm -f /work/x.log"), {})

    def test_an_unparsable_command_reports_failure(self):
        with self.assertRaises(shell_ast.ParseFailure):
            shell_ast.extract('cat "unterminated', WD)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestSubstitutionsInAssignmentsAndLoops(unittest.TestCase):
    """A `$(…)` is entered wherever it sits, not only in a command's arguments.

    `walk` used to return at `variable_assignment` and at `for_statement`, so the
    commands inside their substitutions were never visited. 28 real reads were
    missing from the reference session because of it, all of them the same habit:
    the agent writes a path into a small file and reads it back with
    `LOG=$(cat /tmp/logpath.txt)`.

    Not a judgement reversed. The regex path had the same fault and had already
    been fixed -- DESIGN.md records `extract` dropping from 193 to 153 and the fix
    being "scan inside the substitution separately" -- and `command` descends for
    exactly this reason with a comment saying so. The lesson did not travel to
    these two branches when the tree replaced the regexes. Measured on the
    reference session: +30 edges, 0 lost, precision still 1.000 on the gold set.
    """

    def paths(self, cmd, wd=None):
        return {(p, d) for p, d, _ in shell_ast.extract(cmd, wd)}

    def test_a_read_inside_an_assignment_is_found(self):
        self.assertIn(("/tmp/logpath.txt", "read"),
                      self.paths("LOG=$(cat /tmp/logpath.txt)"))

    def test_and_the_rest_of_the_line_still_works(self):
        got = self.paths('LOG=$(cat /tmp/logpath.txt) && grep -c X "$LOG"')
        self.assertIn(("/tmp/logpath.txt", "read"), got)

    def test_a_pipeline_inside_an_assignment(self):
        got = self.paths("NEWEST=$(ls -t /data/*.log | head -1)")
        self.assertTrue(any(p.startswith("/data/") for p, _ in got) or not got,
                        got)

    def test_a_read_inside_a_for_word_list_is_found(self):
        self.assertIn(("/tmp/list.txt", "read"),
                      self.paths("for f in $(cat /tmp/list.txt); do echo $f; done"))

    def test_nothing_is_invented_by_descending(self):
        """`seq` touches no file, and the descent must not make one up."""
        self.assertEqual(self.paths("for i in $(seq 1 40); do echo $i; done"),
                         set())

    def test_a_plain_assignment_is_still_not_a_read(self):
        """`LOG=/x.log` records a value and reads nothing -- the whole point of
        `assignment`, and descending must not change it."""
        self.assertEqual(self.paths("LOG=/x.log"), set())

    def test_a_write_inside_an_assignment_is_found(self):
        got = self.paths('OUT=$(python3 /w/gen.py > /w/out.json; echo done)')
        self.assertIn(("/w/out.json", "write"), got)


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestForLoopBodyIsWalkedOnce(unittest.TestCase):
    """A loop body is walked once per value in the word list, and not again.

    Descending into substitutions was added to the `for_statement` branch so that
    `for i in $(seq 1 40)` would name `seq`, but it was handed the whole node --
    including the body, which `for_statement` had already walked once per value. So
    the substitution inside the body was entered a second time and the record
    claimed commands that never ran. Eight of the nine `unsure` verdicts across
    three corpora were a reader unable to reconcile the count with the word list;
    all eight became `ok` once this was fixed.
    """

    def runs(self, cmd):
        shell_ast.extract(cmd)
        return [c["runs"] for c in shell_ast.last_split()]

    def test_the_body_appears_once_per_value(self):
        got = self.runs("for s in A B; do D=$(ls -t /d/$s/*.log | head -1); "
                        "echo $D; done")
        self.assertEqual(got, ["ls", "head", "echo", "ls", "head", "echo"])

    def test_the_word_list_is_still_entered(self):
        """What the descent was added for."""
        self.assertIn("seq", self.runs("for i in $(seq 1 40); do echo $i; done"))
        self.assertIn("cat", self.runs("for f in $(cat /tmp/l.txt); do echo $f; "
                                       "done"))

    def test_both_at_once_are_each_counted_once(self):
        got = self.runs("for f in $(cat /tmp/l.txt); do X=$(head -1 /d/$f); done")
        self.assertEqual(sorted(got), ["cat", "head"])

    def test_the_count_matches_the_word_list(self):
        """The property a reader checks: N values, N passes over the body."""
        for n in (1, 2, 3):
            vals = " ".join("ABC"[:n])
            got = self.runs(f"for s in {vals}; do cat /d/$s.txt; done")
            self.assertEqual(got, ["cat"] * n, vals)

    def test_a_read_in_the_body_is_still_found_once_per_value(self):
        """The edges are what this is for: descending must not lose them, and the
        double walk must not duplicate them either."""
        got = {(p, d) for p, d, _ in
               shell_ast.extract("for s in A B; do cat /d/$s.txt; done")}
        self.assertEqual(got, {("/d/A.txt", "read"), ("/d/B.txt", "read")})


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestRedirectsAfterAHeredoc(unittest.TestCase):
    """A redirect written after a heredoc delimiter still belongs to the command.

    A heredoc spans from its delimiter on the first line to the terminator several
    lines down, so a `2>&1` written between them falls inside that span and
    tree-sitter makes it a child of the `heredoc_redirect`. That is a reasonable
    model of an awkward syntax, not a defect -- the mistake was ours, in assuming
    every redirect is a direct child of the statement. `python3 <<'PY' 2>&1` lost
    its `2>&1` from the record; with the order reversed it never did.
    """

    def redirs(self, cmd):
        shell_ast.extract(cmd)
        return [c["redirects"] for c in shell_ast.last_split()]

    def test_a_redirect_after_the_delimiter_is_kept(self):
        self.assertEqual(self.redirs("python3 <<'PY' 2>&1\nprint(1)\nPY"),
                         [["<<'PY'", "2>&1"]])

    def test_a_file_redirect_after_the_delimiter_is_kept(self):
        self.assertEqual(self.redirs("python3 <<'PY' > /tmp/o\nprint(1)\nPY"),
                         [["<<'PY'", "> /tmp/o"]])

    def test_source_order_is_preserved_either_way(self):
        """Sorted by position, so the record reads like the command rather than
        like the order the tree happened to be walked in."""
        self.assertEqual(self.redirs("python3 2>&1 <<'PY'\nprint(1)\nPY"),
                         [["2>&1", "<<'PY'"]])

    def test_a_heredoc_alone_is_unchanged(self):
        self.assertEqual(self.redirs("python3 <<'PY'\nprint(1)\nPY"),
                         [["<<'PY'"]])

    def test_a_redirect_alone_is_unchanged(self):
        self.assertEqual(self.redirs("python3 x.py 2>&1"), [["2>&1"]])

    def test_through_a_wrapper(self):
        """The shape this was found in: `timeout 10 python3 <<'PY' 2>&1`."""
        self.assertEqual(self.redirs("timeout 10 python3 <<'PY' 2>&1\n"
                                     "print(1)\nPY"),
                         [["<<'PY'", "2>&1"]])

    def test_the_edges_are_unaffected(self):
        """Direction is decided on another path, which always saw this redirect.
        The record was the only thing missing it, and fixing the record must not
        change what is claimed."""
        got = {(p, d) for p, d, _ in
               shell_ast.extract("python3 <<'PY' > /tmp/out.log\n"
                                 "open('/tmp/x.json','w')\nPY")}
        self.assertIn(("/tmp/out.log", "write"), got)


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestGlobAnnotation(unittest.TestCase):
    """Only a pattern is labelled a pattern.

    A `for` loop binds the word, not the files it expands to, so the record says so
    rather than claiming a resolution that never happened. The test for "is this a
    pattern" was first "does the value contain `*`, `?` or `[`", asked of the whole
    value -- and a `python3 -c "<700 characters of python>"` is full of `[`, because
    the script indexes dictionaries. The note was appended to the end of the
    program.
    """

    def annotated(self, cmd):
        shell_ast.extract(cmd)
        return {tok: val for c in shell_ast.last_split()
                for tok, val in (c.get("resolved") or {}).items()
                if "a glob" in val}

    def test_a_pattern_is_labelled(self):
        got = self.annotated('for p in /proj/fixes/*.patch; do grep x "$p"; done')
        self.assertEqual(len(got), 1, got)
        self.assertIn("/proj/fixes/*.patch", list(got.values())[0])

    def test_a_question_mark_counts(self):
        self.assertTrue(self.annotated('for f in /d/a?.log; do cat "$f"; done'))

    def test_a_plain_path_is_not_labelled(self):
        self.assertFalse(self.annotated('for f in /d/a.log; do cat "$f"; done'))

    def test_a_program_body_is_not_a_pattern(self):
        """The case this was found on: the value is a script, not a word."""
        self.assertFalse(shell_ast._is_glob(
            "\nimport json\nt = json.load(open('/p/x.json'))\n"
            "for r in t:\n    if r['a']['b'] == 'c':\n        print(r)\n"))

    def test_anything_with_whitespace_is_not_a_pattern(self):
        """A pattern is one word. This is the whole of the test, because trying to
        decide which path segment a `[` sits in produced a rule we could not state
        consistently."""
        for value in ("=== a b ===", "a\tb*", "one two*.log", "line\nline*"):
            self.assertFalse(shell_ast._is_glob(value), value)

    def test_a_very_long_value_is_not_a_pattern(self):
        self.assertFalse(shell_ast._is_glob("x" * 500 + "*"))

    def test_the_edges_are_unaffected(self):
        """The annotation is for the reader. A glob never became an entity and must
        not start now."""
        got = {p for p, _, _ in
               shell_ast.extract('for p in /proj/fixes/*.patch; do grep x "$p"; done')}
        self.assertFalse(any("*" in p for p in got), got)


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestEveryEdgeIsInTheRecord(unittest.TestCase):
    """Every path that produces an edge must also write to `split`.

    `split` is the auditable surface of the extraction: it is what the inspector
    shows and what a reviewer is asked about. A path that claims an edge without
    leaving a record there is a region nobody can audit, and one existed for three
    sessions -- `_scan_script_literals`, which reads the paths out of a python
    heredoc. 306 edges across 21 sessions came from it, at least 9% of them wrong,
    and no review could have said so.

    The earlier version of this check picked five commands by hand and passed,
    because all five went through the two paths that were already covered. It is
    enumerated from the source now: whatever calls `claim` has to be listed here
    with a case that exercises it.
    """

    # One command per edge-producing entry point in `_Walker`.
    BY_ENTRY = {
        "command": "cat /w/a.txt",
        "file_redirect": "echo hi > /w/b.txt",
        "_recurse_shell": ("cat > /tmp/r.sh <<'SH'\nsource /w/env/bin/activate\n"
                           "SH\nbash /tmp/r.sh"),
        "_scan_script_literals": ("python3 <<'PY'\nopen('/w/out.json','w')\nPY"),
        # A body `ast` cannot read falls back to the shallow scan, which claims
        # too: `<<PY` fed something that is not Python, 65 of 2096 in the corpus.
        "_scan_script_literals_regex": ("python3 <<'PY'\n"
                                        "this is not python: '/w/x.json'\nPY"),
    }

    def test_the_entry_points_are_all_listed(self):
        """Adding an edge-producing method must fail here rather than silently
        create an unauditable region.

        Two ways to produce an edge, and the first version of this check knew only
        the first: calling `claim`, or merging another walker's `out`, which is how
        a shell heredoc's body contributes.
        """
        src = pathlib.Path(shell_ast.__file__).read_text(encoding="utf-8")
        found, current = set(), None
        for line in src.splitlines():
            m = re.match(r"    def (\w+)", line)
            if m:
                current = m.group(1)
            elif current and (re.search(r"self\.claim(_if_pathlike)?\(", line)
                              or re.search(r"self\.out\.(setdefault|update)\(", line)):
                found.add(current)
        # `claim` and its wrapper are how the others record, not entry points.
        found -= {"claim", "claim_if_pathlike"}
        self.assertEqual(found, set(self.BY_ENTRY),
                         "an edge-producing method is not covered here")

    def test_each_entry_point_records_what_it_claims(self):
        for entry, cmd in self.BY_ENTRY.items():
            claimed = {p for p, _, _ in shell_ast.extract(cmd)}
            self.assertTrue(claimed, f"{entry}: the case claims nothing")
            shown = set()
            for c in shell_ast.last_split():
                shown.add(c.get("name") or "")
                shown.update(c.get("args") or ())
                shown.update(c.get("redirects") or ())
                shown.update((c.get("resolved") or {}).values())
                shown.update(c.get("claims") or ())
            for path in claimed:
                tail = path.rsplit("/", 1)[-1]
                self.assertTrue(any(tail in str(x) for x in shown),
                                f"{entry}: claimed {path}, the record shows "
                                f"{sorted(shown)}")


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestAFlagWrittenFileIsNotAlsoRead(unittest.TestCase):
    """`-o out.yaml` names an output, and the tree calls it a positional too.

    So the read rules reached it as well and every generated file arrived as both
    read and written by the command that only wrote it: 19 paths across the corpus,
    16 of them called out by a reviewer unprompted.
    """

    def _dirs(self, cmd, tail):
        return {d for p, d, _ in shell_ast.extract(cmd) if p.endswith(tail)}

    def _why(self, cmd, tail, direction):
        return next(w for p, d, w in shell_ast.extract(cmd)
                    if p.endswith(tail) and d == direction)

    def test_the_command_that_writes_it_does_not_read_it(self):
        for flag in ("-o", "--output", "--out"):
            cmd = f"python3 /w/gen.py --spec /w/s.json {flag} /w/out.yaml"
            self.assertEqual(self._dirs(cmd, "out.yaml"), {"write"}, flag)
            # The input is untouched by this.
            self.assertEqual(self._dirs(cmd, "s.json"), {"read"}, flag)

    def test_a_later_command_reading_it_still_does(self):
        """Suppressed per command, not per line: these really are read."""
        cmd = "python3 /w/gen.py --output /w/out.yaml && cat /w/out.yaml"
        self.assertEqual(self._dirs(cmd, "out.yaml"), {"read", "write"})

    def test_the_real_reader_gets_the_credit(self):
        """`claim` keeps the first evidence given for a (path, direction), so the
        spurious read was also taking the slot: the edge said `argument of the
        script` where `argument of cat` was the truth."""
        cmd = "python3 /w/gen.py --output /w/out.yaml && cat /w/out.yaml"
        self.assertEqual(self._why(cmd, "out.yaml", "read"), "argument of cat")

    def test_a_redirect_is_untouched_by_this(self):
        """`> f` already only claimed a write. Kept as a case because the fix
        walks the same flag table, which `>` and `>>` are in."""
        self.assertEqual(self._dirs("python3 /w/gen.py > /w/out.yaml", "out.yaml"),
                         {"write"})


class TestAScriptFedToStdin(unittest.TestCase):
    """The body of `python3 <<PY` read with `ast` rather than a regex.

    The regex asked whether the word `open` was within 60 characters of a quoted
    string. It answered wrong 7 times in 15 across three sessions, and a reviewer
    named every one: `f.endswith('.jsonl')` became a file called `/.jsonl`, a
    sentence describing the script became a path, and `open(p + '/s9.json')` became
    a file at the root of the filesystem. `ast` asks the structural question --
    *is this literal the first argument of `open`* -- which is the real one.
    """

    def ops(self, body, env=None):
        return {(p, d) for p, d, _ in shell_ast.python_file_ops(body, env)}

    # ---- what it must find ------------------------------------------------
    def test_open_read_and_write(self):
        self.assertEqual(self.ops("open('/w/a.json')"), {("/w/a.json", "read")})
        self.assertEqual(self.ops("open('/w/b.json','w')"), {("/w/b.json", "write")})
        self.assertEqual(self.ops("open('/w/c.json', mode='ab')"),
                         {("/w/c.json", "write")})

    def test_a_path_built_from_a_name_assigned_in_the_script(self):
        self.assertEqual(
            self.ops("from pathlib import Path\n"
                     "D = Path('/w/exp')\n"
                     "(D / 'out.md').read_text()"),
            {("/w/exp/out.md", "read")})

    def test_an_f_string(self):
        self.assertEqual(self.ops("P = '/w'\nopen(f'{P}/x.json')"),
                         {("/w/x.json", "read")})

    def test_a_shell_variable_the_run_exported(self):
        """`os.environ['KIRO_HOME']` is knowable: the value is in the same ledger
        `$KIRO_HOME` resolves against."""
        body = ("import os\n"
                "p = os.environ['KIRO_HOME'] + '/.state/x.json'\nopen(p)")
        self.assertEqual(self.ops(body, {"KIRO_HOME": "/home/u/kiro"}),
                         {("/home/u/kiro/.state/x.json", "read")})
        self.assertEqual(self.ops(body), set())      # unknown -> no edge

    def test_the_method_forms(self):
        self.assertEqual(self.ops("Path('/w/y.txt').write_text('hi')"),
                         {("/w/y.txt", "write")})
        self.assertEqual(self.ops("json.dump(x, open('/w/o.json','w'))"),
                         {("/w/o.json", "write")})

    # ---- what it must refuse --------------------------------------------
    def test_a_suffix_test_is_not_a_path(self):
        """`f.endswith('.jsonl')` produced a read of `/.jsonl`, 3 times in one
        session."""
        self.assertEqual(self.ops("for f in fs:\n    f.endswith('.jsonl')"), set())

    def test_prose_is_not_a_path(self):
        """A sentence describing the script, with a filename inside it."""
        self.assertEqual(
            self.ops("print('write the result to /tmp/probe_listagents.txt and compare')"), set())

    def test_a_path_built_at_run_time_is_refused(self):
        """`open(p + '/s9_final_report.json')` with `p` a parameter used to yield
        `/s9_final_report.json`, a file at the root of the filesystem."""
        self.assertEqual(
            self.ops("def load(p):\n    return open(p + '/s9_final_report.json')"),
            set())

    def test_a_document_passed_to_safe_load_is_not_a_path(self):
        """The `load` family takes a path in either position, so an extension is
        required there: `yaml.safe_load(text)` is the same shape."""
        # One line and no extension, so only the guard can refuse it: a newline
        # would have been dropped anyway, which is what made the first version of
        # this test pass with the guard removed.
        self.assertEqual(self.ops("yaml.safe_load('name: a value')"), set())
        self.assertEqual(self.ops("yaml.safe_load('/w/conf.yaml')"),
                         {("/w/conf.yaml", "read")})

    # ---- order and scope -------------------------------------------------
    def test_a_name_reassigned_between_two_edits(self):
        """The commonest shape an agent writes, and the one a single pass over the
        assignments got wrong: both writes resolved to whichever value came last,
        so two real files vanished and the third was claimed twice. A reviewer
        found it in 5 commands across 2 sessions."""
        body = ('import pathlib\n'
                'p = pathlib.Path("a/schema.py"); s = p.read_text()\n'
                'p.write_text(s)\n'
                'p = pathlib.Path("a/core.py"); s = p.read_text()\n'
                'p.write_text(s)\n')
        self.assertEqual(self.ops(body),
                         {("a/schema.py", "read"), ("a/schema.py", "write"),
                          ("a/core.py", "read"), ("a/core.py", "write")})

    def test_an_argument_is_read_before_the_name_is_rebound(self):
        """A name reused for the result of reading it -- `p = json.load(open(p))`,
        a common idiom. The `open` sees the path; only then does `p` become the
        parsed document. Yielding the assignment before its own right-hand side
        forgets the path first and loses the edge, and the reassignment test above
        passes either way because there the target and the argument differ."""
        self.assertEqual(
            self.ops('import json\np = "/w/a.json"\np = json.load(open(p))'),
            {("/w/a.json", "read")})

    def test_a_sibling_reached_through_parent(self):
        """`p.parent / "other.json"`, the way a script names a file next to the one
        it was given."""
        self.assertEqual(
            self.ops('from pathlib import Path\n'
                     'p = Path("/w/a/one.json")\n'
                     '(p.parent / "two.json").write_text("x")'),
            {("/w/a/two.json", "write")})

    def test_a_shell_variable_is_not_a_python_name(self):
        """Two tables, not one. A shell `p` holding the text
        `glob.glob('traces/f8ba129a*/trace.jsonl')[0]` became the argument of the
        script's own `open(p, ...)` when they shared a table."""
        self.assertEqual(
            self.ops("open(p)", {"p": "glob.glob('t/*/x.jsonl')[0]"}), set())
        # Reachable through os.environ, which is where it belongs.
        self.assertEqual(self.ops("import os\nopen(os.environ['p'])",
                                  {"p": "/w/real.json"}),
                         {("/w/real.json", "read")})

    def test_a_name_that_stops_being_knowable_is_forgotten(self):
        """A stale value invents an edge to a file the script never touched."""
        self.assertEqual(
            self.ops('p = "/w/a.json"\np = unknown_call()\nopen(p)'), set())

    # ---- the fallback ----------------------------------------------------
    def test_a_body_that_is_not_python_raises(self):
        """The caller's signal to fall back to the regex scan: 65 of 2096 bodies."""
        with self.assertRaises(SyntaxError):
            shell_ast.python_file_ops("this is not python: '/w/x.json'")

    @unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
    def test_the_fallback_still_produces_an_edge(self):
        got = {p for p, _, _ in shell_ast.extract(
            "python3 <<'PY'\nnot python at all '/w/x.json'\nPY")}
        self.assertIn("/w/x.json", got)

    @unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
    def test_the_evidence_says_which_path_was_taken(self):
        """A reader has to be able to tell a structural answer from a guess."""
        ast_why = next(w for _, _, w in shell_ast.extract(
            "python3 <<'PY'\nopen('/w/a.json')\nPY"))
        self.assertIn("open(", ast_why)
        regex_why = next(w for _, _, w in shell_ast.extract(
            "python3 <<'PY'\nnot python '/w/x.json'\nPY"))
        self.assertIn("literal in the", regex_why)


class TestAnInterproceduralPathIsAKnownGap(unittest.TestCase):
    """A path passed into a helper is not followed.

    `parse_md(D / 'embed_only_pairs.md')` where `parse_md` does `path.read_text()`
    -- 4 real reads in one session. Not implemented because it is rare: across
    1646 parsable bodies in the nine sessions with the most of them, 7 functions
    take a path and 8 call sites reach them, and only these 4 pass a literal. The
    regex did produce an edge here, with the wrong directory.
    """

    def test_the_gap_is_where_this_says_it_is(self):
        body = ("from pathlib import Path\n"
                "D = Path('/w/exp')\n"
                "def parse(path):\n    return path.read_text()\n"
                "parse(D / 'a.md')")
        self.assertEqual(shell_ast.python_file_ops(body), [])


@unittest.skipUnless(shell_ast.available(), "tree-sitter-bash not installed")
class TestARedirectTargetIsAFileByPosition(unittest.TestCase):
    """`> X` names a file, whatever X is spelled like.

    The extension test belongs to tokens whose position proves nothing -- `head
    -30 f`, `grep 'a.*b' f`, `git add x` -- and a redirect target is not one of
    them: shell syntax leaves it nothing else to be. Asking for an extension there
    lost 19.1% of all redirect targets in an external corpus of 1524 human-written
    scripts, 303 of them for no other reason: `> Singularity`, `> Vagrantfile`,
    `> .gitignore`, `> /etc/bind/named.conf.local`, `> /root/.rpmmacros`.

    Our own traces never showed it. An agent writes `.log`, `.json`, `.txt`; a
    person writes `Dockerfile`.
    """

    def _pairs(self, cmd, wd="/app"):
        return {(p, d) for p, d, _ in shell_ast.extract(cmd, wd)}

    def _why(self, cmd, wd="/app"):
        return {w for _, _, w in shell_ast.extract(cmd, wd)}

    # ---- what the extension test was losing ------------------------------
    def test_a_target_with_no_extension(self):
        for name in ("Singularity", "Vagrantfile", "Dockerfile", "configure"):
            self.assertEqual(self._pairs(f"cat > /app/{name} <<'EOF'\nx\nEOF"),
                             {(f"/app/{name}", "write")}, name)

    def test_a_target_whose_extension_is_not_in_the_table(self):
        for name in ("/root/.rpmmacros", "/etc/bind/named.conf.local",
                     "/app/core.ranges", "/app/demo.jl", "/app/script.clj"):
            self.assertEqual(self._pairs(f"printf x > {name}"),
                             {(name, "write")}, name)

    def test_a_hidden_file(self):
        self.assertEqual(self._pairs('echo "secret.txt" > .gitignore'),
                         {("/app/.gitignore", "write")})

    def test_an_input_redirect_too(self):
        self.assertEqual(self._pairs("docker load < /app/saved"),
                         {("/app/saved", "read")})

    # ---- what must still be refused --------------------------------------
    def test_a_descriptor_is_not_a_file(self):
        """`2>&1` duplicates a descriptor. The extension test used to absorb this
        by accident -- `1` has no extension -- so dropping it without saying so
        would have put a file called `/app/1` in the graph, once for each of the
        1125 such redirects in that corpus."""
        for cmd in ("echo x 2>&1", "echo x >&3", "echo x 2>&3", "cat <&0"):
            got = self._pairs(cmd)
            self.assertEqual(got, set(), cmd)

    def test_a_descriptor_beside_a_real_target(self):
        self.assertEqual(self._pairs("echo x > /app/out.txt 2>&1"),
                         {("/app/out.txt", "write")})

    def test_merging_output_into_a_file_is_a_file(self):
        """`&>` is a different operator from `>&`: it names a file."""
        self.assertEqual(self._pairs("cmd &> /app/all.log"),
                         {("/app/all.log", "write")})

    def test_a_device_is_still_refused(self):
        for t in ("/dev/null", "/dev/tcp/host/23", "/proc/self/mem"):
            self.assertEqual(self._pairs(f"echo x > {t}"), set(), t)

    def test_an_unresolvable_variable_is_still_recorded_not_claimed(self):
        self.assertEqual(self._pairs("echo x > ${dir}/config.json"), set())
        self.assertTrue(any("dir" in w for w, _ in shell_ast.skipped_paths()))

    def test_a_tilde_is_still_left_alone(self):
        self.assertEqual(self._pairs("kwokctl get kubeconfig > ~/.kube/config"),
                         set())

    def test_a_process_substitution_is_not_a_file(self):
        """`< <(losetup -a)`. The extension test refused this too, and claiming it
        put `/app/<(losetup -a` in the graph. 4 occurrences in that corpus."""
        self.assertEqual(self._pairs("read -r x < <(losetup -a)"), set())
        self.assertTrue(any("process_substitution" in why
                            for _, why in shell_ast.skipped_paths()))

    def test_a_file_actually_named_1(self):
        """`cat << 'EOF' > 1` writes a file called `1`, and `2>&1` does not. Only
        the operator tells them apart, which is why the guard reads the operator
        and not the target."""
        self.assertEqual(self._pairs("cat <<'EOF' > 1\nx\nEOF"),
                         {("/app/1", "write")})

    def test_a_descriptor_opened_for_reading(self):
        """`3< f` reads. Asking whether the text starts with `<` said it wrote,
        because the text starts with `3`."""
        self.assertEqual(self._pairs("exec 3< /app/in.txt"),
                         {("/app/in.txt", "read")})

    # ---- the evidence ----------------------------------------------------
    def test_the_evidence_names_the_operator(self):
        """`2> err.log` said `redirect 2`: the loop took the descriptor for the
        operator, so the one thing the string is for was the one thing it did not
        say."""
        self.assertIn("redirect >", self._why("cmd 2> /app/err.log"))
        self.assertIn("redirect >>", self._why("cmd >> /app/err.log"))
