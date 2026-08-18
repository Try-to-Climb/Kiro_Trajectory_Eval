"""L1: official thinking -> action.reasoning; optional tool response gating."""
import json
import os
import tempfile
import unittest

from normalize import load_trace_from_official
from normalize.otel_semconv import action_attributes


def _write_official(dirpath, sid):
    """Build an official jsonl containing thinking + toolUse + ToolResults."""
    lines = [
        {"kind": "Prompt", "data": {"content": [{"data": "read the config"}],
                                     "meta": {"timestamp": "t0"}}},
        {"kind": "AssistantMessage", "data": {"content": [
            {"kind": "thinking", "data": {"text": "I need to read the config file before deciding"}},
            {"kind": "text", "data": "ok"},
            {"kind": "toolUse", "data": {"name": "fs_read", "toolUseId": "tu1",
                                          "input": {"operations": [{"mode": "Line", "path": "/cfg.json"}]}}},
        ]}},
        {"kind": "ToolResults", "data": {"results": {
            "tu1": {"tool": "fs_read", "result": {"Success": {"content": "file content ABC"}}}}}},
    ]
    p = os.path.join(dirpath, f"{sid}.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    return p


class TestReasoningResponse(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.sid = "sess-x"
        _write_official(self.d, self.sid)

    def test_reasoning_attached_from_thinking(self):
        ir = load_trace_from_official(self.sid, official_dir=self.d)
        self.assertTrue(ir.actions, "should produce at least one action")
        a = ir.actions[0]
        self.assertEqual(a.action, "read_file")
        self.assertEqual(a.reasoning, "I need to read the config file before deciding")

    def test_response_off_by_default(self):
        ir = load_trace_from_official(self.sid, official_dir=self.d)
        self.assertIsNone(ir.actions[0].response, "responses are not collected by default")

    def test_response_on_when_enabled(self):
        ir = load_trace_from_official(self.sid, official_dir=self.d, include_responses=True)
        resp = ir.actions[0].response
        self.assertIsNotNone(resp)
        self.assertIn("file content ABC", resp)

    def test_otel_projection(self):
        ir = load_trace_from_official(self.sid, official_dir=self.d, include_responses=True)
        attrs = action_attributes(ir.actions[0].to_dict())
        self.assertEqual(attrs["kiro.reasoning"], "I need to read the config file before deciding")
        self.assertIn("file content ABC", attrs["kiro.tool.response"])

    def test_otel_projection_no_response_when_off(self):
        ir = load_trace_from_official(self.sid, official_dir=self.d)  # default off
        attrs = action_attributes(ir.actions[0].to_dict())
        self.assertIn("kiro.reasoning", attrs)          # reasoning present by default
        self.assertNotIn("kiro.tool.response", attrs)   # response absent by default


if __name__ == "__main__":
    unittest.main()
