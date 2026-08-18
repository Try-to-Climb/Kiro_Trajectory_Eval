"""eval-agent unit tests: deterministic parts + validation gate. Does not touch
the LLM backend (all callers are stubbed).

Run: cd eval-agent && python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm                                   # noqa: E402
import schema                                # noqa: E402
import steps                                 # noqa: E402
from evidence import api                     # noqa: E402
from evidence.loader import RunTree, SessionNode   # noqa: E402

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Build a fake run tree (no disk reads)
# ---------------------------------------------------------------------------
def mk_action(sid, idx, action, *, turn=1, command=None, path=None, pattern=None,
              purpose=None, reasoning=None, completed=True, blocked=False):
    return {
        "sid": sid, "idx": idx, "ref": f"{sid[:8]}#{idx}", "turn": turn, "run": 1,
        "action": action, "tool": {"read_file": "read", "create_file": "write",
                                   "run_command": "shell",
                                   "spawn_subagent": "subagent"}.get(action, action),
        "raw_tool": action, "command": command, "path": path, "pattern": pattern,
        "root": None, "subcommands": [], "reasoning": reasoning or "",
        "response": None, "completed": completed, "blocked": blocked,
        "duration_ms": None, "error": None, "ts": "",
        "args": {"__tool_use_purpose": purpose} if purpose else {},
        "depth": 0 if sid == "root0000-0000" else 1,
    }


def mk_tree(actions, turns=1, prompts=None, responses=None,
            window=(None, None), children=()):
    root = "root0000-0000-0000-0000-000000000000"
    ir = types.SimpleNamespace(
        turns=turns, prompts=prompts or ["do something"], responses=responses or ["done"],
        actions=[], agent_name="fake-agent", official=None)
    nodes = {root: SessionNode(sid=root, agent_name="fake-agent", parent=None, depth=0,
                              ir=ir, time_window=window)}
    for csid, cagent in children:
        nodes[csid] = SessionNode(sid=csid, agent_name=cagent, parent=root, depth=1,
                                  ir=ir, time_window=window)
    t = RunTree(root=root, nodes=nodes, actions=actions)
    return t


ROOT = "root0000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
class TestRetrieve(unittest.TestCase):
    def setUp(self):
        self.tree = mk_tree([
            mk_action(ROOT, 0, "read_file", path="/a/agent.py"),
            mk_action(ROOT, 1, "run_command",
                      command="python3 - <<'PY'\njson.dump(x, open('test_cases_sec.json','w'))\nPY"),
            mk_action(ROOT, 2, "run_command", command="ls -1"),
            mk_action(ROOT, 3, "create_file", path="/a/gen.py",
                      purpose="script that generates cases"),
        ])

    def test_score_is_distinct_anchor_count(self):
        r = api.retrieve(self.tree, ["test_cases", ".json", "cases"], k=5)
        top = r["rows"][0]
        self.assertEqual(top["ref"], f"{ROOT[:8]}#1")
        self.assertEqual(top["score"], 3.0)          # all three anchors hit
        self.assertEqual(sorted(top["anchors_hit"]), [".json", "cases", "test_cases"])

    def test_df_counts_actions_not_occurrences(self):
        df = api.anchor_df(self.tree, ["test_cases", "python3"])
        self.assertEqual(df["test_cases"], 1)
        self.assertEqual(df["python3"], 1)

    def test_purpose_is_searchable(self):
        """__tool_use_purpose must join the retrieval surface (DESIGN.md optimization 4)."""
        r = api.retrieve(self.tree, ["generates cases"], k=5)
        self.assertEqual([x["ref"] for x in r["rows"]], [f"{ROOT[:8]}#3"])

    def test_idf_suppresses_common_anchor(self):
        tree = mk_tree([mk_action(ROOT, i, "run_command", command="x.json") for i in range(9)]
                       + [mk_action(ROOT, 9, "run_command", command="rare_token x.json")])
        cnt = api.retrieve(tree, ["rare_token", ".json"], k=3, score_mode="count")
        idf = api.retrieve(tree, ["rare_token", ".json"], k=3, score_mode="idf")
        self.assertEqual(cnt["rows"][0]["ref"], f"{ROOT[:8]}#9")
        self.assertEqual(idf["rows"][0]["ref"], f"{ROOT[:8]}#9")
        # Under idf, the rare anchor's contribution should be markedly larger than the common one's
        self.assertGreater(idf["rows"][0]["score"], idf["rows"][1]["score"] * 1.5)

    def test_empty_anchors_is_safe(self):
        r = api.retrieve(self.tree, [], k=5)
        self.assertEqual(r["rows"], [])


# ---------------------------------------------------------------------------
# scope semantics
# ---------------------------------------------------------------------------
class TestScope(unittest.TestCase):
    def test_idx_bounds_apply_to_root_only(self):
        kid = "child000-0000-0000-0000-000000000000"
        tree = mk_tree(
            [mk_action(ROOT, 1, "run_command", command="early"),
             mk_action(ROOT, 9, "run_command", command="late"),
             mk_action(kid, 1, "run_command", command="kid-early")],
            children=[(kid, None)])
        rows = api._scope(tree.actions, {"idx_gte": 5, "_root_sid": ROOT})
        refs = {a["ref"] for a in rows}
        self.assertIn(f"{ROOT[:8]}#9", refs)
        self.assertNotIn(f"{ROOT[:8]}#1", refs)      # root session bound by idx lower bound
        self.assertIn(f"{kid[:8]}#1", refs)          # sub-session not constrained by parent's idx base

    def test_no_scope_returns_all(self):
        tree = mk_tree([mk_action(ROOT, 1, "read_file", path="/x")])
        self.assertEqual(len(api._scope(tree.actions, None)), 1)


# ---------------------------------------------------------------------------
# hard_check
# ---------------------------------------------------------------------------
class TestHardCheck(unittest.TestCase):
    def test_read_path_direct(self):
        tree = mk_tree([mk_action(ROOT, 0, "read_file",
                                  path="/repo/.kiro/agents/example-dev-agent.json")])
        hc = api.hard_check(tree, {"read_path": ".kiro/agents/example-dev-agent"})
        self.assertEqual(hc["tier"], "direct")
        self.assertEqual(len(hc["hits"]), 1)

    def test_invoke_agent_spawn_exact(self):
        tree = mk_tree([mk_action(ROOT, 0, "spawn_subagent", pattern="eval-security-tester"),
                        mk_action(ROOT, 1, "spawn_subagent", pattern="target-agent")])
        hc = api.hard_check(tree, {"invoke_agent": "target-agent"})
        self.assertEqual(hc["tier"], "direct")
        self.assertEqual([h["ref"] for h in hc["hits"]], [f"{ROOT[:8]}#1"])

    def test_invoke_agent_by_named_flag_in_child(self):
        """A real live call often appears in the child session as --target <name> (P7)."""
        kid = "child000-0000-0000-0000-000000000000"
        tree = mk_tree(
            [mk_action(ROOT, 0, "spawn_subagent", pattern="eval-security-tester"),
             mk_action(kid, 7, "run_command",
                       command="python3 example_target_runner.py --target example-dev-agent --cases-dir x")],
            children=[(kid, None)])
        hc = api.hard_check(tree, {"invoke_agent": "example-dev-agent"})
        self.assertEqual(hc["tier"], "cross_session")
        self.assertTrue(hc.get("invoke_by_flag"))
        self.assertEqual([h["ref"] for h in hc["hits"]], [f"{kid[:8]}#7"])

    def test_invoke_agent_miss(self):
        tree = mk_tree([mk_action(ROOT, 0, "spawn_subagent", pattern="other")])
        hc = api.hard_check(tree, {"invoke_agent": "target"})
        self.assertEqual(hc["tier"], "none")
        self.assertEqual(hc["hits"], [])


# ---------------------------------------------------------------------------
# crosscheck
# ---------------------------------------------------------------------------
class TestCrosscheck(unittest.TestCase):
    def test_strength_by_time_window(self):
        with tempfile.TemporaryDirectory() as d:
            p_in = os.path.join(d, "a.html")
            p_out = os.path.join(d, "b.html")
            for p in (p_in, p_out):
                with open(p, "w") as f:
                    f.write("x")
            now = dt.datetime.now(UTC)
            os.utime(p_in, (now.timestamp(), now.timestamp()))
            old = (now - dt.timedelta(days=3)).timestamp()
            os.utime(p_out, (old, old))
            win = (now - dt.timedelta(hours=1), now + dt.timedelta(hours=1))
            res = api.crosscheck("*.html", [d], win)
            got = {os.path.basename(f["path"]): f["strength"] for f in res["found"]}
            self.assertEqual(got, {"a.html": "strong", "b.html": "stale"})
            self.assertEqual(res["strong"], 1)
            self.assertEqual(res["tier"], "artifact")

    def test_no_window_means_stale(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.json"), "w") as f:
                f.write("{}")
            res = api.crosscheck("*.json", [d], (None, None))
            self.assertEqual(res["found"][0]["strength"], "stale")
            self.assertEqual(res["tier"], "derived")

    def test_shared_state_never_evidence(self):
        r = api.crosscheck_shared_state("port", 8765)
        self.assertEqual(r["strength"], "info")
        self.assertEqual(r["tier"], "none")


# ---------------------------------------------------------------------------
# s5 tri-state
# ---------------------------------------------------------------------------
class TestSearchTriState(unittest.TestCase):
    def _run(self, actions, hard_check, anchors):
        tree = mk_tree(actions)
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="t", origin_turn=1,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1", hard_check=hard_check, anchors=anchors)
        return steps.s5_search(tree, [req], {"R1": cr}, ov)["R1"]

    def test_hard(self):
        s = self._run([mk_action(ROOT, 0, "read_file", path="/x/agent.py")],
                      {"read_path": "agent.py"}, ["agent.py"])
        self.assertEqual(s.status, "hard")
        self.assertEqual(s.tier, "direct")

    def test_candidates(self):
        s = self._run([mk_action(ROOT, 0, "run_command", command="python3 render_all.py")],
                      {}, ["render", "html"])
        self.assertEqual(s.status, "candidates")
        self.assertEqual(s.tier, "retrieved")
        self.assertEqual(s.anchor_df, {"render": 1, "html": 0})

    def test_absent_suspect_anchors(self):
        """Zero anchor hits everywhere -> suspect_anchors; cannot conclude unmet based on this (P4)."""
        s = self._run([mk_action(ROOT, 0, "run_command", command="ls -1")],
                      {}, ["zzz_nope", "qqq_nope"])
        self.assertEqual(s.status, "absent")
        self.assertEqual(s.absent_reason, "suspect_anchors")

    def test_evidence_before_request_is_reported(self):
        """MISS within scope but hit outside scope -> record evidence_before_request (P3)."""
        tree = mk_tree([mk_action(ROOT, 1, "read_file", path="/x/agent.py"),
                        mk_action(ROOT, 9, "run_command", command="ls")], turns=1)
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="read agent.py", origin_turn=1,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1", hard_check={}, anchors=["agent.py"],
                              scope={"idx_gte": 5})
        s = steps.s5_search(tree, [req], {"R1": cr}, ov)["R1"]
        self.assertIn(f"{ROOT[:8]}#1", s.evidence_before_request)

    def test_hard_check_falls_back_to_global(self):
        """Hard criteria must also do a dual query: if the unique hit precedes the request, do not report MISS (measured R3.1)."""
        tree = mk_tree([mk_action(ROOT, 1, "read_file", path="/x/agent.py"),
                        mk_action(ROOT, 9, "run_command", command="ls")], turns=1)
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="read agent.py", origin_turn=1,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1", hard_check={"read_path": "agent.py"},
                              anchors=["agent.py"], scope={"idx_gte": 5})
        s = steps.s5_search(tree, [req], {"R1": cr}, ov)["R1"]
        self.assertEqual(s.status, "hard")
        self.assertTrue(s.hard_hits[0]["before_request"])
        self.assertIn(f"{ROOT[:8]}#1", s.evidence_before_request)

    def test_idf_keeps_rare_anchor_hit_in_topk(self):
        """Common anchors (read/cat/.py) can push the real evidence out of top-K; idf must pull it back (measured R3.1)."""
        acts = [mk_action(ROOT, i, "run_command", command=f"cat x{i}.py && read")
                for i in range(12)]
        acts.append(mk_action(ROOT, 99, "read_file", path="/pkg/agent.py"))
        tree = mk_tree(acts)
        anchors = ["agent.py", ".py", "read", "cat"]
        cnt = api.retrieve(tree, anchors, k=3, score_mode="count")
        idf = api.retrieve(tree, anchors, k=3, score_mode="idf")
        self.assertNotIn(f"{ROOT[:8]}#99", [r["ref"] for r in cnt["rows"]])
        self.assertEqual(idf["rows"][0]["ref"], f"{ROOT[:8]}#99")


# ---------------------------------------------------------------------------
# Synthetic controls
# ---------------------------------------------------------------------------
class TestControls(unittest.TestCase):
    def test_injects_only_when_absent_from_trace(self):
        tree = mk_tree([mk_action(ROOT, 0, "run_command", command="ls -1")])
        reqs, meta = steps.s2b_inject_controls(tree, [], n=1)
        self.assertEqual(len(reqs), 1)
        self.assertTrue(reqs[0].synthetic)
        self.assertTrue(all(v == 0 for v in meta[0]["df"].values()))

    def test_skips_template_that_trace_touches(self):
        """Trace really used slack -> that template is not a valid control; skip it and try the next."""
        tree = mk_tree([mk_action(ROOT, 0, "run_command",
                                  command="curl https://hooks.slack.com/x")])
        reqs, meta = steps.s2b_inject_controls(tree, [], n=1)
        self.assertEqual(len(reqs), 1)
        self.assertNotIn("slack", " ".join(meta[0]["anchors"]).lower())


# ---------------------------------------------------------------------------
# Aggregation and verdict
# ---------------------------------------------------------------------------
def _req(rid, strength="strong", status="active", synthetic=False):
    return schema.Requirement(id=rid, text="t", origin_turn=1, verifiable_by="action",
                              status=status, strength=strength, expect="e",
                              synthetic=synthetic)


def _fnd(rid, sat, tier="direct", overclaim=False, synthetic=False):
    return schema.Finding(req_id=rid, satisfied=sat, tier=tier, confidence=1.0,
                          overclaim=overclaim, synthetic=synthetic, reason="r")


class TestAggregate(unittest.TestCase):
    def test_pass(self):
        agg = steps.s9_aggregate([_fnd("R1", "true")], [_req("R1")])
        self.assertEqual(agg["verdict"], "PASS")
        self.assertEqual(agg["score"], 1.0)

    def test_fail_on_unmet(self):
        agg = steps.s9_aggregate([_fnd("R1", "true"), _fnd("R2", "false")],
                                 [_req("R1"), _req("R2")])
        self.assertEqual(agg["verdict"], "FAIL")
        self.assertEqual(agg["unmet_ids"], ["R2"])

    def test_fail_on_overclaim(self):
        agg = steps.s9_aggregate([_fnd("R1", "true", overclaim=True)], [_req("R1")])
        self.assertEqual(agg["verdict"], "FAIL")

    def test_weak_on_unverifiable(self):
        agg = steps.s9_aggregate([_fnd("R1", "unverifiable")], [_req("R1")])
        self.assertEqual(agg["verdict"], "WEAK_PASS")

    def test_weak_requirements_excluded_from_score(self):
        agg = steps.s9_aggregate([_fnd("R1", "true"), _fnd("R2", "false")],
                                 [_req("R1"), _req("R2", strength="weak")])
        self.assertEqual(agg["verdict"], "PASS")          # weak ones do not enter the verdict
        self.assertEqual(agg["counts"]["strong_active"], 1)

    def test_superseded_excluded(self):
        agg = steps.s9_aggregate([_fnd("R1", "false")],
                                 [_req("R1", status="superseded")])
        self.assertEqual(agg["verdict"], "PASS")

    def test_control_failure_invalidates_run(self):
        """Synthetic control judged satisfied -> verdict is too lenient; entire run marked INVALID."""
        agg = steps.s9_aggregate(
            [_fnd("R1", "true"), _fnd("CTRL1", "true", synthetic=True)],
            [_req("R1"), _req("CTRL1", synthetic=True)])
        self.assertEqual(agg["verdict"], "INVALID")
        self.assertFalse(agg["control"]["passed"])

    def test_control_pass(self):
        agg = steps.s9_aggregate(
            [_fnd("R1", "true"), _fnd("CTRL1", "false", tier="none", synthetic=True)],
            [_req("R1"), _req("CTRL1", synthetic=True)])
        self.assertEqual(agg["verdict"], "PASS")
        self.assertTrue(agg["control"]["passed"])


class TestConfidence(unittest.TestCase):
    def test_tier_caps_confidence(self):
        self.assertEqual(schema.cap_confidence("testimonial", 1.0), 0.30)
        self.assertEqual(schema.cap_confidence("direct", 1.0), 1.0)
        self.assertEqual(schema.cap_confidence("cross_session", 1.0), 0.70)

    def test_residual_discounts(self):
        self.assertAlmostEqual(schema.cap_confidence("artifact", 1.0, True), 0.855, 3)


# ---------------------------------------------------------------------------
# LLM output parsing and validation gate
# ---------------------------------------------------------------------------
class TestJsonExtraction(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(llm.extract_json('preamble\n```json\n{"a":1}\n```\npostscript'), {"a": 1})

    def test_ansi_stripped(self):
        raw = "\x1b[38;5;10m```json\n{\"a\": 1}\n```\x1b[0m"
        self.assertEqual(llm.extract_json(raw), {"a": 1})

    def test_unfenced_nested_braces(self):
        self.assertEqual(llm.extract_json('noise {"a":{"b":[1,2]}} tail'),
                         {"a": {"b": [1, 2]}})

    def test_brace_inside_string_not_confused(self):
        self.assertEqual(llm.extract_json('{"a":"}"}'), {"a": "}"})

    def test_no_json_raises(self):
        with self.assertRaises(llm.LLMOutputError):
            llm.extract_json("no json here at all")


class TestValidators(unittest.TestCase):
    def test_requirements_reject_bad_enum(self):
        errs = steps._v_requirements(2)({
            "turn_intents": [{"turn": 1, "intent": "request"}],
            "requirements": [{"id": "R1", "origin_turn": 1, "verifiable_by": "guess",
                              "status": "active", "strength": "strong", "expect": "x"}]})
        self.assertTrue(any("verifiable_by" in e for e in errs))

    def test_requirements_reject_turn_out_of_range(self):
        errs = steps._v_requirements(2)({
            "turn_intents": [{"turn": 1, "intent": "request"}],
            "requirements": [{"id": "R1", "origin_turn": 7, "verifiable_by": "action",
                              "status": "active", "strength": "strong", "expect": "x"}]})
        self.assertTrue(any("origin_turn" in e for e in errs))

    def test_requirements_reject_from_question_turn(self):
        """Non-request turns must not produce requirements -- an enforcement point against "turning questions into requirements"."""
        errs = steps._v_requirements(2)({
            "turn_intents": [{"turn": 1, "intent": "question"}],
            "requirements": [{"id": "R1", "origin_turn": 1, "verifiable_by": "action",
                              "status": "active", "strength": "strong", "expect": "x"}]})
        self.assertTrue(any("non-request" in e for e in errs))

    def test_requirements_ok(self):
        self.assertEqual(steps._v_requirements(2)({
            "turn_intents": [{"turn": 1, "intent": "request"}],
            "requirements": [{"id": "R1", "origin_turn": 1, "verifiable_by": "action",
                              "status": "active", "strength": "strong", "expect": "x"}]}), [])

    def test_claims_structural_errors_reject_batch(self):
        errs = steps._v_claims(1)({"claims": [{"id": "C1", "turn": 9, "kind": "nope",
                                               "text": "t", "quote": "abcdef"}]})
        self.assertTrue(any("kind" in e for e in errs))
        self.assertTrue(any("turn" in e for e in errs))

    def test_fabricated_quote_dropped_not_batch_failure(self):
        """A single quote that cannot be verified drops only that claim; not all 40 claims should be penalized."""
        rows = [{"id": "C1", "turn": 1, "kind": "produced", "text": "t",
                 "quote": "I finished render_all.py"},
                {"id": "C2", "turn": 1, "kind": "did", "text": "t",
                 "quote": "I deployed to production"}]
        kept, dropped = steps._filter_claims(rows, "agent said: I finished render_all.py and it works")
        self.assertEqual([c.id for c in kept], ["C1"])
        self.assertEqual([d["id"] for d in dropped], ["C2"])

    def test_quote_match_tolerates_markdown_and_fullwidth(self):
        """`**sr-format FAIL**:` quoted as `sr-format FAIL):` should not be judged fabricated."""
        rows = [{"id": "C1", "turn": 1, "kind": "verified", "text": "t",
                 "quote": "sr-format FAIL): 3 SKILL.md all noncompliant"}]
        kept, dropped = steps._filter_claims(
            rows, "**sr-format FAIL**: 3 SKILL.md all noncompliant")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_quote_match_ignores_whitespace(self):
        rows = [{"id": "C1", "turn": 1, "kind": "produced", "text": "t",
                 "quote": "render_all.py and it works"}]
        kept, _ = steps._filter_claims(rows, "I finished\n  render_all.py and it works")
        self.assertEqual(len(kept), 1)

    def test_compiled_rejects_brace_glob(self):
        errs = steps._v_compiled({"R1"})({"compiled": [
            {"req_id": "R1", "hard_check": {"artifact_glob": "*.{html,json}"},
             "anchors": ["html"]}]})
        self.assertTrue(any("braces" in e for e in errs))

    def test_compiled_rejects_unknown_hard_check_key(self):
        errs = steps._v_compiled({"R1"})({"compiled": [
            {"req_id": "R1", "hard_check": {"regex": "x"}, "anchors": ["html"]}]})
        self.assertTrue(any("unknown key" in e for e in errs))

    def test_compiled_rejects_missing_requirement(self):
        errs = steps._v_compiled({"R1", "R2"})({"compiled": [
            {"req_id": "R1", "hard_check": {}, "anchors": ["x"]}]})
        self.assertTrue(any("missing these requirements" in e for e in errs))

    def test_judgments_reject_hallucinated_ref(self):
        """A ref not in the evidence pack -> hallucination; must be rejected."""
        v = steps._v_judgments({"R1"}, {"aaaaaaaa#1"})
        errs = v({"judgments": [{"req_id": "R1", "satisfied": "true", "tier": "direct",
                                 "evidence_actions": ["bbbbbbbb#9"], "reason": "r"}]})
        self.assertTrue(any("hallucination" in e for e in errs))

    def test_judgments_ok(self):
        v = steps._v_judgments({"R1"}, {"aaaaaaaa#1"})
        self.assertEqual(v({"judgments": [
            {"req_id": "R1", "satisfied": "true", "tier": "direct",
             "evidence_actions": ["aaaaaaaa#1"], "reason": "r"}]}), [])


class TestAskRetry(unittest.TestCase):
    def test_retries_once_then_succeeds(self):
        calls = []

        def caller(p):
            calls.append(p)
            return '{"a": 1}' if len(calls) > 1 else '{"a": 0}'

        data = llm.ask("P", lambda d: [] if d.get("a") == 1 else ["a must be 1"],
                       caller=caller)
        self.assertEqual(data, {"a": 1})
        self.assertEqual(len(calls), 2)
        self.assertIn("Previous output failed validation", calls[1])

    def test_raises_after_retries(self):
        with self.assertRaises(llm.LLMOutputError):
            llm.ask("P", lambda d: ["never passes"], caller=lambda p: '{"a":1}')

    def test_backend_failure_is_distinct(self):
        def boom(p):
            raise OSError("no kiro-cli")
        with self.assertRaises(llm.LLMUnavailable):
            llm.ask("P", None, caller=boom, backend_retries=0)

    def test_transient_backend_crash_is_retried(self):
        """kiro-cli occasionally panics (measured exit 101); the same prompt succeeds on retry -- do not give up after one failure."""
        n = {"i": 0}

        def flaky(p):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("kiro-cli exit code 101")
            return '{"ok": 1}'

        data = llm.ask("P", None, caller=flaky, backend_retries=2, backoff=0)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(n["i"], 2)


# ---------------------------------------------------------------------------
# Evidence pack
# ---------------------------------------------------------------------------
class TestS6Probe(unittest.TestCase):
    """s6 nearby-action slicing: root-session actions starting from origin_turn, no preceding actions mixed in."""

    def _tree(self):
        acts = [
            mk_action(ROOT, 0, "run_command", command="ls",  turn=1),
            mk_action(ROOT, 1, "run_command", command="pwd", turn=1),
            mk_action(ROOT, 2, "read_file", path="/x/before.py",           turn=2),
            mk_action(ROOT, 3, "write_file", path="/x/hello.py",           turn=3),
            mk_action(ROOT, 4, "run_command", command="python3 hello.py",  turn=3),
            mk_action(ROOT, 5, "run_command", command="cat hello.py",      turn=4),
            mk_action(ROOT, 6, "write_file",  path="/x/output.txt",        turn=5),
        ]
        return mk_tree(acts, turns=5, prompts=["a", "b", "make something new", "d", "e"])

    def test_nearby_excludes_before_request(self):
        tree = self._tree()
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="t", origin_turn=3,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1", hard_check={},
                              anchors=["nonexistent_xyz"])
        search = steps.s5_search(tree, [req], {"R1": cr}, ov)
        probe = steps.s6_probe(tree, [req], search, ov)
        refs = [r["ref"] for r in probe["per_req"]["R1"]["nearby_actions"]]
        # origin_turn=3 starts at idx=3; preceding idx 0/1/2 must be excluded
        self.assertNotIn(f"{ROOT[:8]}#0", refs)
        self.assertNotIn(f"{ROOT[:8]}#1", refs)
        self.assertNotIn(f"{ROOT[:8]}#2", refs)
        # idx>=3 must be present
        self.assertEqual(refs, [f"{ROOT[:8]}#{i}" for i in (3, 4, 5, 6)])

    def test_hard_status_skips_probe(self):
        """Requirements that hit hard do not enter per_req."""
        tree = self._tree()
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="t", origin_turn=2,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1",
                              hard_check={"read_path": "before.py"},
                              anchors=["before"])
        search = steps.s5_search(tree, [req], {"R1": cr}, ov)
        self.assertEqual(search["R1"].status, "hard")
        probe = steps.s6_probe(tree, [req], search, ov)
        self.assertNotIn("R1", probe["per_req"])

    def test_enough_candidates_skips_probe(self):
        """Sufficient candidates (>=3) also skip."""
        tree = self._tree()
        ov = api.overview(tree)
        req = schema.Requirement(id="R1", text="t", origin_turn=3,
                                 verifiable_by="action", status="active",
                                 strength="strong", expect="e")
        cr = schema.Criterion(req_id="R1", hard_check={},
                              anchors=["hello.py"])   # matches multiple
        search = steps.s5_search(tree, [req], {"R1": cr}, ov)
        self.assertEqual(search["R1"].status, "candidates")
        self.assertGreaterEqual(len(search["R1"].candidates), 3)
        probe = steps.s6_probe(tree, [req], search, ov)
        self.assertNotIn("R1", probe["per_req"])


class TestBuildPack(unittest.TestCase):
    def test_pack_refs_are_exactly_what_llm_may_cite(self):
        tree = mk_tree([mk_action(ROOT, 0, "read_file", path="/x/agent.py"),
                        mk_action(ROOT, 5, "run_command", command="ls")])
        ov = api.overview(tree)
        req = _req("R1")
        cr = schema.Criterion(req_id="R1", hard_check={"read_path": "agent.py"},
                              anchors=["agent.py"], residual="contents not verified")
        search = steps.s5_search(tree, [req], {"R1": cr}, ov)
        probe = steps.s6_probe(tree, [req], search, ov)
        pack, refs = steps.build_pack([req], {"R1": cr}, search, probe, {},
                                      [schema.Claim("C1", 1, "did", "read", "read")], ov)
        self.assertIn(f"{ROOT[:8]}#0", refs)
        self.assertIn("residual", pack)
        self.assertIn("R1", pack)
        for r in refs:
            self.assertIn(r, pack)          # refs must actually appear in the text

    def test_before_request_refs_are_citable(self):
        """Hits that precede the request must join the citable set.

        Measured lesson: they were only written into the pack text and not into refs, so s8
        could see but not cite -- validation flagged them as hallucination and pushed back,
        misjudging a satisfied requirement as unverifiable.
        """
        tree = mk_tree([mk_action(ROOT, 1, "read_file", path="/x/agent.py"),
                        mk_action(ROOT, 9, "run_command", command="ls")], turns=1)
        ov = api.overview(tree)
        req = _req("R1")
        cr = schema.Criterion(req_id="R1", hard_check={}, anchors=["agent.py"],
                              scope={"idx_gte": 5})
        search = steps.s5_search(tree, [req], {"R1": cr}, ov)
        probe = steps.s6_probe(tree, [req], search, ov)
        pack, refs = steps.build_pack([req], {"R1": cr}, search, probe, {}, [], ov)
        self.assertTrue(search["R1"].evidence_before_request)
        for r in search["R1"].evidence_before_request:
            self.assertIn(r, refs)
            self.assertIn(r, pack)


if __name__ == "__main__":
    unittest.main(verbosity=2)
