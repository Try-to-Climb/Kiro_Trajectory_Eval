"""Tests for the split dump.

The file this produces is going to be handed to a language model and its answers
believed. Two things therefore matter more than usual: the command has to arrive
verbatim, and the split has to be the one the graph was built from rather than a
description of it.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

from normalize.schema import Action
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import shell_ast                                         # noqa: E402
from graph.tests import dump_splits as ds                           # noqa: E402

HAVE_TREE = shell_ast.available()
needs_tree = unittest.skipUnless(HAVE_TREE, "tree-sitter-bash is not installed")


def _Action(idx, command, turn=1, purpose="", wd="", cwd=None, response=None):
    """A real `normalize.Action`, not a stand-in.

    A hand-written stand-in carried only the fields it was thought the dump reads,
    and the thinking was wrong: it had `args.working_dir` but not `cwd`, the field
    the graph actually resolves relative paths against, so no test here could have
    caught the dump using the other one.
    """
    return Action(idx=idx, call_idx=idx, op_idx=0, ts="", turn=turn, run=1,
                  raw_tool="execute_bash", tool="shell", action="run_command",
                  purpose=purpose, command=command, cwd=cwd,
                  args={"working_dir": wd} if wd else {},
                  response=response)


class TestTheRecord(unittest.TestCase):

    @needs_tree
    def test_the_command_survives_verbatim(self):
        """Anything else and the reader is auditing our paraphrase. Newlines,
        quotes, backslashes and a code fence inside the command all included."""
        raw = ('python3 <<\'PY\'\n'
               'x = "a\\tb"   # ``` inside a command\n'
               "open('/w/o.json','w').write(x)\n"
               'PY')
        rec = ds.record(_Action(1, raw))
        self.assertEqual(rec["command"], raw)

    @needs_tree
    def test_a_fence_in_the_command_does_not_break_the_block(self):
        rec = ds.record(_Action(1, 'echo "```"'))
        md = ds.as_markdown(rec)
        opener = next(l for l in md.splitlines() if l.startswith("`") and "sh" in l)
        self.assertGreater(len(opener) - len("sh"), 3, opener)
        # The closing fence has to be the same length, or the block never closes.
        fence = opener[:-2]
        self.assertEqual(md.count(fence + "\n"), 1)
        self.assertTrue(md.rstrip().count(fence) >= 2)

    @needs_tree
    def test_the_split_is_the_extractor_s_own(self):
        """Not a description of it. Same source as `inspect_command`, which reads
        `shell_ast.last_split()`."""
        cmd = "nohup timeout 240 python3 /w/x.py --cfg /w/a.yaml > /w/o.log"
        rec = ds.record(_Action(7, cmd))
        shell_ast.extract(cmd)
        theirs = [c["runs"] for c in shell_ast.last_split()]
        self.assertEqual([c["runs"] for c in rec["split"]], theirs)

    @needs_tree
    def test_wrappers_redirects_and_substitutions_are_all_reported(self):
        cmd = ('L="/w/run.log" && nohup timeout 240 python3 /w/x.py '
               '--cfg /w/a.yaml > /w/o.log 2>&1 && grep -c ERROR "$L"')
        rec = ds.record(_Action(7, cmd))
        runs = {c["runs"]: c for c in rec["split"]}
        self.assertIn("python3", runs)
        self.assertEqual(runs["python3"]["through"], ["nohup", "timeout"])
        self.assertIn("> /w/o.log", runs["python3"]["redirects"])
        self.assertEqual(runs["grep"]["resolved"]['"$L"'], "/w/run.log")
        self.assertEqual(rec["assignments"][0]["name"], "L")
        self.assertTrue(rec["assignments"][0]["knowable"])

    @needs_tree
    def test_a_heredoc_body_is_marked_as_nesting(self):
        cmd = ("cat > /tmp/r.sh <<'SH'\nsource /w/env/bin/activate\nSH\n"
               "bash /tmp/r.sh")
        rec = ds.record(_Action(9, cmd))
        inner = [c for c in rec["split"] if c["in"]]
        self.assertEqual([c["runs"] for c in inner], ["source"])
        self.assertIn("heredoc", inner[0]["in"][0])

    @needs_tree
    def test_an_ignored_command_says_so(self):
        """`rm` is found and deliberately not turned into an edge. Silence here
        would read as a parsing failure."""
        rec = ds.record(_Action(3, "rm -rf /w/tmp"))
        self.assertTrue(rec["split"][0]["ignored"])
        self.assertIn("ignored by the rules", ds.as_markdown(rec))

    @needs_tree
    def test_an_empty_split_carries_a_note(self):
        """The reader has to be told this is worth reporting, not skipped over.

        The example used to be `X=$(ls …)`, which was empty because the walk
        returned at the assignment. That is fixed, so a command with genuinely
        nothing in it is used instead.
        """
        rec = ds.record(_Action(4, "FOO=1"))
        self.assertEqual(rec["split"], [])
        self.assertIn("defect", rec["note"])

    @needs_tree
    def test_a_substitution_in_an_assignment_is_no_longer_empty(self):
        rec = ds.record(_Action(5, 'X=$(ls -t /w/*.log | head -1)'), None)
        self.assertEqual([c["runs"] for c in rec["split"]], ["ls", "head"])
        self.assertNotIn("note", rec)

    @needs_tree
    def test_a_parse_failure_is_labelled_and_not_silent(self):
        rec = ds.record(_Action(5, "cat 'unclosed"))
        self.assertFalse(rec["parsed"])
        self.assertIn("syntax tree", rec["note"])


class TestTheFileIsReadable(unittest.TestCase):

    @needs_tree
    def test_the_preamble_says_what_to_check(self):
        """A dump with no question attached comes back with an essay."""
        for phrase in ("Missing commands", "Wrong boundaries", "Lost redirects",
                       "Wrong substitution", "Known gaps"):
            self.assertIn(phrase, ds.PREAMBLE, phrase)

    @needs_tree
    def test_the_preamble_names_the_known_gaps(self):
        """So the reader does not spend its answer on defects already recorded."""
        self.assertIn("inside an assignment", ds.PREAMBLE)
        self.assertIn("~", ds.PREAMBLE)

    @needs_tree
    def test_every_field_in_a_record_is_explained(self):
        rec = ds.record(_Action(1, 'L=/w/a && grep x "$L" > /w/b'))
        for field in ("runs", "through", "positional", "options", "redirects",
                      "resolved", "ignored"):
            self.assertIn(field, ds.PREAMBLE, field)
        for field in rec["split"][0]:
            self.assertIn(field, ds.PREAMBLE, f"{field} is dumped but not explained")
        # And the one that only appears on a script body.
        self.assertIn("claims", ds.PREAMBLE)

    @needs_tree
    def test_sections_are_addressable(self):
        """The reply comes back as "a[22] is wrong", so the id has to be in the
        heading and unique."""
        md = ds.as_markdown(ds.record(_Action(22, "ls /w", turn=6)))
        self.assertIn("## a[22]", md)
        self.assertIn("turn 6", md)

    @needs_tree
    def test_jsonl_is_one_record_per_line(self):
        recs = [ds.record(_Action(i, f"cat /w/{i}.txt")) for i in (1, 2)]
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
        self.assertEqual(len(body.splitlines()), 2)
        for line in body.splitlines():
            self.assertIn("command", json.loads(line))


class TestTheCommandLine(unittest.TestCase):

    def test_a_folder_with_one_session_needs_no_id(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "abc-123.jsonl"), "w").close()
            self.assertEqual(ds._find_session(d, ""), ("abc-123", d))

    def test_a_folder_with_several_names_them(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.jsonl"), "w").close()
            open(os.path.join(d, "b.jsonl"), "w").close()
            with self.assertRaises(SystemExit) as caught:
                ds._find_session(d, "")
            self.assertIn("a", str(caught.exception))
            self.assertIn("b", str(caught.exception))

    def test_an_explicit_session_wins(self):
        """It was ignored at first: the ambiguous-folder branch exited before the
        caller could apply it."""
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.jsonl"), "w").close()
            open(os.path.join(d, "b.jsonl"), "w").close()
            self.assertEqual(ds._find_session(d, "", "b"), ("b", d))

    def test_a_file_gives_its_own_id(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "9c8ef9e5-ce20.jsonl")
            open(path, "w").close()
            self.assertEqual(ds._find_session(path, ""), ("9c8ef9e5-ce20", d))


if __name__ == "__main__":
    unittest.main()


class TestRedirectOwnership(unittest.TestCase):
    """Which command a redirect is filed against. Four of the first eight commands
    a language model looked at were reported for getting this wrong, and it was
    right about all four: the rule was "the last command in the statement", and
    three real shapes defeat it."""

    @needs_tree
    def _split(self, cmd):
        """`{command: redirects}`, skipping the script-body records.

        A python heredoc leaves two entries named `python3`: the command, and the
        paths read out of its body. Keyed by name alone, the second overwrote the
        first and this class started asserting against an empty list.
        """
        shell_ast.extract(cmd)
        return {c["runs"]: c["redirects"] for c in shell_ast.last_split()
                if not c.get("claims")}

    @needs_tree
    def test_a_pipeline_redirect_goes_to_the_last_stage(self):
        got = self._split("cat a.txt | grep x > b.txt")
        self.assertEqual(got["grep"], ["> b.txt"])
        self.assertEqual(got["cat"], [])

    @needs_tree
    def test_a_substitution_in_the_filename_does_not_steal_it(self):
        """`> run_$(date +%s).log 2>&1`: the `date` starts before the `2>&1` and
        was the nearest candidate, so `2>&1` went to a command that only computes
        a name."""
        got = self._split(
            "nohup python3 /w/x.py > /w/run_$(date +%s).log 2>&1 &")
        self.assertEqual(len(got["python3"]), 2, got)
        self.assertNotIn("date", [k for k, v in got.items() if v])

    @needs_tree
    def test_a_substitution_in_an_earlier_assignment_does_not_steal_it(self):
        """`cd x && LOG=$(date) && nohup … > f`: a breadth-first scan reached the
        `date` last -- deeper, but later -- and filed `> f` against a command that
        leaves no record, so the redirect vanished entirely."""
        got = self._split('cd /w && LOG="/w/$(date +%s).log" && '
                          'nohup python3 /w/x.py > "$LOG" 2>&1 &')
        self.assertEqual(got["python3"], ['> "$LOG"', "2>&1"])

    @needs_tree
    def test_later_commands_in_the_same_statement_do_not_take_it(self):
        """tree-sitter makes `mkdir && nohup … > f & echo …; ls … | head -1` one
        statement, so "the last command in it" is `head`, three commands past the
        one the shell redirects."""
        got = self._split(
            "mkdir -p /w/out && nohup python3 /w/x.py > /w/o.log 2>&1 &\n"
            'echo "PID=$!"\n'
            "ls -t /w/*.log 2>/dev/null | head -1")
        self.assertEqual(got["python3"], ["> /w/o.log", "2>&1"])
        self.assertEqual(got["ls"], ["2>/dev/null"])
        self.assertEqual(got.get("head", []), [])

    @needs_tree
    def test_an_inner_statement_keeps_its_own(self):
        got = self._split("ls /w 2>/dev/null | head -1 > /w/first.txt")
        self.assertEqual(got["ls"], ["2>/dev/null"])
        self.assertEqual(got["head"], ["> /w/first.txt"])

    @needs_tree
    def test_a_heredoc_delimiter_not_its_body(self):
        got = self._split("python3 <<'PY'\nopen('/w/o.json','w')\nPY")
        self.assertEqual(got["python3"], ["<<'PY'"])


class TestItFollowsTheRealFlow(unittest.TestCase):
    """The dump must be produced the way the graph is produced.

    Three divergences existed at once, and each made the dump describe something
    the graph was not built from: no `working_dir`, so a relative path resolved to
    nothing -- 44 of 140 commands in one session; no carried `env`, so a variable
    set by an earlier command stayed a variable; and no fallback, so the commands
    the tree cannot parse were reported as empty while the graph had used the regex
    path for them.
    """

    def test_it_calls_the_same_entry_point(self):
        src = pathlib.Path(ds.__file__).read_text(encoding="utf-8")
        self.assertIn("_command_paths", src)

    def test_the_working_directory_reaches_the_extractor(self):
        """A relative path is unresolvable without it."""
        rec = ds.record(_Action(1, "cat run_capture.py", cwd="/work/proj"))
        self.assertIn(("/work/proj/run_capture.py", "read"),
                      {(p, d) for p, d, _ in rec["edges"]})

    @needs_tree
    def test_a_carried_variable_reaches_the_extractor(self):
        rec = ds.record(_Action(1, 'cat "$OUT/x.log"'), {"OUT": "/w/out"})
        self.assertIn(("/w/out/x.log", "read"),
                      {(p, d) for p, d, _ in rec["edges"]})

    @needs_tree
    def test_the_fallback_is_followed_and_labelled(self):
        """`python3 <<'PY' 2>&1 | tail -5` does not parse. The graph falls back to
        the regexes for it, so the dump has to as well, and say which it used."""
        rec = ds.record(_Action(1, "python3 <<'PY' 2>&1 | tail -5\n"
                                  "open('/w/o.json','w')\nPY"), None)
        self.assertEqual(rec["via"], "regex fallback")
        self.assertIn("edges", rec)

    @needs_tree
    def test_the_edges_match_what_the_graph_would_build(self):
        """Compared against the same action, not a dict assembled here: a dict
        assembled here is where the `cwd`/`working_dir` mix-up came from."""
        from graph.build import _command_paths
        for cmd, cwd in [("cat /w/a.txt > /w/b.txt", None),
                         ("cat run.py", "/work"),
                         ("python3 <<'PY'\nopen('/w/o.json','w')\nPY", None)]:
            a = _Action(1, cmd, cwd=cwd)
            want = sorted(_command_paths(a.to_dict(), {})[0])
            self.assertEqual([tuple(e) for e in ds.record(a)["edges"]], want, cmd)

    def test_the_session_loop_carries_env_with_the_graphs_own_function(self):
        """Not a copy of those lines. The copy drifted: it had the assignments but
        not `echoed_var`, so a value the agent had only printed (`echo $KIRO_HOME`,
        4 commands in one session) stayed unknown in the dump while the graph knew
        it."""
        src = pathlib.Path(ds.__file__).read_text(encoding="utf-8")
        self.assertIn("carry_env", src)
        self.assertNotIn("keys.shell_vars", src)
        self.assertNotIn("keys.assigned_names", src)

    def test_carry_env_picks_up_a_printed_value(self):
        from graph.build import carry_env
        env: dict = {}
        carry_env(env, "echo $KIRO_HOME", "/home/u/kiro\n")
        self.assertEqual(env.get("KIRO_HOME"), "/home/u/kiro")

    def test_carry_env_drops_a_value_it_cannot_read(self):
        """A stale value invents an edge to an earlier run's file."""
        from graph.build import carry_env
        env = {"LOG": "/w/old.log"}
        carry_env(env, "LOG=$(date +%s).log", None)
        self.assertNotIn("LOG", env)
