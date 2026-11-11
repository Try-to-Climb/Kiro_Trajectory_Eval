"""Task 6b tests: the interactive HTML view.

The value of this format is the detail a PNG cannot carry -- the full purpose,
the command that actually ran, and the evidence string behind every edge -- so
the tests check that this detail is present and correctly escaped, and that the
file depends on nothing external.

Run:  cd evalkit && python3 -m unittest graph.tests.test_html -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import html as html_mod                                 # noqa: E402
from graph.build import build_skeleton                              # noqa: E402
from graph.schema import CERTAIN                                    # noqa: E402

SID = "a1b2c3d4-1111"
HAVE_DOT = shutil.which("dot") is not None
needs_dot = unittest.skipUnless(HAVE_DOT, "graphviz not installed; layout unavailable")


def _cmd(idx, command, turn=1, stdout="", purpose=None):
    resp = json.dumps({"Success": {"items": [{"Json": {"stdout": stdout}}]}})
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "run_command", "command": command, "response": resp,
            "purpose": purpose or "run something",
            "args": {"working_dir": "/work"}}


def _modify(idx, path, turn=1, purpose=None, reasoning=None):
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "modify_file", "path": path,
            "purpose": purpose or f"edit {path}", "reasoning": reasoning}


def _payload(page):
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                  page, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


class TestCamera(unittest.TestCase):
    """Zoom and pan. Before this the svg was sized to the drawing and #graph
    scrolled, so a 1300x1500 picture could only be reached with scrollbars and
    never seen whole -- which reads as a bad drawing before styling enters into
    it."""

    ACTS = [_modify(1, "/work/x.py"), _cmd(2, "cat /work/x.py")]

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))

    @needs_dot
    def test_the_svg_fills_the_pane_rather_than_the_drawing(self):
        """Sizing it to the drawing is what made the pane scroll. The markers
        keep their own viewBox, so the check is on the svg element itself."""
        self.assertIn('width="100%" height="100%"', self.page)
        svg_tag = self.page[self.page.index("<svg id="):]
        svg_tag = svg_tag[:svg_tag.index(">")]
        self.assertNotIn("viewBox", svg_tag)

    @needs_dot
    def test_a_camera_layer_wraps_the_drawing(self):
        """Everything moves together, so zoom is one transform rather than a
        recompute of every coordinate."""
        self.assertIn('id="view"', self.page)
        self.assertIn("viewG.setAttribute", self.page)

    @needs_dot
    def test_the_view_is_fitted_on_load_and_on_resize(self):
        self.assertIn("fitView();", self.page)
        self.assertIn('addEventListener("resize", fitView)', self.page)

    @needs_dot
    def test_wheel_zooms_about_the_cursor(self):
        """Zooming about the centre instead makes the thing you were looking at
        slide away."""
        self.assertIn('addEventListener("wheel"', self.page)
        self.assertIn("cam.x = mx - before.x * cam.k", self.page)

    @needs_dot
    def test_the_pane_no_longer_scrolls(self):
        self.assertIn("overflow:hidden", self.page)

    @needs_dot
    def test_layout_can_be_switched_and_switched_back(self):
        """Force gives the organic arrangement; layered is the only one that
        guarantees idx order, so returning has to restore exact positions."""
        self.assertIn('id="mbtn"', self.page)
        self.assertIn("layout: layered", self.page)
        self.assertIn("p.x = h.x; p.y = h.y;", self.page)

    @needs_dot
    def test_the_force_pass_keeps_nodes_apart(self):
        """Without collision the labels sat on top of one another as soon as the
        springs pulled nodes together."""
        self.assertIn("overlap > 0", self.page)


class TestTimelineLayout(unittest.TestCase):
    """For reading one turn. Without it the vertical order is whatever minimised
    edge crossings -- measured on turn 6 of the reference session, top to bottom
    came out as 21, 26, 18, 16, 14, 13, 10, 9, … which says nothing about what
    the agent did."""

    ACTS = [_cmd(9, "cat /work/a.log"), _modify(10, "/work/b.py"),
            _cmd(11, "python3 /work/b.py > /work/c.log")]

    def _dot(self, **kw):
        from graph.export import to_dot
        return to_dot(build_skeleton(self.ACTS, session_id=SID), **kw)

    def test_activities_are_pinned_in_idx_order(self):
        dot = self._dot(timeline=True)
        spine = re.findall(r'"a:a1b2c3d4#(\d+)" -> "a:a1b2c3d4#(\d+)" '
                           r'\[style=invis', dot)
        self.assertEqual(spine, [("9", "10"), ("10", "11")])

    def test_the_spine_is_invisible_and_outweighs_the_rest(self):
        dot = self._dot(timeline=True)
        self.assertIn("style=invis, weight=100", dot)

    def test_it_lays_out_top_to_bottom(self):
        self.assertIn("rankdir=TB", self._dot(timeline=True))

    def test_reads_point_from_the_action_without_moving_the_file(self):
        """`dir=back` flips only the arrowhead. Writing the edge the other way
        round would tell dot the file came into being after being read."""
        dot = self._dot(timeline=True)
        read_edges = [l for l in dot.splitlines()
                      if " -> " in l and "dir=back" in l]
        self.assertTrue(read_edges)
        for line in read_edges:
            self.assertRegex(line.strip(), r'^"e:')      # still entity -> activity

    def test_the_default_layout_is_left_untouched(self):
        dot = self._dot()
        self.assertNotIn("style=invis", dot)
        self.assertNotIn("dir=back", dot)
        self.assertIn("rankdir=LR", dot)


class TestEdgeColourAndLegend(unittest.TestCase):
    """Colour carries direction, dashes carry confidence. Before this, colour was
    unused in the graph and the panel claimed it meant confidence -- a legend
    describing something the picture did not do."""

    ACTS = [_modify(1, "/work/x.py"), _cmd(2, "cat /work/x.py")]

    def test_dot_colours_reads_and_writes_differently(self):
        from graph.export import EDGE_COLOURS, to_dot
        from graph.schema import GENERATED_BY, USED_BY
        dot = to_dot(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn(EDGE_COLOURS[GENERATED_BY], dot)
        self.assertIn(EDGE_COLOURS[USED_BY], dot)
        self.assertNotEqual(EDGE_COLOURS[GENERATED_BY], EDGE_COLOURS[USED_BY])

    def test_dashes_still_carry_confidence(self):
        """A path field is certain, a parsed command is not; that distinction has
        to survive colour being used for something else."""
        from graph.export import to_dot
        dot = to_dot(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn("style=solid", dot)      # Action.path -> certain
        self.assertIn("style=dashed", dot)     # parsed command -> likely

    @needs_dot
    def test_the_page_carries_a_legend_using_those_same_colours(self):
        from graph.export import EDGE_COLOURS
        from graph.schema import GENERATED_BY, USED_BY
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn('id="legend"', page)
        self.assertIn(EDGE_COLOURS[USED_BY], page)
        self.assertIn(EDGE_COLOURS[GENERATED_BY], page)

    @needs_dot
    def test_the_legend_only_explains_what_the_picture_shows(self):
        """A row for the parsed/declared distinction, on a view where every edge
        is parsed, reads as "everything here is uncertain". It appears only when
        both kinds are present."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        # Built at render time from the view's own confidences, so the check is
        # on the data the page carries rather than on baked markup.
        import json as _json, re as _re
        views = _json.loads(_re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            page, _re.S).group(1).replace("<\\/", "</"))
        self.assertIn("certain", views[0]["confidences"])
        self.assertIn("likely", views[0]["confidences"])

        only_parsed = [_cmd(1, "cat /work/a.log"), _cmd(2, "cat /work/b.log")]
        page2 = html_mod.to_html(build_skeleton(only_parsed, session_id=SID))
        v2 = _json.loads(_re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            page2, _re.S).group(1).replace("<\\/", "</"))
        self.assertEqual(v2[0]["confidences"], ["likely"])

    @needs_dot
    def test_a_background_process_is_on_the_picture(self):
        """A review round removed PID nodes entirely -- six diamonds labelled with
        seven-digit numbers were said to take a column of weight for nothing. The
        numbers, yes; but removing them also removed the only sign that a step
        started something in the background. They are back, small and unlabelled."""
        acts = [_cmd(1, "nohup python3 /work/a.py & echo started")]
        acts[0]["response"] = json.dumps(
            {"Success": {"items": [{"Json": {"stdout": "PID=4242"}}]}})
        g = build_skeleton(acts, session_id=SID)
        self.assertTrue(any(e.kind == "pid" for e in g.entities.values()),
                        "fixture should produce a pid entity")
        page = html_mod.to_html(g)
        self.assertIn("e:4242@v1", _payload(page).keys())

    @needs_dot
    def test_every_arrow_leaves_the_action(self):
        """Colour carries the direction of the data. Letting the arrowhead carry
        it as well put arrows into and out of the same card, saying nothing the
        colour had not said."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn("const from = write ? e.src : e.dst;", page)

    @needs_dot
    def test_consecutive_actions_are_joined(self):
        """Execution order is a fact the graph does not carry as an edge -- only
        as idx -- so it is drawn as its own quiet layer, dashed and behind
        everything, and the legend says it is order rather than data."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn('id="seq"', page)
        self.assertIn("seq-line", page)
        self.assertIn("execution order, not data", page)

    @needs_dot
    def test_the_legend_can_be_reopened_after_collapsing(self):
        """Collapsing hid the body only; the header and its button sit outside it,
        so there is always something left to click."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn("#legend.off #lgbody {{ display:none; }}".replace("{{", "{").replace("}}", "}"), page)
        self.assertIn('id="lbtn"', page)
        hdr = page.index('class="lghdr"')
        body = page.index('id="lgbody"')
        self.assertLess(hdr, body, "the header must not be inside the folded body")

    @needs_dot
    def test_action_cards_do_not_move(self):
        """They carry the run's order down the page."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertIn('=== "act" || KINDS[n.id] === "turn") continue;', page)

    @needs_dot
    def test_every_read_is_drawn(self):
        """A review round asked for repeated reads of one file to be folded into
        the panel, on the grounds that six near-parallel arcs say one thing six
        times. Seen on screen it read as a lost edge: `run.py` feeding six
        relaunches became `run.py` feeding one, and nothing in the picture said
        otherwise. Drawing all of them is the honest option."""
        page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        self.assertNotIn("firstRead", page)
        self.assertNotIn("also read by", page)


