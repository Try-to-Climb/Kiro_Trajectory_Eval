"""Artifact fates, on graphs small enough to write the answer down.

The four fates are mutually exclusive by construction, so each test pins one
shape: an input that was only ever read, a version replaced by a later one, a
newest version nobody read, and one that was read.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import artifacts                                  # noqa: E402
from graph.artifacts import (CONSUMED, INPUT, SUPERSEDED,     # noqa: E402
                            UNCONSUMED)
from graph.build import build_skeleton                        # noqa: E402


def _a(idx, action, path=None, command=None):
    return {"ref": f"s#{idx}", "sid": "s", "idx": idx, "turn": 1,
            "action": action, "path": path, "command": command}


def _fates(actions):
    g = build_skeleton(actions, session_id="s")
    return {(f.key, f.version): f for f in artifacts.classify(g)}


class TestTheFourFates(unittest.TestCase):

    def test_a_file_only_read_is_an_input(self):
        got = _fates([_a(1, "read_file", "/x.py")])
        self.assertEqual(got[("/x.py", 0)].fate, INPUT)

    def test_a_version_replaced_by_a_later_one_is_superseded(self):
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "create_file", "/x.py")])
        self.assertEqual(got[("/x.py", 1)].fate, SUPERSEDED)
        self.assertEqual(got[("/x.py", 1)].superseded_by, "e:/x.py@v2")

    def test_the_newest_version_nobody_read_is_unconsumed(self):
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "create_file", "/x.py")])
        self.assertEqual(got[("/x.py", 2)].fate, UNCONSUMED)

    def test_a_version_something_read_is_consumed(self):
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "read_file", "/x.py")])
        self.assertEqual(got[("/x.py", 1)].fate, CONSUMED)
        self.assertEqual(got[("/x.py", 1)].consumers, ("s#2",))


class TestWrittenAndNeverRead(unittest.TestCase):
    """The one judgement worth flagging: overwritten with nothing reading it."""

    def test_overwritten_without_a_read_is_flagged(self):
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "create_file", "/x.py")])
        self.assertTrue(got[("/x.py", 1)].written_and_never_read)

    def test_read_before_being_overwritten_is_not_flagged(self):
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "read_file", "/x.py"),
                      _a(3, "create_file", "/x.py")])
        self.assertFalse(got[("/x.py", 1)].written_and_never_read)

    def test_the_newest_version_is_never_flagged(self):
        """Unread is not the same as wasted while it is still current."""
        got = _fates([_a(1, "create_file", "/x.py")])
        self.assertFalse(got[("/x.py", 1)].written_and_never_read)

    def test_a_modify_reads_before_writing_so_v1_is_consumed(self):
        """`modify_file` consumes the version it replaces, which is why an edit
        chain is not a pile of wasted writes."""
        got = _fates([_a(1, "create_file", "/x.py"),
                      _a(2, "modify_file", "/x.py")])
        self.assertEqual(got[("/x.py", 1)].fate, SUPERSEDED)
        self.assertFalse(got[("/x.py", 1)].written_and_never_read)


class TestWhatIsExcluded(unittest.TestCase):

    def test_pids_are_not_artifacts(self):
        acts = [_a(1, "run_command", command="nohup python3 /a/x.py & echo hi")]
        acts[0]["response"] = "PID=4242"
        g = build_skeleton(acts, session_id="s")
        keys_seen = {f.key for f in artifacts.classify(g)}
        self.assertNotIn("4242", keys_seen)
        self.assertIn("4242", {f.key for f in
                               artifacts.classify(g, include_pids=True)})


class TestSummary(unittest.TestCase):

    def test_counts_add_up_and_inputs_are_not_produced(self):
        s = artifacts.summary(build_skeleton(
            [_a(1, "read_file", "/in.py"),
             _a(2, "create_file", "/out.json"),
             _a(3, "create_file", "/out.json")], session_id="s"))
        self.assertEqual(s["entities"], sum(s["counts"].values()))
        self.assertEqual(s["counts"][INPUT], 1)
        self.assertEqual(s["produced"], 2)
        self.assertEqual(s["written_and_never_read"], 1)

    def test_rewrite_champion_names_the_most_rewritten_file(self):
        s = artifacts.summary(build_skeleton(
            [_a(1, "create_file", "/a.py"), _a(2, "create_file", "/a.py"),
             _a(3, "create_file", "/a.py"), _a(4, "create_file", "/b.py")],
            session_id="s"))
        self.assertEqual(s["rewrite_champion"], ("/a.py", 3))


if __name__ == "__main__":
    unittest.main()
