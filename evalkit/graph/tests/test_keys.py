"""Task 4 tests: entity keys parsed out of shell command text.

Fixtures are the real command forms measured on session `a1b2c3d4`, including
the ones that exposed bugs during implementation: a heredoc whose *body*
contained `chmod`, and python's `-u` being read as a file mode.

Run:  cd evalkit && python3 -m unittest graph.tests.test_keys -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import keys                                       # noqa: E402
from graph.build import build_skeleton                        # noqa: E402
from graph.schema import GENERATED_BY, LIKELY, USED_BY        # noqa: E402

WD = "/work/proj/sut"


def _cmd(idx, command, working_dir=WD, turn=6):
    return {"ref": f"a1b2c3d4#{idx}", "sid": "a1b2c3d4-1111", "idx": idx,
            "turn": turn, "action": "run_command", "path": None,
            "command": command, "args": {"working_dir": working_dir}}


def _dirs(command, working_dir=WD):
    """`{path: direction}` for readability in assertions."""
    return {p: d for p, d, _ in keys.extract(command, working_dir)}


class TestWhatCountsAsAPath(unittest.TestCase):

    def test_extension_whitelist_decides(self):
        self.assertTrue(keys.looks_like_path("run.py"))
        self.assertTrue(keys.looks_like_path("/a/b/x.yaml"))
        self.assertFalse(keys.looks_like_path("python3.11"))   # .11 not an ext
        self.assertFalse(keys.looks_like_path("-u"))
        self.assertFalse(keys.looks_like_path("ABC1234"))

    def test_quotes_and_punctuation_are_stripped(self):
        self.assertTrue(keys.looks_like_path('"/a/b.log"'))
        self.assertTrue(keys.looks_like_path("'x.sh',"))

    def test_device_files_are_not_artifacts(self):
        """/dev/null is noise; the real target still comes through."""
        got = _dirs("cat /dev/null > out.txt")
        self.assertEqual(got, {"/work/proj/sut/out.txt": "write"})
        self.assertNotIn("/dev/null", got)


class TestDirection(unittest.TestCase):

    def test_redirect_is_a_write(self):
        self.assertEqual(_dirs("cat > /tmp/run.sh"), {"/tmp/run.sh": "write"})
        self.assertEqual(_dirs("echo x >> /tmp/a.log"), {"/tmp/a.log": "write"})

    def test_attached_redirect_is_detached_first(self):
        """`>/tmp/x.log` is one token to str.split, which would hide the mark."""
        self.assertEqual(_dirs("echo x >/tmp/x.log"), {"/tmp/x.log": "write"})

    def test_read_verbs(self):
        self.assertEqual(_dirs("tail -50 /a/b.log"), {"/a/b.log": "read"})
        self.assertEqual(_dirs("grep ERROR /a/b.log"), {"/a/b.log": "read"})
        self.assertEqual(_dirs("source /a/env.sh"), {"/a/env.sh": "read"})

    def test_interpreter_reads_its_script(self):
        self.assertEqual(_dirs("python3 /a/gen.py"), {"/a/gen.py": "read"})
        self.assertEqual(_dirs("bash /a/run.sh"), {"/a/run.sh": "read"})

    def test_options_are_skipped_when_scanning_left(self):
        """`python3 -u script.py` must still see the interpreter."""
        self.assertEqual(_dirs("python3 -u /a/gen.py"), {"/a/gen.py": "read"})

    def test_prefixes_need_no_special_handling(self):
        """`nohup` / `timeout` always precede the interpreter, never the path,
        so the leftward scan reaches the interpreter first. An earlier version
        passed through them explicitly; removing that changed nothing on 188
        real commands."""
        self.assertEqual(_dirs("nohup python3 -u /a/run.py"), {"/a/run.py": "read"})
        self.assertEqual(_dirs("timeout 900 python3 /a/run.py"), {"/a/run.py": "read"})
        self.assertEqual(_dirs("nohup bash /a/run.sh"), {"/a/run.sh": "read"})
        # nohup directly in front of a script: nothing else can stand in for it
        self.assertEqual(_dirs("nohup bash /a/run.sh"), {"/a/run.sh": "read"})

    def test_one_command_can_read_and_write(self):
        self.assertEqual(_dirs("python3 /a/gen.py --out /a/res.yaml"),
                         {"/a/gen.py": "read", "/a/res.yaml": "write"})

    def test_sed_in_place_is_a_write(self):
        self.assertEqual(_dirs("sed -i 's/a/b/' /a/pyproject.toml"),
                         {"/a/pyproject.toml": "write"})

    def test_command_substitution_reading_a_file(self):
        self.assertEqual(_dirs("LOGFILE=$(cat /tmp/logpath.txt)"),
                         {"/tmp/logpath.txt": "read"})

    def test_bare_assignment_has_no_direction_of_its_own(self):
        """`LOGFILE="/x.log"` states no direction. It was read as a write on the
        evidence of launch commands; poll commands have the identical shape and
        the opposite meaning."""
        self.assertEqual(_dirs('LOGFILE="/work/results/run.log"'), {})

    def test_the_use_site_decides_the_direction(self):
        self.assertEqual(
            _dirs('LOGFILE="/work/results/run.log" && tail -40 "$LOGFILE"'),
            {"/work/results/run.log": "read"})
        self.assertEqual(
            _dirs('LOGFILE="/work/results/run.log" && echo hi > "$LOGFILE"'),
            {"/work/results/run.log": "write"})

    def test_unknown_neighbour_yields_nothing(self):
        """Guessing would be worse than an isolated node."""
        self.assertEqual(_dirs("mytool --frobnicate /a/b.yaml"), {})

    def test_script_argument_is_a_read(self):
        """`python3 run.py suites/case.yaml` -- the yaml is an input to the
        script being executed. Largest single gap on real data before this."""
        self.assertEqual(_dirs("python3 /a/run.py /a/suites/case.yaml"),
                         {"/a/run.py": "read", "/a/suites/case.yaml": "read"})

    def test_adjacent_paths_without_an_interpreter_are_left_alone(self):
        """Guards the rule above: two paths side by side mean nothing on their
        own. `diff` is already covered by READ_VERBS, so only its first
        argument's direction comes from the verb."""
        got = _dirs("mytool /a/one.log /a/two.log")
        self.assertEqual(got, {})

    def test_quoted_argument_stays_one_token(self):
        r"""`grep -i "a\|ERROR\|no b" /x.log` split on the `\|` inside the
        quotes, so the segment scan started after a phantom pipe and `grep` was
        invisible to the path -- a real read went unrecorded."""
        self.assertEqual(_dirs(r'grep -i "iperf\|ERROR\|no iperf" /a/run.log'),
                         {"/a/run.log": "read"})


