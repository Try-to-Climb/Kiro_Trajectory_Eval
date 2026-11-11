"""Task 2 tests: generatedBy edges and entity versioning.

The fixtures are hand-written action dicts, but they replicate real sequences
measured on session `a1b2c3d4` (see DESIGN.md §3) rather than being invented:
`ctrl.py` was read 6 times and modified 3 times, and 4 of the 20 paths
were created by the run and so have no prior state.

Run:  cd evalkit && python3 -m unittest graph.tests.test_build -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph.build import build_skeleton, version_chain, writes_by_path  # noqa: E402
from graph.schema import CERTAIN, GENERATED_BY                          # noqa: E402


def _a(idx, action, path=None, turn=1, purpose=None, command=None):
    """One normalized Action dict, shaped like RunTree.actions entries."""
    return {"ref": f"a1b2c3d4#{idx}", "sid": "a1b2c3d4-1111", "idx": idx,
            "turn": turn, "action": action, "path": path,
            "purpose": purpose, "command": command}


# The real read/modify interleaving on ctrl.py: 6 reads (idx 32, 46,
# 47, 51, 67, 72) and 3 modifies (68, 71, 73).
CTRL = "/work/proj/sut/.kiro/skills/test-engine/scripts/ctrl.py"
_REAL_SEQUENCE = [
    _a(32, "read_file", CTRL, turn=7, purpose="Read the iperf binary detection logic"),
    _a(46, "read_file", CTRL, turn=7),
    _a(47, "read_file", CTRL, turn=7),
    _a(51, "read_file", CTRL, turn=7),
    _a(67, "read_file", CTRL, turn=11),
    _a(68, "modify_file", CTRL, turn=11, purpose="Fix _resolve_iperf_bin"),
    _a(71, "modify_file", CTRL, turn=11),
    _a(72, "read_file", CTRL, turn=11),
    _a(73, "modify_file", CTRL, turn=11),
]


class TestFailedToolCallsEmitNothing(unittest.TestCase):
    """`modify_file` with a non-matching old_str reports an error and leaves the
    file untouched. It used to earn a generatedBy edge for a version that was
    never written, plus a usedBy edge for the version it supposedly replaced."""

    P = "/work/x.py"

    def _seq(self, error):
        first = _a(1, "modify_file", self.P)
        second = _a(2, "modify_file", self.P)
        second["error"] = error
        return [first, second]

    def test_failed_edit_creates_no_version(self):
        g = build_skeleton(self._seq('{"Custom": "old_str was not found"}'),
                           session_id="s")
        versions = {e.version for e in g.entities.values() if e.key == self.P}
        self.assertEqual(versions, {0, 1})       # v0 prior state, v1 from idx 1

    def test_failed_edit_creates_no_edges(self):
        g = build_skeleton(self._seq('{"Custom": "old_str was not found"}'),
                           session_id="s")
        touching = [e for e in g.edges if e.src.endswith("#2") or e.dst.endswith("#2")]
        self.assertEqual(touching, [])

    def test_the_failure_is_reported_not_silent(self):
        g = build_skeleton(self._seq('{"Custom": "old_str was not found"}'),
                           session_id="s")
        self.assertTrue(any("failed" in w and "#2" in w for w in g.warnings))

    def test_a_successful_edit_is_unaffected(self):
        g = build_skeleton(self._seq(None), session_id="s")
        versions = {e.version for e in g.entities.values() if e.key == self.P}
        self.assertEqual(versions, {0, 1, 2})


class TestRealSequence(unittest.TestCase):
    """Reproduces the chain DESIGN.md §2.2 predicts for ctrl.py."""

    def setUp(self):
        self.g = build_skeleton(_REAL_SEQUENCE, session_id="a1b2c3d4-1111")

    def test_graph_is_valid(self):
        self.g.assert_bipartite()

    def test_four_entity_versions(self):
        self.assertEqual([e.version for e in self.g.versions_of(CTRL)], [0, 1, 2, 3])

    def test_each_modify_produces_the_next_version(self):
        producers = {e.version: e.producer_ref for e in self.g.versions_of(CTRL)}
        self.assertIsNone(producers[0])                        # pre-existing
        self.assertEqual(producers[1], "a1b2c3d4#68")
        self.assertEqual(producers[2], "a1b2c3d4#71")
        self.assertEqual(producers[3], "a1b2c3d4#73")

    def test_one_generated_by_edge_per_modify(self):
        edges = self.g.filter_edges(CERTAIN, kinds=(GENERATED_BY,))
        self.assertEqual(len(edges), 3)
        self.assertEqual(
            sorted((e.src, e.dst) for e in edges),
            [("a:a1b2c3d4#68", f"e:{CTRL}@v1"),
             ("a:a1b2c3d4#71", f"e:{CTRL}@v2"),
             ("a:a1b2c3d4#73", f"e:{CTRL}@v3")])

    def test_born_idx_follows_the_producing_activity(self):
        by_ver = {e.version: e.born_idx for e in self.g.versions_of(CTRL)}
        self.assertEqual(by_ver, {0: -1, 1: 68, 2: 71, 3: 73})

    def test_generated_by_count_is_write_count(self):
        """This file owns the generatedBy half; usedBy is test_used_by.py."""
        self.assertEqual(len(self.g.filter_edges(CERTAIN, kinds=(GENERATED_BY,))), 3)

    def test_every_action_becomes_an_activity(self):
        """Including the reads, so isolated_activities() means something."""
        self.assertEqual(self.g.n_activities, 9)


class TestPriorStateAsymmetry(unittest.TestCase):
    """create_file gets no v0; modify_file does. See build.py's docstring."""

    def test_create_file_starts_at_v1(self):
        g = build_skeleton([_a(3, "create_file", "/tmp/new.sh")])
        self.assertEqual([e.version for e in g.versions_of("/tmp/new.sh")], [1])

    def test_modify_file_synthesises_v0(self):
        g = build_skeleton([_a(3, "modify_file", "/proj/existing.py")])
        vs = g.versions_of("/proj/existing.py")
        self.assertEqual([e.version for e in vs], [0, 1])
        self.assertIsNone(vs[0].producer_ref)
        self.assertEqual(vs[0].born_idx, -1)

    def test_read_only_path_gets_v0_and_no_new_version(self):
        g = build_skeleton([_a(3, "read_file", "/proj/readme.md")])
        self.assertEqual([e.version for e in g.versions_of("/proj/readme.md")], [0])
        self.assertEqual(g.filter_edges(CERTAIN, kinds=(GENERATED_BY,)), [])

    def test_create_then_modify_does_not_backfill_v0(self):
        """The run created it, so there is no prior state to invent."""
        g = build_skeleton([_a(1, "create_file", "/tmp/f.sh"),
                            _a(2, "modify_file", "/tmp/f.sh")])
        self.assertEqual([e.version for e in g.versions_of("/tmp/f.sh")], [1, 2])


