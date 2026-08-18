"""OTLP/JSON exporter tests."""
import json
import re
import unittest

from normalize.schema import Action, TraceIR
from normalize.otel_export import build_otlp, to_otlp_json, _trace_id, _span_id

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _act(idx, action, run=1, **kw):
    base = dict(idx=idx, call_idx=idx, op_idx=0, ts="2026-08-10T00:00:0%d.000Z" % (idx % 10),
                turn=1, run=run, raw_tool="fs_read", tool="read", action=action)
    base.update(kw)
    return Action(**base)


def _ir(actions, **kw):
    base = dict(session_id="sess-123", source="/x/trace.jsonl", agent_name="agent-eval")
    base.update(kw)
    ir = TraceIR(**base)
    ir.actions = actions
    return ir


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.ir = _ir([
            _act(0, "run_command", tool="shell", raw_tool="execute_bash",
                 command="ls /tmp", duration_ms=12, completed=True, tool_use_id="tu0"),
            _act(1, "read_file", path="/a.txt", completed=True),
            _act(2, "spawn_subagent", tool="subagent", raw_tool="subagent",
                 pattern="sub-x", completed=True),
            _act(3, "modify_file", tool="write", raw_tool="fs_write",
                 path="/b.txt", completed=False, error="boom"),
        ])
        self.otlp = build_otlp(self.ir, source="hook")
        self.spans = self.otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]

    def test_envelope(self):
        rs = self.otlp["resourceSpans"][0]
        keys = {a["key"] for a in rs["resource"]["attributes"]}
        self.assertIn("service.name", keys)
        self.assertIn("kiro.trace.source", keys)
        self.assertIn("gen_ai.conversation.id", keys)

    def test_root_plus_action_spans(self):
        # 1 run -> 1 root span + 4 action spans = 5
        self.assertEqual(len(self.spans), 5)
        roots = [s for s in self.spans if "parentSpanId" not in s]
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0]["name"].startswith("invoke_agent"))

    def test_ids_wellformed_and_parented(self):
        ids = {s["spanId"] for s in self.spans}
        for s in self.spans:
            self.assertRegex(s["traceId"], _HEX32)
            self.assertRegex(s["spanId"], _HEX16)
            if "parentSpanId" in s:
                self.assertIn(s["parentSpanId"], ids)   # parent ref resolvable

    def test_same_trace_id(self):
        self.assertEqual(len({s["traceId"] for s in self.spans}), 1)

    def test_status_mapping(self):
        by_name = {s["name"]: s for s in self.spans}
        self.assertEqual(by_name["execute_tool shell"]["status"]["code"], 1)   # OK
        # Failed modify_file(write) -> ERROR
        err = [s for s in self.spans if s["name"].startswith("execute_tool write")][0]
        self.assertEqual(err["status"]["code"], 2)

    def test_arguments_json_encoded(self):
        sh = [s for s in self.spans if s["name"] == "execute_tool shell"][0]
        args = [a for a in sh["attributes"] if a["key"] == "gen_ai.tool.call.arguments"][0]
        # Complex value encoded as JSON string
        parsed = json.loads(args["value"]["stringValue"])
        self.assertEqual(parsed["command"], "ls /tmp")

    def test_int_as_string(self):
        # startTimeUnixNano must be a string (OTLP int64 convention)
        self.assertIsInstance(self.spans[0]["startTimeUnixNano"], str)
        # kiro.idx and similar int attributes are also encoded as intValue strings
        act = [s for s in self.spans if s.get("parentSpanId")][0]
        idxs = [a for a in act["attributes"] if a["key"] == "kiro.idx"]
        if idxs:
            self.assertIsInstance(idxs[0]["value"]["intValue"], str)

    def test_start_le_end(self):
        for s in self.spans:
            self.assertLessEqual(int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"]))


class TestMultiRun(unittest.TestCase):
    def test_multi_run_multiple_roots(self):
        ir = _ir([_act(0, "read_file", run=1, completed=True),
                  _act(1, "read_file", run=2, completed=True)])
        spans = build_otlp(ir)["resourceSpans"][0]["scopeSpans"][0]["spans"]
        roots = [s for s in spans if "parentSpanId" not in s]
        self.assertEqual(len(roots), 2)   # one root per run


class TestDeterminism(unittest.TestCase):
    def test_ids_stable(self):
        self.assertEqual(_trace_id("s"), _trace_id("s"))
        self.assertEqual(_span_id("s", "act", 1, 2), _span_id("s", "act", 1, 2))
        self.assertNotEqual(_span_id("s", "act", 1, 2), _span_id("s", "act", 1, 3))


class TestOfficialNoTimestamps(unittest.TestCase):
    def test_synth_time_when_ts_empty(self):
        # Official source ts empty -> synthesize ordered times; must not crash; start<=end
        ir = _ir([_act(0, "read_file", ts="", completed=True),
                  _act(1, "read_file", ts="", completed=True)])
        spans = build_otlp(ir, source="official")["resourceSpans"][0]["scopeSpans"][0]["spans"]
        for s in spans:
            self.assertLessEqual(int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"]))
        # Valid JSON
        json.dumps(build_otlp(ir, source="official"))


if __name__ == "__main__":
    unittest.main()
