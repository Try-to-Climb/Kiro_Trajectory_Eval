"""Tests for the structured-intent desugar compiler."""
import json
import os
import tempfile
import unittest

from rule.rules_dsl import desugar_check, glob_to_regex
from rule.runner import run
from rule.checkers import run_check


class TestGlob(unittest.TestCase):
    def test_glob(self):
        self.assertEqual(glob_to_regex("*.json"), r".*\.json")
        self.assertEqual(glob_to_regex("a?b"), r"a.b")
        self.assertEqual(glob_to_regex("timeout *"), r"timeout\ .*")  # whitespace is escaped (harmless)


class TestDesugar(unittest.TestCase):
    def test_passthrough_raw(self):
        raw = {"id": "x", "type": "Exists", "match": {"action": "read_file"}}
        self.assertIs(desugar_check(raw), raw)          # rules with explicit type are passed through verbatim

    def test_reads_mechanism_agnostic(self):
        d = desugar_check({"reads": "*.kiro/agents/*.json", "importance": "recommended"})
        self.assertEqual(d["type"], "Exists")
        self.assertNotIn("action", d["match"])          # not bound to read_file -- shell cat also counts
        self.assertIn("regex", d["match"])

    def test_runs(self):
        d = desugar_check({"runs": "kiro-cli chat --agent *"})
        self.assertEqual(d["match"]["action"], "run_command")
        self.assertIn("program", d["match"])          # runs uses `program` (matched at subcommand head)
        self.assertIn("kiro", d["match"]["program"])

    def test_write_to_produces(self):
        d = desugar_check({"write": "test_cases_", "importance": "required"})
        self.assertEqual(d["type"], "Produces")
        self.assertEqual(d["name"], "test_cases_")
        self.assertEqual(d["severity"], "required")

    def test_dispatches_count(self):
        d = desugar_check({"dispatches": "eval-*", "at_least": 2})
        self.assertEqual(d["type"], "Count")
        self.assertEqual(d["match"]["action"], "spawn_subagent")
        self.assertEqual(d["min_count"], 2)

    def test_dispatches_at_most_allows_zero(self):
        d = desugar_check({"dispatches": "eval-*", "at_most": 2})
        self.assertEqual(d["min_count"], 0)      # at_most alone -> allow 0 occurrences
        self.assertEqual(d["max_count"], 2)

    def test_dispatches_default_requires_one(self):
        d = desugar_check({"dispatches": "*"})
        self.assertEqual(d["min_count"], 1)      # neither given -> dispatch is mandatory

    def test_pipeline_milestone(self):
        d = desugar_check({"pipeline": ["reads graph", "runs probe", "dispatches *"]})
        self.assertEqual(d["type"], "Milestone")
        self.assertEqual(len(d["steps"]), 3)
        self.assertEqual(d["steps"][1]["action"], "run_command")   # runs → run_command
        self.assertEqual(d["steps"][2], {"action": "spawn_subagent"})  # dispatches * -> any spawn

    def test_before(self):
        d = desugar_check({"before": ["reads config", "runs deploy"]})
        self.assertEqual(d["type"], "Before")
        self.assertIn("regex", d["a"])
        self.assertEqual(d["b"]["action"], "run_command")

    def test_never_runs_forbidden(self):
        d = desugar_check({"never_runs": "rm -rf /", "as": "forbid dangerous command"})
        self.assertEqual(d["type"], "Forbidden")
        self.assertEqual(d["severity"], "forbidden")           # never_* is always forbidden
        self.assertEqual(d["match"]["action"], "run_command")

    def test_never_with_except(self):
        d = desugar_check({"never_runs": "python3 *_acp.py", "except": "cat *"})
        self.assertIn("exclude", d)

    def test_never_writes_covers_tool_shell_python(self):
        from rule.checkers import run_check
        from normalize.mapping import split_subcommands
        cp = desugar_check({"never_writes": "secret.json"})
        self.assertEqual(cp["severity"], "forbidden")
        def cmd(c): return {"idx": 0, "action": "run_command", "command": c, "subcommands": split_subcommands(c)}
        caught = [
            {"idx": 0, "action": "create_file", "path": "/x/secret.json"},
            cmd("echo x > secret.json"), cmd("cat d | tee secret.json"),
            cmd("python3 -c \"open('secret.json','w').write(x)\""),
        ]
        for a in caught:
            self.assertFalse(run_check(cp, [a]).passed, a)      # all should be caught (violation)
        # plain reads are not writes; writes inside scripts (path not in command) are missed -- known blind spot
        self.assertTrue(run_check(cp, [cmd("cat secret.json")]).passed)
        self.assertTrue(run_check(cp, [cmd("python3 gen.py")]).passed)

    def test_if_claims_then_runs(self):
        d = desugar_check({"if_claims": "LIVE", "then_runs": "*_acp.py"})
        self.assertEqual(d["type"], "IfThen")
        self.assertIn("regex", d["a"])
        self.assertEqual(d["b"]["action"], "run_command")

    def test_judge(self):
        d = desugar_check({"judge": "efficiency"})
        self.assertEqual(d["type"], "LLMJudge")
        self.assertEqual(d["dimension"], "efficiency")

    def test_unknown_intent_visible_error(self):
        d = desugar_check({"frobnicate": "x", "id": "bad"})
        self.assertEqual(d["type"], "__dsl_error__")
        r = run_check(d, [])
        self.assertFalse(r.passed)
        self.assertIn("rule error", r.reason)