class TestOrderingAndCounts(unittest.TestCase):

    def test_versions_follow_idx_not_input_order(self):
        shuffled = [_a(9, "modify_file", "/f"), _a(1, "modify_file", "/f"),
                    _a(5, "modify_file", "/f")]
        g = build_skeleton(shuffled)
        by_ver = {e.version: e.born_idx for e in g.versions_of("/f")}
        self.assertEqual(by_ver, {0: -1, 1: 1, 2: 5, 3: 9})

    def test_edge_count_equals_write_count(self):
        acts = [_a(1, "modify_file", "/a"), _a(2, "create_file", "/b"),
                _a(3, "read_file", "/c"), _a(4, "run_command", command="ls")]
        g = build_skeleton(acts)
        self.assertEqual(len(g.filter_edges(CERTAIN, kinds=(GENERATED_BY,))), 2)
        self.assertEqual(g.n_activities, 4)

    def test_entity_count_splits_into_v0_and_produced(self):
        acts = [_a(1, "modify_file", "/a"), _a(2, "create_file", "/b"),
                _a(3, "read_file", "/c")]
        g = build_skeleton(acts)
        n_v0 = sum(1 for e in g.entities.values() if e.version == 0)
        n_new = sum(1 for e in g.entities.values() if e.version > 0)
        self.assertEqual(n_v0, 2)                             # /a and /c
        self.assertEqual(n_new, 2)                            # /a@v1 and /b@v1
        self.assertEqual(n_new,
                         len(g.filter_edges(CERTAIN, kinds=(GENERATED_BY,))))

    def test_writes_by_path_reports_zero_for_read_only(self):
        g = build_skeleton([_a(1, "modify_file", "/a"), _a(2, "modify_file", "/a"),
                            _a(3, "read_file", "/b")])
        self.assertEqual(writes_by_path(g), {"/a": 2, "/b": 0})