class TestFateAndIsolationInThePanel(unittest.TestCase):
    """Two questions a reader asks first, answered from data the graph already
    had: what became of this file, and why is this action connected to nothing.
    The payload is built without graphviz, so these run everywhere."""

    def _payload_for(self, actions):
        from graph.html import _node_payload
        from graph.export import select
        g = build_skeleton(actions, session_id=SID)
        return _node_payload(g, select(g)), g

    def test_a_replaced_version_is_marked_superseded(self):
        data, _ = self._payload_for([_modify(1, "/work/x.py"),
                                     _modify(2, "/work/x.py")])
        self.assertEqual(data["e:/work/x.py@v1"]["fate"], "superseded")
        self.assertEqual(data["e:/work/x.py@v1"]["superseded_by"],
                         "e:/work/x.py@v2")

    def test_the_newest_unread_version_is_marked_unconsumed(self):
        data, _ = self._payload_for([_modify(1, "/work/x.py"),
                                     _modify(2, "/work/x.py")])
        self.assertEqual(data["e:/work/x.py@v2"]["fate"], "unconsumed")

    def test_written_then_overwritten_unread_is_flagged(self):
        data, _ = self._payload_for([_modify(1, "/work/x.py"),
                                     _modify(2, "/work/x.py")])
        # v1 is consumed by the modify at idx 2 (an edit reads before writing),
        # so the flag belongs on a version nothing read at all.
        self.assertIn("wasted", data["e:/work/x.py@v1"])

    def test_an_isolated_activity_says_so(self):
        data, _ = self._payload_for([_cmd(1, "echo hello")])
        self.assertTrue(data["a:a1b2c3d4#1"]["isolated"])

    def test_an_isolated_activity_carries_the_recorded_reason(self):
        """`(isolated)` alone is not information. The warning explaining why the
        path could not be resolved is."""
        data, g = self._payload_for(
            [_cmd(1, 'tail -40 "$MYSTERY_LOG"')])
        node = data["a:a1b2c3d4#1"]
        self.assertTrue(node["isolated"])
        self.assertTrue(node["warnings"],
                        f"expected a reason; graph warnings were {g.warnings}")

    def test_a_connected_activity_is_not_marked_isolated(self):
        data, _ = self._payload_for([_cmd(1, "cat /work/a.log")])
        self.assertFalse(data["a:a1b2c3d4#1"]["isolated"])