class TestChmodRegression(unittest.TestCase):
    """`[+-][rwxXstugoa]+` also matches ordinary short options, which turned 60
    reads into writes until the command had to actually contain `chmod`."""

    def test_chmod_target_is_a_write(self):
        self.assertEqual(_dirs("chmod +x /tmp/run.sh"), {"/tmp/run.sh": "write"})

    def test_python_dash_u_is_not_a_file_mode(self):
        self.assertEqual(_dirs("python3 -u /a/run.py"), {"/a/run.py": "read"})


class TestHeredocRegression(unittest.TestCase):
    """A `chmod` inside a generated script used to make the outer command look
    like a chmod, which then let `-u` be read as a file mode.

    The body was dropped wholesale to stop that. The real defect was the chmod
    guard's scope -- it asked whether the whole command contained `chmod` -- so
    these tests assert the behaviour that mattered (`-u` is not a file mode)
    rather than the means (the body's text is gone).
    """

    HEREDOC = (
        "cat > /tmp/run.sh << 'EOF'\n"
        "#!/bin/bash\n"
        "chmod +x /tmp/inner.sh\n"
        "python3 -u tfw/run.py tfw/suites/case.yaml\n"
        "EOF\n"
        "bash /tmp/run.sh"
    )

    def test_dash_u_is_not_a_file_mode_despite_chmod_in_the_body(self):
        """The point of the original fix, stated as behaviour."""
        got = _dirs(self.HEREDOC)
        self.assertEqual(got["/work/proj/sut/tfw/run.py"], "read")

    def test_chmod_in_the_body_still_marks_its_own_target(self):
        got = _dirs(self.HEREDOC)
        self.assertEqual(got["/tmp/inner.sh"], "write")

    def test_executed_script_contributes_its_body(self):
        """`bash /tmp/run.sh` runs what was just written, so the body's reads
        are this command's reads."""
        got = _dirs(self.HEREDOC)
        self.assertEqual(got["/work/proj/sut/tfw/suites/case.yaml"], "read")

    def test_script_written_but_never_run_contributes_nothing(self):
        """Without execution the body is data; its reads have not happened."""
        written_only = self.HEREDOC.rsplit("\n", 1)[0]   # drop `bash /tmp/run.sh`
        got = _dirs(written_only)
        self.assertEqual(got, {"/tmp/run.sh": "write"})

    def test_the_script_itself_is_written_then_read(self):
        """extract() keys on (path, direction), so both survive."""
        got = keys.extract(self.HEREDOC, WD)
        pairs = {(p, d) for p, d, _ in got}
        self.assertIn(("/tmp/run.sh", "write"), pairs)
        self.assertIn(("/tmp/run.sh", "read"), pairs)

    def test_strip_heredoc_still_removes_a_body(self):
        """Kept as a unit: `expand_heredoc` builds on it."""
        stripped = keys.strip_heredoc(self.HEREDOC)
        self.assertIn("cat >", stripped)
        self.assertIn("bash /tmp/run.sh", stripped)
        self.assertNotIn("chmod", stripped)

    def test_unterminated_heredoc_does_not_lose_the_head(self):
        got = _dirs("cat > /tmp/x.sh << 'EOF'\nsome content\n")
        self.assertEqual(got, {"/tmp/x.sh": "write"})


