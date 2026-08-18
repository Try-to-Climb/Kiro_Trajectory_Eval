"""Regression tests for issues found during e2e testing.

Source: real parameter shapes measured with the hook-coverage-test agent
running kiro-cli 2.11.0.
Run: python3 -m unittest discover -s normalize/tests -t .
"""

from __future__ import annotations

import unittest

from normalize import normalize_events


def pre(tool, tool_input, ts="2026-08-04T08:00:00.000Z"):
    return {"ts": ts, "event": "pre_tool_use", "session_id": "s",
            "tool": tool, "tool_input": tool_input}


def post(tool, tool_input, ts="2026-08-04T08:00:01.000Z"):
    return {"ts": ts, "event": "post_tool_use", "session_id": "s", "tool": tool,
            "success": True, "response_size": 10, "tool_input": tool_input}


def blocked(tool, tool_input, reason="denied", ts="2026-08-04T08:00:01.000Z"):
    return {"ts": ts, "event": "tool_blocked", "session_id": "s", "tool": tool,
            "reason": reason, "tool_input": tool_input}


def prompt(text="q"):
    return {"ts": "2026-07-04T07:59:00.000Z", "event": "user_prompt",
            "session_id": "s", "prompt": text}



def call(tool, tool_input):
    """One successful call = pre + post. Missing post is treated as an orphan."""
    return [pre(tool, tool_input), post(tool, tool_input)]


class TestParamNamingStyles(unittest.TestCase):
    """kiro-cli 2.11.0 uses snake_case (str_replace); old traces use camelCase (strReplace)."""

    def test_snake_case_str_replace(self):
        ir = normalize_events([prompt(), *call("fs_write",
                                            {"command": "str_replace", "path": "/a.md"})])
        self.assertEqual(ir.actions[0].action, "modify_file")
        self.assertEqual(ir.warnings, [])

    def test_camel_case_strReplace(self):
        ir = normalize_events([prompt(), *call("write",
                                            {"command": "strReplace", "path": "/a.md"})])
        self.assertEqual(ir.actions[0].action, "modify_file")
        self.assertEqual(ir.warnings, [])

    def test_both_styles_agree(self):
        a = normalize_events([prompt(), *call("fs_write", {"command": "str_replace", "path": "/x"})])
        b = normalize_events([prompt(), *call("write", {"command": "strReplace", "path": "/x"})])
        self.assertEqual(a.actions[0].action, b.actions[0].action)

    def test_read_mode_case_insensitive(self):
        ir = normalize_events([prompt(), *call("fs_read", {"operations": [
            {"mode": "line", "path": "/a"}, {"mode": "DIRECTORY", "path": "/d"}]})])
        self.assertEqual([a.action for a in ir.actions], ["read_file", "list_dir"])
        self.assertEqual(ir.warnings, [])

    def test_fs_write_alias(self):
        ir = normalize_events([prompt(), *call("fs_write",
                                            {"command": "create", "path": "/n.md"})])
        self.assertEqual(ir.actions[0].tool, "write")
        self.assertEqual(ir.actions[0].action, "create_file")


class TestBlockedVsOrphan(unittest.TestCase):
    """Policy-blocked and execution timeout both present as "pre without post"; must distinguish via tool_blocked."""

    def test_blocked_is_not_orphan(self):
        ir = normalize_events([
            prompt(),
            pre("use_aws", {"service_name": "sts", "operation_name": "get-caller-identity"}),
            blocked("use_aws", {"service_name": "sts", "operation_name": "get-caller-identity"},
                    reason="Tool 'use_aws' denied by policy"),
        ])
        a = ir.actions[0]
        self.assertTrue(a.blocked)
        self.assertFalse(a.completed)
        self.assertEqual(len(ir.orphans), 0)
        self.assertEqual(ir.warnings, [])

    def test_orphan_without_blocked_event(self):
        ir = normalize_events([prompt(), pre("execute_bash", {"command": "slow"})])
        self.assertFalse(ir.actions[0].blocked)
        self.assertEqual(len(ir.orphans), 1)

    def test_blocked_and_orphan_coexist(self):
        ir = normalize_events([
            prompt(),
            pre("execute_bash", {"command": "chmod 777 /x"}),
            blocked("execute_bash", {"command": "chmod 777 /x"}),
            pre("execute_bash", {"command": "hangs"}),
        ])
        self.assertTrue(ir.actions[0].blocked)
        self.assertEqual(len(ir.orphans), 1)
        self.assertEqual(ir.orphans[0].command, "hangs")