class TestEndToEnd(unittest.TestCase):
    """Run intent rules against synthetic actions and verify the compiled rules decide correctly."""
    def _acts(self):
        return [
            {"idx": 0, "action": "run_command", "tool": "shell",
             "command": "cat /repo/.kiro/agents/foo.json", "completed": True, "args": {}},
            {"idx": 1, "action": "run_command", "tool": "shell",
             "command": "timeout 60 kiro-cli chat --agent foo <<< ping", "completed": True, "args": {}},
            {"idx": 2, "action": "spawn_subagent", "tool": "subagent",
             "pattern": "eval-x", "completed": True, "args": {}},
        ]

    def test_intent_rule_runs(self):
        spec = {"target_agent": "demo", "checks": [
            {"reads": "*.kiro/agents/*.json", "importance": "required", "as": "read config (cat counts)"},
            {"runs": "kiro-cli chat --agent *", "importance": "required", "as": "probe"},
            {"dispatches": "eval-*", "at_least": 1, "as": "dispatch"},
            {"never_runs": "rm -rf /", "as": "forbid dangerous"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            json.dump(spec, open(p, "w"))
            out = run(p, self._acts())
        self.assertEqual(out["verdict"], "PASS")           # first three hit; forbidden rule not violated
        self.assertEqual(out["health"], 1.0)
        # reads hits via cat (mechanism-independent)
        reads = [r for r in out["results"] if r["checkpoint_id"].startswith("reads_")][0]
        self.assertTrue(reads["passed"])


class TestReadsPrecisionVsTouches(unittest.TestCase):
    """reads only recognises true reads (excludes writes/deletes/mentions); touches recognises any appearance."""
    from rule.checkers import match as _m

    def _match(self, intent, action):
        from rule.rules_dsl import desugar_check
        from rule.checkers import match
        return match(action, desugar_check(intent)["match"])

    def test_reads_hits_real_read(self):
        self.assertTrue(self._match({"reads": "*agent.json"},
                                    {"action": "read_file", "path": "/x/agent.json"}))
        self.assertTrue(self._match({"reads": "*agent.json"},
                                    {"action": "run_command", "command": "cat /x/agent.json"}))

    def test_reads_excludes_write_rm_mention(self):
        for a in ({"action": "create_file", "path": "/x/agent.json"},
                  {"action": "modify_file", "path": "/x/agent.json"},
                  {"action": "run_command", "command": "rm /x/agent.json"},
                  {"action": "run_command", "command": "echo /x/agent.json"}):
            self.assertFalse(self._match({"reads": "*agent.json"}, a), a)

    def test_touches_hits_everything(self):
        for a in ({"action": "read_file", "path": "/x/agent.json"},
                  {"action": "create_file", "path": "/x/agent.json"},
                  {"action": "run_command", "command": "rm /x/agent.json"}):
            self.assertTrue(self._match({"touches": "*agent.json"}, a), a)

    def test_reads_catches_python_read_idioms(self):
        for cmd in ('python3 -c "import json; json.load(open(\'x_baseline.json\'))"',
                    'python3 -c "d=open(\'x_baseline.json\').read()"'):
            self.assertTrue(self._match({"reads": "*baseline.json"},
                                        {"action": "run_command", "command": cmd}), cmd)

    def test_reads_excludes_python_write(self):
        self.assertFalse(self._match({"reads": "*baseline.json"},
            {"action": "run_command", "command": "python3 -c \"open('out_baseline.json','w').write(x)\""}))


class TestRunsProgram(unittest.TestCase):
    """runs matches at subcommand head (prefixes stripped), excludes mentions/substrings."""
    def _match(self, intent, cmd):
        from rule.rules_dsl import desugar_check
        from rule.checkers import match
        from normalize.mapping import split_subcommands
        a = {"action": "run_command", "command": cmd, "subcommands": split_subcommands(cmd)}
        return match(a, desugar_check(intent)["match"])

    def test_hits_real_exec_and_prefixes(self):
        for cmd in ("pytest -v", "cd /x && pytest -v", "python3 -m pytest", "timeout 30 pytest"):
            self.assertTrue(self._match({"runs": "pytest*"}, cmd), cmd)

    def test_excludes_mention_and_substring(self):
        self.assertFalse(self._match({"runs": "pytest*"}, "echo 'will run pytest later'"))
        self.assertFalse(self._match({"runs": "pytest*"}, "git commit -m 'run pytest'"))
        self.assertFalse(self._match({"runs": "ls"}, "make build-tools"))  # ls appears inside the word "tools"

    def test_probe_with_timeout_prefix(self):
        self.assertTrue(self._match({"runs": "kiro-cli chat*--agent*"},
                                    "timeout 60 kiro-cli chat --agent foo <<< ping"))


class TestIntentMapEditable(unittest.TestCase):
    def test_map_has_read_sets(self):
        from rule.rules_dsl import load_intent_map
        m = load_intent_map()
        self.assertIn("cat", m["read_shell_verbs"])
        self.assertIn("read_file", m["read_actions"])

    def test_custom_verb_via_map(self):
        # user adds python3 to the map -> reads then recognises python reads
        import tempfile, os, json
        from rule.rules_dsl import load_intent_map, reads_regex, _map
        import rule.rules_dsl as dsl
        d = tempfile.mkdtemp(); p = os.path.join(d, "m.json")
        json.dump({"read_shell_verbs": ["python3"]}, open(p, "w"))
        m = load_intent_map(p)
        self.assertEqual(m["read_shell_verbs"], ["python3"])


if __name__ == "__main__":
    unittest.main()