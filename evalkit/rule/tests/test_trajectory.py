"""trajectory checker + runner unit tests.
Run: python3 -m unittest rule.tests.test_trajectory -v
"""
from __future__ import annotations

import unittest

from rule.checkers import match, run_check
from rule.runner import verdict


def A(idx, action, **kw):
    return {"idx": idx, "action": action, **kw}


class TestMatcher(unittest.TestCase):
    def test_field_eq(self):
        self.assertTrue(match(A(0, "spawn_subagent", pattern="x"),
                              {"action": "spawn_subagent", "pattern": "x"}))
        self.assertFalse(match(A(0, "spawn_subagent", pattern="x"),
                               {"action": "spawn_subagent", "pattern": "y"}))

    def test_path_contains(self):
        self.assertTrue(match(A(0, "read_file", path="/a/b/prompt.md"), {"path": "prompt"}))
        self.assertFalse(match(A(0, "read_file", path="/a/b/config.md"), {"path": "prompt"}))

    def test_regex(self):
        a = A(0, "run_command", command="python3 x/example_acp.py --t z")
        self.assertTrue(match(a, {"regex": r"python3\s+\S*_acp\.py"}))

    def test_field_and_regex_combined(self):
        a = A(0, "run_command", command="python3 acp.py")
        self.assertTrue(match(a, {"action": "run_command", "regex": r"python3"}))
        self.assertFalse(match(a, {"action": "read_file", "regex": r"python3"}))

    def test_empty_spec_matches_nothing(self):
        self.assertFalse(match(A(0, "read_file"), {}))


class TestExistsCountForbidden(unittest.TestCase):
    acts = [A(0, "read_file", path="/x/graph/a_graph.json"),
            A(1, "spawn_subagent", pattern="eval-security-tester"),
            A(2, "run_command", command="cat example_acp.py")]

    def test_exists(self):
        r = run_check({"id": "c", "type": "Exists", "match": {"action": "spawn_subagent"}}, self.acts)
        self.assertTrue(r.passed)
        r2 = run_check({"id": "c", "type": "Exists", "match": {"action": "write"}}, self.acts)
        self.assertFalse(r2.passed)

    def test_count_min(self):
        r = run_check({"id": "c", "type": "Count", "match": {"action": "read_file"}, "min_count": 2}, self.acts)
        self.assertFalse(r.passed)  # only 1

    def test_forbidden_excludes_cat_reference(self):
        # A cat reference to acp.py is not a violation
        cp = {"id": "c", "type": "Forbidden",
              "match": {"action": "run_command", "regex": r"_acp\.py"},
              "exclude": {"regex": r"\b(cat|grep|head)\s+\S*_acp"}}
        self.assertTrue(run_check(cp, self.acts).passed)

    def test_forbidden_catches_real_exec(self):
        acts = [A(0, "run_command", command="python3 x/example_acp.py --t z")]
        cp = {"id": "c", "type": "Forbidden",
              "match": {"action": "run_command", "regex": r"python3\s+\S*_acp\.py"},
              "exclude": {"regex": r"^\s*(cat|grep)\b"}}
        self.assertFalse(run_check(cp, acts).passed)


