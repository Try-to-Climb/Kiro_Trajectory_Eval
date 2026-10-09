"""Task 5 tests: values only the recorded output can pin down.

The response is used to *fill in a blank*, never to decide direction. Three
commands in the reference session print a concrete log path while writing it,
merely locating it, and reading it respectively -- so "a path in stdout is a
produced artifact" would be wrong two times out of three.

Run:  cd evalkit && python3 -m unittest graph.tests.test_response -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import keys                                        # noqa: E402
from graph.build import build_skeleton                         # noqa: E402
from graph.schema import GENERATED_BY, LIKELY, USED_BY         # noqa: E402

WD = "/work/proj/sut"
LOG = "/work/results/test_results/testrun_20260811_162941_162941.log"


def _resp(stdout, stderr=""):
    """A tool result in the shape Kiro records."""
    return json.dumps({"Success": {"items": [{"Json": {
        "exit_status": "exit status: 0", "stdout": stdout, "stderr": stderr}}]}})


def _cmd(idx, command, stdout="", working_dir=WD):
    return {"ref": f"a1b2c3d4#{idx}", "sid": "a1b2c3d4-1111", "idx": idx,
            "turn": 6, "action": "run_command", "path": None,
            "command": command, "response": _resp(stdout),
            "args": {"working_dir": working_dir}}


class TestUnpackingTheEnvelope(unittest.TestCase):

    def test_stdout_and_stderr_are_returned(self):
        got = keys.response_text(_resp("out here", "err here"))
        self.assertIn("out here", got)
        self.assertIn("err here", got)

    def test_unrecognised_shape_degrades_to_raw(self):
        self.assertEqual(keys.response_text("plain text"), "plain text")
        self.assertEqual(keys.response_text('{"Weird": 1}'), '{"Weird": 1}')

    def test_empty_response(self):
        self.assertEqual(keys.response_text(None), "")
        self.assertEqual(keys.response_text(""), "")


class TestWhichOutputLinesAreTrusted(unittest.TestCase):
    """Line-anchored on purpose. `tail`ing a log emits whatever the log holds,
    and a traceback in there names source files the command never touched."""

    TRACEBACK = (
        "Traceback (most recent call last):\n"
        '  File "/work/proj/sut/tfw/run.py", line 921, in <module>\n'
        "    main()\n"
        "ModuleNotFoundError: No module named 'tfw'\n"
    )

    def test_key_equals_path(self):
        self.assertEqual(keys.response_paths(_resp(f"PID=3301766\nLOG={LOG}\n")),
                         [LOG])

    def test_bare_path_line_is_ls_output(self):
        self.assertEqual(keys.response_paths(_resp(f"{LOG}\n")), [LOG])

    def test_successfully_created(self):
        self.assertEqual(
            keys.response_paths(_resp("Successfully created /tmp/out.sh (10 lines).")),
            ["/tmp/out.sh"])

    def test_traceback_paths_are_ignored(self):
        """The decisive case: these files are log content, not dependencies."""
        self.assertEqual(keys.response_paths(_resp(self.TRACEBACK)), [])

    def test_a_trusted_line_survives_alongside_a_traceback(self):
        """a[16] prints LOG=<path> and then dumps a traceback."""
        got = keys.response_paths(_resp(f"LOG={LOG}\n" + self.TRACEBACK))
        self.assertEqual(got, [LOG])

    def test_pids(self):
        self.assertEqual(keys.response_pids(_resp("PID=3301766\n")), ["3301766"])
        self.assertEqual(keys.response_pids(_resp("    PID STAT ELAPSED\n")), [])


class TestResolution(unittest.TestCase):

    def test_date_substitution_resolves_by_prefix(self):
        """a[9]: `> /work/.../testrun_$(date +%Y%m%d_%H%M%S).log`"""
        got = keys.resolve_with_response(
            'nohup python3 run.py > /work/results/test_results/'
            'testrun_$(date +%Y%m%d_%H%M%S).log 2>&1 &',
            WD, _resp(f"PID=3301766\nLOG={LOG}\n"))
        self.assertEqual([(p, d) for p, d, _ in got], [(LOG, "write")])

    def test_variable_reference_resolves_as_sole_candidate(self):
        """a[16]: `tail -50 "$LOGFILE"` -- no literal head to match on."""
        got = keys.resolve_with_response('tail -50 "$LOGFILE"', WD,
                                         _resp(f"LOG={LOG}\n"))
        self.assertEqual([(p, d) for p, d, _ in got], [(LOG, "read")])

    def test_glob_resolves_by_prefix(self):
        got = keys.resolve_with_response(
            "tail -50 /work/results/test_results/testrun_*.log",
            WD, _resp(f"{LOG}\n"))
        self.assertEqual([(p, d) for p, d, _ in got], [(LOG, "read")])

    def test_direction_comes_from_the_command_not_the_output(self):
        """Same output, opposite directions -- the point of the whole design."""
        out = _resp(f"LOG={LOG}\n")
        w = keys.resolve_with_response('echo x > "$LOGFILE"', WD, out)
        r = keys.resolve_with_response('tail -50 "$LOGFILE"', WD, out)
        self.assertEqual([d for _, d, _ in w], ["write"])
        self.assertEqual([d for _, d, _ in r], ["read"])

    def test_ambiguous_output_is_left_alone(self):
        other = "/work/results/test_results/testrun_20260811_162941_163022.log"
        got = keys.resolve_with_response('tail -50 "$LOGFILE"', WD,
                                         _resp(f"{LOG}\n{other}\n"))
        self.assertEqual(got, [])

    def test_already_literal_paths_are_left_to_extract(self):
        got = keys.resolve_with_response(f"tail -50 {LOG}", WD, _resp(f"{LOG}\n"))
        self.assertEqual(got, [])

    def test_no_output_means_no_resolution(self):
        self.assertEqual(
            keys.resolve_with_response('tail -50 "$LOGFILE"', WD, _resp("")), [])

    def test_unclassifiable_position_is_not_resolved(self):
        """`ls -t <glob>` locates a file without reading its content."""
        got = keys.resolve_with_response(
            "ls -t /work/results/test_results/testrun_*.log | head -1",
            WD, _resp(f"{LOG}\n"))
        self.assertEqual(got, [])


class TestSubstitutionTokenising(unittest.TestCase):
    """`$(date +%Y%m%d_%H%M%S)` contains a space, so splitting on whitespace
    tore `/a/run_$(date +%s).log` into fragments and separated the `$` from its
    path -- which is exactly the marker Task 5 looks for."""

    def test_substitution_stays_in_one_token(self):
        toks = keys.tokenize("cat > /a/run_$(date +%Y%m%d).log")
        self.assertIn("/a/run_$(date +%Y%m%d).log", toks)

    def test_paths_inside_a_substitution_are_still_found(self):
        """`LOGFILE=$(cat /tmp/logpath.txt)` holds a real read inside a token
        that looks unresolvable as a whole. Collapsing substitutions without
        scanning inside them lost 10 real dependencies."""
        got = {(p, d) for p, d, _ in
               keys.extract("LOGFILE=$(cat /tmp/logpath.txt)", WD)}
        self.assertEqual(got, {("/tmp/logpath.txt", "read")})

    def test_resolvable_prefix(self):
        self.assertEqual(keys._resolvable_prefix("/a/b/run_$(date).log"), "/a/b/run_")
        self.assertEqual(keys._resolvable_prefix('"$LOGFILE"'), "")
        self.assertEqual(keys._resolvable_prefix("/a/run_*.log"), "/a/run_")

    def test_variable_reference_detection(self):
        self.assertTrue(keys.is_variable_reference('"$LOGFILE"'))
        self.assertTrue(keys.is_variable_reference("${LOG}"))
        self.assertFalse(keys.is_variable_reference("/a/b.log"))
        self.assertFalse(keys.is_variable_reference("$(date)"))


class TestWiredIntoTheGraph(unittest.TestCase):

    def test_resolved_write_creates_a_version(self):
        g = build_skeleton([_cmd(
            1, 'nohup python3 run.py > /work/results/r_$(date +%s).log 2>&1 &',
            stdout="LOG=/work/results/r_162941.log\n")])
        self.assertEqual([e.version for e in g.versions_of("/work/results/r_162941.log")],
                         [1])
        self.assertEqual([(e.kind, e.confidence) for e in g.edges
                          if e.dst.startswith("e:")],
                         [(GENERATED_BY, LIKELY)])

    def test_pid_links_a_later_ps_to_the_launcher(self):
        acts = [_cmd(1, "nohup python3 run.py & echo PID=$!", stdout="PID=3304381\n"),
                _cmd(2, "ps -p 3304381 -o pid,stat")]
        g = build_skeleton(acts)
        pid_nodes = [e for e in g.entities.values() if e.kind == "pid"]
        self.assertEqual([e.key for e in pid_nodes], ["3304381"])
        self.assertEqual(pid_nodes[0].producer_ref, "a1b2c3d4#1")
        consumers = {e.dst for e in g.out_edges("e:3304381@v1")}
        self.assertIn("a:a1b2c3d4#2", consumers)

    def test_pid_is_not_consumed_by_an_earlier_command(self):
        """Time order still applies: a command cannot use a pid not yet born."""
        acts = [_cmd(1, "ps -p 3304381"),
                _cmd(2, "nohup python3 run.py & echo PID=$!", stdout="PID=3304381\n")]
        g = build_skeleton(acts)
        self.assertEqual(g.out_edges("e:3304381@v1"), [])

    def test_resolution_produces_likely_not_certain(self):
        g = build_skeleton([_cmd(
            1, 'tail -50 "$LOGFILE"', stdout="LOG=/work/results/r.log\n")])
        self.assertEqual(g.filter_edges("certain"), [])
        self.assertEqual(len(g.filter_edges(LIKELY)), 1)
        self.assertEqual(g.edges[0].kind, USED_BY)

    def test_still_warns_when_nothing_resolves(self):
        g = build_skeleton([_cmd(1, "ls -t /a/run_*.log", stdout="")])
        self.assertTrue(any("unresolved" in w for w in g.warnings))


if __name__ == "__main__":
    unittest.main()