class TestPayload(unittest.TestCase):
    """The payload is built without graphviz, so these run everywhere."""

    def setUp(self):
        self.g = build_skeleton([
            _modify(1, "/work/engine.py", purpose="add state",
                    reasoning="the existing branch cannot express it"),
            _cmd(2, 'python3 /work/engine.py > /work/out.log',
                 purpose="run it"),
        ], session_id=SID)
        from graph.export import select
        self.v = select(self.g)
        self.d = html_mod._node_payload(self.g, self.v)

    def test_every_shown_node_is_in_the_payload(self):
        ids = ({a.node_id for a in self.v.activities}
               | {e.node_id for e in self.v.entities})
        self.assertEqual(set(self.d), ids)

    def test_the_command_is_carried_not_just_the_label(self):
        """The whole reason for this format: a PNG label is truncated and the
        command is the fact behind it."""
        self.assertEqual(self.d["a:a1b2c3d4#2"]["command"],
                         "python3 /work/engine.py > /work/out.log")

    def test_reasoning_is_carried_when_present(self):
        self.assertIn("cannot express it", self.d["a:a1b2c3d4#1"]["reasoning"])

    def test_path_is_carried_for_file_actions(self):
        self.assertEqual(self.d["a:a1b2c3d4#1"]["path"], "/work/engine.py")

    def test_every_edge_carries_its_evidence(self):
        """An edge whose provenance cannot be inspected is not auditable, which
        is why Edge.evidence is required at all."""
        seen = 0
        for node in self.d.values():
            for e in node["in"] + node["out"]:
                self.assertTrue(e["evidence"], e)
                self.assertIn(e["confidence"], ("certain", "likely", "weak"))
                seen += 1
        self.assertGreater(seen, 0)

    def test_edges_appear_on_both_endpoints(self):
        """So clicking either end reaches the same relation."""
        for node_id, node in self.d.items():
            for e in node["out"]:
                self.assertIn(e, self.d[e["dst"]]["in"])
            for e in node["in"]:
                self.assertIn(e, self.d[e["src"]]["out"])

    def test_entity_records_its_producer(self):
        ent = self.d["e:/work/out.log@v1"]
        self.assertEqual(ent["producer"], "a1b2c3d4#2")
        self.assertFalse(ent["outside"])

    def test_outside_flag_survives_into_the_payload(self):
        g = build_skeleton([
            {"ref": "a1b2c3d4#1", "sid": SID, "idx": 1, "turn": 1,
             "action": "create_file", "path": "/work/suite.yaml"},
            _cmd(2, "python3 /work/run.py /work/suite.yaml", turn=2),
        ], session_id=SID)
        from graph.export import select
        v = select(g, turns=[2])
        d = html_mod._node_payload(g, v)
        self.assertTrue(d["e:/work/suite.yaml@v1"]["outside"])


