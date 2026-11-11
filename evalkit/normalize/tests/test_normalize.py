"""Normalization layer unit tests.

Covers four behavior classes (no noise-filtering in this version):
  1. Alias folding   execute_bash->shell, fs_read->read
  2. Fan-out         read.operations array -> N actions
  3. Semantic extraction   tool + params -> action label; path moved to the semantically correct field
  4. Derived fields  completed (pre/post pairing), turn (split by user_prompt)

Run: python3 -m unittest discover -s normalize/tests -t .
"""

from __future__ import annotations

import unittest

from normalize import (apply_cd, command_working_dir, normalize_events,
                       split_subcommands)


def pre(tool, tool_input, ts="2026-08-04T10:00:00.000Z"):
    return {"ts": ts, "event": "pre_tool_use", "session_id": "s",
            "tool": tool, "tool_input": tool_input}


def post(tool, tool_input, ts="2026-08-04T10:00:01.000Z", size=10):
    return {"ts": ts, "event": "post_tool_use", "session_id": "s", "tool": tool,
            "success": True, "response_size": size, "tool_input": tool_input}


def prompt(text="q", ts="2026-08-04T09:59:00.000Z"):
    return {"ts": ts, "event": "user_prompt", "session_id": "s", "prompt": text}


def read_op(path, mode="Line"):
    return {"mode": mode, "path": path}


# ---------------------------------------------------------------------------
# 1. Alias folding
# ---------------------------------------------------------------------------
class TestAliases(unittest.TestCase):

    def test_execute_bash_maps_to_shell(self):
        ir = normalize_events([prompt(), pre("execute_bash", {"command": "ls"})])
        a = ir.actions[0]
        self.assertEqual(a.raw_tool, "execute_bash")
        self.assertEqual(a.tool, "shell")
        self.assertEqual(a.action, "run_command")

    def test_fs_read_maps_to_read(self):
        ir = normalize_events([
            prompt(),
            pre("fs_read", {"operations": [read_op("/a.md")]}),
        ])
        a = ir.actions[0]
        self.assertEqual(a.raw_tool, "fs_read")
        self.assertEqual(a.tool, "read")
        self.assertEqual(a.action, "read_file")

    def test_same_rule_catches_both_names(self):
        """A rule that only mentions action must cover both tool-name generations."""
        ir = normalize_events([
            prompt(),
            pre("shell", {"command": "chmod 777 /tmp/x"}),
            pre("execute_bash", {"command": "chmod 777 /tmp/y"}),
        ])
        hits = [a for a in ir.actions
                if a.action == "run_command" and "chmod 777" in (a.command or "")]
        self.assertEqual(len(hits), 2)


# ---------------------------------------------------------------------------
# 2. Fan-out
# ---------------------------------------------------------------------------
class TestFanOut(unittest.TestCase):

    def test_batch_read_expands(self):
        paths = [f"/cases/sr-00{i}.md" for i in range(1, 8)]
        ir = normalize_events([
            prompt(),
            pre("read", {"operations": [read_op(p) for p in paths]}),
        ])
        self.assertEqual(len(ir.actions), 7)
        self.assertEqual([a.path for a in ir.actions], paths)
        # All from the same call
        self.assertEqual({a.call_idx for a in ir.actions}, {0})
        self.assertEqual([a.op_idx for a in ir.actions], list(range(7)))
        # idx is globally monotonic; the rule engine relies on it for order
        self.assertEqual([a.idx for a in ir.actions], list(range(7)))

    def test_non_first_position_is_visible(self):
        """A file at position 3 in the array must be counted (naive impls miss it)."""
        ir = normalize_events([
            prompt(),
            pre("read", {"operations": [read_op("/a"), read_op("/b"), read_op("/c")]}),
        ])
        self.assertIn("/c", {a.path for a in ir.actions})

    def test_single_read_is_one_action(self):
        ir = normalize_events([prompt(), pre("read", {"operations": [read_op("/a")]})])
        self.assertEqual(len(ir.actions), 1)
        self.assertEqual(ir.actions[0].op_idx, 0)

    def test_image_paths_expand(self):
        ir = normalize_events([
            prompt(),
            pre("read", {"operations": [
                {"mode": "Image", "image_paths": ["/x.png", "/y.png"]}]}),
        ])
        self.assertEqual([a.action for a in ir.actions], ["read_image"] * 2)
        self.assertEqual([a.path for a in ir.actions], ["/x.png", "/y.png"])

    def test_shell_subcommands(self):
        ir = normalize_events([
            prompt(),
            pre("shell", {"command": "cd /x && git status ; ls -l"}),
        ])
        self.assertEqual(len(ir.actions), 1)          # shell is not split into multiple actions
        self.assertEqual(ir.actions[0].subcommands,
                         ["cd /x", "git status", "ls -l"])


