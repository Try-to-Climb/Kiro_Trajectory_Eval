"""Regression tests for issues found comparing ACP multi-turn tests with official session records.

Covers:
  - run dimension: sub-agents inherit the same KIRO_SESSION_ID; agent_spawn splits them
  - Official session record enrichment: agent_name / usage / cost / full assistant messages
  - Cross-source self-check: fold aliases on both sides; order differences are not misses; validation-rejected calls explain misses
Run: python3 -m unittest discover -s normalize/tests -t .
"""

from __future__ import annotations

import unittest

from normalize import normalize_events
from normalize.official import OfficialRecord, TurnMeta, cross_check

CWD = "/w"


def spawn(agent=None, ts="2026-08-04T10:00:00.000Z"):
    r = {"ts": ts, "event": "agent_spawn", "session_id": "s", "cwd": CWD}
    if agent:
        r["agent_name"] = agent
    return r


def prompt(text="q", ts="2026-08-04T10:00:01.000Z"):
    return {"ts": ts, "event": "user_prompt", "session_id": "s",
            "prompt": text, "cwd": CWD}


def pre(tool, tool_input, ts="2026-08-04T10:00:02.000Z"):
    return {"ts": ts, "event": "pre_tool_use", "session_id": "s",
            "tool": tool, "tool_input": tool_input, "cwd": CWD}


def post(tool, tool_input, ts="2026-08-04T10:00:03.000Z"):
    return {"ts": ts, "event": "post_tool_use", "session_id": "s", "tool": tool,
            "success": True, "response_size": 1, "tool_input": tool_input, "cwd": CWD}


def call(tool, tool_input):
    return [pre(tool, tool_input), post(tool, tool_input)]


def rd(path):
    return {"operations": [{"mode": "Line", "path": path}]}


class TestRunDimension(unittest.TestCase):
    """Sub-agents inherit the parent's KIRO_SESSION_ID; hook events land in the same directory.

    Measured: out of 87 historical sessions, 12 contain multiple agent_spawn events, and those
    12 are exactly the ones that fail to line up with Kiro's official record. We must split
    by run to do trajectory evaluation.
    """

    def test_single_run_multi_turn_is_acp_shape(self):
        """ACP shape: 1 agent_spawn + N prompt turns."""
        ev = [spawn("a"), prompt("t1"), *call("read", rd("/a")),
              prompt("t2"), *call("read", rd("/b")),
              prompt("t3"), *call("read", rd("/c"))]
        ir = normalize_events(ev)
        self.assertEqual(ir.runs, 1)
        self.assertEqual(ir.turns, 3)
        self.assertEqual([a.run for a in ir.actions], [1, 1, 1])
        self.assertEqual([a.turn for a in ir.actions], [1, 2, 3])
        self.assertEqual(ir.warnings, [])

    def test_multi_run_is_subagent_shape(self):
        """--no-interactive / sub-agent shape: each dispatch has one agent_spawn + 1 turn."""
        ev = [spawn("parent"), prompt("p1"), *call("read", rd("/a")),
              spawn("child"), prompt("p2"), *call("write", {"command": "create", "path": "/b"}),
              spawn("child"), prompt("p3"), *call("shell", {"command": "ls"})]
        ir = normalize_events(ev)
        self.assertEqual(ir.runs, 3)
        self.assertEqual([a.run for a in ir.actions], [1, 2, 3])
        self.assertEqual([len(ir.by_run(r)) for r in (1, 2, 3)], [1, 1, 1])
        self.assertTrue(any("agent spawns" in w for w in ir.warnings))

    def test_run_count_survives_empty_trailing_run(self):
        """When no tool call follows the last spawn, runs must not be undercounted."""
        ir = normalize_events([spawn("a"), prompt(), *call("read", rd("/a")),
                               spawn("b"), prompt()])
        self.assertEqual(ir.runs, 2)

    def test_no_spawn_defaults_to_run_1(self):
        ir = normalize_events([prompt(), *call("read", rd("/a"))])
        self.assertEqual(ir.runs, 1)
        self.assertEqual(ir.actions[0].run, 1)

    def test_per_run_per_turn_grouping(self):
        """The correct grouping unit for rules is (run, turn), not turn alone."""
        ev = [spawn("a"), prompt("p1")]
        ev += call("read", rd("/x")) * 1
        ev += call("read", rd("/x"))
        ev += [spawn("b"), prompt("p2")]
        ev += call("read", rd("/x"))
        ir = normalize_events(ev)
        per = {(a.run, a.turn) for a in ir.actions}
        self.assertEqual(per, {(1, 1), (2, 2)})
        self.assertEqual(len(ir.by_run(1)), 2)
        self.assertEqual(len(ir.by_run(2)), 1)


def _rec(tool_names_per_turn, rejected_per_turn=None, agent="a") -> OfficialRecord:
    rec = OfficialRecord(session_id="s", meta_path="/fake.json", agent_name=agent)
    for i, names in enumerate(tool_names_per_turn, 1):
        rec.turns.append(TurnMeta(
            turn=i, tool_uses=len(names), tool_names=list(names),
            rejected_tools=list((rejected_per_turn or [[]] * len(tool_names_per_turn))[i - 1]),
        ))
    return rec