@needs_dot
class TestPage(unittest.TestCase):

    def setUp(self):
        self.g = build_skeleton([
            _modify(1, "/work/engine.py"),
            _cmd(2, "python3 /work/engine.py > /work/out.log"),
            _cmd(3, "nohup python3 /work/x.py & echo PID=$!", stdout="PID=4242\n"),
        ], session_id=SID)
        self.page = html_mod.to_html(self.g)

    def test_nothing_is_loaded_from_the_network(self):
        """session-viz's convention, and what makes the file shareable. The
        only http:// strings allowed are SVG's XML namespaces, which are
        identifiers rather than requests."""
        for url in re.findall(r'https?://[^"\'> ]+', self.page):
            self.assertIn("w3.org", url, url)
        self.assertNotRegex(self.page, r'<(script|link|img)[^>]+(src|href)=')

    def test_the_svg_is_inlined_without_its_xml_prologue(self):
        self.assertIn("<svg", self.page)
        self.assertNotIn("<?xml", self.page)
        self.assertNotIn("<!DOCTYPE svg", self.page)

    def test_every_node_in_the_payload_has_a_position(self):
        """Nodes are drawn from a view's geometry and clicked through the shared
        payload, keyed the same way. A node in one and not the other is either
        invisible or unclickable, and the page looks fine either way."""
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))
        positioned = {n["id"] for v in views for n in v["geometry"]["nodes"]}
        self.assertEqual(positioned, set(_payload(self.page)))

    def test_a_script_close_tag_in_the_data_cannot_end_the_block(self):
        """A command containing `</script>` would otherwise terminate the JSON
        block and break the page."""
        g = build_skeleton([_cmd(1, "echo '</script><b>x</b>' > /work/a.txt")],
                           session_id=SID)
        page = html_mod.to_html(g)
        body = re.search(r'<script id="data"[^>]*>(.*?)</script>', page, re.S)
        self.assertIn("<\\/script>", body.group(1))
        self.assertEqual(_payload(page)["a:a1b2c3d4#1"]["command"],
                         "echo '</script><b>x</b>' > /work/a.txt")

    def test_markup_in_a_purpose_is_escaped_in_the_header(self):
        g = build_skeleton([_modify(1, "/work/a.py", purpose="<b>bold</b>")],
                           session_id=SID)
        page = html_mod.to_html(g)
        self.assertNotIn("<b>bold</b>", page)

    def test_each_view_states_its_own_counts(self):
        """The header counts are rebuilt per view, so they live in the view data
        rather than in the markup."""
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))
        for v in views:
            labels = {k for k, _ in v["stats"]}
            self.assertIn("files", labels)
            self.assertIn("duration", labels)
            self.assertTrue({"actions", "turns"} & labels)

    def test_the_document_skeleton_is_explicit(self):
        """Not pedantry. With no <body>, tools that inject their own markup --
        VS Code's Live Preview does -- land inside the implicit body, and the
        flex layout would then treat the injection as a sibling of the graph.
        The flex container is a wrapper for the same reason."""
        for tag in ("<html", "<head>", "</head>", "<body>", "</body>", "</html>"):
            self.assertIn(tag, page_of := html_mod.to_html(self.g), tag)
        self.assertLess(page_of.index("</head>"), page_of.index("<body>"))
        self.assertIn('<div id="app">', page_of)

    def test_no_inline_event_handlers(self):
        """A content security policy blocks `onclick=` along with inline
        scripts, which is one reason clicking did nothing inside an embedded
        preview. Delegation via data attributes also avoids breaking the
        attribute on a node_id containing `&` or a quote."""
        self.assertNotRegex(html_mod.to_html(self.g), r"\son[a-z]+\s*=\s*[\"']")

    def test_the_hint_says_what_a_blocked_script_looks_like(self):
        """If scripts are blocked, nothing at all happens on click; the page
        should say so rather than appear broken."""
        page = html_mod.to_html(self.g)
        self.assertIn("scripts are being", page)
        self.assertIn("Live Preview", page)

    def test_a_node_id_needing_escaping_survives_into_the_panel(self):
        """`data-goto` carries a node_id; `&` in a path would break a naive
        attribute."""
        g = build_skeleton([
            _cmd(1, "cat '/work/a&b.log' > /work/out.log"),
            _cmd(2, "cat /work/out.log"),
        ], session_id=SID)
        page = html_mod.to_html(g)
        self.assertIn("/work/a&b.log", _payload(page)["e:/work/a&b.log@v0"]["key"])

    def test_confidence_filter_reaches_the_page(self):
        page = html_mod.to_html(self.g, min_confidence=CERTAIN)
        for node in _payload(page).values():
            for e in node["in"] + node["out"]:
                self.assertEqual(e["confidence"], "certain")

    def test_empty_graph_still_renders(self):
        page = html_mod.to_html(build_skeleton([], session_id=SID))
        self.assertIn("<svg", page)
        self.assertEqual(_payload(page), {})


class TestMissingGraphviz(unittest.TestCase):

    def test_absent_dot_gives_an_actionable_error(self):
        """Not a traceback: the message must say what to install and what to
        use instead."""
        real = shutil.which
        shutil.which = lambda name: None
        try:
            with self.assertRaises(html_mod.HtmlError) as ctx:
                html_mod.to_html(build_skeleton([], session_id=SID))
        finally:
            shutil.which = real
        msg = str(ctx.exception)
        self.assertIn("graphviz", msg)
        self.assertIn("--format dot", msg)


if __name__ == "__main__":
    unittest.main()


class TestOverviewOrder(unittest.TestCase):
    """The overview answers "what happened when" first, so its cards run down the
    page in turn order. Layered by dependency alone, turn 22 sat above turn 4
    whenever that crossed fewer edges."""

    # Two chains that share nothing: turns 2->3 hand one file over, turns 6->7
    # another. Dependency layering has no reason to put the first pair above the
    # second, so it ranks turn 2 and turn 6 side by side. A single chain would
    # have made this test pass whatever the layout did -- there the dependency
    # order *is* the turn order, and removing the spine left it green.
    ACTS = [
        _cmd(1, "echo hi > /work/a.txt", turn=2),
        _cmd(2, "cat /work/a.txt", turn=3),
        _cmd(3, "echo hi > /work/b.txt", turn=6),
        _cmd(4, "cat /work/b.txt", turn=7),
    ]

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))
        self.ov = next(v for v in views if v["name"] == "overview")
        self.data = _payload(self.page)

    def _cards(self):
        return sorted((n["y"], self.data[n["id"]]["turn"])
                      for n in self.ov["geometry"]["nodes"]
                      if self.data[n["id"]].get("action") == "turn")

    @needs_dot
    def test_turn_cards_descend_in_turn_order(self):
        cards = self._cards()
        self.assertGreater(len(cards), 2)
        order = [t for _, t in cards]
        self.assertEqual(order, sorted(order),
                         f"cards top-to-bottom are turns {order}")

    @needs_dot
    def test_the_overview_runs_down_the_page(self):
        """Top to bottom, so a long run scrolls the way a transcript does."""
        self.assertGreater(self.ov["height"], self.ov["width"])