class TestDegenerateInput(unittest.TestCase):

    def test_empty_input(self):
        g = build_skeleton([])
        self.assertEqual(len(g), 0)
        self.assertEqual(g.stats()["isolated_pct"], 0.0)

    def test_write_without_a_path_is_warned_not_dropped(self):
        g = build_skeleton([_a(1, "modify_file", None)])
        self.assertEqual(g.n_activities, 1)                   # activity still there
        self.assertEqual(g.n_entities, 0)
        self.assertEqual(len(g.warnings), 1)
        self.assertIn("without a path", g.warnings[0])

    def test_ref_is_reconstructed_when_missing(self):
        """Older IR pickles predate Action.sid/ref."""
        g = build_skeleton([{"sid": "abcd1234-9999", "idx": 4,
                             "turn": 1, "action": "read_file", "path": "/f"}])
        self.assertIn("a:abcd1234#4", g.activities)

    def test_purpose_falls_back_to_tool_use_purpose(self):
        g = build_skeleton([{"ref": "s#0", "idx": 0, "turn": 1,
                             "action": "read_file", "path": "/f",
                             "args": {"__tool_use_purpose": "Read the config"}}])
        self.assertEqual(g.activities["a:s#0"].purpose, "Read the config")


class TestVersionChainRendering(unittest.TestCase):

    def test_chain_names_each_producer(self):
        g = build_skeleton(_REAL_SEQUENCE, session_id="a1b2c3d4-1111")
        chain = version_chain(g, CTRL)
        self.assertEqual(len(chain), 4)
        self.assertIn("pre-existing", chain[0])
        for step, ref in zip(chain[1:], ["#68", "#71", "#73"]):
            self.assertIn(ref, step)
            self.assertIn("modify_file", step)


class TestAProbeIsRecordedWithoutAnEdge(unittest.TestCase):
    """`grep`, `find`, `ls`, LSP symbol queries look at a target without
    necessarily touching it. The target is still worth showing on the card so
    a reader can see what was asked, but the edge is withheld: a probe that
    misses touches nothing, and claiming otherwise fabricates reads a judge
    has already called out twice on the ExampleCorpus corpus.
    """

    def test_search_content_fills_pattern_and_path(self):
        act = _a(1, "search_content", turn=1)
        act["args"] = {"pattern": "iperf|downlink",
                       "path": "/proj/x/engines/scenario.py"}
        g = build_skeleton([act])
        activity = g.activities["a:a1b2c3d4#1"]
        self.assertEqual(activity.probe,
                         {"pattern": "iperf|downlink",
                          "path": "/proj/x/engines/scenario.py"})

    def test_list_dir_fills_path_only(self):
        act = _a(2, "list_dir", turn=1)
        act["args"] = {"path": "/proj/x/engines"}
        g = build_skeleton([act])
        self.assertEqual(g.activities["a:a1b2c3d4#2"].probe,
                         {"path": "/proj/x/engines"})

    def test_code_search_symbols_fills_pattern_only(self):
        act = _a(3, "code_search_symbols", turn=1)
        act["args"] = {"pattern": "_step_iperf"}
        g = build_skeleton([act])
        self.assertEqual(g.activities["a:a1b2c3d4#3"].probe,
                         {"pattern": "_step_iperf"})

    def test_path_and_root_dedup_when_equal(self):
        """normalize already copies `args.path` into `root` for grep, so a
        probe held both and the card would have shown the same path twice."""
        act = _a(4, "search_content", turn=1)
        act["args"] = {"pattern": "x", "path": "/p/a.py"}
        act["root"] = "/p/a.py"
        g = build_skeleton([act])
        self.assertEqual(g.activities["a:a1b2c3d4#4"].probe,
                         {"pattern": "x", "path": "/p/a.py"})

    def test_path_and_root_both_kept_when_different(self):
        act = _a(5, "search_content", turn=1)
        act["args"] = {"pattern": "x", "path": "/p/a.py", "root": "/p"}
        g = build_skeleton([act])
        self.assertEqual(g.activities["a:a1b2c3d4#5"].probe,
                         {"pattern": "x", "path": "/p/a.py", "root": "/p"})

    def test_a_non_probe_action_has_no_probe(self):
        g = build_skeleton([_a(6, "read_file", "/p/a.py", turn=1)])
        self.assertIsNone(g.activities["a:a1b2c3d4#6"].probe)

    def test_probe_produces_no_edges(self):
        """The point of the whole exercise: display information, not links.
        A `search_content` on an existing file still leaves the activity
        isolated."""
        acts = [_a(1, "read_file", "/p/a.py", turn=1)]
        probe = _a(2, "search_content", turn=1)
        probe["args"] = {"pattern": "x", "path": "/p/a.py"}
        acts.append(probe)
        g = build_skeleton(acts)
        probe_id = "a:a1b2c3d4#2"
        self.assertFalse(
            any(e.src == probe_id or e.dst == probe_id for e in g.edges),
            "probe activity should have no edges")


if __name__ == "__main__":
    unittest.main()
