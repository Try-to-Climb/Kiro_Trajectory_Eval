"""Regression tests for issues found by the normalization layer audit (round 2).

Covers:
  - Path normalization (trailing slash / ./ / ../ / relative paths resolved via cwd)
  - subagent / use_subagent fan-out and role extraction
  - knowledge / summary parameter extraction
Run: python3 -m unittest discover -s normalize/tests -t .
"""

from __future__ import annotations

import unittest

from normalize import normalize_events

CWD = "/home/u/proj"


def pre(tool, tool_input, cwd=CWD, ts="2026-08-04T09:00:00.000Z"):
    return {"ts": ts, "event": "pre_tool_use", "session_id": "s",
            "tool": tool, "tool_input": tool_input, "cwd": cwd}


def post(tool, tool_input, cwd=CWD, ts="2026-08-04T09:00:01.000Z"):
    return {"ts": ts, "event": "post_tool_use", "session_id": "s", "tool": tool,
            "success": True, "response_size": 1, "tool_input": tool_input, "cwd": cwd}


def call(tool, tool_input, cwd=CWD):
    return [pre(tool, tool_input, cwd), post(tool, tool_input, cwd)]


def prompt():
    return {"ts": "2026-08-04T08:59:00.000Z", "event": "user_prompt",
            "session_id": "s", "prompt": "q", "cwd": CWD}


def rd(path, mode="Line"):
    return {"operations": [{"mode": mode, "path": path}]}


class TestPathNormalization(unittest.TestCase):
    """The same file must have only one representation; otherwise path-based counting/comparison distorts."""

    def test_trailing_slash_folded(self):
        ir = normalize_events([prompt(), *call("read", rd("/a/b/", mode="Directory"))])
        self.assertEqual(ir.actions[0].path, "/a/b")

    def test_dot_segments_folded(self):
        ir = normalize_events([prompt(), *call("read", rd("/a/./b/../c.py"))])
        self.assertEqual(ir.actions[0].path, "/a/c.py")

    def test_double_slash_folded(self):
        ir = normalize_events([prompt(), *call("write",
                                              {"command": "create", "path": "/a//b.py"})])
        self.assertEqual(ir.actions[0].path, "/a/b.py")

    def test_relative_path_resolved_against_cwd(self):
        ir = normalize_events([prompt(), *call("write",
                                              {"command": "create", "path": "src/x.py"})])
        self.assertEqual(ir.actions[0].path, f"{CWD}/src/x.py")

    def test_same_file_two_writings_collapse(self):
        """/a/b/c.py and a/b/c.py (relative) should be normalized to the same path."""
        ir = normalize_events([
            prompt(),
            *call("read", rd(f"{CWD}/a/c.py")),
            *call("read", rd("a/c.py")),
        ])
        self.assertEqual(len({a.path for a in ir.actions}), 1)

    def test_root_path_survives(self):
        ir = normalize_events([prompt(), *call("read", rd("/", mode="Directory"))])
        self.assertEqual(ir.actions[0].path, "/")

    def test_search_root_also_normalized(self):
        ir = normalize_events([prompt(), *call("grep", {"pattern": "x", "path": "/a/b/"})])
        self.assertEqual(ir.actions[0].root, "/a/b")


