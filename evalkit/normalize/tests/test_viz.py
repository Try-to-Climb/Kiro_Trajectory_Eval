"""Smoke regression for the viz module (timeline / turn summary / replay / compare).

Only verifies no exception and that key content is produced; does not check
pixel-level rendering.
"""

from __future__ import annotations

import unittest

from normalize import normalize_events
from normalize import viz


def pre(tool, ti, ts):
    return {"ts": ts, "event": "pre_tool_use", "session_id": "s", "tool": tool,
            "tool_input": ti, "cwd": "/w"}


def post(tool, ti, ts):
    return {"ts": ts, "event": "post_tool_use", "session_id": "s", "tool": tool,
            "success": True, "response_size": 1, "tool_input": ti, "cwd": "/w"}


def prompt(t="q", ts="2026-08-05T10:00:00.000Z"):
    return {"ts": ts, "event": "user_prompt", "session_id": "s", "prompt": t, "cwd": "/w"}


def _ir():
    ev = [prompt("t1", "2026-08-05T10:00:00.000Z"),
          pre("read", {"operations": [{"mode": "Line", "path": "/a"}]}, "2026-08-05T10:00:01.000Z"),
          post("read", {"operations": [{"mode": "Line", "path": "/a"}]}, "2026-08-05T10:00:02.000Z"),
          pre("shell", {"command": "ls && pwd"}, "2026-08-05T10:00:03.000Z"),
          post("shell", {"command": "ls && pwd"}, "2026-08-05T10:00:04.000Z"),
          pre("shell", {"command": "slow"}, "2026-08-05T10:00:05.000Z")]  # orphan
    return normalize_events(ev, session_id="testsess", source="x")


class TestViz(unittest.TestCase):

    def setUp(self):
        self.ir = _ir()

    def test_text_timeline_has_lane_and_symbols(self):
        out = viz.text_timeline(self.ir)
        self.assertIn("timeline", out)
        self.assertIn("|", out)
        self.assertIn("x", out)          # orphan marked x

    def test_turn_summary_lists_turn(self):
        out = viz.turn_summary(self.ir)
        self.assertIn("turn summary", out)
        self.assertIn("cmd", out)

    def test_mermaid_is_gantt(self):
        out = viz.to_mermaid(self.ir)
        self.assertIn("```mermaid", out)
        self.assertIn("gantt", out)

    def test_replay_reconstructs_commands(self):
        out = viz.to_replay(self.ir)
        self.assertIn("#!/bin/bash", out)
        self.assertIn("ls && pwd", out)      # run_command verbatim
        self.assertIn("cat /a", out)          # read -> cat
        self.assertIn("✗", out)               # orphan marked failed

    def test_replay_marks_side_effects(self):
        ev = [prompt(), pre("write", {"command": "create", "path": "/x"}, "2026-08-05T10:00:01.000Z"),
              post("write", {"command": "create", "path": "/x"}, "2026-08-05T10:00:02.000Z")]
        ir = normalize_events(ev, session_id="s2", source="x")
        out = viz.to_replay(ir)
        self.assertIn("side effect", out)

    def test_png_renders(self):
        import tempfile, os
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed")
        p = os.path.join(tempfile.mkdtemp(), "t.png")
        viz.to_png(self.ir, p)
        self.assertTrue(os.path.getsize(p) > 0)

    def test_no_ts_falls_back_to_index(self):
        """When official source has no per-action ts, the text lane falls back to an index axis without raising."""
        for a in self.ir.actions:
            a.ts = ""
        out = viz.text_timeline(self.ir)
        self.assertIn("action index", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