class TestSubcommandSplit(unittest.TestCase):

    def test_plain(self):
        self.assertEqual(split_subcommands("a && b || c ; d"), ["a", "b", "c", "d"])

    def test_separator_inside_quotes_is_kept(self):
        self.assertEqual(
            split_subcommands("""echo "a && b" && ls"""),
            ['echo "a && b"', "ls"],
        )

    def test_single_quotes(self):
        self.assertEqual(split_subcommands("""echo 'x;y' ; ls"""),
                         ["echo 'x;y'", "ls"])

    def test_empty(self):
        self.assertEqual(split_subcommands(""), [])

    def test_newline_is_separator(self):
        self.assertEqual(split_subcommands("a\nb\n\nc"), ["a", "b", "c"])


# ---------------------------------------------------------------------------
# 3. Semantic extraction
# ---------------------------------------------------------------------------
class TestSemantics(unittest.TestCase):

    def test_write_create_vs_modify(self):
        ir = normalize_events([
            prompt(),
            pre("write", {"command": "create", "path": "/new.py"}),
            pre("write", {"command": "strReplace", "path": "/old.cpp"}),
        ])
        self.assertEqual([a.action for a in ir.actions],
                         ["create_file", "modify_file"])

    def test_read_line_vs_directory(self):
        ir = normalize_events([
            prompt(),
            pre("read", {"operations": [read_op("/f.md"),
                                        read_op("/dir", mode="Directory")]}),
        ])
        self.assertEqual([a.action for a in ir.actions], ["read_file", "list_dir"])

    def test_grep_path_goes_to_root_not_path(self):
        """grep's path is a search directory and must not pollute the path field / file set."""
        ir = normalize_events([
            prompt(),
            pre("grep", {"pattern": "X", "path": "/repo"}),
        ])
        a = ir.actions[0]
        self.assertEqual(a.action, "search_content")
        self.assertIsNone(a.path)
        self.assertEqual(a.root, "/repo")
        self.assertEqual(a.pattern, "X")

    def test_glob_same(self):
        ir = normalize_events([prompt(), pre("glob", {"pattern": "**/*.py"})])
        self.assertEqual(ir.actions[0].action, "search_files")
        self.assertIsNone(ir.actions[0].path)

    def test_unknown_tool_is_flagged_not_dropped(self):
        ir = normalize_events([prompt(), pre("brand_new_tool", {"x": 1})])
        self.assertEqual(len(ir.actions), 1)
        self.assertTrue(ir.actions[0].action.startswith("unknown:"))
        self.assertTrue(any("unknown tool" in w for w in ir.warnings))

    def test_read_before_modify_rule_works(self):
        """Example 3 rule target: create should not be counted as a violation; only modify should be checked."""
        events = [prompt()]
        # A scaled-down version of the 118:3 ratio: 3 creates + 1 read-then-modify
        for i in range(3):
            events.append(pre("write", {"command": "create", "path": f"/out{i}.json"}))
        events.append(pre("read", {"operations": [read_op("/src.cpp")]}))
        events.append(pre("write", {"command": "strReplace", "path": "/src.cpp"}))
        ir = normalize_events(events)

        read_paths: set[str] = set()
        violations = []
        for a in ir.actions:
            if a.action == "read_file" and a.path:
                read_paths.add(a.path)
            if a.action == "modify_file" and a.path not in read_paths:
                violations.append(a)
        self.assertEqual(violations, [])

        # Naive approach (tool == write means modify) yields 3 false positives
        naive = [a for a in ir.actions
                 if a.tool == "write" and a.path not in read_paths]
        self.assertEqual(len(naive), 3)