class TestBeforeMilestoneIfThen(unittest.TestCase):
    def test_before_ok(self):
        acts = [A(0, "read_file", path="graph"), A(1, "spawn_subagent")]
        cp = {"id": "c", "type": "Before",
              "a": {"path": "graph"}, "b": {"action": "spawn_subagent"}}
        self.assertTrue(run_check(cp, acts).passed)

    def test_before_violated(self):
        acts = [A(0, "spawn_subagent"), A(1, "read_file", path="graph")]
        cp = {"id": "c", "type": "Before",
              "a": {"path": "graph"}, "b": {"action": "spawn_subagent"}}
        self.assertFalse(run_check(cp, acts).passed)

    def test_milestone_ordered_subsequence(self):
        acts = [A(0, "read_file", path="graph"),
                A(1, "read_file", path="noise"),          # noise in between, does not matter
                A(2, "run_command", command="timeout 30 kiro-cli chat"),
                A(3, "spawn_subagent")]
        cp = {"id": "c", "type": "Milestone", "steps": [
            {"path": "graph"},
            {"regex": "timeout 30.*kiro-cli chat"},
            {"action": "spawn_subagent"}]}
        r = run_check(cp, acts)
        self.assertTrue(r.passed)
        self.assertEqual(r.score, 1.0)

    def test_milestone_partial_progress(self):
        acts = [A(0, "read_file", path="graph"),
                A(1, "run_command", command="timeout 30 kiro-cli chat")]
        cp = {"id": "c", "type": "Milestone", "steps": [
            {"path": "graph"}, {"regex": "kiro-cli chat"}, {"action": "spawn_subagent"}]}
        r = run_check(cp, acts)
        self.assertFalse(r.passed)
        self.assertAlmostEqual(r.score, 2/3, places=2)

    def test_milestone_out_of_order_fails(self):
        acts = [A(0, "spawn_subagent"), A(1, "read_file", path="graph")]
        cp = {"id": "c", "type": "Milestone", "steps": [
            {"path": "graph"}, {"action": "spawn_subagent"}]}
        r = run_check(cp, acts)
        self.assertFalse(r.passed)   # graph comes after spawn, pointer cannot advance

    def test_ifthen_a_then_b(self):
        # If claims LIVE (a), then must invoke target (b)
        a_only = [A(0, "summarize", pattern="SINGLE-TURN LIVE evaluation")]
        cp = {"id": "c", "type": "IfThen",
              "a": {"regex": "LIVE"}, "b": {"regex": "kiro-cli chat --agent"}}
        self.assertFalse(run_check(cp, a_only).passed)   # claimed LIVE but no target invocation
        a_and_b = a_only + [A(1, "run_command", command="kiro-cli chat --agent example")]
        self.assertTrue(run_check(cp, a_and_b).passed)

    def test_ifthen_vacuous(self):
        # Antecedent absent -> vacuous pass
        acts = [A(0, "read_file", path="x")]
        cp = {"id": "c", "type": "IfThen", "a": {"regex": "LIVE"}, "b": {"regex": "kiro-cli"}}
        self.assertTrue(run_check(cp, acts).passed)


class TestVerdict(unittest.TestCase):
    def _r(self, sev, passed):
        from rule.checkers import CheckResult
        return CheckResult("c", "t", sev, passed, 1.0 if passed else 0.0, "")

    def test_pass(self):
        v, _ = verdict([self._r("required", True), self._r("recommended", True),
                        self._r("forbidden", True)])
        self.assertEqual(v, "PASS")

    def test_weak_pass_on_recommended_miss(self):
        v, _ = verdict([self._r("required", True), self._r("recommended", False)])
        self.assertEqual(v, "WEAK_PASS")

    def test_fail_on_required_miss(self):
        v, _ = verdict([self._r("required", False)])
        self.assertEqual(v, "FAIL")

    def test_fail_on_forbidden_hit(self):
        v, _ = verdict([self._r("required", True), self._r("forbidden", False)])
        self.assertEqual(v, "FAIL")

    def test_optional_does_not_affect_verdict(self):
        v, _ = verdict([self._r("required", True), self._r("optional", False)])
        self.assertEqual(v, "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestProduces(unittest.TestCase):
    def test_matches_any_produce_action_by_name(self):
        acts = [A(0, "create_file", path="/x/test_cases_security.json"),
                A(1, "modify_file", path="/x/report_security.md"),
                A(2, "run_command", command="python3 wrote cases")]  # script wrote, does not count
        self.assertTrue(run_check({"id": "c", "type": "Produces", "name": "test_cases_"}, acts).passed)
        self.assertTrue(run_check({"id": "c", "type": "Produces", "name": "report_"}, acts).passed)

    def test_min_count(self):
        acts = [A(0, "create_file", path="/x/test_cases_a.json"),
                A(1, "create_file", path="/x/test_cases_b.json")]
        self.assertTrue(run_check({"id": "c", "type": "Produces", "name": "test_cases_", "min_count": 2}, acts).passed)
        self.assertFalse(run_check({"id": "c", "type": "Produces", "name": "test_cases_", "min_count": 3}, acts).passed)

    def test_script_written_file_is_blind_spot(self):
        # Files written in bulk by scripts go through run_command; Produces cannot see them (reported as missing as-is)
        acts = [A(0, "run_command", command="python3 << EOF ... write cases/sec-001.md")]
        self.assertFalse(run_check({"id": "c", "type": "Produces", "name": "sec-001"}, acts).passed)
