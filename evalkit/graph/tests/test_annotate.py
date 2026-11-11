"""Smoke tests for the flag-style annotation overlay.

The overlay attaches at most one `tag` + optional `note` to each flagged
activity. These tests verify the page is self-contained, the tagset is
embedded, and the backward-compat shim for the deprecated `Codebook`
dataclass still imports.
"""
from __future__ import annotations

import json
import re
import unittest

from graph.annotate_schema import (STARTER_TAGS, STARTER_TAGSET, TagSet,
                                   tagset_to_dict,
                                   Codebook, STARTER_CODEBOOK)
from graph.html import to_html
from graph.build import build_skeleton
from graph.schema import Activity, Entity, Edge, ProvGraph, USED_BY, LIKELY


def _toy_graph() -> ProvGraph:
    g = ProvGraph(session_id="demo1234")
    g.add_activity(Activity(ref="demo1234#0", idx=0, turn=1,
                            action="run_command", purpose="list files",
                            command="ls -la /tmp"))
    g.add_activity(Activity(ref="demo1234#1", idx=1, turn=1,
                            action="read_file", purpose="read config",
                            path="/etc/hosts"))
    e_id = g.add_entity(Entity(key="/etc/hosts", version=0, born_idx=-1))
    g.add_edge(e_id, "a:demo1234#1", USED_BY, LIKELY, "read_file target")
    return g


class TestTagSet(unittest.TestCase):
    def test_starter_has_four_tags(self):
        self.assertEqual(len(STARTER_TAGSET.tags), 4)
        self.assertIn("Standout", STARTER_TAGSET.tags)
        self.assertIn("Violation", STARTER_TAGSET.tags)
        self.assertIn("Duplication", STARTER_TAGSET.tags)
        self.assertIn("Mistake", STARTER_TAGSET.tags)

    def test_tags_module_constant_matches_tagset(self):
        self.assertEqual(STARTER_TAGS, STARTER_TAGSET.tags)

    def test_tagset_has_contains(self):
        self.assertTrue(STARTER_TAGSET.has("Standout"))
        self.assertFalse(STARTER_TAGSET.has("made up"))

    def test_serialisation(self):
        d = tagset_to_dict(STARTER_TAGSET)
        self.assertEqual(d["id"], "default-v1")
        self.assertEqual(d["tags"], list(STARTER_TAGS))
        self.assertIn("house_rule", d)


class TestAnnotateOverlay(unittest.TestCase):
    def setUp(self):
        g = _toy_graph()
        self.off_html = to_html(g)                       # no tagset: overlay off
        self.on_html = to_html(g, tagset=STARTER_TAGSET) # overlay on

    def test_overlay_absent_without_tagset(self):
        for marker in ("ann-bar", "ann-tag", "ann-progress", "TAGSET"):
            self.assertNotIn(marker, self.off_html)

    def test_overlay_present_with_tagset(self):
        for marker in ("ann-bar", "ann-tag", "ann-progress", "ann-panel"):
            self.assertIn(marker, self.on_html)

    def test_panel_positioned_below_prompt_banner(self):
        """The flag widget is a left-side fixed panel that opens below the
        `#prompt-banner` -- NOT inside the right-side detail body. The test
        pins both halves of that contract."""
        self.assertIn("id = 'ann-panel'", self.on_html)
        self.assertIn("positionPanel", self.on_html)
        # No `.ann-sect` injection into #body any more; the previous design
        # injected into the right-side panel. Keep this red if a refactor
        # accidentally brings that back.
        self.assertNotIn("body.insertAdjacentHTML", self.on_html)

    def test_tagset_embedded_as_json(self):
        # Find the TAGSET constant definition
        m = re.search(r'const TAGSET\s*=\s*({[^;]+});', self.on_html)
        self.assertIsNotNone(m)
        ts = json.loads(m.group(1).replace("<\\/", "</"))
        self.assertEqual(ts["id"], "default-v1")
        self.assertIn("Standout", ts["tags"])

    def test_session_scoped_storage_key(self):
        self.assertIn("'evalkit:annotate:' + SESSION", self.on_html)

    def test_action_buttons_present(self):
        for btn in ("ann-export", "ann-import", "ann-clear"):
            self.assertIn(f'id="{btn}"', self.on_html)

    def test_comment_autosaves(self):
        """Comment is auto-saved on `input` -- no Save button, no Ctrl+Enter
        shortcut. This test pins that contract so a future refactor cannot
        reintroduce a dead button without noticing."""
        self.assertNotIn("Save comment", self.on_html)
        self.assertNotIn("ann-save", self.on_html)
        # The handler persists via setFlag on each keystroke.
        self.assertIn("setFlag(ref, cur.tag, el.value)", self.on_html)

    def test_note_requires_tag_invariant(self):
        """A comment is only ever stored alongside a tag. The textarea must
        be disabled by default (`pick a tag first`) and `setFlag` must drop
        the whole entry when the tag goes empty, otherwise orphan notes
        accumulate in localStorage and show up on steps the user never
        actually flagged."""
        self.assertIn("pick a tag first", self.on_html)
        # setFlag's early-return on empty tag deletes the whole entry.
        self.assertIn("delete state[ref]; saveState(); bumpProgress(); return",
                      self.on_html)
        # Export filter requires a non-empty tag.
        self.assertIn("state[r].tag", self.on_html)

    def test_default_output_unchanged_size_regression(self):
        """Without tagset, the overlay must not add characters."""
        # Base page end is `</script>\n</body>\n</html>\n`. Overlay inserts
        # between `</script>` and `</body>`; with no tagset, the inserted
        # string must be empty.
        self.assertNotIn("ann-bar", self.off_html)
        self.assertNotIn("</style>\n<script>\n(function", self.off_html)


class TestLegacyShim(unittest.TestCase):
    """`Codebook` / `STARTER_CODEBOOK` are deprecated but kept importable so
    older callers (and any pickled state) do not break mid-migration."""
    def test_codebook_still_imports(self):
        self.assertEqual(STARTER_CODEBOOK.id, "default-v1")
        self.assertEqual(len(STARTER_CODEBOOK.questions), 1)
        q = STARTER_CODEBOOK.questions[0]
        self.assertEqual(q.labels, STARTER_TAGS)

    def test_codebook_question_lookup(self):
        self.assertIsNotNone(STARTER_CODEBOOK.question("flag"))
        self.assertIsNone(STARTER_CODEBOOK.question("missing"))


class TestNoLLMCoupling(unittest.TestCase):
    """Phase 1 must not depend on `llm.py` / `kiro_acp.py`. Keeping this test
    makes the Phase 3 LLM addition an explicit commit, not drift."""
    def test_annotate_schema_has_no_llm_imports(self):
        import graph.annotate_schema as mod
        src = open(mod.__file__).read()
        self.assertNotIn("from llm", src)
        self.assertNotIn("import llm", src)
        self.assertNotIn("kiro_acp", src)


if __name__ == "__main__":
    unittest.main()