class TestRefusedCall(unittest.TestCase):
    """A call the tool refused produces no entity, which is right -- nothing was
    read. Read from the picture alone it looked like a gap in extraction: a
    read_file with a path and no line to any file. The card has to say so."""

    def setUp(self):
        act = {"ref": f"{SID[:8]}#1", "sid": SID, "idx": 1, "turn": 1,
               "action": "read_file", "path": "/work/missing.yaml",
               "purpose": "Read the test case YAML",
               "error": '{"Custom": "path does not exist"}', "completed": False}
        ok = {"ref": f"{SID[:8]}#2", "sid": SID, "idx": 2, "turn": 1,
              "action": "read_file", "path": "/work/there.yaml",
              "purpose": "Read the other one"}
        self.g = build_skeleton([act, ok], session_id=SID)
        self.page = html_mod.to_html(self.g)
        self.data = _payload(self.page)

    def test_the_refused_call_has_no_entity(self):
        """The fact layer is unchanged: no file was read, so no edge."""
        node = self.data[f"a:{SID[:8]}#1"]
        self.assertEqual(node["in"], [])
        self.assertEqual(node["out"], [])

    def test_the_page_marks_it_as_refused(self):
        self.assertTrue(self.data[f"a:{SID[:8]}#1"]["failed"])
        self.assertFalse(self.data[f"a:{SID[:8]}#2"]["failed"])

    def test_the_reason_travels_with_it(self):
        """Not just that it failed -- why, in the tool's own words."""
        warn = " ".join(self.data[f"a:{SID[:8]}#1"]["warnings"])
        self.assertIn("does not exist", warn)

    def test_the_drawing_reads_the_flag(self):
        """A flag no renderer looks at would let this regress silently."""
        self.assertIn("d.failed", self.page)


class TestTrail(unittest.TestCase):
    """A file's history. No single view holds it -- the overview drops versions
    only one turn touched, a turn view drops the other turns -- so it is shipped
    separately and keyed by artifact."""

    # engine.py is read in turn 1, rewritten in turn 2, read again in turn 3.
    ACTS = [
        _cmd(1, "cat /work/engine.py", turn=1),
        _modify(2, "/work/engine.py", turn=2),
        _cmd(3, "python3 /work/engine.py", turn=3),
    ]

    def setUp(self):
        self.g = build_skeleton(self.ACTS, session_id=SID)
        self.page = html_mod.to_html(self.g)
        self.trails = json.loads(re.search(
            r'<script id="trails" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))

    def test_the_trail_covers_every_version(self):
        steps = self.trails["/work/engine.py"]
        self.assertEqual(sorted({s["v"] for s in steps}), [0, 1])

    def test_it_names_the_turn_and_the_action(self):
        """"Which turn edited it" is the question; a version number alone is not
        an answer."""
        wrote = [s for s in self.trails["/work/engine.py"] if s["verb"] == "wrote"]
        self.assertEqual([s["turn"] for s in wrote], [2])
        self.assertEqual(wrote[0]["action"], "modify_file")
        self.assertTrue(wrote[0]["ref"].endswith("#2"))

    def test_reads_are_in_the_trail_too(self):
        reads = [s for s in self.trails["/work/engine.py"] if s["verb"] == "read"]
        self.assertEqual(sorted(s["turn"] for s in reads), [1, 2, 3])

    def test_a_step_can_be_jumped_to(self):
        """Each step carries the view and the node, so the panel can open it."""
        for s in self.trails["/work/engine.py"]:
            if s["verb"] == "existed":
                continue
            self.assertTrue(s["view"].startswith("turn"), s)
            self.assertTrue(s["node"].startswith("a:"), s)
        self.assertIn("data-jump", self.page)

    def test_the_history_is_stored_once_per_file(self):
        """Keyed by artifact, not by node: a five-version file would otherwise
        carry its whole history five times."""
        self.assertIn("/work/engine.py", self.trails)
        self.assertNotIn("e:/work/engine.py@v1", self.trails)

    def test_the_dimming_uses_the_whole_chain(self):
        """Clicking a version lights every version of the same file, matched on
        the payload key -- the overview namespaces its node ids."""
        self.assertIn("function applyChainFocus", self.page)
        self.assertIn("d.key === n.key", self.page)
        self.assertIn("applyChainFocus(id)", self.page)


class TestOverviewEdgeDirection(unittest.TestCase):
    """Read and write are told apart by colour, on every view. The overview
    namespaces its node ids (`ov:a:turn10`), and a prefix test for "a:" answered
    no to all 124 of its edges: every one was drawn as a read, and the writes had
    their arrows pointing back at the turn that made the file."""

    ACTS = [
        _modify(1, "/work/shared.py", turn=1),          # turn 1 writes
        _cmd(2, "python3 /work/shared.py", turn=2),     # turn 2 reads
    ]

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))
        self.ov = next(v for v in views if v["name"] == "overview")
        self.data = _payload(self.page)

    def test_the_overview_has_both_directions(self):
        """A handover is one write and one read; if the view had only one kind,
        the test below would pass on a graph that cannot show the bug."""
        kinds = {("write" if self.data[e["src"]]["type"] == "activity" else "read")
                 for e in self.ov["geometry"]["edges"]}
        self.assertEqual(kinds, {"read", "write"})

    def test_the_renderer_asks_kinds_not_the_id(self):
        """The prefix test is what broke; assert on the fix, not on its absence."""
        self.assertIn("const write = isAct(e.src);", self.page)
        self.assertNotIn('e.src.startsWith("a:")', self.page)

    def test_every_overview_node_is_classifiable(self):
        """`isAct` reads KINDS, which is built from the payload -- so every node
        drawn on the overview has to be in it."""
        for n in self.ov["geometry"]["nodes"]:
            self.assertIn(n["id"], self.data)
            self.assertIn(self.data[n["id"]]["type"], ("activity", "entity"))


def _script(page):
    """The page's own script, without the JSON data blocks."""
    return page.split("<script>\n")[-1].split("</script>")[0]


