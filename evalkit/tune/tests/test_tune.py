"""Tests for the tune feature: agent loader, flag extractor, prompt composer,
and the CLI driver with a stub ACP caller. No real Kiro process is spawned."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from tune.agent_loader import (AgentInfo, _find_kiro_dir, _resolve_prompt_path,
                               load_agent_from_json)
from tune.extractor import (FlagContext, StepSnippet, build_contexts,
                            load_flags)
from tune import prompts as prompt_mod
from tune import runner as runner_mod


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _make_agent_tree(tmp: str) -> str:
    """Create a minimal `.kiro/agents/<name>.json` + prompt + skill tree."""
    kiro = os.path.join(tmp, ".kiro")
    agents_dir = os.path.join(kiro, "agents")
    prompts_dir = os.path.join(kiro, "prompts")
    skills_dir = os.path.join(kiro, "skills")
    for d in (agents_dir, prompts_dir, skills_dir):
        os.makedirs(d)
    with open(os.path.join(prompts_dir, "demo.md"), "w") as f:
        f.write("You are the demo orchestrator.\n\nRules:\n- never run `rm -rf /`\n")
    with open(os.path.join(skills_dir, "pick.md"), "w") as f:
        f.write("---\npurpose: cherry-pick commits\ntriggers: pick, cherry\n---\n\nBody.\n")
    with open(os.path.join(skills_dir, "compile.md"), "w") as f:
        f.write("---\npurpose: compile\n---\n")
    json_path = os.path.join(agents_dir, "demo.json")
    with open(json_path, "w") as f:
        json.dump({
            "name": "demo",
            "description": "demo orchestrator",
            "model": "claude-sonnet-4.6",
            "prompt": "file://../prompts/demo.md",
            "tools": ["execute_bash", "fs_read"],
        }, f)
    return json_path


# ---------------------------------------------------------------------------
# agent loader
# ---------------------------------------------------------------------------
class TestAgentLoader(unittest.TestCase):
    def test_resolve_prompt_path_handles_file_uri_and_relative(self):
        self.assertEqual(
            _resolve_prompt_path("file://../prompts/x.md", "/a/b/.kiro/agents"),
            "/a/b/.kiro/prompts/x.md")
        self.assertEqual(
            _resolve_prompt_path("/abs/x.md", "/anywhere"),
            "/abs/x.md")
        self.assertIsNone(_resolve_prompt_path("", "/x"))

    def test_find_kiro_dir_walks_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            kiro = os.path.join(tmp, ".kiro")
            inner = os.path.join(kiro, "agents")
            os.makedirs(inner)
            self.assertEqual(_find_kiro_dir(inner), kiro)
        self.assertIsNone(_find_kiro_dir("/no/such/dir"))

    def test_load_agent_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            jp = _make_agent_tree(tmp)
            info = load_agent_from_json(jp)
            self.assertEqual(info.name, "demo")
            self.assertEqual(info.model, "claude-sonnet-4.6")
            self.assertIn("execute_bash", info.tools)
            self.assertIn("demo orchestrator", info.prompt_text)
            self.assertEqual(len(info.skills), 2)
            self.assertEqual(info.skills[0][0], "compile")
            self.assertIn("purpose: compile", info.skills[0][1])

    def test_load_agent_rejects_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("[1, 2]")
            bad = f.name
        try:
            with self.assertRaises(ValueError):
                load_agent_from_json(bad)
        finally:
            os.unlink(bad)

    def test_load_agent_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_agent_from_json("/no/such/file.json")

    def test_prompt_block_truncates_long_prompt(self):
        info = AgentInfo(name="x", description="", model="", tools=(),
                         prompt_text="A" * 50000, prompt_path="/x/prompt.md",
                         skills=(), kiro_dir="")
        block = info.to_prompt_block(prompt_max_chars=1000)
        self.assertIn("truncated", block)
        # Head counts: original + "truncated" message, both bounded.
        self.assertLess(len(block), 2000)


# ---------------------------------------------------------------------------
# flag extractor
# ---------------------------------------------------------------------------
class TestExtractor(unittest.TestCase):
    def _write_flags(self, tmp: str) -> str:
        path = os.path.join(tmp, "f.json")
        with open(path, "w") as f:
            json.dump({
                "session": "demo1234",
                "flags": [
                    {"ref": "demo1234#5", "idx": 5, "action": "run_command",
                     "purpose": "p", "tag": "Duplication", "note": "repeat"},
                    {"ref": "demo1234#99", "idx": 99, "action": "run_command",
                     "tag": "Mistake", "note": "out of bounds"},
                ],
            }, f)
        return path

    def test_load_flags_rejects_bad_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.json")
            with open(p, "w") as f:
                f.write('{"not": "flags"}')
            with self.assertRaises(ValueError):
                load_flags(p)

    def test_build_contexts_attaches_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = self._write_flags(tmp)
            data = load_flags(fp)
            actions = [
                {"ref": f"demo1234#{i}", "idx": i, "turn": 1,
                 "action": "run_command", "purpose": f"p{i}",
                 "command": f"echo {i}"}
                for i in range(10)
            ]
            ctxs = build_contexts(data["flags"], actions, window=2)
            self.assertEqual(len(ctxs), 2)
            # First flag at idx 5 -> before=[3,4], flagged=5, after=[6,7]
            c0 = ctxs[0]
            self.assertEqual([s.idx for s in c0.before], [3, 4])
            self.assertEqual(c0.flagged.idx, 5)
            self.assertEqual([s.idx for s in c0.after], [6, 7])
            # Second flag has no matching action -> no snippets
            c1 = ctxs[1]
            self.assertIsNone(c1.flagged)
            self.assertEqual(c1.before, [])
            self.assertEqual(c1.note, "out of bounds")

    def test_window_clamps_at_boundaries(self):
        actions = [{"ref": f"s#{i}", "idx": i, "turn": 1,
                    "action": "x"} for i in range(3)]
        flags = [{"ref": "s#0", "tag": "Standout", "note": ""}]
        c = build_contexts(flags, actions, window=5)[0]
        self.assertEqual(c.before, [])      # nothing before idx 0
        self.assertEqual([s.idx for s in c.after], [1, 2])

    def test_embedded_context_is_used_without_trajectory(self):
        """Older flat shape: `context.flagged` carries the snapshot. Kept
        for backward compatibility with existing flag files."""
        flags = [{
            "ref": "s#5", "tag": "Duplication", "note": "repeat",
            "context": {
                "window": 2,
                "flagged": {"idx": 5, "turn": 1, "action": "run_command",
                            "purpose": "p5", "command": "echo 5"},
                "before": [
                    {"idx": 3, "turn": 1, "action": "x", "purpose": "p3"},
                    {"idx": 4, "turn": 1, "action": "x", "purpose": "p4"},
                ],
                "after": [
                    {"idx": 6, "turn": 1, "action": "x", "purpose": "p6"},
                ],
            },
        }]
        ctxs = build_contexts(flags, actions=[], window=2)
        self.assertEqual(len(ctxs), 1)
        c = ctxs[0]
        self.assertEqual(c.flagged.idx, 5)
        self.assertEqual(c.flagged.purpose, "p5")
        self.assertEqual([s.idx for s in c.before], [3, 4])
        self.assertEqual([s.idx for s in c.after], [6])

    def test_layered_shape_is_used_without_trajectory(self):
        """New layered shape puts the user signal (tag/note) first, the
        flagged step in `step`, and the neighbours in `context`. No
        trajectory needed."""
        flags = [{
            "tag": "Duplication",
            "note": "already done in the previous step",
            "ts": "2026-11-03T10:00:00Z",
            "step": {"ref": "s#5", "idx": 5, "turn": 1,
                     "action": "run_command", "purpose": "p5",
                     "command": "echo 5"},
            "context": {
                "window": 2,
                "before": [
                    {"ref": "s#3", "idx": 3, "turn": 1,
                     "action": "x", "purpose": "p3"},
                    {"ref": "s#4", "idx": 4, "turn": 1,
                     "action": "x", "purpose": "p4"},
                ],
                "after": [
                    {"ref": "s#6", "idx": 6, "turn": 1,
                     "action": "x", "purpose": "p6"},
                ],
            },
        }]
        ctxs = build_contexts(flags, actions=[], window=2)
        self.assertEqual(len(ctxs), 1)
        c = ctxs[0]
        self.assertEqual(c.ref, "s#5")
        self.assertEqual(c.note, "already done in the previous step")
        self.assertEqual(c.flagged.idx, 5)
        self.assertEqual(c.flagged.purpose, "p5")
        self.assertEqual([s.idx for s in c.before], [3, 4])
        self.assertEqual([s.idx for s in c.after], [6])

    def test_embedded_context_takes_priority_over_trajectory(self):
        """If both are present, the embedded snapshot wins -- it is what the
        user actually saw when they flagged, and the trajectory may have
        drifted (file moved, re-normalised, etc.)."""
        flags = [{
            "ref": "s#1", "tag": "Mistake", "note": "n",
            "context": {
                "flagged": {"idx": 1, "action": "x",
                            "purpose": "FROM-EMBEDDED-CONTEXT"},
                "before": [], "after": [],
            },
        }]
        actions = [{"ref": "s#1", "idx": 1, "turn": 1, "action": "x",
                    "purpose": "FROM-TRAJECTORY"}]
        c = build_contexts(flags, actions, window=2)[0]
        self.assertEqual(c.flagged.purpose, "FROM-EMBEDDED-CONTEXT")


# ---------------------------------------------------------------------------
# prompt composer
# ---------------------------------------------------------------------------
class TestPrompt(unittest.TestCase):
    def test_compose_mentions_each_tag_and_flag(self):
        info = AgentInfo(name="demo", description="", model="", tools=(),
                         prompt_text="", prompt_path="", skills=(), kiro_dir="")
        ctxs = [
            FlagContext(ref="s#1", tag="Duplication", note="n1"),
            FlagContext(ref="s#2", tag="Standout", note=""),
        ]
        p = prompt_mod.compose(info, ctxs, session_id="demo1234")
        self.assertIn("demo1234", p)
        self.assertIn("Standout", p)
        self.assertIn("Duplication", p)
        self.assertIn("s#1", p)
        self.assertIn("s#2", p)
        self.assertIn("n1", p)
        # The agent name section is present even with an empty prompt.
        self.assertIn("# Agent: demo", p)

    def test_glossary_covers_all_four_tags(self):
        for tag in ("Standout", "Violation", "Duplication", "Mistake"):
            self.assertIn(tag, prompt_mod.TAG_GLOSSARY)


# ---------------------------------------------------------------------------
# runner (no real Kiro)
# ---------------------------------------------------------------------------
class TestRunner(unittest.TestCase):
    def test_dry_run_builds_prompt_without_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({"session": "demo1234", "flags": []}, f)
            # No flags -> no trajectory load needed; no caller -> dry run.
            composed, reply = runner_mod.run(flags, agent_json,
                                             caller=None, window=1)
            self.assertEqual(reply, "")
            self.assertIn("demo", composed)
            self.assertIn("Agent definition", composed)

    def test_run_invokes_caller_with_composed_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({"session": "demo1234", "flags": []}, f)
            seen = {}
            def stub(text: str) -> str:
                seen["prompt"] = text
                return "ok"
            composed, reply = runner_mod.run(flags, agent_json,
                                             caller=stub, window=1)
            self.assertEqual(reply, "ok")
            self.assertEqual(seen["prompt"], composed)

    def test_run_loads_trajectory_only_when_flags_present(self):
        """Trajectory loading can be expensive; empty flags skip it."""
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({"session": "demo1234", "flags": []}, f)
            # If normalize.load_trace_from_official is called at all, this
            # test fails with a TypeError (the mock has no side_effect).
            with mock.patch("normalize.load_trace_from_official") as m:
                m.side_effect = AssertionError("should not be called")
                runner_mod.run(flags, agent_json, caller=None)
                m.assert_not_called()

    def test_run_skips_trajectory_when_all_flags_self_contained(self):
        """If every flag carries its own `step` (new layered shape), no
        trajectory load is needed -- `--official-dir` becomes optional and
        the flag file is self-sufficient for the whole pipeline."""
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({
                    "session": "demo1234",
                    "flags": [{
                        "tag": "Duplication", "note": "r",
                        "step": {"ref": "demo1234#1", "idx": 1,
                                 "action": "run_command",
                                 "command": "echo"},
                        "context": {"before": [], "after": []},
                    }],
                }, f)
            with mock.patch("normalize.load_trace_from_official") as m:
                m.side_effect = AssertionError("should not be called")
                composed, _ = runner_mod.run(flags, agent_json, caller=None)
                m.assert_not_called()
            self.assertIn("demo1234#1", composed)
            self.assertIn("echo", composed)

    def test_cli_writes_prompt_sidecar_alongside_out(self):
        """`--out suggestions.md` auto-writes `suggestions.prompt.md` next to
        it so the user can audit exactly what was sent to Kiro. This is a
        documented contract -- changing it breaks reproducibility."""
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({"session": "demo1234", "flags": []}, f)
            out = os.path.join(tmp, "sug.md")
            # Patch the Kiro ACP caller so no process is spawned.
            with mock.patch("tune.runner.default_kiro_caller",
                            return_value=lambda t: "REPLY-BODY"):
                rc = runner_mod.main([
                    "--flags", flags, "--agent", agent_json,
                    "--out", out, "--quiet",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(open(out).read(), "REPLY-BODY")
            sidecar = os.path.join(tmp, "sug.prompt.md")
            self.assertTrue(os.path.exists(sidecar),
                            "prompt sidecar missing: " + sidecar)
            self.assertIn("# Agent: demo", open(sidecar).read())

    def test_cli_dry_run_does_not_write_sidecar(self):
        """Dry run's `--out` IS the prompt; adding a `.prompt.md` next to it
        would be redundant and confusing."""
        with tempfile.TemporaryDirectory() as tmp:
            agent_json = _make_agent_tree(tmp)
            flags = os.path.join(tmp, "f.json")
            with open(flags, "w") as f:
                json.dump({"session": "demo1234", "flags": []}, f)
            out = os.path.join(tmp, "p.md")
            rc = runner_mod.main([
                "--flags", flags, "--agent", agent_json,
                "--out", out, "--no-llm", "--quiet",
            ])
            self.assertEqual(rc, 0)
            self.assertIn("# Agent: demo", open(out).read())
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "p.prompt.md")))


if __name__ == "__main__":
    unittest.main()
