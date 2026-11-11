"""Deterministic graph skeleton from the `path` field.

Everything here comes from a structured field, so every edge is `certain` --
no inference. Commands, whose targets live inside the command text rather than
in a field of their own, are Task 4/5.

    write  -> generatedBy a fresh entity version        (Task 2)
    read   -> usedBy the version current at that idx    (Task 3)

Versioning
----------
`vN` counts the writes *this run* performed on a path, so the version number is
readable on its own: `engine.py@v3` means the run rewrote it three times.

    v0    the state before this run touched the file
    v1..  one per write, in idx order

`v0` therefore exists only when the run had something to start from:

    read_file    the file was there to be read       -> v0 created
    modify_file  modifying implies a prior state     -> v0 created, then v1
    create_file  the file is new                     -> no v0, first write is v1

That asymmetry is deliberate. Emitting a `v0` for `create_file` would assert a
prior state that never existed, and Task 9 walks backwards from final artifacts
to find dead work -- a fictional root would show up as a real one.

Every action becomes an activity, including the 211 commands in the reference
session that carry no `path`. They sit isolated until Task 4/5 connects them,
and `isolated_activities()` is the metric that tracks that (Task 7).
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Optional

from . import keys, shell_ast
from .keys import extract_from_action
from .schema import (
    CERTAIN,
    GENERATED_BY,
    LIKELY,
    USED_BY,
    Activity,
    Entity,
    ProvGraph,
)

# Same set efficiency/steps.py uses, so "what counts as a write" does not drift
# between the two modules.
WRITE_ACTIONS = frozenset({"write_file", "modify_file", "create_file", "edit_file"})

# A write to one of these implies the file already existed, so a v0 is created
# to stand for the prior state. `create_file` is deliberately absent.
IMPLIES_PRIOR_STATE = frozenset({"modify_file", "write_file", "edit_file"})

# A *partial* edit reads the prior version before rewriting it, so it consumes
# the previous entity as well as generating the next one. Without this the
# version chain is not connected -- v0 would have no path to v1, and Task 9
# walks that chain backwards from final artifacts.
#
# `write_file` is excluded on purpose: a full overwrite replaces the content
# without reading it, so asserting that it consumed the prior version would be
# an unfounded dependency. It still gets a v0 (the file existed), just no
# usedBy edge.
CONSUMES_PRIOR = frozenset({"modify_file", "edit_file"})

READ_ACTIONS = frozenset({"read_file"})


def _ref_of(a: Mapping[str, Any]) -> str:
    """`ref` as filled by the normalization layer, with a fallback.

    Older IR pickles predate Action.sid/ref, so reconstruct from whatever is
    present rather than raising.
    """
    ref = a.get("ref")
    if ref:
        return str(ref)
    sid = str(a.get("sid") or "")
    return f"{sid[:8]}#{a.get('idx')}"


def _activity_of(a: Mapping[str, Any]) -> Activity:
    return Activity(
        ref=_ref_of(a),
        idx=int(a.get("idx", -1)),
        turn=int(a.get("turn", 0)),
        action=str(a.get("action") or ""),
        purpose=a.get("purpose") or (a.get("args") or {}).get("__tool_use_purpose"),
        reasoning=a.get("reasoning") or None,
        command=a.get("command") or None,
        path=a.get("path") or None,
    )


def build_skeleton(actions: Iterable[Mapping[str, Any]],
                   session_id: str = "") -> ProvGraph:
    """Build activities, versioned entities and `generatedBy` edges.

    `actions` are normalized Action dicts (what `RunTree.actions` holds, or
    `[a.to_dict() for a in ir.actions]`). They are processed in idx order so
    version numbers follow execution order regardless of input ordering.
    """
    g = ProvGraph(session_id=session_id)
    ordered = sorted(actions, key=lambda a: int(a.get("idx", -1)))

    # path -> number of writes seen so far, i.e. the current version
    version: dict[str, int] = {}

    for a in ordered:
        act = _activity_of(a)
        a_id = g.add_activity(act)

        action = act.action
        path = a.get("path") or ""

        # A tool call that returned an error changed nothing. `modify_file` with
        # an old_str that does not match reports `The provided old_str was not
        # found in the file` and leaves the file untouched, yet it still earned a
        # generatedBy edge for a version that was never written -- and, via
        # CONSUMES_PRIOR, a usedBy edge as well. The normalization layer already
        # records this in `Action.error`; the graph simply was not reading it.
        if a.get("error") and action in WRITE_ACTIONS | READ_ACTIONS:
            g.warnings.append(
                f"{act.ref}: {action} failed, no entity emitted "
                f"({str(a.get('error'))[:80]})")
            continue

        if action in WRITE_ACTIONS and not path:
            g.warnings.append(f"{act.ref}: {action} without a path; no entity emitted")
            continue
        if not path:
            continue

        if action in WRITE_ACTIONS:
            if path not in version and action in IMPLIES_PRIOR_STATE:
                # Stand-in for the state the run started from.
                g.add_entity(Entity(key=path, version=0, born_idx=-1))
                version[path] = 0

            # A partial edit consumes the version it is about to replace. This
            # runs *before* the new version exists, so `latest_version` cannot
            # accidentally return it.
            if action in CONSUMES_PRIOR:
                prior = g.latest_version(path, before_idx=act.idx)
                if prior is not None:
                    g.add_edge(prior.node_id, a_id, USED_BY, CERTAIN,
                               f"Action.path on {action} (reads before rewriting)")

            ver = version.get(path, 0) + 1
            version[path] = ver
            e_id = g.add_entity(Entity(key=path, version=ver,
                                       producer_ref=act.ref, born_idx=act.idx))
            g.add_edge(a_id, e_id, GENERATED_BY, CERTAIN,
                       f"Action.path on {action}")

        elif action in READ_ACTIONS:
            # Reading a path the run has not written means it pre-existed.
            if path not in version:
                g.add_entity(Entity(key=path, version=0, born_idx=-1))
                version[path] = 0
            # Consume whichever version was current when the read happened.
            prior = g.latest_version(path, before_idx=act.idx)
            if prior is None:
                g.warnings.append(
                    f"{act.ref}: read {path} but no version was visible at idx {act.idx}")
            else:
                g.add_edge(prior.node_id, a_id, USED_BY, CERTAIN,
                           "Action.path on read_file")

    _connect_commands(g, ordered, version)
    return g


def command_cwd(a: Mapping[str, Any]) -> Optional[str]:
    """The directory a command starts in, before any `cd` of its own.

    Deliberately not `Action.cwd`. That field is normalize's fold of every
    `cd` in the command -- where it ends up -- and the extractor folds them
    again as it walks, so starting from the folded value counts each one
    twice: `cd sub` under `/app` would resolve `out.txt` to
    `/app/sub/sub/out.txt`.

    `working_dir` is often absent -- 182 of 269 commands in one session
    carry a `cwd` and no `working_dir` -- and that is fine: those commands
    open with an absolute `cd`, which needs no base. A command with only a
    relative `cd` and no `working_dir` has no base at all, and then a
    relative path is refused rather than resolved against a guess.
    """
    return (a.get("args") or {}).get("working_dir")


def carry_env(session_env: dict, command: str, response: Any = None) -> None:
    """Advance the variables one command's worth.

    A module-level function because the split dump has to do this identically:
    it resolves `$VAR` against the same ledger, and a copy of these lines drifted
    once -- it had the assignments but not `echoed_var`, so a value the agent had
    only ever printed (`echo $KIRO_HOME`, 4 commands in one session) stayed unknown
    there while the graph knew it.

    Values whose assignment could not be read are dropped rather than kept: a
    stale value is worse than none, because `$LOGFILE` then resolves to some
    earlier run's file and invents an edge to it.
    """
    for name in keys.assigned_names(command):
        session_env.pop(name, None)
    session_env.update(keys.shell_vars(command))
    echoed = keys.echoed_var(command, response)
    if echoed:
        session_env[echoed[0]] = echoed[1]


def _command_paths(a: Mapping[str, Any], session_env: Mapping[str, str]
                   ) -> tuple[Iterable[tuple[str, str, str]], list[str]]:
    """Paths from a command's text, preferring the syntax tree.

    `shell_ast` answers the structural questions -- which command a token belongs
    to, where a heredoc ends and who it feeds -- that `keys` has to rediscover
    with regexes. Measured on the gold set it raises recall from 0.74 to 0.87 on
    the blind turn with precision still 1.000, and on 20 corpus sessions it adds
    10.4% more edges. It cannot parse 1.5% of real commands, mostly very long
    Python heredocs, and those fall back to the regex path rather than silently
    yielding less.
    """
    command = a.get("command") or ""
    if command and shell_ast.available():
        try:
            got = shell_ast.extract(command, command_cwd(a), session_env)
            return got, shell_ast.skipped_paths()
        except shell_ast.ParseFailure:
            pass
    return extract_from_action(a, session_env), []


def _connect_commands(g: ProvGraph, ordered: list[Mapping[str, Any]],
                      version: dict[str, int]) -> None:
    """Second pass: attach shell commands via paths parsed from their text.

    Runs after the `path`-field pass so command-derived entities join chains
    that already exist rather than creating parallel ones. Every edge here is
    `LIKELY`: the direction comes from a lookup table applied to command text,
    not from a structured field.

    A command writing a path the run has not touched yet starts at `v1` with no
    `v0`, for the same reason `create_file` does -- there is no evidence the
    file existed, and Task 9 reads a fabricated root as a real starting point.

    Process ids reported in a command's output become entities too, so that a
    later `ps -p <pid>` links back to whatever launched it -- a dependency with
    no file anywhere in it.
    """
    pids: dict[str, str] = {}
    # Variables carried forward in idx order, so a command only ever sees an
    # assignment that ran before it. Values from `echo $VAR` are included:
    # a variable inherited from the process environment is otherwise unknowable
    # from the record, and one session states $KIRO_HOME that way four times.
    session_env: dict[str, str] = {}
    for a in ordered:
        if (a.get("action") or "") != "run_command":
            continue
        command = a.get("command") or ""
        if not command:
            continue
        ref = _ref_of(a)
        a_id = f"a:{ref}"
        idx = int(a.get("idx", -1))

        found, skipped = _command_paths(a, session_env)
        for p, direction, evidence in found:
            _attach(g, a_id, ref, idx, p, direction, evidence, version)
        for tok, reason in skipped:
            g.warnings.append(f"{ref}: skipped path {tok!r} ({reason})")

        # Task 5: values only the recorded output can pin down.
        for p, direction, evidence in keys.resolve_from_action(a):
            _attach(g, a_id, ref, idx, p, direction, evidence, version)

        for pid in keys.response_pids(a.get("response")):
            e_id = g.add_entity(Entity(key=pid, version=1, kind="pid",
                                       producer_ref=ref, born_idx=idx))
            g.add_edge(a_id, e_id, GENERATED_BY, LIKELY,
                       f"response reported PID={pid}")
            pids[pid] = e_id

        # A later command naming an earlier PID consumes it: `ps -p 3304381`.
        for pid, e_id in pids.items():
            if pid in command and g.entities[e_id].born_idx < idx:
                g.add_edge(e_id, a_id, USED_BY, LIKELY,
                           f"command references PID {pid}")

        for tok in keys.unresolved(command):
            g.warnings.append(
                f"{ref}: unresolved path {tok!r} (variable or glob; no value in output)")

        _warn_unlinked_mentions(g, ref, a_id, command)

        carry_env(session_env, command, a.get("response"))


def _warn_unlinked_mentions(g: ProvGraph, ref: str, a_id: str,
                            command: str) -> None:
    """Record when a command names a known artifact but got no edge for it.

    Deliberately a warning rather than an edge. Mentioning a path is not the
    same as depending on it -- measured cases include a grep pattern that
    happens to look like a filename, and paths inside a `python -c` string,
    which the *script* opens at run time rather than the command itself
    (README §10's blind spot). Linking on mention alone would bypass the
    direction check that filters exactly those out.

    Kept visible so Task 7 can report the size of the gap instead of it being
    silently dropped.
    """
    linked = {g.entities[e.src].key for e in g.in_edges(a_id) if e.src in g.entities}
    linked |= {g.entities[e.dst].key for e in g.out_edges(a_id) if e.dst in g.entities}
    for key in {e.key for e in g.entities.values() if e.kind == "file"}:
        if key in linked:
            continue
        base = os.path.basename(key)
        if key in command:
            g.warnings.append(
                f"{ref}: mentions {key!r} but no direction could be determined")
        elif base and len(base) > 4 and base in command:
            g.warnings.append(
                f"{ref}: mentions {base!r} but no direction could be determined")


def _attach(g: ProvGraph, a_id: str, ref: str, idx: int, path: str,
            direction: str, evidence: str, version: dict[str, int]) -> None:
    """One command-derived dependency, as a LIKELY edge."""
    if direction == keys.WRITE:
        ver = version.get(path, 0) + 1
        version[path] = ver
        e_id = g.add_entity(Entity(key=path, version=ver,
                                   producer_ref=ref, born_idx=idx))
        g.add_edge(a_id, e_id, GENERATED_BY, LIKELY, evidence)
    else:
        if path not in version:
            g.add_entity(Entity(key=path, version=0, born_idx=-1))
            version[path] = 0
        prior = g.latest_version(path, before_idx=idx)
        if prior is not None:
            g.add_edge(prior.node_id, a_id, USED_BY, LIKELY, evidence)


def version_chain(g: ProvGraph, key: str) -> list[str]:
    """Human-readable version chain for one artifact.

    Renders as `v0 ->(a[68] edit) v1 ->(a[71] edit) v2`, i.e. each arrow labelled with
    the activity that produced the next version.
    """
    out: list[str] = []
    for e in g.versions_of(key):
        if e.producer_ref is None:
            out.append(f"v{e.version} (pre-existing)")
        else:
            prod = g.activities.get(f"a:{e.producer_ref}")
            label = prod.action if prod else "?"
            out.append(f"--[{e.producer_ref} {label}]--> v{e.version}")
    return out


def writes_by_path(g: ProvGraph) -> dict[str, int]:
    """Highest version reached per artifact, most rewritten first.

    A value of 0 means the run only ever read it -- one `v0` node, no writes.
    """
    counts: dict[str, int] = {}
    for e in g.entities.values():
        counts[e.key] = max(counts.get(e.key, 0), e.version)
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