class TestCrossCheck(unittest.TestCase):
    """Cross-check hook records against official records."""

    def test_identical_passes(self):
        rec = _rec([["read", "glob"], ["write"]])
        self.assertEqual(cross_check(rec, 2, [2, 1], [["read", "glob"], ["write"]]), [])

    def test_alias_folded_on_both_sides(self):
        """Official mostly uses modern names, but sessions using old names exist (c9312b31 has fs_read)."""
        rec = _rec([["fs_read", "execute_bash"]])
        out = cross_check(rec, 1, [2], [["read", "shell"]])
        self.assertEqual(out, [], f"after folding aliases both sides no warning should remain; actual: {out}")

    def test_order_difference_is_not_a_miss(self):
        """Parallel calls: hook records completion order, official records issue order, so sequences diverge."""
        rec = _rec([["read", "glob", "grep"]])
        out = cross_check(rec, 1, [3], [["grep", "read", "glob"]])
        self.assertEqual(out, [], f"same multiset with different order should not warn; actual: {out}")

    def test_validation_rejected_explains_missing(self):
        """Calls rejected in validation do not fire preToolUse; hook cannot see them."""
        rec = _rec([["glob", "read", "glob"]], rejected_per_turn=[["read"]])
        out = cross_check(rec, 1, [2], [["glob", "glob"]])
        self.assertTrue(any("already rejected via parameter validation" in w for w in out), out)
        self.assertFalse(any("suspected missing record" in w for w in out), out)

    def test_unexplained_miss_is_flagged(self):
        rec = _rec([["glob", "read", "glob"]])
        out = cross_check(rec, 1, [2], [["glob", "glob"]])
        miss = [w for w in out if "suspected missing record" in w]
        self.assertEqual(len(miss), 1, out)
        self.assertIn("read", miss[0])

    def test_large_span_downgraded_to_coverage_note(self):
        """Official may cover a wider time span than the trace; a large total delta does not imply a hook bug."""
        rec = _rec([["shell"] * 100])
        out = cross_check(rec, 1, [3], [["shell"] * 3])
        self.assertTrue(any("coverage scope may differ" in w for w in out), out)
        self.assertFalse(any("suspected missing record" in w for w in out), out)

    def test_no_official_record_no_warnings(self):
        rec = OfficialRecord(session_id="s")      # found == False
        self.assertEqual(cross_check(rec, 3, [1, 2, 3], [["read"]]), [])

    def test_turn_count_mismatch_flagged(self):
        rec = _rec([["read"], ["read"], ["read"]])
        out = cross_check(rec, 1, [3], [["read", "read", "read"]])
        self.assertTrue(any("turn count does not match official record" in w for w in out))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRunAttribution(unittest.TestCase):
    """Restore each run to its real official session.

    Control experiment (acptest/traces_sub) confirms: Kiro allocates an independent session to each
    sub-agent, but the KIRO_SESSION_ID seen by hook is still the parent's, so sub-agent events land
    in the parent's directory. Restoration relies on: the first user_prompt of each run == that
    session's first Prompt.
    """

    def test_cannot_assume_run1_is_parent(self):
        """The parent agent may not have the hook attached at all; then run1 is actually the first sub-agent.

        Measured on f8ba129a: hook trace's first agent_spawn is at 03:27:05, but the parent session
        was created at 03:20:56 -- the parent's own activity is entirely absent from the trace, and
        run1's 35 calls match the 35 tool uses of child session a88da625 tool by tool.
        """
        from normalize.attribution import resolve_runs
        # With no candidate official records, we should not force run1 to be the trace directory session
        atts = resolve_runs("parent-sid", {1: "child task text"}, {1: None},
                            cwd="/nonexistent", official_dir="/nonexistent")
        self.assertEqual(len(atts), 1)
        self.assertFalse(atts[0].resolved)
        self.assertIsNone(atts[0].session_id)

    def test_prompt_head_recorded(self):
        from normalize.attribution import resolve_runs
        atts = resolve_runs("p", {1: "x" * 100}, {1: None},
                            cwd=None, official_dir="/nonexistent")
        self.assertEqual(len(atts[0].prompt_head), 60)

    def test_run_prompts_captured_per_run(self):
        ev = [spawn("p"), prompt("first run prompt"), *call("read", rd("/a")),
              spawn("c"), prompt("second run prompt"), *call("read", rd("/b"))]
        ir = normalize_events(ev)
        self.assertEqual(ir.run_prompts, {1: "first run prompt", 2: "second run prompt"})
        self.assertEqual(sorted(ir.run_started), [1, 2])

    def test_multi_run_warning_mentions_mechanism(self):
        ev = [spawn("p"), prompt("a"), *call("read", rd("/a")),
              spawn("c"), prompt("b"), *call("read", rd("/b"))]
        ir = normalize_events(ev)
        w = " ".join(ir.warnings)
        self.assertIn("KIRO_SESSION_ID", w)
        self.assertIn("independent sessions", w)
