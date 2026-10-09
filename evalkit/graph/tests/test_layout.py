"""Positions from graphviz, drawing left to us.

The bezier point count is the load-bearing assertion here. A malformed `d`
attribute still renders -- the curve is simply wrong -- so nothing about the page
looks broken when it is, and there is no javascript engine on this machine to
catch it by running the thing.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import layout                                      # noqa: E402
from graph.build import build_skeleton                        # noqa: E402
from graph.export import to_dot                               # noqa: E402

needs_dot = unittest.skipUnless(shutil.which("dot") is not None,
                          "graphviz not installed")


def _a(idx, action, path=None, command=None):
    return {"ref": f"s#{idx}", "sid": "s", "idx": idx, "turn": 1,
            "action": action, "path": path, "command": command}


ACTS = [_a(1, "create_file", "/work/a.py"),
        _a(2, "run_command", command="python3 /work/a.py > /work/b.log"),
        _a(3, "read_file", "/work/b.log")]


@needs_dot
class TestGeometry(unittest.TestCase):

    def setUp(self):
        g = build_skeleton(ACTS, session_id="s")
        self.plain = layout.layout(to_dot(g))
        self.tl = layout.layout(to_dot(g, timeline=True))

    def test_positions_are_inside_the_canvas(self):
        """y is flipped from graphviz's bottom-left origin; getting that wrong
        puts half the graph off-screen."""
        for n in self.plain["nodes"]:
            self.assertGreaterEqual(n["x"], 0)
            self.assertGreaterEqual(n["y"], 0)
            self.assertLessEqual(n["x"], self.plain["width"])
            self.assertLessEqual(n["y"], self.plain["height"])

    def test_the_invisible_ordering_spine_is_not_drawn(self):
        """It exists to pin the layout, and an arrow between consecutive actions
        would be a claim the graph does not make."""
        pairs = {(e["src"], e["dst"]) for e in self.tl["edges"]}
        self.assertNotIn(("a:s#1", "a:s#2"), pairs)
        self.assertNotIn(("a:s#2", "a:s#3"), pairs)

    def test_timeline_orders_activities_down_the_page(self):
        acts = sorted((n for n in self.tl["nodes"] if n["id"].startswith("a:")),
                      key=lambda n: n["y"])
        self.assertEqual([n["id"] for n in acts],
                         ["a:s#1", "a:s#2", "a:s#3"])

    def test_edge_endpoints_are_nodes_that_exist(self):
        ids = {n["id"] for n in self.plain["nodes"]}
        for e in self.plain["edges"]:
            self.assertIn(e["src"], ids)
            self.assertIn(e["dst"], ids)

    def test_an_edge_reaches_from_one_node_to_the_other(self):
        """`pos` carries an arrow stub whose end depends on the arrow direction:
        `e,` for the head, `s,` for the tail -- and `dir=back`, which timeline mode
        puts on every read, produces the `s,` form. Treating both alike overwrote
        the real endpoint with the start stub, so every read curved out towards
        its action and then came back to land three pixels from its own file.

        Checked by containment rather than by which centre is nearer: an endpoint
        sits on the node's outline, and for a 209px-wide card that outline can be
        closer to the other node's centre than to its own.
        """
        for geo in (self.plain, self.tl):
            box = {n["id"]: n for n in geo["nodes"]}
            for e in geo["edges"]:
                for point, node_id, which in ((e["points"][0], e["src"], "start"),
                                              (e["points"][-1], e["dst"], "end")):
                    n = box[node_id]
                    # A generous margin: graphviz leaves a gap for the arrowhead.
                    dx = abs(point[0] - n["x"]) - n["w"] / 2
                    dy = abs(point[1] - n["y"]) - n["h"] / 2
                    self.assertLess(min(dx, dy), 30,
                                    f"{e['src']} -> {e['dst']}: {which} is "
                                    f"{dx:.0f},{dy:.0f} outside {node_id}")

    def test_an_edge_is_just_its_two_ends(self):
        """The bezier chain is dropped: once a node can be dragged the curve is
        recomputed from positions, and keeping two ways to draw one edge made the
        picture change shape the moment anything moved."""
        for e in self.plain["edges"]:
            self.assertEqual(len(e["points"]), 2)


class TestMissingGraphviz(unittest.TestCase):

    def test_it_says_what_to_install(self):
        if shutil.which("dot"):
            self.skipTest("graphviz is installed")
        with self.assertRaises(layout.LayoutError) as cm:
            layout.layout("digraph {}")
        self.assertIn("graphviz", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