class TestSubagentDispatch(unittest.TestCase):
    """Trajectory evaluation of orchestrator agents must know which sub-agent was dispatched."""

    def test_subagent_stages_fan_out(self):
        ir = normalize_events([prompt(), *call("subagent", {
            "task": "build",
            "stages": [
                {"name": "plan", "role": "example-planner-agent"},
                {"name": "dev", "role": "example-dev-agent"},
            ]})])
        self.assertEqual(len(ir.actions), 2)                      # one call, two stages -> two actions
        self.assertEqual([a.action for a in ir.actions], ["spawn_subagent"] * 2)
        self.assertEqual([a.pattern for a in ir.actions],
                         ["example-planner-agent", "example-dev-agent"])
        self.assertEqual([a.command for a in ir.actions], ["plan", "dev"])
        self.assertEqual({a.call_idx for a in ir.actions}, {0})    # same call

    def test_subagent_without_stages_falls_back_to_task(self):
        ir = normalize_events([prompt(), *call("subagent", {"task": "do the thing"})])
        self.assertEqual(ir.actions[0].action, "spawn_subagent")
        self.assertEqual(ir.actions[0].pattern, "do the thing")

    def test_use_subagent_list_agents(self):
        ir = normalize_events([prompt(), *call("use_subagent", {"command": "ListAgents"})])
        self.assertEqual(ir.actions[0].action, "list_subagents")

    def test_use_subagent_invoke_fans_out(self):
        ir = normalize_events([prompt(), *call("use_subagent", {
            "command": "InvokeSubagents",
            "content": {"subagents": [{"agent_name": "a1", "query": "x"},
                                      {"agent_name": "a2", "query": "y"}]}})])
        self.assertEqual([a.action for a in ir.actions], ["spawn_subagent"] * 2)
        self.assertEqual([a.pattern for a in ir.actions], ["a1", "a2"])

    def test_use_subagent_alias_still_folds_tool_name(self):
        ir = normalize_events([prompt(), *call("use_subagent", {"command": "ListAgents"})])
        self.assertEqual(ir.actions[0].raw_tool, "use_subagent")
        self.assertEqual(ir.actions[0].tool, "subagent")

    def test_missing_agent_name_is_not_invented(self):
        """When the record has no agent_name, do not invent one; leave it empty faithfully."""
        ir = normalize_events([prompt(), *call("use_subagent", {
            "command": "InvokeSubagents",
            "content": {"subagents": [{"query": "no name given"}]}})])
        self.assertEqual(ir.actions[0].action, "spawn_subagent")
        self.assertIsNone(ir.actions[0].pattern)


class TestKnowledgeAndSummary(unittest.TestCase):

    def test_knowledge_search_query_extracted(self):
        ir = normalize_events([prompt(), *call("knowledge", {
            "command": "search", "query": "system constant", "context_id": "kb-1"})])
        a = ir.actions[0]
        self.assertEqual(a.action, "knowledge_search")
        self.assertEqual(a.pattern, "system constant")
        # context_id is an opaque ID and must not go into the root field that gets path-normalized
        self.assertIsNone(a.root)
        self.assertEqual(a.args["context_id"], "kb-1")

    def test_knowledge_show_has_no_params(self):
        ir = normalize_events([prompt(), *call("knowledge", {"command": "show"})])
        self.assertEqual(ir.actions[0].action, "knowledge_show")
        self.assertIsNone(ir.actions[0].pattern)

    def test_summary_task_description_extracted(self):
        ir = normalize_events([prompt(), *call("summary", {
            "taskDescription": "Evaluate sr-format case",
            "taskResult": "PASS", "contextSummary": "..."})])
        a = ir.actions[0]
        self.assertEqual(a.action, "summarize")
        self.assertEqual(a.pattern, "Evaluate sr-format case")


class TestNoSilentInformationLoss(unittest.TestCase):
    """Every action must have at least one semantic field, except tools that truly have no parameters."""

    NO_PARAM_ACTIONS = {"list_subagents", "knowledge_show"}

    def test_common_tools_all_surface_something(self):
        events = [prompt()]
        events += call("fs_read", rd("/a.md"))
        events += call("fs_write", {"command": "create", "path": "/b.md"})
        events += call("execute_bash", {"command": "ls"})
        events += call("grep", {"pattern": "x", "path": "/r"})
        events += call("glob", {"pattern": "**/*.py"})
        events += call("code", {"operation": "search_symbols", "symbol_name": "C", "path": "/r"})
        events += call("knowledge", {"command": "search", "query": "q"})
        events += call("summary", {"taskDescription": "t"})
        events += call("subagent", {"stages": [{"name": "s", "role": "r"}]})
        events += call("introspect", {"query": "q"})
        events += call("use_aws", {"service_name": "s3", "operation_name": "ListBuckets"})
        ir = normalize_events(events)
        blank = [a.action for a in ir.actions
                 if a.action not in self.NO_PARAM_ACTIONS
                 and not any([a.path, a.root, a.command, a.pattern])]
        self.assertEqual(blank, [], f"these actions have no semantic field: {blank}")
        self.assertEqual(ir.warnings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