# ---------------------------------------------------------------------------
# 4. Derived fields
# ---------------------------------------------------------------------------
class TestDerived(unittest.TestCase):

    def test_completed_true_when_paired(self):
        ir = normalize_events([
            prompt(),
            pre("shell", {"command": "ls"}),
            post("shell", {"command": "ls"}, ts="2026-08-04T10:00:02.500Z", size=42),
        ])
        a = ir.actions[0]
        self.assertTrue(a.completed)
        self.assertEqual(a.resp_size, 42)
        self.assertEqual(a.duration_ms, 2500)

    def test_orphan_detected(self):
        """pre without post = execution not completed. The success field is always true, unreliable."""
        ir = normalize_events([
            prompt(),
            pre("shell", {"command": "slow"}, ts="2026-08-04T10:12:33.000Z"),
            pre("shell", {"command": "retry"}, ts="2026-08-04T10:14:31.000Z"),
            post("shell", {"command": "retry"}, ts="2026-08-04T10:14:35.000Z"),
        ])
        self.assertEqual([a.completed for a in ir.actions], [False, True])
        self.assertEqual(len(ir.orphans), 1)
        self.assertEqual(ir.orphans[0].command, "slow")
        self.assertTrue(any("pre without post" in w for w in ir.warnings))

    def test_pairing_prefers_matching_args_not_nearest_post(self):
        """Long-running calls: post may not immediately follow pre. Naive "next post" matching mispairs."""
        ir = normalize_events([
            prompt(),
            pre("shell", {"command": "slow"}, ts="2026-08-04T10:00:00.000Z"),
            pre("shell", {"command": "fast"}, ts="2026-08-04T10:00:05.000Z"),
            post("shell", {"command": "fast"}, ts="2026-08-04T10:00:06.000Z"),
            post("shell", {"command": "slow"}, ts="2026-08-04T10:06:48.000Z"),
        ])
        by_cmd = {a.command: a for a in ir.actions}
        self.assertTrue(by_cmd["slow"].completed)
        self.assertTrue(by_cmd["fast"].completed)
        self.assertEqual(by_cmd["fast"].duration_ms, 1000)
        self.assertEqual(by_cmd["slow"].duration_ms, 408_000)

    def test_turn_from_user_prompt_not_stop(self):
        """stop events can be absent; turns must be split by user_prompt."""
        events = [
            prompt("first turn", ts="2026-08-04T10:00:00.000Z"),
            pre("read", {"operations": [read_op("/a")]}),
            # Note: no stop event here
            prompt("second turn", ts="2026-08-04T10:05:00.000Z"),
            pre("read", {"operations": [read_op("/a")]}),
            pre("read", {"operations": [read_op("/a")]}),
        ]
        ir = normalize_events(events)
        self.assertEqual(ir.turns, 2)
        self.assertEqual([a.turn for a in ir.actions], [1, 2, 2])

    def test_per_turn_counting_differs_from_global(self):
        """Example 5 rule target: per-turn counting catches "repeatedly reading the same file in one turn"."""
        events = [prompt("t1")]
        events.append(pre("read", {"operations": [read_op("/x.cpp")]}))
        events.append(prompt("t2"))
        for _ in range(5):
            events.append(pre("read", {"operations": [read_op("/x.cpp")]}))
        ir = normalize_events(events)

        global_count = sum(1 for a in ir.actions if a.path == "/x.cpp")
        self.assertEqual(global_count, 6)

        per_turn = {t: sum(1 for a in ir.by_turn(t) if a.path == "/x.cpp")
                    for t in (1, 2)}
        self.assertEqual(per_turn, {1: 1, 2: 5})
        # Threshold-3 rule: cannot be detected globally; per-turn catches it
        self.assertTrue(max(per_turn.values()) > 3)

    def test_tool_before_first_prompt_is_turn_1(self):
        ir = normalize_events([pre("shell", {"command": "ls"})])
        self.assertEqual(ir.actions[0].turn, 1)

    def test_blocked_event(self):
        ir = normalize_events([
            prompt(),
            pre("use_aws", {"service_name": "s3"}),
            {"ts": "2026-08-04T10:00:01.000Z", "event": "tool_blocked",
             "session_id": "s", "tool": "use_aws", "reason": "denied_tools"},
        ])
        self.assertTrue(ir.actions[0].blocked)
        self.assertEqual(len(ir.orphans), 0)   # blocked is not an orphan

    def test_stop_missing_is_warned(self):
        ir = normalize_events([
            prompt("a"), prompt("b"),
            {"ts": "2026-08-04T10:00:00.000Z", "event": "stop",
             "session_id": "s", "turn": 1, "response_preview": "ok"},
        ])
        self.assertTrue(any("stop event missing" in w for w in ir.warnings))


def pre_cwd(tool, tool_input, cwd, ts="2026-08-04T10:00:00.000Z"):
    """A pre_tool_use event carrying the agent process cwd (hook source only)."""
    e = pre(tool, tool_input, ts)
    e["cwd"] = cwd
    return e


