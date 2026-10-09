"""Tests for the command inspector.

The inspector is a debugging tool, and a debugging tool that has quietly stopped
telling the truth is worse than none: it is consulted precisely when something is
already confusing. These tests hold it to the three things it claims to show --
the tree, the commands found, and what was claimed from them -- on cases where the
right answer is known.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import shell_ast                                         # noqa: E402
from graph.tests import inspect_command as ic                       # noqa: E402

HAVE_TREE = shell_ast.available()
needs_tree = unittest.skipUnless(HAVE_TREE, "tree-sitter-bash is not installed")


def run(*argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ic.main(list(argv))
    return buf.getvalue()


class TestItShowsTheTree(unittest.TestCase):

    @needs_tree
    def test_the_tree_names_the_nodes(self):
        out = run("cat /work/a.txt > /work/b.txt")
        for node in ("program", "redirected_statement", "command",
                     "command_name", "file_redirect"):
            self.assertIn(node, out, node)

    @needs_tree
    def test_punctuation_is_kept(self):
        """A missing `>` is what turns a write into a read, so the unnamed nodes
        are shown rather than filtered out as noise.

        Asserted against the TREE section alone: the regex fallback quotes `'>'`
        in its evidence string, and an earlier version of this test passed on that
        while the tree had stopped printing punctuation entirely.
        """
        out = run("--no-regex", "echo hi > /work/a.txt")
        tree = out[out.index("TREE"):out.index("SPLIT")]
        self.assertIn("'>'", tree)

    @needs_tree
    def test_a_syntax_error_is_called_out(self):
        """The 1.5% where `extract` raises and the caller falls back. Both places
        are asserted separately: joined by `or`, either one going silent left the
        test green."""
        out = run("--no-regex", "cat 'unclosed")
        tree = out[out.index("TREE"):out.index("SPLIT")]
        self.assertIn("syntax error", tree)
        claimed = out[out.index("PATHS CLAIMED"):]
        self.assertIn("ParseFailure", claimed)
        self.assertIn("falls back", out)


class TestItShowsWhatWasClaimed(unittest.TestCase):

    @needs_tree
    def test_direction_and_evidence(self):
        out = run("--no-tree", "--no-regex", "cat /work/a.txt > /work/b.txt")
        self.assertIn("← reads   /work/a.txt", out)
        self.assertIn("→ writes  /work/b.txt", out)
        self.assertIn("argument of cat", out)
        self.assertIn("redirect >", out)

    @needs_tree
    def test_a_heredoc_script_is_read_as_a_script(self):
        """339 of 1751 real commands are `python3 <<PY`, where the body is the
        program being run. Nothing about that is visible without this."""
        out = run("--no-tree", "--no-regex",
                  'python3 <<\'PY\'\nopen("/work/out.json","w")\nPY')
        self.assertIn("/work/out.json", out)

    @needs_tree
    def test_an_unresolvable_variable_is_reported_not_dropped(self):
        """The main gap in the extractor, and the tool's job is to name it.

        The reason as well as the name: `$LOGFILE` also appears in the echoed
        command line, so asserting only on the name passed even when the skip list
        came back empty.
        """
        out = run("--no-tree", "--no-regex", 'python3 run.py > "$LOGFILE"')
        skipped = out[out.index("SKIPPED"):]
        self.assertIn("$LOGFILE", skipped)
        self.assertIn("never stated", skipped)

    @needs_tree
    def test_nothing_claimed_says_none(self):
        out = run("--no-tree", "--no-regex", "echo hello")
        self.assertIn("PATHS CLAIMED", out)
        self.assertIn("none", out)


class TestItComparesTheTwoExtractors(unittest.TestCase):
    """The regexes are the fallback for the 1.5% the tree cannot read, so the two
    are meant to agree everywhere else. Showing both is how a disagreement gets
    noticed."""

    @needs_tree
    def test_agreement_is_stated(self):
        self.assertIn("the two agree", run("--no-tree", "cat /work/a.txt"))

    @needs_tree
    def test_a_difference_is_itemised(self):
        """A command where they are known to differ: the tree follows `source`
        into a path the regexes do not treat as a read."""
        out = run("--no-tree", "for f in /work/a.log /work/b.log; do "
                             'grep ERROR "$f"; done')
        self.assertTrue("the two agree" in out or "DIFFERENCE" in out, out)
        if "DIFFERENCE" in out:
            self.assertTrue("only the tree" in out or "only the regex" in out)


class TestTheFileMode(unittest.TestCase):

    @needs_tree
    def test_blocks_are_separated_by_blank_lines(self):
        """One command per line would cut every heredoc in half."""
        import tempfile
        body = ('cat /work/a.txt\n'
                '\n'
                'python3 <<\'PY\'\n'
                'open("/work/b.json","w")\n'
                'PY\n')
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            out = run("--file", path, "--no-tree", "--no-regex")
            self.assertIn("/work/a.txt", out)
            self.assertIn("/work/b.json", out)
            self.assertIn("inspected 2 command(s)", out)
        finally:
            os.unlink(path)


class TestItRunsWithoutTreeSitter(unittest.TestCase):
    """The package imports and the tool still runs where tree-sitter is absent --
    that is the environment the regex fallback exists for."""

    def test_the_module_imports(self):
        self.assertTrue(hasattr(ic, "main"))
        self.assertTrue(hasattr(ic, "report"))

    def test_it_warns_rather_than_crashing(self):
        saved = shell_ast._PARSER
        shell_ast._PARSER = None
        try:
            out = run("--no-regex", "cat /work/a.txt")
            self.assertIn("not installed", out + "")
        finally:
            shell_ast._PARSER = saved


if __name__ == "__main__":
    unittest.main()


class TestSplitOnly(unittest.TestCase):
    """`--split-only`: what the tree says a command is made of, and nothing else.

    `shell_ast` does two separable things -- the tree decides the structure, the
    rules decide the meaning -- and a wrong edge could not be attributed to one of
    them while both were reported together. This half says nothing about read or
    write, resolves no path, and consults no rule from `keys.py`, so a disagreement
    here is a parsing problem and only that.
    """

    @needs_tree
    def test_it_says_nothing_about_direction(self):
        out = run("--split-only", "cat /work/a.txt > /work/b.txt")
        for word in ("reads", "writes", "read", "write", "PATHS CLAIMED",
                     "because:"):
            self.assertNotIn(word, out, f"{word!r} leaked into the split")

    @needs_tree
    def test_every_command_is_listed_with_its_own_arguments(self):
        out = run("--split-only", "cat a.txt | grep -i x | wc -l")
        for name in ("cat", "grep", "wc"):
            self.assertIn(name, out)
        self.assertIn("positional  a.txt", out)
        self.assertIn("option      -i", out)

    @needs_tree
    def test_a_heredoc_body_is_named_as_context(self):
        """The one nesting the extraction tracks. `$(...)` and a pipeline are not
        distinguished by it, so the tool does not invent the distinction."""
        out = run("--split-only", "cat > /tmp/r.sh <<'SH'\n"
                  "source /work/env/bin/activate\nSH\nbash /tmp/r.sh")
        self.assertIn("inside a heredoc body", out)

    @needs_tree
    def test_a_substitution_in_an_assignment_is_entered(self):
        """It was not, for two sessions: `walk` returned at the assignment and the
        `ls` inside `X=$(ls …)` was never visited. 28 real reads were absent from
        the reference session because of it -- `LOG=$(cat /tmp/logpath.txt)` is how
        the agent passes a path from one turn to the next. Fixed by descending;
        +30 edges, 0 lost, precision unchanged on the gold set."""
        out = run("--split-only", 'X=$(ls -t /work/*.log | head -1)')
        self.assertIn("ls", out)
        self.assertIn("head", out)
        st = ic.structure('LOG=$(cat /tmp/logpath.txt)')
        self.assertEqual([c["runs"] for c in st["commands"]], ["cat"])

    @needs_tree
    def test_a_substitution_in_a_for_word_list_is_entered(self):
        """`for i in $(seq 1 40)` runs `seq`. No edge comes of it, but a split that
        omits a command is wrong regardless."""
        st = ic.structure("for i in $(seq 1 40); do echo $i; done")
        self.assertIn("seq", [c["runs"] for c in st["commands"]])

    @needs_tree
    def test_wrappers_are_stripped_and_reported(self):
        """`timeout 240 python3 x.py` once claimed `240` as a path. Both halves are
        asserted: what actually runs, and that its arguments are its own."""
        out = run("--split-only",
                  "nohup timeout 240 python3 /work/x.py --cfg /work/a.yaml")
        self.assertIn("[1] python3", out)
        self.assertIn("through nohup -> timeout", out)
        self.assertIn("positional  /work/x.py", out)
        self.assertNotIn("positional  240", out)
        self.assertNotIn("positional  timeout", out)

    @needs_tree
    def test_a_pipeline_redirect_belongs_to_the_last_command(self):
        """`a | b > c` puts the redirect beside the pipeline, not beside `b`, and
        looking only at a command's own children dropped it entirely."""
        out = run("--split-only", "cat a.txt | grep x > b.txt")
        block = out[out.index("[2] grep"):]
        self.assertIn("> b.txt", block)

    @needs_tree
    def test_a_redirect_keeps_its_own_spelling(self):
        """Reassembled from its children, `2>&1` came out as `2 >& 1`."""
        self.assertIn("2>&1", run("--split-only", "python3 x.py > out.log 2>&1"))

    @needs_tree
    def test_a_heredoc_reports_its_size_not_its_whole_body(self):
        """The body is a child of the redirect, so the redirect line was printing
        an entire python script."""
        out = run("--split-only",
                  'python3 <<\'PY\'\nimport json\nopen("/work/o.json","w")\nPY')
        self.assertIn("HEREDOC BODIES", out)
        self.assertIn("2 line(s)", out)
        # The redirect line itself, not the output as a whole: `assertIn` is a
        # substring test, so a line reading `<<'PY' import json open(...)` matched
        # the shorter string and the check passed on the very bug it was for.
        line = next(l for l in out.splitlines() if "redirect" in l)
        self.assertEqual(line.strip(), "redirect    <<'PY'")
        self.assertNotIn("import json", line)
        self.assertNotIn("open(", line)

    @needs_tree
    def test_an_assignment_says_whether_its_value_is_knowable(self):
        """The single biggest reason a path never becomes a node."""
        literal = run("--split-only", "OUT=/proj/results; cp a.log $OUT/b.log")
        self.assertIn("value: literal", literal)
        computed = run("--split-only", 'L=$(date +%s); echo $L')
        self.assertIn("computed at run time", computed)

    @needs_tree
    def test_json_is_machine_readable(self):
        """So a gold set of correct splits can be checked against it later."""
        import json
        got = json.loads(run("--split-only", "--json",
                             "cat a.txt | grep x > b.txt"))
        self.assertTrue(got["ok"])
        self.assertEqual([c["runs"] for c in got["commands"]], ["cat", "grep"])
        # Redirects come from the extractor's own record now, as the text it saw.
        self.assertEqual(got["commands"][1]["redirects"], ["> b.txt"])

    @needs_tree
    def test_a_syntax_error_is_reported_in_the_structure(self):
        got = ic.structure("cat 'unclosed")
        self.assertFalse(got["ok"])
        self.assertIn("syntax error", got["why"])


