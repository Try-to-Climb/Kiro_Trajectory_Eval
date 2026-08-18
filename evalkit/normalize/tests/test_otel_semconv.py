"""OTel GenAI semantic alignment layer tests."""
import unittest

from normalize.otel_semconv import (
    operation_of, action_attributes, span_name, span_status,
    OP_EXECUTE_TOOL, OP_INVOKE_AGENT,
)
from normalize.schema import Action


def _act(**kw):
    base = dict(idx=0, call_idx=0, op_idx=0, ts="2026-08-10T00:00:00Z",
                turn=1, run=1, raw_tool="fs_read", tool="read", action="read_file")
    base.update(kw)
    return Action(**base)


class TestOperationMapping(unittest.TestCase):
    def test_tool_actions_map_to_execute_tool(self):
        for a in ("read_file", "run_command", "modify_file", "search_content", "aws_call"):
            self.assertEqual(operation_of(a), OP_EXECUTE_TOOL)

    def test_spawn_maps_to_invoke_agent(self):
        self.assertEqual(operation_of("spawn_subagent"), OP_INVOKE_AGENT)


class TestAttributes(unittest.TestCase):
    def test_execute_tool_attrs(self):
        a = _act(tool="shell", raw_tool="execute_bash", action="run_command",
                 command="ls /tmp", tool_use_id="tu_1")
        at = a.otel_attributes()
        self.assertEqual(at["gen_ai.operation.name"], OP_EXECUTE_TOOL)
        self.assertEqual(at["gen_ai.tool.name"], "shell")
        self.assertEqual(at["gen_ai.tool.call.id"], "tu_1")
        self.assertEqual(at["gen_ai.tool.call.arguments"], {"command": "ls /tmp"})
        self.assertEqual(at["kiro.action"], "run_command")
        self.assertEqual(at["kiro.raw_tool"], "execute_bash")

    def test_invoke_agent_uses_pattern_as_agent_name(self):
        a = _act(tool="subagent", raw_tool="subagent", action="spawn_subagent",
                 pattern="eval-security-tester")
        at = a.otel_attributes()
        self.assertEqual(at["gen_ai.operation.name"], OP_INVOKE_AGENT)
        self.assertEqual(at["gen_ai.agent.name"], "eval-security-tester")
        # invoke_agent should not carry tool.name
        self.assertNotIn("gen_ai.tool.name", at)

    def test_error_maps_to_error_type(self):
        a = _act(error="Timeout", completed=False)
        self.assertEqual(a.otel_attributes()["error.type"], "Timeout")

    def test_blocked_is_kiro_extension(self):
        a = _act(blocked=True)
        self.assertTrue(a.otel_attributes()["kiro.blocked"])


class TestSpanMeta(unittest.TestCase):
    def test_span_name(self):
        self.assertEqual(span_name(_act(tool="read", action="read_file").to_dict()),
                         "execute_tool read")
        self.assertEqual(span_name(_act(action="spawn_subagent", pattern="sub").to_dict()),
                         "invoke_agent sub")

    def test_status(self):
        self.assertEqual(span_status(_act(completed=True).to_dict()), "OK")
        self.assertEqual(span_status(_act(error="x").to_dict()), "ERROR")
        self.assertEqual(span_status(_act(blocked=True).to_dict()), "ERROR")
        self.assertEqual(span_status(_act(completed=False).to_dict()), "UNSET")


if __name__ == "__main__":
    unittest.main()