class TestVariableScope(unittest.TestCase):
    """Carrying values across commands is only safe if they are also forgotten.
    A stale value resolves `$LOGFILE` to some earlier run's file, which invents
    an edge to a real but unrelated path."""

    def test_bare_variable_reference_resolves(self):
        """`"$LOGFILE"` has no extension, so it only becomes a path after
        expansion -- the test the ordering bug hid."""
        got = keys.extract('tail -40 "$LOGFILE"', WD,
                           session_env={"LOGFILE": "/work/a.log"})
        self.assertEqual({(p, d) for p, d, _ in got},
                         {("/work/a.log", "read")})

    def test_unreadable_reassignment_forgets_the_carried_value(self):
        cmd = 'LOGFILE="/work/run_$(date +%s).log" && echo hi > "$LOGFILE"'
        got = keys.extract(cmd, WD, session_env={"LOGFILE": "/work/stale.log"})
        self.assertEqual([p for p, _, _ in got if "stale" in p], [])

    def test_literal_reassignment_replaces_the_carried_value(self):
        cmd = 'LOGFILE="/work/fresh.log" && tail "$LOGFILE"'
        got = keys.extract(cmd, WD, session_env={"LOGFILE": "/work/stale.log"})
        self.assertEqual({(p, d) for p, d, _ in got},
                         {("/work/fresh.log", "read")})

    def test_assigned_names_reports_unreadable_assignments_too(self):
        self.assertEqual(keys.assigned_names('A="/x.log" B=$(date) C=$D/y'),
                         {"A", "B", "C"})


class TestInterpreterNamedByPath(unittest.TestCase):
    """A venv interpreter is usually spelled out in full. Matching the whole
    token missed it, and because the script-argument rules key off recognising
    the interpreter, the command yielded nothing at all rather than one less
    edge."""

    VENV = "/home/u/python311_env/bin/python"

    def test_absolute_interpreter_still_reads_its_script(self):
        got = _dirs(f"{self.VENV} -u /a/run.py")
        self.assertEqual(got["/a/run.py"], "read")

    def test_absolute_interpreter_still_reads_the_scripts_argument(self):
        got = _dirs(f"nohup {self.VENV} -u /a/run.py /a/suites/case.yaml")
        self.assertEqual(got["/a/suites/case.yaml"], "read")

    def test_plain_name_keeps_working(self):
        self.assertEqual(_dirs("python3.11 -u /a/run.py")["/a/run.py"], "read")

    def test_a_random_binary_is_not_an_interpreter(self):
        """The basename must still be a known interpreter, not any executable."""
        self.assertEqual(_dirs("/usr/local/bin/mytool /a/b.yaml"), {})


