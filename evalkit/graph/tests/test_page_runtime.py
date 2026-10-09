"""Run the generated page in a real DOM, when a javascript engine is available.

The page ships as one self-contained file with no libraries, and that does not
change. This is the test harness only: three bugs reached the reader because the
machine that builds the page has no javascript engine on PATH, and static checks
(`test_html.py::TestScriptSanity`) only cover the shapes already known to have
gone wrong.

Skipped unless both are present:

  * a node binary -- `GRAPH_NODE=/path/to/node`, or `node` on PATH. On this host,
    `module load cdt2/node/2.6.4` provides 6.10.2.
  * `graph/tests/page-harness/node_modules` -- run `npm install` there once.
    Installing is not done from a test: a test that reaches the network is a test
    that fails for reasons unrelated to the code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from graph import html as html_mod                                 # noqa: E402
from graph.build import build_skeleton                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "page-harness")
SID = "a1b2c3d4-1111"


def _node() -> str | None:
    return os.environ.get("GRAPH_NODE") or shutil.which("node")


def _reason() -> str | None:
    if not _node():
        return ("no node: set GRAPH_NODE, or `module load cdt2/node/2.6.4`")
    if not os.path.isdir(os.path.join(HARNESS, "node_modules")):
        return f"no node_modules: run `npm install` in {HARNESS}"
    return None


def _cmd(idx, command, turn=1, purpose=None):
    resp = json.dumps({"Success": {"items": [{"Json": {"stdout": ""}}]}})
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "run_command", "command": command, "response": resp,
            "purpose": purpose or "run something",
            "args": {"working_dir": "/work"}}


def _modify(idx, path, turn=1):
    return {"ref": f"a1b2c3d4#{idx}", "sid": SID, "idx": idx, "turn": turn,
            "action": "modify_file", "path": path, "purpose": f"edit {path}"}


@unittest.skipIf(_reason(), _reason() or "")
class TestPageRuns(unittest.TestCase):
    """The page loads, draws, switches views and answers a click."""

    # Enough turns and shared files that the overview has something to dim: the
    # first version of this fixture had five nodes, the harness skips the dimming
    # check below six, and the check was silently not running -- the same trap the
    # check itself exists to catch.
    ACTS = [
        _modify(1, "/work/engine.py", turn=1),
        _cmd(2, "cat /work/engine.py", turn=1),
        _cmd(3, "python3 /work/engine.py > /work/out.log", turn=2),
        _cmd(4, "grep ERROR /work/out.log", turn=2),
        _cmd(5, "cp /work/out.log /work/kept.log", turn=3),
        _cmd(6, "wc -l /work/kept.log", turn=3),
        _cmd(7, "cat /work/engine.py /work/kept.log", turn=4),
        _cmd(8, "nohup python3 /work/engine.py & echo PID=$!", turn=4),
    ]

    @classmethod
    def setUpClass(cls):
        page = html_mod.to_html(build_skeleton(cls.ACTS, session_id=SID))
        fd, cls.path = tempfile.mkstemp(suffix=".html", prefix="graph-page-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page)
        cls.proc = subprocess.run(
            [_node(), os.path.join(HARNESS, "run_page.js"), cls.path],
            cwd=HARNESS, capture_output=True, text=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_the_page_runs_without_a_script_error(self):
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stdout + self.proc.stderr)

    def test_the_script_parses(self):
        """`node --check` on the script alone. A syntax error is the failure that
        shows as a blank page with nothing in the console but one line."""
        page = open(self.path, encoding="utf-8").read()
        js = page.split("<script>\n")[-1].split("</script>")[0]
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(js)
            out = subprocess.run([_node(), "--check", path],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
        finally:
            os.unlink(path)

    def test_every_view_draws_something(self):
        """A view switch rebuilds the drawing from scratch, so the first one being
        fine says nothing about the rest."""
        self.assertIn("all drew", self.proc.stdout, self.proc.stdout)

    def test_the_turn_box_focuses_and_open_navigates(self):
        """Two targets on one card, and the harness checks both: clicking the box
        must stay on the overview, clicking OPEN must leave it."""
        self.assertIn("should not have moved", self.proc.stdout, self.proc.stdout)
        self.assertNotIn("the box navigated", self.proc.stdout, self.proc.stdout)
        self.assertIn("click OPEN        -> all turns", self.proc.stdout,
                      self.proc.stdout)

    def test_clicking_a_file_fills_the_panel(self):
        self.assertIn("click a file", self.proc.stdout, self.proc.stdout)
        self.assertNotIn("panel unchanged", self.proc.stdout, self.proc.stdout)

    def test_selecting_a_file_dims_the_rest(self):
        self.assertNotIn("did not dim the rest", self.proc.stdout, self.proc.stdout)

    def test_hovering_does_not_dim(self):
        """The picture used to rearrange itself as the pointer crossed it, so the
        reader could not look at anything on the way to what they wanted."""
        self.assertNotIn("only a click should", self.proc.stdout,
                         self.proc.stdout)
        self.assertIn("hover a file      -> dimmed 0", self.proc.stdout,
                      self.proc.stdout)

    def test_panning_keeps_the_selection(self):
        """A drag ends with a `click` on the svg. Treated as a click on empty
        space, it cleared the selection -- so looking around the highlighted file
        undid the highlight."""
        self.assertIn("drag the canvas   -> panel kept", self.proc.stdout,
                      self.proc.stdout)
        self.assertNotIn("panning cleared", self.proc.stdout, self.proc.stdout)

    def test_typing_offers_suggestions(self):
        self.assertNotIn("produced no suggestions", self.proc.stdout,
                         self.proc.stdout)

    def test_typing_does_not_dim_the_drawing(self):
        """Greying most of a view after one letter said "almost nothing matches"
        while the reader was still halfway through the word."""
        self.assertNotIn("typing dimmed the drawing", self.proc.stdout,
                         self.proc.stdout)

    def test_a_row_says_what_it_is_and_where_it_lives(self):
        self.assertNotIn("does not say what it is", self.proc.stdout,
                         self.proc.stdout)

    def test_a_miss_says_no_match(self):
        self.assertIn("'no match' shown", self.proc.stdout, self.proc.stdout)

    def test_clearing_closes_the_list(self):
        self.assertIn("clear the box     -> list closed", self.proc.stdout,
                      self.proc.stdout)

    def test_picking_one_selects_and_highlights_it(self):
        """The whole point: the reader lands on the thing they picked, already
        highlighted, without hunting for it."""
        self.assertNotIn("did not select anything", self.proc.stdout,
                         self.proc.stdout)
        self.assertNotIn("did not highlight it", self.proc.stdout,
                         self.proc.stdout)

    def test_a_real_click_on_empty_space_still_clears(self):
        """The guard must not swallow every click, only the one that ends a drag."""
        self.assertIn("click empty space -> cleared", self.proc.stdout,
                      self.proc.stdout)


class TestHarnessIsReachable(unittest.TestCase):
    """Runs whether or not node is installed: a harness that silently stops being
    run is worse than none, because the skip reads as a pass."""

    def test_the_harness_is_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(HARNESS, "run_page.js")))
        self.assertTrue(os.path.isfile(os.path.join(HARNESS, "package.json")))

    def test_its_dependency_is_pinned(self):
        with open(os.path.join(HARNESS, "package.json"), encoding="utf-8") as fh:
            pkg = json.load(fh)
        for name, spec in pkg["dependencies"].items():
            self.assertRegex(spec, r"^\d+\.\d+\.\d+$",
                             f"{name} is not pinned to an exact version")

    def test_the_skip_reason_is_actionable(self):
        why = _reason()
        if why:
            self.assertTrue("node" in why, why)
            print(f"\n  [page-harness skipped] {why}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