def _strip_templates(js):
    """`js` with template literals removed but their `${...}` code kept.

    Template literals may span lines, so a per-line quote check reports every
    multi-line one as unterminated. Their interpolations are code, though, and
    that is exactly where the newline-in-a-string bug lived -- dropping them was
    what made this check miss it the first time it was written.
    """
    out, i, n, depth, in_tpl = [], 0, len(js), 0, False
    while i < n:
        c = js[i]
        if c == "\\":
            out.append("  " if in_tpl and not depth else js[i:i + 2])
            i += 2
            continue
        if in_tpl:
            if c == "`" and depth == 0:
                in_tpl = False
            elif js.startswith("${", i):
                depth += 1
                i += 2
                continue
            elif c == "}" and depth:
                depth -= 1
            elif depth or c == "\n":
                out.append(c)
            i += 1
            continue
        if c == "`":
            in_tpl = True
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class TestScriptSanity(unittest.TestCase):
    """Static checks on the generated javascript.

    There is no javascript engine on the machine that builds this file, so a
    thrown error ships as a blank canvas. Three have, and each was found by the
    reader opening the page rather than by anything here:

      * a python "\\n" written into the file as a real newline, which split a
        template literal and made the whole script a syntax error;
      * a `return` inside a `for..of`, which stopped the drawing at the first
        action instead of skipping it;
      * an assignment placed above the `const` it assigns to, a reference error
        before initialisation.

    These are the checks that would have caught them. Their own correctness is
    checked in TestScriptSanityCatchesRealBugs below, because a check that cannot
    fail is worse than no check: it reads as a guarantee.
    """

    def setUp(self):
        acts = [_modify(1, "/work/x.py"), _cmd(2, "cat /work/x.py"),
                {"ref": SID[:8] + "#3", "sid": SID, "idx": 3, "turn": 2,
                 "action": "read_file", "path": "/work/gone.yaml",
                 "purpose": "read the missing one", "completed": False,
                 "error": '{"Custom": "does not exist"}'}]
        self.js = _script(html_mod.to_html(build_skeleton(acts, session_id=SID)))

    def test_quoted_strings_close_on_their_line(self):
        """A quote left open is a syntax error, and the character that does it --
        a real newline inside a literal -- is invisible in a diff."""
        for ln, line in enumerate(_strip_templates(self.js).splitlines(), 1):
            j, w = 0, len(line)
            while j < w:
                c = line[j]
                if c == "\\":
                    j += 2
                elif c == "/" and j + 1 < w and line[j + 1] == "/":
                    break                        # a comment, outside any string
                elif c in "\"'":
                    k = j + 1
                    while k < w and line[k] != c:
                        k += 2 if line[k] == "\\" else 1
                    self.assertLess(k, w, "unterminated " + c + " on line "
                                    + str(ln) + ": " + line.strip())
                    j = k + 1
                else:
                    j += 1

    def test_no_assignment_above_its_declaration(self):
        """`X = 1` above `const X` is a reference error, and it is what a careless
        search-and-replace produces."""
        decl, assign = {}, []
        for i, line in enumerate(self.js.splitlines(), 1):
            m = re.match(r"(?:const|let)\s+([A-Za-z_$][\w$]*)", line)
            if m:
                decl.setdefault(m.group(1), i)
            m = re.match(r"([A-Z][A-Z0-9_]*)\s*=[^=]", line)
            if m:
                assign.append((m.group(1), i))
        for name, at in assign:
            self.assertFalse(name in decl and decl[name] > at,
                             "line " + str(at) + " assigns " + name
                             + ", declared at " + str(decl.get(name)))

    def test_brackets_balance(self):
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(self.js.count(opener), self.js.count(closer),
                             opener + closer + " unbalanced")
        self.assertEqual(self.js.count("`") % 2, 0, "unclosed template literal")

    def test_no_loop_exits_the_function(self):
        """`return` inside the node loop stopped the drawing at the first action.

        Only statements at the loop body's own indent count: the same loop
        registers event handlers, and a `return` inside one of those closures is
        both common and correct. Indent is what separates them without parsing.
        """
        lines = self.js.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"( *)for \((?:const|let) .+ of .+\) \{", line)
            if not m:
                continue
            outer = len(m.group(1))
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip() or nxt.lstrip().startswith("//"):
                    continue          # a comment may well discuss the word
                indent = len(nxt) - len(nxt.lstrip())
                if indent <= outer:
                    break                            # the loop closed
                # `if (...) return;` is the shape the bug took, so the token is
                # looked for anywhere on the line -- but not on a line that opens
                # a closure, where a return belongs to that closure.
                if (indent == outer + 2 and re.search(r"\breturn\b", nxt)
                        and "=>" not in nxt and "function" not in nxt):
                    self.fail("line " + str(j + 1) + ": `return` at the body "
                              "level of the for..of on line " + str(i + 1)
                              + " leaves the whole function -- `continue` was "
                              "meant: " + nxt.strip())

    def test_no_placeholder_survived_formatting(self):
        """A `{name}` the template did not replace ships as literal text. `${name}`
        is a javascript interpolation and is not one of them."""
        for m in re.finditer(r"(?<!\$)\{([a-z_]{3,})\}", self.js):
            self.fail("unreplaced placeholder " + m.group(0))

    def test_every_element_it_asks_for_exists(self):
        """`getElementById` on a missing id returns null and the next line
        dereferences it."""
        page = html_mod.to_html(build_skeleton([_modify(1, "/work/x.py")],
                                              session_id=SID))
        for wanted in sorted(set(re.findall(r'getElementById\("([^"]+)"\)', page))):
            self.assertIn('id="' + wanted + '"', page, "no element #" + wanted)


class TestScriptSanityCatchesRealBugs(unittest.TestCase):
    """The checks above, run against the three bugs that actually shipped.

    Written after two of the four checks turned out to pass on the very code they
    were written for: the quote check dropped template interpolations, which is
    where the newline bug was, and the loop check mis-tracked where a loop ended.
    A check nobody has seen fail is an assumption.
    """

    def _js_with(self, old, new):
        page = html_mod.to_html(build_skeleton(
            [_modify(1, "/work/x.py"), _cmd(2, "cat /work/x.py")],
            session_id=SID))
        self.assertIn(old, page, "the code this bug was injected into moved")
        return _script(page.replace(old, new))

    def _run(self, name, js):
        case = TestScriptSanity(name)
        case.js = js
        with self.assertRaises(AssertionError):
            getattr(case, name)()

    def test_a_real_newline_in_a_string_is_caught(self):
        js = self._js_with('join(String.fromCharCode(10))', 'join("\n")')
        self._run("test_quoted_strings_close_on_their_line", js)

    def test_a_return_in_the_node_loop_is_caught(self):
        js = self._js_with('=== "turn") continue;', '=== "turn") return;')
        self._run("test_no_loop_exits_the_function", js)

    def test_an_assignment_above_its_const_is_caught(self):
        js = self._js_with('const FAILED_COLOUR = ',
                           'FAILED_COLOUR = "#f87171";\nconst FAILED_COLOUR = ')
        self._run("test_no_assignment_above_its_declaration", js)

    def test_an_unbalanced_brace_is_caught(self):
        js = self._js_with("function clear(node) {", "function clear(node) {{")
        self._run("test_brackets_balance", js)