class TestSegmentBoundaries(unittest.TestCase):
    """Newlines end commands, with two exceptions. Each case here cost real
    edges when it was wrong."""

    def test_newline_separates_commands(self):
        self.assertEqual(keys._flatten("a\nb"), "a ; b")

    def test_backslash_continuation_stays_one_command(self):
        """`run.py \\` + `suite.yaml` -- split here and the argument loses its
        interpreter. Whitespace is free; what matters is that no boundary is
        inserted."""
        self.assertNotIn(";", keys._flatten("a \\\nb"))
        got = _dirs("python3 -u /a/run.py \\\n  /a/suites/case.yaml")
        self.assertEqual(got["/a/suites/case.yaml"], "read")

    def test_newline_inside_quotes_is_not_a_boundary(self):
        """`python3 -c "…multi-line…"` is one argument. Cutting it into
        segments hid the interpreter and lost four real reads in a measured
        session."""
        self.assertEqual(keys._flatten('x "a\nb"'), 'x "a b"')

    def test_a_verb_cannot_claim_a_path_two_lines_later(self):
        """The leak this fixed: `grep` on line 1 was classifying a path on
        line 3, which is how `ls -l x.py` -- not a read verb at all -- came out
        as a read."""
        got = _dirs('grep -E "Location" /tmp/f1.log\n'
                    'echo "---"\n'
                    'ls -l /tmp/a.py')
        self.assertEqual(got.get("/tmp/f1.log"), "read")
        self.assertNotIn("/tmp/a.py", got)


class TestPathNormalization(unittest.TestCase):

    def test_relative_path_resolves_against_working_dir(self):
        self.assertEqual(_dirs("python3 tfw/run.py"),
                         {"/work/proj/sut/tfw/run.py": "read"})

    def test_dot_segments_are_folded(self):
        self.assertEqual(_dirs("python3 ./scripts/../gen.py"),
                         {"/work/proj/sut/gen.py": "read"})

    def test_relative_path_without_working_dir_is_skipped(self):
        self.assertEqual(_dirs("python3 tfw/run.py", working_dir=None), {})

    def test_tilde_is_left_unexpanded(self):
        """The home directory of the account that ran the agent is not in the
        trace, so guessing it would silently merge or split entities."""
        self.assertEqual(_dirs("source ~/py311env/bin/activate.sh"),
                         {"~/py311env/bin/activate.sh": "read"})

    def test_variables_and_globs_are_deferred(self):
        self.assertEqual(_dirs('tail -50 "$LOGFILE.log"'), {})
        self.assertEqual(_dirs("ls -t /a/results/run_*.log"), {})

    def test_unresolved_reports_what_was_skipped(self):
        got = keys.unresolved("ls -t /a/results/run_*.log")
        self.assertEqual(got, ["/a/results/run_*.log"])