class TestCodeAndAwsSemantics(unittest.TestCase):
    """code's file_path is a specific file; path is a search root. They must not be mixed."""

    def test_code_document_symbols(self):
        ir = normalize_events([prompt(), *call("code", {
            "operation": "get_document_symbols", "file_path": "/calc.py"})])
        a = ir.actions[0]
        self.assertEqual(a.action, "code_get_document_symbols")
        self.assertEqual(a.path, "/calc.py")
        self.assertIsNone(a.root)

    def test_code_search_symbols_root_not_path(self):
        ir = normalize_events([prompt(), *call("code", {
            "operation": "search_symbols", "symbol_name": "Calculator", "path": "/repo"})])
        a = ir.actions[0]
        self.assertEqual(a.action, "code_search_symbols")
        self.assertIsNone(a.path)          # search root must not land in path
        self.assertEqual(a.root, "/repo")
        self.assertEqual(a.pattern, "Calculator")

    def test_aws_call_surfaces_service_and_op(self):
        ir = normalize_events([prompt(), *call("use_aws", {
            "service_name": "sts", "operation_name": "get-caller-identity",
            "region": "us-east-1"})])
        self.assertEqual(ir.actions[0].action, "aws_call")
        self.assertEqual(ir.actions[0].pattern, "sts/get-caller-identity")

    def test_introspect_query(self):
        ir = normalize_events([prompt(), *call("introspect", {"query": "slash commands"})])
        self.assertEqual(ir.actions[0].action, "docs_query")
        self.assertEqual(ir.actions[0].pattern, "slash commands")


class TestRealSequenceShape(unittest.TestCase):
    """Reproduce the observed shape: 16 calls -> 19 actions."""

    def test_sixteen_calls_expand_to_nineteen_actions(self):
        ev = [prompt()]
        ev += call("fs_read", {"operations": [{"mode": "Line", "path": "/notes.md"}]})
        ev += call("fs_read", {"operations": [
            {"mode": "Line", "path": f"/f{i}"} for i in range(4)]})          # batch of 4
        ev += call("fs_read", {"operations": [{"mode": "Directory", "path": "/sb"}]})
        ev += call("glob", {"pattern": "**/*.py", "path": "/sb"})
        ev += call("grep", {"pattern": "TODO", "path": "/sb"})
        ev += call("code", {"operation": "get_document_symbols", "file_path": "/c.py"})
        ev += call("code", {"operation": "search_symbols", "symbol_name": "C", "path": "/sb"})
        ev += call("execute_bash", {"command": "wc -l /notes.md"})
        ev += call("execute_bash", {"command": "cd /sb && ls -1 && echo DONE"})
        ev += call("fs_write", {"command": "create", "path": "/report.md"})
        ev += call("fs_write", {"command": "str_replace", "path": "/notes.md"})
        ev += call("fs_write", {"command": "insert", "path": "/config.yaml"})
        # Two blocked calls: only pre + tool_blocked, no post
        ev += [pre("execute_bash", {"command": "chmod 777 /notes.md"}),
               blocked("execute_bash", {"command": "chmod 777 /notes.md"})]
        ev += [pre("use_aws", {"service_name": "sts", "operation_name": "get-caller-identity"}),
               blocked("use_aws", {"service_name": "sts", "operation_name": "get-caller-identity"})]
        ev += call("introspect", {"query": "slash"})
        ev += call("knowledge", {"command": "show"})

        ir = normalize_events(ev)
        self.assertEqual(len({a.call_idx for a in ir.actions}), 16)
        self.assertEqual(len(ir.actions), 19)
        self.assertEqual(sum(1 for a in ir.actions if a.blocked), 2)
        self.assertEqual(len(ir.orphans), 0)
        self.assertEqual(ir.warnings, [])
        # Compound command split into 3 pieces
        cmd = next(a for a in ir.actions if a.command and "&&" in a.command)
        self.assertEqual(cmd.subcommands, ["cd /sb", "ls -1", "echo DONE"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
