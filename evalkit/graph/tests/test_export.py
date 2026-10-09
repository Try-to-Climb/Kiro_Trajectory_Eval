"""Task 6 tests: selection and the three output formats.

Selection carries the judgement (which turns, which confidence, what to do with
an entity produced outside the window); the emitters are templating. The tests
are weighted accordingly, plus escaping, because one unescaped quote in one
`purpose` breaks an entire diagram.

Run:  cd evalkit && python3 -m unittest graph.tests.test_export -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import export                                          # noqa: E402
from graph.build import build_skeleton                             # noqa: E402
from graph.schema import CERTAIN, LIKELY                           # noqa: E402

SID = "a1b2c3d4-1111"


def _read(idx, path, turn=1, purpose=None):
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "read_file", "path": path,
            "purpose": purpose or f"read {path}"}


def _create(idx, path, turn=1, purpose=None):
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "create_file", "path": path,
            "purpose": purpose or f"create {path}"}


def _cmd(idx, command, turn=1, stdout="", purpose=None):
    import json
    resp = json.dumps({"Success": {"items": [{"Json": {"stdout": stdout}}]}})
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "run_command", "command": command, "response": resp,
            "purpose": purpose or "run a command",
            "args": {"working_dir": "/work"}}


class TestSelection(unittest.TestCase):

    def setUp(self):
        self.g = build_skeleton([
            _create(1, "/work/suite.yaml", turn=1),
            _cmd(2, "python3 /work/run.py /work/suite.yaml", turn=2),
            _read(3, "/work/out.log", turn=2),
            _cmd(4, "true", turn=3),                 # nothing to extract
        ], session_id=SID)

    def test_no_filter_keeps_everything(self):
        v = export.select(self.g)
        self.assertEqual([a.idx for a in v.activities], [1, 2, 3, 4])
        self.assertIsNone(v.turns)

    def test_turn_filter_keeps_only_that_turn(self):
        v = export.select(self.g, turns=[2])
        self.assertEqual([a.idx for a in v.activities], [2, 3])
        self.assertEqual(v.turns, (2,))

    def test_entity_produced_earlier_is_kept_and_marked(self):
        """The point of `outside`: turn 2 consuming turn 1's output is exactly
        the kind of dependency worth seeing, so the entity stays even though
        its producer is filtered out."""
        v = export.select(self.g, turns=[2])
        keys = {e.key for e in v.entities}
        self.assertIn("/work/suite.yaml", keys)
        outside = {self.g.entities[n].key for n in v.outside}
        self.assertEqual(outside, {"/work/suite.yaml"})

    def test_entity_produced_inside_the_window_is_not_outside(self):
        v = export.select(self.g, turns=[1])
        self.assertEqual(v.outside, set())

    def test_confidence_filter_drops_weaker_edges(self):
        v = export.select(self.g, min_confidence=CERTAIN)
        self.assertTrue(all(e.confidence == CERTAIN for e in v.edges))
        self.assertTrue(any(e.confidence == LIKELY for e in self.g.edges))

    def test_isolated_activities_are_shown_by_default(self):
        """An action nothing links to is information, not noise."""
        v = export.select(self.g)
        self.assertIn(4, [a.idx for a in v.activities])
        self.assertEqual(v.hidden_isolated, 0)

    def test_hide_isolated_removes_them_and_counts_them(self):
        v = export.select(self.g, hide_isolated=True)
        self.assertNotIn(4, [a.idx for a in v.activities])
        self.assertEqual(v.hidden_isolated, 1)

    def test_no_edge_is_left_with_a_missing_endpoint(self):
        """Every rendered edge must have both nodes present, or the output is
        not a valid graph in any format."""
        for turns in ([1], [2], [3], [1, 2], None):
            v = export.select(self.g, turns=turns)
            ids = ({a.node_id for a in v.activities}
                   | {e.node_id for e in v.entities})
            for e in v.edges:
                missing = {e.src, e.dst} - ids
                # An edge may reach an activity outside the window; that is the
                # one allowed absence and it must be an activity, never an
                # entity, or a version chain would be rendered half-present.
                for m in missing:
                    self.assertTrue(m.startswith("a:"), f"{m} in turns={turns}")


class TestMermaid(unittest.TestCase):

    def test_shape_distinguishes_activity_entity_and_pid(self):
        g = build_skeleton([
            _cmd(1, "nohup python3 /work/run.py & echo PID=$!",
                 stdout="PID=3304381\n"),
        ], session_id=SID)
        out = export.to_mermaid(g)
        self.assertIn('["a[1] ', out)              # activity: box
        self.assertIn("(run.py)", out)             # file: rounded
        self.assertIn("([PID 3304381])", out)      # pid: stadium

    def test_confidence_distinguishes_the_arrow(self):
        g = build_skeleton([
            _create(1, "/work/a.txt"),
            _cmd(2, "cat /work/b.txt"),
        ], session_id=SID)
        out = export.to_mermaid(g)
        self.assertIn(" --> ", out)                # certain, from path field
        self.assertIn(" -.-> ", out)               # likely, from command text

    def test_quotes_and_brackets_in_a_purpose_are_escaped(self):
        r"""One unescaped `"` ends the label and the whole diagram fails to
        parse, so this is correctness rather than presentation."""
        g = build_skeleton([
            _read(1, "/work/a.txt", purpose='Check "foo" [bar] (baz) <x> a|b;'),
        ], session_id=SID)
        out = export.to_mermaid(g)
        label = [l for l in out.split("\n") if "a[1]" in l][0]
        # The `a[1] ` prefix is emitted by us and safe; check only the purpose.
        purpose = label.split('["', 1)[1].rsplit('"]', 1)[0].split("] ", 1)[1]
        for ch in '"[]()<>|':
            self.assertNotIn(ch, purpose)

    def test_ampersand_is_escaped_once(self):
        g = build_skeleton([_read(1, "/work/a.txt", purpose="a & b")],
                           session_id=SID)
        out = export.to_mermaid(g)
        self.assertIn("a #amp; b", out)
        self.assertNotIn("#35;amp;", out)   # the introducer must not re-escape

    def test_newlines_do_not_split_a_statement(self):
        g = build_skeleton([_read(1, "/work/a.txt", purpose="line one\nline two")],
                           session_id=SID)
        out = export.to_mermaid(g)
        self.assertIn("line one line two", out)
        self.assertEqual(len([l for l in out.split("\n") if "a[1]" in l]), 1)

    def test_node_ids_are_syntax_safe_and_unique(self):
        g = build_skeleton([
            _read(1, "/work/deep/nested/path.with.dots/file-name.txt"),
            _read(2, "/work/other.txt"),
        ], session_id=SID)
        out = export.to_mermaid(g)
        decls = [l.strip().split("[")[0].split("(")[0]
                 for l in out.split("\n")[1:]
                 if l.strip() and "-->" not in l and "-.->" not in l]
        for d in decls:
            self.assertTrue(d.replace("_", "").isalnum(), d)
        self.assertEqual(len(decls), len(set(decls)))

    def test_direction_is_honoured(self):
        g = build_skeleton([_read(1, "/work/a.txt")], session_id=SID)
        self.assertTrue(export.to_mermaid(g, direction="TD").startswith("graph TD"))

    def test_hidden_isolated_count_is_stated_not_silent(self):
        g = build_skeleton([_cmd(1, "true")], session_id=SID)
        self.assertIn("1 isolated activities hidden",
                      export.to_mermaid(g, hide_isolated=True))


class TestDot(unittest.TestCase):
    """Present to keep the formatter boundary honest: a second syntax reusing
    `select` unchanged is what makes the structure claim true."""

    def test_reuses_selection(self):
        g = build_skeleton([
            _create(1, "/work/suite.yaml", turn=1),
            _cmd(2, "python3 /work/run.py /work/suite.yaml", turn=2),
        ], session_id=SID)
        out = export.to_dot(g, turns=[2])
        self.assertIn("digraph prov", out)
        self.assertIn("a[2]", out)
        self.assertNotIn("a[1]", out)

    def test_quotes_are_escaped(self):
        g = build_skeleton([_read(1, "/work/a.txt", purpose='say "hi"')],
                           session_id=SID)
        line = [l for l in export.to_dot(g).split("\n") if "a[1]" in l][0]
        self.assertIn(r'\"hi\"', line)

    def test_confidence_becomes_line_style(self):
        g = build_skeleton([
            _create(1, "/work/a.txt"),
            _cmd(2, "cat /work/b.txt"),
        ], session_id=SID)
        out = export.to_dot(g)
        self.assertIn("style=solid", out)
        self.assertIn("style=dashed", out)


class TestText(unittest.TestCase):

    def test_reads_before_writes(self):
        """So each line reads as what the action did: consumed these, then
        produced those."""
        g = build_skeleton([
            _cmd(1, "python3 /work/run.py > /work/out.log"),
        ], session_id=SID)
        body = export.to_text(g)
        self.assertLess(body.index("← run.py"), body.index("→ out.log"))

    def test_isolated_is_labelled(self):
        g = build_skeleton([_cmd(1, "true")], session_id=SID)
        self.assertIn("(isolated)", export.to_text(g))

    def test_outside_entities_are_marked(self):
        g = build_skeleton([
            _create(1, "/work/suite.yaml", turn=1),
            _cmd(2, "python3 /work/run.py /work/suite.yaml", turn=2),
        ], session_id=SID)
        self.assertIn("·outside", export.to_text(g, turns=[2]))

    def test_header_states_scope_and_counts(self):
        g = build_skeleton([_read(1, "/work/a.txt", turn=3)], session_id=SID)
        head = export.to_text(g, turns=[3]).split("\n")[0]
        self.assertIn("turn 3", head)
        self.assertIn("1 activities", head)
        self.assertIn(SID, head)

    def test_all_turns_is_stated_explicitly(self):
        g = build_skeleton([_read(1, "/work/a.txt")], session_id=SID)
        self.assertIn("all turns", export.to_text(g).split("\n")[0])


class TestEmptyGraph(unittest.TestCase):

    def test_every_format_survives_an_empty_graph(self):
        g = build_skeleton([], session_id=SID)
        self.assertEqual(export.to_mermaid(g).strip(), "graph LR")
        self.assertIn("digraph prov", export.to_dot(g))
        self.assertIn("0 activities", export.to_text(g))

    def test_a_turn_with_nothing_in_it(self):
        g = build_skeleton([_read(1, "/work/a.txt", turn=1)], session_id=SID)
        v = export.select(g, turns=[99])
        self.assertEqual(v.activities, [])
        self.assertEqual(v.edges, [])


if __name__ == "__main__":
    unittest.main()