class TestWiredIntoTheGraph(unittest.TestCase):

    def test_command_write_creates_v1_with_no_v0(self):
        """Same reasoning as create_file: no evidence the file existed."""
        g = build_skeleton([_cmd(1, "cat > /tmp/run.sh")])
        self.assertEqual([e.version for e in g.versions_of("/tmp/run.sh")], [1])
        self.assertEqual([(e.kind, e.confidence) for e in g.edges],
                         [(GENERATED_BY, LIKELY)])

    def test_command_read_of_a_pre_existing_file(self):
        g = build_skeleton([_cmd(1, "tail -50 /a/b.log")])
        self.assertEqual([e.version for e in g.versions_of("/a/b.log")], [0])
        self.assertEqual([(e.kind, e.confidence) for e in g.edges],
                         [(USED_BY, LIKELY)])

    def test_command_joins_a_chain_started_by_the_path_field(self):
        """The command pass runs second so it extends existing chains rather
        than building a parallel one."""
        acts = [
            {"ref": "a1b2c3d4#1", "idx": 1, "turn": 1, "action": "create_file",
             "path": "/work/proj/sut/gen.py"},
            _cmd(2, "python3 gen.py"),
        ]
        g = build_skeleton(acts)
        self.assertEqual([e.version for e in g.versions_of("/work/proj/sut/gen.py")], [1])
        kinds = sorted((e.kind, e.confidence) for e in g.edges)
        self.assertEqual(kinds, [(GENERATED_BY, "certain"), (USED_BY, LIKELY)])

    def test_path_field_edges_stay_certain(self):
        """filter_edges(CERTAIN) must still yield only structured-field edges."""
        acts = [
            {"ref": "a1b2c3d4#1", "idx": 1, "turn": 1, "action": "modify_file",
             "path": "/a/f.py"},
            _cmd(2, "python3 /a/f.py"),
        ]
        g = build_skeleton(acts)
        self.assertEqual(len(g.filter_edges("certain")), 2)   # usedBy v0 + generatedBy
        self.assertEqual(len(g.filter_edges(LIKELY)), 3)

    def test_mention_without_direction_is_warned_not_linked(self):
        """Mentioning a path is not depending on it. Measured cases include a
        grep pattern shaped like a filename and paths inside a `python -c`
        string, which the script opens at run time rather than the command.
        Linking on mention alone would bypass the direction check."""
        acts = [
            {"ref": "a1b2c3d4#1", "idx": 1, "turn": 1, "action": "create_file",
             "path": "/a/known.py"},
            _cmd(2, "mytool --frobnicate /a/known.py"),
        ]
        g = build_skeleton(acts)
        self.assertEqual(len(g.in_edges("a:a1b2c3d4#2")), 0)
        self.assertEqual(len(g.out_edges("a:a1b2c3d4#2")), 0)
        self.assertTrue(any("no direction could be determined" in w
                            for w in g.warnings))

    def test_unresolved_paths_are_warned_not_silent(self):
        """Without an output to resolve it, the glob stays a recorded gap."""
        g = build_skeleton([_cmd(1, "ls -t /a/run_*.log")])
        self.assertEqual(len(g.warnings), 1)
        self.assertIn("unresolved", g.warnings[0])


class TestExtensionCoverage(unittest.TestCase):
    """The whitelist is what turns a token into a path candidate, so its gaps
    are silent. The first version had 18 entries from one Python/YAML project;
    on a C codebase 1124 tokens were rejected on extension alone -- .c 583
    times, .h 321."""

    def test_c_and_header_files(self):
        self.assertTrue(keys.looks_like_path("framework/net/api/sr_data.c"))
        self.assertTrue(keys.looks_like_path("/repo/x/util.h"))

    def test_interface_and_diff_files(self):
        self.assertTrue(keys.looks_like_path("api/sr_data.ifx"))
        self.assertTrue(keys.looks_like_path("/tmp/baseline.diff"))

    def test_still_rejects_a_version_number(self):
        """The extension guard must not start matching dotted numbers."""
        self.assertFalse(keys.looks_like_path("python3.11"))
        self.assertFalse(keys.looks_like_path("v1.2.3"))


class TestVariableExpansion(unittest.TestCase):
    """A variable assigned in the same command is not a guess -- the value is
    in the text. 195 path tokens across five sessions begin with a variable the
    command itself assigns."""

    def test_assignment_then_use_in_one_command(self):
        got = _dirs('KIRO_HOME="/work/bb/.kiro" && cat $KIRO_HOME/settings/info.json')
        self.assertEqual(got, {"/work/bb/.kiro/settings/info.json": "read"})

    def test_braced_form(self):
        got = _dirs('D="/work/out" && cat ${D}/result.log')
        self.assertEqual(got, {"/work/out/result.log": "read"})

    def test_unknown_variable_is_left_for_task5(self):
        """No assignment in the command; expanding it would be a guess."""
        self.assertEqual(_dirs("cat $KIRO_HOME/settings/info.json"), {})

    def test_assignment_whose_value_is_itself_a_variable_is_not_expanded(self):
        """`SB=$BASE/x` is not literal; half-expanding it would be wrong."""
        self.assertEqual(_dirs('SB=$BASE/sub && cat $SB/file.log'), {})

    def test_shell_vars_only_collects_literals(self):
        env = keys.shell_vars('A="/x" B=$A/y C=$(pwd) && cat $A/f.log')
        self.assertEqual(env, {"A": "/x"})

    def test_expand_vars_returns_none_when_incomplete(self):
        self.assertEqual(keys.expand_vars("$A/f.log", {"A": "/x"}), "/x/f.log")
        self.assertIsNone(keys.expand_vars("$A/$B", {"A": "/x"}))