class TestItUsesTheShippedCode(unittest.TestCase):
    """The inspector must describe the split the extractor performs, not a similar
    one. It had its own list of wrappers for one session -- four names short of
    `shell_ast.WRAPPERS` -- so `exec setsid python3 x.py` was reported as running
    `exec` while the extractor resolved it to `python3`. A debugging tool that
    disagrees with the thing being debugged is worse than none.
    """

    def test_no_second_walk_of_the_tree(self):
        """The tool reads the record the extraction keeps. It had its own walk for
        two sessions and disagreed with the real one on 20 of 188 commands."""
        src = pathlib.Path(ic.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(src, r"^WRAPPERS\s*=", "a second wrapper list is back")
        self.assertIn("shell_ast.last_split()", src)
        # No peeling, no wrapper test, no argument splitting of its own.
        self.assertNotIn("peel_wrappers", src.split("def structure")[1])

    def test_the_same_parser_instance(self):
        """Not a second parser with its own grammar version."""
        src = pathlib.Path(ic.__file__).read_text(encoding="utf-8")
        self.assertIn("shell_ast._PARSER", src)
        self.assertNotIn("tree_sitter_bash", src)

    @needs_tree
    def test_every_wrapper_the_extractor_knows_is_peeled_here_too(self):
        """The whole set, so adding one to `shell_ast` cannot leave this behind."""
        for w in sorted(shell_ast.WRAPPERS):
            if w in ("xargs", "command", "time"):
                continue        # these take a command in a form of their own
            got = ic.structure(f"{w} python3 /work/x.py")
            runs = [c["runs"] for c in got["commands"]]
            self.assertIn("python3", runs, f"{w} was not peeled: {runs}")
            self.assertIn(w, got["commands"][0]["wrappers"], w)

    @needs_tree
    def test_a_chain_is_reported_in_order(self):
        got = ic.structure("nohup timeout 240 python3 /work/x.py")
        self.assertEqual(got["commands"][0]["wrappers"], ["nohup", "timeout"])
        self.assertEqual(got["commands"][0]["runs"], "python3")
        self.assertEqual(got["commands"][0]["positional"], ["/work/x.py"])

    @needs_tree
    def test_the_split_is_the_extractor_s_own_record(self):
        """Not a second walk of the tree. The second walk disagreed with the real
        one on 20 of 188 commands in the reference session."""
        src = pathlib.Path(ic.__file__).read_text(encoding="utf-8")
        self.assertIn("shell_ast.last_split()", src)

    @needs_tree
    def test_every_claimed_path_appears_in_the_split(self):
        """The strongest check available without a gold set. Cases chosen for the
        two things a second walk got wrong: a variable resolved through `env`, and
        a command that lives only inside a heredoc body."""
        cases = [
            "cat /work/a.txt > /work/b.txt",
            "nohup timeout 240 python3 /work/x.py --cfg /work/a.yaml",
            "cat /work/a.txt | grep x > /work/b.txt",
            'L="/work/run.log" && grep -c ERROR "$L"',
            "cat > /tmp/r.sh <<'SH'\nsource /work/env/bin/activate\n"
            "python3 /work/y.py\nSH\nbash /tmp/r.sh",
        ]
        for cmd in cases:
            claimed = {p for p, _, _ in shell_ast.extract(cmd)}
            st = ic.structure(cmd)
            shown = set()
            for c in st["commands"]:
                shown.add(c["name"])
                shown.update(c["args"])
                shown.update(c.get("resolved", {}).values())
                shown.update(c.get("redirects", ()))
            for path in claimed:
                tail = path.rsplit("/", 1)[-1]
                self.assertTrue(any(tail in str(x) for x in shown),
                                f"{cmd!r}: claimed {path}, the split never showed "
                                f"it -- shown was {sorted(shown)}")

    @needs_tree
    def test_a_variable_shows_what_it_resolved_to(self):
        st = ic.structure('L="/work/run.log" && grep -c ERROR "$L"')
        grep = next(c for c in st["commands"] if c["runs"] == "grep")
        self.assertEqual(grep["resolved"]['"$L"'], "/work/run.log")

    @needs_tree
    def test_a_heredoc_body_is_recursed_into(self):
        """Eighteen `source …/bin/activate` calls in the reference session are
        reachable only this way."""
        st = ic.structure("cat > /tmp/r.sh <<'SH'\n"
                          "source /work/env/bin/activate\nSH\nbash /tmp/r.sh")
        inner = [c for c in st["commands"]
                 if "inside a heredoc body" in c.get("context", ())]
        self.assertEqual([c["runs"] for c in inner], ["source"])

    @needs_tree
    def test_a_command_the_rules_ignore_is_still_shown(self):
        """`rm`, `ls`, `mkdir` are deliberately not turned into edges. A reader
        asking why gets an answer here instead of an absence."""
        st = ic.structure("rm -rf /work/tmp && ls /work")
        runs = {c["runs"]: c["ignored"] for c in st["commands"]}
        self.assertTrue(runs.get("rm"), runs)
        self.assertTrue(runs.get("ls"), runs)


class TestTheRecordDoesNotChangeExtraction(unittest.TestCase):
    """`_Walker.split` is written and never read by the extraction. A record that
    altered what was extracted would be a fine way to introduce a bug that only
    appears when nobody is looking."""

    CASES = [
        "cat /work/a.txt > /work/b.txt",
        "nohup timeout 240 python3 /work/x.py --cfg /work/a.yaml",
        'L="/work/run.log" && grep -c ERROR "$L"',
        "rm -rf /work/tmp && ls /work",
        "cat a.txt | grep x | wc -l > out.txt",
        "source /work/env/bin/activate && python3 /work/y.py",
        "cat > /tmp/r.sh <<'SH'\nsource /work/env/bin/activate\nSH\nbash /tmp/r.sh",
        'python3 <<\'PY\'\nopen("/work/o.json","w")\nPY',
        "for f in /work/a.log /work/b.log; do grep E \"$f\"; done",
        "git diff --stat -- /work/src/x.py",
    ]

    @needs_tree
    def test_nothing_branches_on_the_record(self):
        """Asserted by reading the source: `self.split` may be appended to and
        extended, never tested."""
        src = pathlib.Path(shell_ast.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            if "self.split" not in line:
                continue
            body = line.strip()
            self.assertTrue(
                body.startswith("self.split: ")
                or body.startswith("self.split.append(")
                or body.startswith("self.split.extend(")
                or body.startswith("#"),
                f"the record is being read, not just written: {body}")

    @needs_tree
    def test_the_split_is_a_copy(self):
        """`last_split()` handing out the walker's own dicts would let a caller
        edit what the next reader sees."""
        shell_ast.extract("cat /work/a.txt")
        first = shell_ast.last_split()
        first[0]["runs"] = "tampered"
        self.assertNotEqual(shell_ast.last_split()[0]["runs"], "tampered")

    @needs_tree
    def test_extraction_is_unchanged_on_every_shape_we_know(self):
        """A weaker stand-in for the check that mattered: the full corpus was
        compared before and after adding the record, 188 commands, byte for byte
        identical. That comparison needs a real session; this one travels."""
        for cmd in self.CASES:
            got = sorted(shell_ast.extract(cmd))
            again = sorted(shell_ast.extract(cmd))
            self.assertEqual(got, again, cmd)
            self.assertTrue(all(len(t) == 3 for t in got), cmd)