class TestCardIsAsummary(unittest.TestCase):
    """The card summarises; the panel holds the whole command.

    Putting the full command on the card made the drawing stop reading as a
    diagram -- a screen held a dozen paragraphs of shell and the thing the picture
    is for was buried in them. The command did not become less important, so it
    has to be complete somewhere, and that somewhere is the panel.
    """

    LONG = ("for f in /work/a /work/b /work/c; do\n"
            "  echo \"checking $f\" >> /work/audit.log\n"
            "  python3 /work/check.py --input \"$f\" --strict --verbose "
            "--report /work/report.json\n"
            "done\n"
            "grep -c ERROR /work/audit.log > /work/errors.count")

    def setUp(self):
        self.g = build_skeleton([_cmd(1, self.LONG, purpose="audit every input "
                                      "file and count the errors that came out")],
                                session_id=SID)
        self.page = html_mod.to_html(self.g)
        self.data = _payload(self.page)
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            self.page, re.S).group(1).replace("<\\/", "</"))
        self.geo = views[0]["geometry"]

    def _card(self):
        return next(n for n in self.geo["nodes"] if n["id"].startswith("a:"))

    def test_the_card_is_short(self):
        card = self._card()
        self.assertLessEqual(len(card["lines"]), 2,
                             f"card body is {len(card['lines'])} lines")
        # 100px is a badge row, two lines of purpose and one quoted command line.
        # The number is here to catch a return to putting the whole command on the
        # card, which ran to 638px, not to pin the layout to the pixel.
        self.assertLessEqual(card["ch"], 100,
                             "card is taller than a summary needs")

    def test_the_card_shows_the_purpose(self):
        self.assertIn("audit", " ".join(self._card()["lines"]).lower())

    def test_the_card_hints_at_the_command(self):
        """One line of it, so the reader knows what ran without reading a script."""
        hint = self._card().get("hint", "")
        self.assertTrue(hint, "no command hint on the card")
        self.assertLess(len(hint), 60, hint)
        self.assertNotIn("\n", hint)

    def test_the_panel_has_the_command_in_full(self):
        """Every line of it, byte for byte -- this is the evidence every `likely`
        edge was derived from."""
        got = self.data[f"a:{SID[:8]}#1"]["command"]
        self.assertEqual(got, self.LONG)
        for line in self.LONG.split("\n"):
            self.assertIn(line.strip(), got)

    def test_the_renderer_puts_it_in_the_panel(self):
        self.assertIn("<h2>command</h2><pre>", self.page)
        self.assertIn("esc(n.command)", self.page)

    def test_cards_are_a_uniform_height(self):
        """Twelve different heights stopped the eye scanning a column."""
        g = build_skeleton([_cmd(1, "ls"), _cmd(2, self.LONG),
                            _cmd(3, "echo " + "x" * 300)], session_id=SID)
        views = json.loads(re.search(
            r'<script id="views" type="application/json">(.*?)</script>',
            html_mod.to_html(g), re.S).group(1).replace("<\\/", "</"))
        heights = {n["ch"] for v in views for n in v["geometry"]["nodes"]
                   if n["id"].startswith("a:") and "ch" in n}
        self.assertLessEqual(len(heights), 2, f"card heights: {sorted(heights)}")


class TestOpeningShot(unittest.TestCase):
    """The page opens on the first few steps, not on the whole run shrunk to fit.

    Thirty-one turn cards scaled into one pane is an accurate picture of nothing:
    no label is legible and the only thing it conveys is that a lot happened.
    """

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(
            [_cmd(i, f"cat /work/f{i}.txt", turn=i) for i in range(1, 13)],
            session_id=SID))

    def test_the_first_render_does_not_fit_everything(self):
        self.assertIn("fitStart()", self.page)
        self.assertIn("  fitStart();", self.page)

    def test_a_small_view_is_still_shown_whole(self):
        """Zooming into the first four of six actions would hide half of a picture
        that fitted anyway, so the whole-view scale decides."""
        self.assertIn("scaleFor(whole, r) >= 0.7", self.page)

    def test_the_opening_shot_is_anchored_at_the_top(self):
        """A run starts at the top; centring on four cards inside a tall drawing
        left the reader unsure which way was forward."""
        self.assertIn("cam.y = 24 - b.y0 * k", self.page)

    def test_the_whole_graph_is_still_one_gesture_away(self):
        self.assertIn("fitView()", self.page)
        self.assertIn('addEventListener("dblclick"', self.page)

    def test_the_zoom_is_set_by_the_cards_not_their_files(self):
        """Including the files those steps touched was the first attempt and it
        did nothing: on the overview they are spread the full width of the
        drawing, so the box came out the size of the whole graph and the scale
        with it -- 0.167 either way. A fixed margin shows the nearest of them
        without letting the furthest set the zoom."""
        self.assertIn("b.x0 -= 300; b.x1 += 300;", self.page)
        self.assertIn("boundsOf(new Set(first))", self.page)