class TestEchoedVariable(unittest.TestCase):
    """`echo $KIRO_HOME` states a value the command never assigned, because it
    came from the environment. One session echoes it four times."""

    def _resp(self, stdout):
        import json
        return json.dumps({"Success": {"items": [{"Json": {"stdout": stdout}}]}})

    def test_echo_reports_the_value(self):
        got = keys.echoed_var('echo $KIRO_HOME', self._resp("/work/bb/.kiro\n"))
        self.assertEqual(got, ("KIRO_HOME", "/work/bb/.kiro"))

    def test_not_an_echo(self):
        self.assertIsNone(keys.echoed_var("cat $X/f", self._resp("/x\n")))

    def test_multiword_output_is_rejected(self):
        """`echo $PS1` or similar -- not a single clean path/value."""
        self.assertIsNone(keys.echoed_var("echo $X", self._resp("a b c\n")))

    def test_value_carried_forward_resolves_a_later_command(self):
        """The build accumulates echoed values in idx order."""
        g = build_skeleton([
            _cmd(1, "echo $KIRO_HOME", turn=1),
            _cmd(2, "cat $KIRO_HOME/settings/info.json", turn=1),
        ])
        # a[1] has no response here, so attach one via the dict form.
        acts = [
            dict(_cmd(1, "echo $KIRO_HOME", turn=1),
                 response=self._resp("/work/bb/.kiro\n")),
            _cmd(2, "cat $KIRO_HOME/settings/info.json", turn=1),
        ]
        g = build_skeleton(acts)
        reads = [e for e in g.edges if e.kind == USED_BY]
        keys_read = {g.entities[e.src].key for e in reads}
        self.assertIn("/work/bb/.kiro/settings/info.json", keys_read)

    def test_reassignment_does_not_leak_backwards(self):
        """A command only sees assignments from earlier idx."""
        acts = [
            _cmd(1, "cat $D/early.log", turn=1),          # D unknown here
            dict(_cmd(2, "echo $D", turn=1), response=self._resp("/late\n")),
        ]
        g = build_skeleton(acts)
        self.assertNotIn("/late/early.log",
                         {e.key for e in g.entities.values()})


class TestCommandCwdConsumed(unittest.TestCase):
    """The graph reads the effective cwd the normalization layer resolved
    (Action.cwd), rather than parsing `cd` itself. Here the dict carries cwd
    directly, as the normalized Action would."""

    def test_relative_path_uses_the_actions_cwd(self):
        a = {"ref": "a1b2c3d4#1", "sid": "a1b2c3d4-1111", "idx": 1, "turn": 1,
             "action": "run_command", "command": "grep -n foo sub/dir/file.c",
             "cwd": "/repo/x", "args": {}}
        got = {p: d for p, d, _ in keys.extract_from_action(a)}
        self.assertEqual(got, {"/repo/x/sub/dir/file.c": "read"})

    def test_falls_back_to_args_working_dir(self):
        a = {"ref": "a1b2c3d4#1", "sid": "a1b2c3d4-1111", "idx": 1, "turn": 1,
             "action": "run_command", "command": "grep -n foo file.c",
             "args": {"working_dir": "/wd"}}
        got = {p: d for p, d, _ in keys.extract_from_action(a)}
        self.assertEqual(got, {"/wd/file.c": "read"})


if __name__ == "__main__":
    unittest.main()
