"""Regression tests for the official session record loader.

Driven by inline-built official .json/.jsonl in temp directories; no reliance
on real environment records. Covers: dual-source isomorphism, fan-out, result
backfill (Success/Error), parallel toolUse, turn splitting.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from normalize import load_trace_from_official


def _write_session(tmp: str, sid: str, jsonl_lines: list[dict],
                   meta: dict | None = None) -> None:
    with open(os.path.join(tmp, f"{sid}.jsonl"), "w", encoding="utf-8") as fh:
        for r in jsonl_lines:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(tmp, f"{sid}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta or {"session_id": sid, "cwd": "/w"}, fh)


def _prompt(text):
    return {"kind": "Prompt", "data": {"message_id": "m", "content": [{"kind": "text", "data": text}]}}


def _asst(tool_uses, text="", thinking=""):
    content = []
    if text:
        content.append({"kind": "text", "data": text})
    if thinking:
        content.append({"kind": "thinking", "data": {"text": thinking}})
    for tid, name, inp in tool_uses:
        content.append({"kind": "toolUse", "data": {"toolUseId": tid, "name": name, "input": inp}})
    return {"kind": "AssistantMessage", "data": {"message_id": "a", "content": content}}


def _results(pairs):
    """pairs: [(toolUseId, ok_bool_or_error_str)]"""
    results = {}
    for tid, outcome in pairs:
        if outcome is True:
            results[tid] = {"tool": {}, "result": {"Success": {"items": []}}}
        else:
            results[tid] = {"tool": {}, "result": {"Error": {"Custom": outcome}}}
    return {"kind": "ToolResults", "data": {"message_id": "r", "results": results}}


class TestOfficialLoader(unittest.TestCase):

    def _load(self, lines, meta=None):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "sid1", lines, meta)
        return load_trace_from_official("sid1", official_dir=tmp)

    def test_basic_sequence(self):
        ir = self._load([
            _prompt("do stuff"),
            _asst([("t1", "read", {"operations": [{"mode": "Line", "path": "/a"}]})]),
            _results([("t1", True)]),
        ])
        self.assertEqual(len(ir.actions), 1)
        self.assertEqual(ir.actions[0].action, "read_file")
        self.assertEqual(ir.actions[0].path, "/a")
        self.assertTrue(ir.actions[0].completed)
        self.assertEqual(ir.turns, 1)

    def test_modern_tool_names_need_no_alias(self):
        """Official uses modern names read/write/shell; canonicalization is a no-op."""
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "shell", {"command": "ls && pwd"})]),
            _results([("t1", True)]),
        ])
        a = ir.actions[0]
        self.assertEqual(a.tool, "shell")
        self.assertEqual(a.action, "run_command")
        self.assertEqual(a.subcommands, ["ls", "pwd"])

    def test_batch_read_fans_out(self):
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "read", {"operations": [
                {"mode": "Line", "path": "/a"},
                {"mode": "Line", "path": "/b"},
                {"mode": "Directory", "path": "/d"}]})]),
            _results([("t1", True)]),
        ])
        self.assertEqual([a.action for a in ir.actions],
                         ["read_file", "read_file", "list_dir"])
        self.assertEqual([a.op_idx for a in ir.actions], [0, 1, 2])
        self.assertEqual({a.call_idx for a in ir.actions}, {0})

    def test_error_result_marks_incomplete(self):
        """Error result -> completed=False with error info (hook has to guess; here it is explicit)."""
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "read", {"operations": [{"mode": "Directory", "path": "/tmp"}]})]),
            _results([("t1", "Permission denied")]),
        ])
        a = ir.actions[0]
        self.assertFalse(a.completed)
        self.assertIn("Permission denied", a.error)

    def test_captures_validation_rejected_call(self):
        """Validation-rejected calls are invisible to hook; official source captures them (with Error)."""
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "glob", {"pattern": "**/*.py"})]),
            _results([("t1", True)]),
            _asst([("t2", "read", {"operations": [{"mode": "Directory", "path": "./x"}]})]),
            _results([("t2", "Failed to parse the tool use: validation")]),
        ])
        self.assertEqual(len(ir.actions), 2)
        self.assertFalse(ir.actions[1].completed)

    def test_parallel_tooluses_in_one_message(self):
        """One AssistantMessage with multiple toolUses (parallel); each counts as one call."""
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "read", {"operations": [{"mode": "Line", "path": "/a"}]}),
                   ("t2", "glob", {"pattern": "*.py"}),
                   ("t3", "grep", {"pattern": "TODO", "path": "/r"})]),
            _results([("t1", True), ("t2", True), ("t3", True)]),
        ])
        self.assertEqual(len(ir.actions), 3)
        self.assertEqual({a.call_idx for a in ir.actions}, {0, 1, 2})

    def test_multi_turn(self):
        ir = self._load([
            _prompt("t1"),
            _asst([("t1", "read", {"operations": [{"mode": "Line", "path": "/a"}]})]),
            _results([("t1", True)]),
            _prompt("t2"),
            _asst([("t2", "shell", {"command": "ls"})]),
            _results([("t2", True)]),
        ])
        self.assertEqual(ir.turns, 2)
        self.assertEqual([a.turn for a in ir.actions], [1, 2])

    def test_result_backfill_by_tool_use_id(self):
        """Results are backfilled precisely by toolUseId; order does not matter."""
        ir = self._load([
            _prompt("q"),
            _asst([("t1", "read", {"operations": [{"mode": "Line", "path": "/a"}]}),
                   ("t2", "read", {"operations": [{"mode": "Line", "path": "/b"}]})]),
            _results([("t2", "boom"), ("t1", True)]),   # order scrambled
        ])
        by_path = {a.path: a for a in ir.actions}
        self.assertTrue(by_path["/a"].completed)
        self.assertFalse(by_path["/b"].completed)
        self.assertIn("boom", by_path["/b"].error)

    def test_missing_jsonl_warns(self):
        tmp = tempfile.mkdtemp()
        ir = load_trace_from_official("nope", official_dir=tmp)
        self.assertTrue(any("does not exist" in w for w in ir.warnings))
        self.assertEqual(ir.actions, [])


class TestDualSourceIsomorphic(unittest.TestCase):
    """Official and hook sources produce isomorphic Actions -- downstream need not distinguish sources."""

    def test_same_fields_present(self):
        tmp = tempfile.mkdtemp()
        _write_session(tmp, "sid1", [
            _prompt("q"),
            _asst([("t1", "fs_read", {"operations": [{"mode": "Line", "path": "/a"}]})]),
            _results([("t1", True)]),
        ])
        ir = load_trace_from_official("sid1", official_dir=tmp)
        a = ir.actions[0]
        # fs_read should fold into read (same rule as on the hook side)
        self.assertEqual(a.raw_tool, "fs_read")
        self.assertEqual(a.tool, "read")
        self.assertEqual(a.action, "read_file")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOrphanBackfill(unittest.TestCase):
    """Orphans caused by hook missing post are corrected when the official record confirms success."""

    def test_orphan_corrected_by_official(self):
        import json as _json
        import tempfile as _tf
        from normalize import normalize_file

        tmp = _tf.mkdtemp()
        # Official record: one successful shell
        _write_session(tmp, "sidx", [
            _prompt("q"),
            _asst([("t1", "shell", {"command": "cd /x && kiro-cli chat -a"})]),
            _results([("t1", True)]),
        ])
        # hook trace: the same shell has only pre, no post (orphan)
        hook_dir = os.path.join(tmp, "hooktrace", "sidx")
        os.makedirs(hook_dir)
        with open(os.path.join(hook_dir, "trace.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(_json.dumps({"ts": "2026-08-06T09:00:00.000Z", "event": "user_prompt",
                                  "session_id": "sidx", "prompt": "q", "cwd": "/w"}) + "\n")
            fh.write(_json.dumps({"ts": "2026-08-06T09:00:01.000Z", "event": "pre_tool_use",
                                  "session_id": "sidx", "tool": "shell",
                                  "tool_input": {"command": "cd /x && kiro-cli chat -a"},
                                  "cwd": "/w"}) + "\n")

        # Without enrichment: orphan
        ir0 = normalize_file(os.path.join(hook_dir, "trace.jsonl"), enrich=False)
        self.assertEqual(len(ir0.orphans), 1)

        # With enrichment: official confirms success -> corrected
        ir = normalize_file(os.path.join(hook_dir, "trace.jsonl"), official_dir=tmp)
        self.assertEqual(len(ir.orphans), 0)
        vf = [a for a in ir.actions if a.official_verified]
        self.assertEqual(len(vf), 1)
        self.assertTrue(vf[0].completed)
        self.assertTrue(any("corrected" in w for w in ir.warnings))

    def test_orphan_not_corrected_when_official_absent(self):
        import json as _json
        import tempfile as _tf
        from normalize import normalize_file
        tmp = _tf.mkdtemp()
        hook_dir = os.path.join(tmp, "sidy")
        os.makedirs(hook_dir)
        with open(os.path.join(hook_dir, "trace.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(_json.dumps({"ts": "2026-08-06T09:00:00.000Z", "event": "pre_tool_use",
                                  "session_id": "sidy", "tool": "shell",
                                  "tool_input": {"command": "sleep 999"}, "cwd": "/w"}) + "\n")
        ir = normalize_file(os.path.join(hook_dir, "trace.jsonl"), official_dir=tmp)
        # No official record; orphan persists
        self.assertEqual(len(ir.orphans), 1)
        self.assertFalse(ir.actions[0].official_verified)
