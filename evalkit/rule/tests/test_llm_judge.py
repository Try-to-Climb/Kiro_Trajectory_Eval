"""LLM-as-judge offline tests: use a fake caller; do not call kiro for real."""
import unittest

from rule import llm_judge
from rule.checkers import run_check


def _acts():
    return [
        {"idx": 0, "action": "read_file", "tool": "read", "path": "/a.py",
         "completed": True, "args": {"__tool_use_purpose": "inspect implementation"}, "reasoning": "read the code first"},
        {"idx": 1, "action": "run_command", "tool": "shell", "command": "pytest",
         "completed": True, "args": {"__tool_use_purpose": "run tests"}},
    ]


class TestPromptAssembly(unittest.TestCase):
    def test_contains_three_parts(self):
        p = llm_judge.assemble_prompt("efficiency", "Fix bug X", "[0] read_file(read) /a.py")
        self.assertIn("efficiency", p)            # rubric
        self.assertIn("Fix bug X", p)             # objective
        self.assertIn("[0] read_file", p)         # trajectory
        self.assertIn("```json", p)               # output format constraint

    def test_unknown_dimension(self):
        with self.assertRaises(ValueError):
            llm_judge.assemble_prompt("nope", "o", "v")


class TestParse(unittest.TestCase):
    def test_fenced(self):
        r = llm_judge.parse_response('preamble\n```json\n{"score":3,"justification":"okay"}\n```')
        self.assertEqual(r["score"], 3)
        self.assertEqual(r["justification"], "okay")

    def test_bare_json(self):
        self.assertEqual(llm_judge.parse_response('{"score": 4, "justification": "good"}')["score"], 4)

    def test_na_and_out_of_range(self):
        self.assertIn("error", llm_judge.parse_response('{"score":"N/A"}'))
        self.assertIn("error", llm_judge.parse_response('{"score":9}'))

    def test_garbage(self):
        self.assertIn("error", llm_judge.parse_response("not json at all"))


class TestJudgeWithFakeCaller(unittest.TestCase):
    def test_good(self):
        fake = lambda p: '```json\n{"score":4,"justification":"efficient"}\n```'
        r = llm_judge.judge("efficiency", "obj", "view", caller=fake)
        self.assertEqual(r["score"], 4)

    def test_caller_raises(self):
        def boom(p): raise RuntimeError("x")
        self.assertIn("error", llm_judge.judge("efficiency", "o", "v", caller=boom))


class TestLLMJudgeChecker(unittest.TestCase):
    def test_skip_when_no_caller(self):
        cp = {"id": "eff", "type": "LLMJudge", "severity": "required", "dimension": "efficiency"}
        r = run_check(cp, _acts(), context={"llm_caller": None})
        self.assertTrue(r.passed)          # no backend -> non-punitive pass
        self.assertEqual(r.confidence, 0.0)
        self.assertIn("not run", r.reason)

    def test_pass_above_threshold(self):
        cp = {"id": "eff", "type": "LLMJudge", "severity": "recommended",
              "dimension": "efficiency", "pass_threshold": 0.75}
        ctx = {"objective": "o", "llm_caller": lambda p: '{"score":4,"justification":"good"}'}
        r = run_check(cp, _acts(), context=ctx)
        self.assertTrue(r.passed)
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.confidence, 0.9)

    def test_fail_below_threshold(self):
        cp = {"id": "eff", "type": "LLMJudge", "severity": "recommended",
              "dimension": "efficiency", "pass_threshold": 0.75}
        ctx = {"objective": "o", "llm_caller": lambda p: '{"score":2,"justification":"detour"}'}
        r = run_check(cp, _acts(), context=ctx)
        self.assertFalse(r.passed)         # 2/4=0.5 < 0.75
        self.assertEqual(r.score, 0.5)

    def test_judge_error_non_punitive(self):
        cp = {"id": "eff", "type": "LLMJudge", "severity": "required", "dimension": "efficiency"}
        def boom(p): raise RuntimeError("kiro is down")
        r = run_check(cp, _acts(), context={"llm_caller": boom})
        self.assertTrue(r.passed)          # error is not punitive
        self.assertEqual(r.confidence, 0.0)

    def test_validate_missing_dimension(self):
        r = run_check({"id": "x", "type": "LLMJudge", "severity": "required"}, _acts())
        self.assertFalse(r.passed)
        self.assertIn("rule error", r.reason)

    def test_validate_unknown_dimension(self):
        r = run_check({"id": "x", "type": "LLMJudge", "dimension": "zzz"}, _acts())
        self.assertFalse(r.passed)
        self.assertIn("rule error", r.reason)


if __name__ == "__main__":
    unittest.main()
