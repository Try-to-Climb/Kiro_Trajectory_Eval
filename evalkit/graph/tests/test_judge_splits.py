"""What the judge is told, and that the telling reaches it."""

import unittest

from graph.tests import dump_splits as ds
from graph.tests import judge_splits as js


class TestTheKnownGapsReachTheModel(unittest.TestCase):
    """Two texts say what is already known, and only one is sent.

    `dump_splits.PREAMBLE` heads the markdown a person reads; `judge_splits.BRIEF`
    is what goes into the prompt. An edit meant for both landed only on the
    PREAMBLE once, so a run was made believing four gaps had been declared and the
    model never saw them -- 59 of 217 reports came back about one of the four, and
    that attention was spent for nothing.
    """

    # Each gap, and a phrase that has to survive rewording.
    GAPS = {
        "a $(...) in an assignment is not entered": "inside an assignment",
        "~ is not expanded": "does not carry HOME",
        "a run-time value is left as written": "produced at run time",
        "a mid-command cd does not move the directory": "part-way through",
        "cp and mv produce no edge": "in either direction",
        "a URL is never a node": "URL is never a node",
        "mkdir/rm/ls/touch/stat/test are dropped": "move no content",
    }

    def test_the_brief_declares_every_known_gap(self):
        for gap, phrase in self.GAPS.items():
            self.assertIn(phrase, js.BRIEF, gap)

    def test_the_preamble_declares_them_too(self):
        """The dump is read by people, who need the same list."""
        for gap, phrase in self.GAPS.items():
            self.assertIn(phrase, ds.PREAMBLE, gap)

    def test_the_brief_is_what_gets_sent(self):
        """The assertion above is worth nothing if the prompt is built elsewhere."""
        import inspect
        src = inspect.getsource(js)
        self.assertIn("BRIEF +", src)

    def test_every_verdict_the_brief_names_is_accepted(self):
        """A verdict the model is invited to give and the parser then drops is a
        finding thrown away."""
        for v in js.VERDICTS:
            self.assertIn(v, js.BRIEF, v)


if __name__ == "__main__":
    unittest.main()