class TestApplyCd(unittest.TestCase):
    """What `cd X` does to a directory, as a pure function.

    The rule lives here and the traversal lives with the caller, because the
    two need different things. The rule is string arithmetic. Deciding which
    `cd`s count needs the command's structure -- a `cd` inside `( … )` or a
    pipeline is gone when the subshell ends -- and that needs a syntax tree,
    which only `graph` has. So `command_working_dir` below folds with a coarse
    string split and `graph` folds while walking the tree, and both call this.
    """

    def test_absolute_overwrites(self):
        self.assertEqual(apply_cd(None, "/app"), "/app")
        self.assertEqual(apply_cd("/elsewhere", "/app"), "/app")

    def test_relative_composes(self):
        self.assertEqual(apply_cd("/app", "sub"), "/app/sub")
        self.assertEqual(apply_cd("/app", "./bin"), "/app/bin")
        self.assertEqual(apply_cd("/app/x", ".."), "/app")

    def test_relative_without_a_base_is_unknown(self):
        self.assertIsNone(apply_cd(None, "sub"))

    def test_what_the_command_text_cannot_answer(self):
        for target in ("~/scratch", "$DIR", "${D}/x", "*/x", "-", ""):
            self.assertIsNone(apply_cd("/app", target), target)

    def test_a_quoted_target_with_a_space(self):
        self.assertEqual(apply_cd("/app", '"my dir"'), "/app/my dir")


class TestCommandWorkingDir(unittest.TestCase):
    """The pure parser. `working_dir` is a shell-tool argument the model often
    omits, so the command's own `cd` is the real base for its relative paths --
    neither the working_dir arg nor the agent process cwd."""

    def test_absolute_cd(self):
        self.assertEqual(
            command_working_dir("cd /repo/x && grep -n foo sub/file.c"),
            "/repo/x")

    def test_no_cd_is_none(self):
        self.assertIsNone(command_working_dir("grep -n foo /abs/file.c"))

    def test_relative_cd_is_not_a_base(self):
        """`cd ..` cannot resolve another relative path."""
        self.assertIsNone(command_working_dir("cd .. && ls"))

    def test_variable_cd_is_not_a_base(self):
        self.assertIsNone(command_working_dir("cd $ROOT && ls file.c"))

    def test_every_cd_is_folded_not_only_the_first(self):
        """It used to take the first absolute literal and stop, on the grounds
        that one `cd` up front is the dominant shape. True of the agent whose
        traces shaped this; not true of people. 130 of 1524 reference solutions
        in an external corpus hold more than one `cd`, and keeping only the
        first resolves everything after the second one directory too high -- a
        node that does not exist."""
        self.assertEqual(
            command_working_dir("cd /a && do && cd /b && more"), "/b")
        self.assertEqual(
            command_working_dir("cd /app && cd sublib && ls"),
            "/app/sublib")
        self.assertEqual(command_working_dir("cd /a/b && cd .. && ls"), "/a")

    def test_an_unreadable_cd_clears_the_base(self):
        """`cd /app && cd $D && > out.txt` has no base for `out.txt`. Keeping
        `/app` would name a file that does not exist, and a wrong edge to a
        real file is worse than no edge."""
        self.assertIsNone(command_working_dir("cd /app && cd $D && ls f.c"))
        self.assertIsNone(command_working_dir("cd /app && cd ~/x && ls f.c"))
        self.assertIsNone(command_working_dir("cd /app && cd - && ls f.c"))

    def test_quoted_target(self):
        self.assertEqual(command_working_dir('cd "/a b/c" && ls'), "/a b/c")

    def test_cd_not_at_the_start(self):
        self.assertEqual(command_working_dir("echo hi && cd /repo && ls"),
                         "/repo")

    def test_empty(self):
        self.assertIsNone(command_working_dir(""))


class TestEffectiveCwd(unittest.TestCase):
    """The `cwd` field on run_command Actions, end to end through the hook
    path. Priority: command `cd` > working_dir arg > process cwd."""

    def test_cd_beats_working_dir_arg(self):
        ir = normalize_events([prompt(), pre_cwd(
            "shell", {"command": "cd /repo/x && ls f.c", "working_dir": "/wd"},
            cwd="/proc/here")])
        self.assertEqual(ir.actions[0].cwd, "/repo/x")

    def test_working_dir_arg_beats_process_cwd(self):
        ir = normalize_events([prompt(), pre_cwd(
            "shell", {"command": "ls f.c", "working_dir": "/wd"},
            cwd="/proc/here")])
        self.assertEqual(ir.actions[0].cwd, "/wd")

    def test_process_cwd_is_the_last_resort(self):
        ir = normalize_events([prompt(), pre_cwd(
            "shell", {"command": "ls f.c"}, cwd="/proc/here")])
        self.assertEqual(ir.actions[0].cwd, "/proc/here")

    def test_non_command_actions_have_no_cwd(self):
        """cwd is meaningful only for run_command; a read must not inherit the
        process directory into this field."""
        ir = normalize_events([prompt(), pre_cwd(
            "read", {"operations": [read_op("/a.md")]}, cwd="/proc/here")])
        self.assertIsNone(ir.actions[0].cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
