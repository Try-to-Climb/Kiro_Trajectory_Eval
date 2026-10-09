"""Task 3 tests: usedBy edges and version-chain connectivity.

Reuses the real ctrl.py interleaving from test_build.py (6 reads, 3
modifies) and adds what Task 3 makes possible: a walkable chain from the final
artifact back to the state the run started from.

Run:  cd evalkit && python3 -m unittest graph.tests.test_used_by -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph.build import build_skeleton                    # noqa: E402
from graph.schema import (                                # noqa: E402
    CERTAIN,
    GENERATED_BY,
    USED_BY,
    ProvGraph,
)
from graph.tests.test_build import CTRL, _REAL_SEQUENCE, _a  # noqa: E402


def _reverse_reachable(g: ProvGraph, start: str) -> set[str]:
    """Nodes from which `start` can be reached, i.e. its provenance."""
    rev: dict[str, list[str]] = {}
    for e in g.edges:
        rev.setdefault(e.dst, []).append(e.src)
    seen, frontier = {start}, [start]
    while frontier:
        for p in rev.get(frontier.pop(), []):
            if p not in seen:
                seen.add(p)
                frontier.append(p)
    return seen


class TestReadsConsumeCurrentVersion(unittest.TestCase):
    """A read attaches to whichever version was current at its idx."""

    def setUp(self):
        self.g = build_skeleton(_REAL_SEQUENCE, session_id="a1b2c3d4-1111")

    def test_graph_is_valid(self):
        self.g.assert_bipartite()

    def test_early_reads_consume_v0(self):
        v0_consumers = {e.dst for e in self.g.out_edges(f"e:{CTRL}@v0")}
        for idx in (32, 46, 47, 51, 67):
            self.assertIn(f"a:a1b2c3d4#{idx}", v0_consumers,
                          f"read at idx {idx} should consume v0")

    def test_late_read_consumes_v2(self):
        """a[72] reads after a[71] produced v2, so it must not see v0."""
        consumers = {e.dst for e in self.g.out_edges(f"e:{CTRL}@v2")}
        self.assertIn("a:a1b2c3d4#72", consumers)
        self.assertNotIn("a:a1b2c3d4#72",
                         {e.dst for e in self.g.out_edges(f"e:{CTRL}@v0")})

    def test_every_read_has_exactly_one_incoming_version(self):
        for idx in (32, 46, 47, 51, 67, 72):
            ins = self.g.in_edges(f"a:a1b2c3d4#{idx}")
            self.assertEqual(len(ins), 1, f"read at idx {idx}")
            self.assertEqual(ins[0].kind, USED_BY)
            self.assertEqual(ins[0].confidence, CERTAIN)

    def test_no_activity_is_isolated_any_more(self):
        """Task 2 left the 6 reads isolated; Task 3 connects all of them."""
        self.assertEqual(self.g.isolated_activities(), [])


class TestModifyConsumesPriorVersion(unittest.TestCase):
    """A partial edit reads before rewriting, so it consumes the version it
    replaces. Without this the version chain is disconnected."""

    def setUp(self):
        self.g = build_skeleton(_REAL_SEQUENCE, session_id="a1b2c3d4-1111")

    def test_modify_consumes_the_version_it_replaces(self):
        pairs = {(e.src, e.dst) for e in self.g.edges if e.kind == USED_BY}
        self.assertIn((f"e:{CTRL}@v0", "a:a1b2c3d4#68"), pairs)
        self.assertIn((f"e:{CTRL}@v1", "a:a1b2c3d4#71"), pairs)
        self.assertIn((f"e:{CTRL}@v2", "a:a1b2c3d4#73"), pairs)

    def test_modify_does_not_consume_what_it_produced(self):
        """a[68] generates v1; it must consume v0, never v1."""
        pairs = {(e.src, e.dst) for e in self.g.edges if e.kind == USED_BY}
        self.assertNotIn((f"e:{CTRL}@v1", "a:a1b2c3d4#68"), pairs)

    def test_chain_is_walkable_from_final_artifact_to_v0(self):
        reachable = _reverse_reachable(self.g, f"e:{CTRL}@v3")
        for node in (f"e:{CTRL}@v0", f"e:{CTRL}@v1", f"e:{CTRL}@v2",
                     "a:a1b2c3d4#68", "a:a1b2c3d4#71", "a:a1b2c3d4#73"):
            self.assertIn(node, reachable)

    def test_reads_are_not_on_the_chain(self):
        """A read consumes a version but produces nothing, so it is a leaf --
        it cannot be part of the provenance of a later artifact."""
        reachable = _reverse_reachable(self.g, f"e:{CTRL}@v3")
        for idx in (32, 46, 47, 51, 67, 72):
            self.assertNotIn(f"a:a1b2c3d4#{idx}", reachable)

    def test_create_file_consumes_nothing(self):
        g = build_skeleton([_a(1, "create_file", "/tmp/new.sh")])
        self.assertEqual([e.kind for e in g.edges], [GENERATED_BY])

    def test_repeated_create_still_consumes_nothing(self):
        """A single create has no prior version to consume, so it cannot tell
        whether create_file is treated as consuming. Creating twice can: v1
        exists by the time the second one runs."""
        g = build_skeleton([_a(1, "create_file", "/tmp/f.sh"),
                            _a(2, "create_file", "/tmp/f.sh")])
        self.assertEqual([e.version for e in g.versions_of("/tmp/f.sh")], [1, 2])
        self.assertEqual([e.kind for e in g.edges],
                         [GENERATED_BY, GENERATED_BY])

    def test_full_overwrite_gets_v0_but_no_usedBy(self):
        """write_file replaces content without reading it, so asserting it
        consumed the prior version would be an unfounded dependency."""
        g = build_skeleton([_a(1, "write_file", "/proj/f.py")])
        self.assertEqual([e.version for e in g.versions_of("/proj/f.py")], [0, 1])
        self.assertEqual([e.kind for e in g.edges], [GENERATED_BY])


class TestVisibilityBoundary(unittest.TestCase):
    """`latest_version(before_idx=N)` is strictly-earlier, which is what keeps a
    write invisible to anything at its own idx."""

    def test_read_before_a_write_sees_the_older_version(self):
        g = build_skeleton([_a(1, "modify_file", "/f"),   # v0 -> v1
                            _a(2, "read_file", "/f"),     # sees v1
                            _a(3, "modify_file", "/f"),   # v1 -> v2
                            _a(4, "read_file", "/f")])    # sees v2
        srcs = {e.dst: e.src for e in g.edges if e.kind == USED_BY}
        self.assertEqual(srcs["a:a1b2c3d4#2"], "e:/f@v1")
        self.assertEqual(srcs["a:a1b2c3d4#4"], "e:/f@v2")

    def test_before_idx_matches_incremental_construction(self):
        """`before_idx` is belt-and-braces during a build, not load-bearing.

        The builder walks actions in idx order and adds each version as it goes,
        so when a read at idx N is processed, no version born after N exists in
        the graph yet -- `latest_version(path)` and
        `latest_version(path, before_idx=N)` return the same thing. Dropping the
        argument does not change any built graph, which is why no test can
        catch its removal.

        It still matters for anything querying a *finished* graph (Task 9), so
        this test pins the equivalence rather than the argument, and documents
        why breaking it is invisible here.
        """
        acts = [_a(1, "modify_file", "/f"), _a(2, "read_file", "/f"),
                _a(3, "modify_file", "/f"), _a(5, "modify_file", "/f")]
        g = build_skeleton(acts)
        srcs = {e.dst: e.src for e in g.edges if e.kind == USED_BY}
        self.assertEqual(srcs["a:a1b2c3d4#2"], "e:/f@v1")

        # On the finished graph the two calls now disagree, which is the case
        # `before_idx` exists for.
        self.assertEqual(g.latest_version("/f").version, 3)
        self.assertEqual(g.latest_version("/f", before_idx=2).version, 1)

    def test_a_write_does_not_consume_its_own_output(self):
        """Both sit at the same idx; strict comparison is what prevents it."""
        g = build_skeleton([_a(5, "modify_file", "/f")])
        used = [e for e in g.edges if e.kind == USED_BY]
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0].src, "e:/f@v0")          # not @v1
        g.assert_bipartite()                              # would flag a 2-cycle


class TestCountsAfterTask3(unittest.TestCase):

    def test_edge_counts_on_the_real_sequence(self):
        g = build_skeleton(_REAL_SEQUENCE)
        by_kind: dict[str, int] = {}
        for e in g.edges:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        # 3 writes generate; 6 reads plus 3 modifies consume.
        self.assertEqual(by_kind, {GENERATED_BY: 3, USED_BY: 9})

    def test_read_of_a_never_written_path_still_connects(self):
        g = build_skeleton([_a(1, "read_file", "/proj/readme.md")])
        self.assertEqual(len(g.edges), 1)
        self.assertEqual(g.edges[0].src, "e:/proj/readme.md@v0")
        self.assertEqual(g.isolated_activities(), [])

    def test_command_actions_stay_isolated(self):
        """Commands carry no path; connecting them is Task 4/5."""
        g = build_skeleton([_a(1, "read_file", "/f"),
                            _a(2, "run_command", command="ls -l")])
        iso = [a.ref for a in g.isolated_activities()]
        self.assertEqual(iso, ["a1b2c3d4#2"])


if __name__ == "__main__":
    unittest.main()