class TestSelectionStandsOut(unittest.TestCase):
    """Selecting a file dims the rest, without a toggle being found first."""

    ACTS = [_modify(1, "/work/x.py", turn=1), _cmd(2, "cat /work/x.py", turn=1),
            _cmd(3, "cat /work/other.txt", turn=2)]

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))

    def test_dimming_is_on_by_default(self):
        self.assertIn("let dimming = true", self.page)
        self.assertIn("dim others: on", self.page)

    def test_a_click_applies_it_and_a_hover_does_not(self):
        """Hovering used to dim the whole drawing, so the picture rearranged itself
        as the pointer crossed it on the way to what the reader wanted."""
        self.assertIn("if (dimming && !hovering) applyFocus(id);", self.page)
        self.assertIn("show(n.id, true)", self.page)      # hover still previews

    def test_the_unrelated_are_blurred_as_well_as_faded(self):
        """Pale alone left the rest of the drawing competing for attention."""
        self.assertRegex(self.page, r"\.dimmed \{ opacity:\.1\d; filter:blur")

    def test_three_levels_not_two(self):
        """The file's versions lit, the actions that touched them still sharp,
        everything else out of the way -- two levels put those actions in the same
        bin as the rest and the answer to "who edited it" went dark with them."""
        for cls in (".dimmed", ".faded", ".lit"):
            self.assertIn(cls + " {", self.page)
        self.assertIn('g.classList.toggle("faded"', self.page)

    def test_it_can_still_be_turned_off(self):
        self.assertIn("dimming = !dimming", self.page)
        self.assertIn('"dim others: "', self.page)


class TestSearch(unittest.TestCase):
    """Search is a dropdown over the whole run, not a filter of the current view.

    Filtering greyed most of a view as soon as one letter was typed -- it said
    "almost nothing matches" while the reader was halfway through the word -- and
    it left every edge solid on top of the greyed nodes. A list answers the
    question actually being asked, which of these did I mean, and picking one goes
    to it and highlights it there.
    """

    ACTS = [_modify(1, "/work/engine.py", turn=1),
            _cmd(2, "python3 /work/engine.py > /work/out.log", turn=2),
            _cmd(3, "grep ERROR /work/out.log", turn=3)]

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(self.ACTS, session_id=SID))

    def test_there_is_a_dropdown(self):
        self.assertIn('<div id="sug"></div>', self.page)
        self.assertIn("function renderSug", self.page)
        self.assertIn("#sug {", self.page)

    def test_typing_no_longer_greys_the_drawing(self):
        """The complaint was a greyed view with solid lines over it. Nothing in the
        input path touches a node's classes now."""
        self.assertNotIn('classList.toggle("nomatch"', self.page)
        self.assertNotIn(".nomatch {", self.page)
        self.assertIn('findEl.addEventListener("input", ev => renderSug(', self.page)

    def test_the_index_covers_the_whole_run(self):
        """Looking for a file whose name you remember is the case where you do not
        know which turn to open first."""
        self.assertIn("function buildIndex", self.page)
        self.assertIn("for (const id in DATA)", self.page)

    def test_each_row_says_where_it_will_take_you(self):
        self.assertIn("r.viewTitle", self.page)
        self.assertIn("function viewOf", self.page)

    def test_picking_one_navigates_and_highlights(self):
        """`jump` goes to the view and `show` is what dims the rest, so the reader
        lands on the thing they picked already highlighted."""
        self.assertIn("jump(r.view, r.id);", self.page)

    def test_what_is_on_screen_is_offered_first(self):
        """Picking something already visible should cost no navigation."""
        self.assertIn("(a.view === here ? 0 : 1) - (b.view === here ? 0 : 1)",
                      self.page)

    def test_an_empty_result_says_so(self):
        self.assertIn('>no match<', self.page)
        self.assertIn(".sgnone {", self.page)

    def test_the_keyboard_works(self):
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(key, self.page)
        self.assertIn("function moveSug", self.page)

    def test_a_click_lands_before_the_blur_closes_the_list(self):
        """On `click` the input's blur had already emptied the list."""
        self.assertIn('addEventListener("mousedown"', self.page)

    def test_the_list_closes_on_a_view_switch(self):
        """Its rows name views, and half of them would now point at the view
        already on screen."""
        self.assertIn("  closeSug();", self.page)

    def test_a_long_result_is_capped_and_says_so(self):
        self.assertIn("hit.slice(0, 12)", self.page)
        self.assertIn("keep typing", self.page)


class TestChromeIsLegible(unittest.TestCase):
    """The header and the panel are set twice the size of the drawing's own text.
    They are read at arm's length on a high-density display; the cards are read
    zoomed in."""

    def setUp(self):
        self.page = html_mod.to_html(build_skeleton(
            [_modify(1, "/work/x.py")], session_id=SID))
        self.css = self.page[self.page.index("<style>"):self.page.index("</style>")]

    def _size(self, sel):
        i = self.css.index(sel)
        block = self.css[i:self.css.index("}", i)]
        m = re.search(r"font-size:([\d.]+)px", block)
        return float(m.group(1)) if m else None

    def test_the_chrome_is_one_and_a_half_times_the_drawing(self):
        """2x was too much. The numbers are 1.5x the sizes the reference implies:
        a 17px title becomes 25.5px."""
        for sel, want in (("#crumbs {", 20.2), ("#vsel {", 20.2),
                          ("#find {", 20.2), (".row {", 20.2), ("h1 {", 21.8),
                          (".brand {", 25.5)):
            self.assertAlmostEqual(self._size(sel), want, delta=0.6, msg=sel)

    def test_the_cards_are_not(self):
        """Doubling the card text would have made every card twice the size and
        the layout with it; the cards are sized by graphviz from these numbers."""
        self.assertLess(self._size(".nd .t1 {"), 16)
        self.assertLess(self._size(".nd .cmd {"), 16)

    def test_the_containers_match_the_text(self):
        self.assertIn("height:78px", self.css)
        self.assertIn("padding-top:78px", self.css)
        self.assertIn("width:545px", self.css)
