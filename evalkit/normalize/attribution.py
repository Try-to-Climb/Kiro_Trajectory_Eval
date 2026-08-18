"""Restore each run in a trace directory to its true Kiro session.

Background (confirmed by controlled experiments):
    When a parent agent dispatches child agents via the subagent tool, **Kiro
    assigns each child an independent session id and writes an independent
    official record**; but the `KIRO_SESSION_ID` the hook observes is still
    the parent's, so the child's hook events are written into the parent's
    trace directory, and the child session has no trace directory of its own.

    Experimental evidence (acptest/traces_sub):
      Parent official 70f03e0f  agent=acp-parent-test  tools read, subagent
      Child  official 57fea8ea  agent=null             tools read, summary
      hook trace has only the 70f03e0f directory; all 4 calls are there,
      including 2 agent_spawn events.

    Side effect: the agent_name inferred from /proc in the `agent_spawn`
    event is **wrong** for child agents (it comes from the --agent in the
    parent process command line). In the experiment both spawns reported
    acp-parent-test, but the second was actually acp-child-test.

Restoration method:
    Every run's first user_prompt is verbatim identical to the first Prompt
    of its corresponding official session (verified), and the official
    `.json`'s `title` is a prefix of that Prompt. So we can map a run back
    to its real session id by cwd + time window + title prefix.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .official import OfficialRecord, default_official_dir, load_official


@dataclass
class RunAttribution:
    """Attribution result for one run."""

    run: int
    prompt_head: str
    session_id: Optional[str] = None      # restored real session id
    agent_name: Optional[str] = None      # agent from official record (often null for child sessions)
    is_trace_dir_session: bool = False    # whether this is the session matching the trace directory name
    official: Optional[OfficialRecord] = None

    @property
    def resolved(self) -> bool:
        return self.session_id is not None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _index_official(official_dir: str, cwd: Optional[str],
                    lo: Optional[datetime], hi: Optional[datetime]) -> list[tuple[str, str, datetime]]:
    """Filter candidates by cwd + creation-time window: (session_id, title, created_at).

    Reads only the small `.json` and pre-filters by file mtime to avoid the
    cost of walking thousands of files.
    """
    out: list[tuple[str, str, datetime]] = []
    if not os.path.isdir(official_dir):
        return out
    lo_ts = (lo - timedelta(hours=6)).timestamp() if lo else None
    hi_ts = (hi + timedelta(hours=6)).timestamp() if hi else None
    for name in os.listdir(official_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(official_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if lo_ts is not None and st.st_mtime < lo_ts:
            continue
        if hi_ts is not None and st.st_mtime > hi_ts:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if cwd and meta.get("cwd") != cwd:
            continue
        created = _parse_iso(meta.get("created_at"))
        if created is None:
            continue
        if lo and created < lo - timedelta(minutes=5):
            continue
        if hi and created > hi + timedelta(minutes=5):
            continue
        out.append((meta.get("session_id") or name[:-5], meta.get("title") or "", created))
    return out


def resolve_runs(trace_session_id: str,
                 run_prompts: dict[int, str],
                 run_started: dict[int, Optional[datetime]],
                 cwd: Optional[str],
                 official_dir: Optional[str] = None,
                 load_full: bool = False) -> list[RunAttribution]:
    """Map each run to its real official session.

    Args:
        trace_session_id: trace directory name (= parent session id)
        run_prompts: run number → the run's first user_prompt
        run_started: run number → the run's agent_spawn timestamp
        cwd: cwd recorded in the trace, used to narrow candidates
        load_full: whether to load the full OfficialRecord for each hit (slower)
    """
    base = official_dir or default_official_dir()
    times = [t for t in run_started.values() if t]
    cands = _index_official(base, cwd, min(times) if times else None,
                            max(times) if times else None)

    used: set[str] = set()
    out: list[RunAttribution] = []
    for run in sorted(run_prompts):
        prompt = run_prompts.get(run) or ""
        att = RunAttribution(run=run, prompt_head=prompt[:60])

        # We cannot assume run1 corresponds to the session matching the trace
        # directory. Measured on f8ba129a: the parent agent itself had no hook
        # attached, so the first run in the hook trace is actually the first
        # dispatched child agent (its 35 calls match child session a88da625
        # tool-for-tool). So match every run by prompt text.
        started = run_started.get(run)
        best: Optional[tuple[float, str]] = None
        for sid, title, created in cands:
            if sid in used or not title:
                continue
            key = title.rstrip(".… ")
            if not key or not prompt.startswith(key[: min(len(key), 40)]):
                continue
            gap = abs((created - started).total_seconds()) if started else 0.0
            if best is None or gap < best[0]:
                best = (gap, sid)
        if best:
            att.session_id = best[1]
            used.add(best[1])
            att.is_trace_dir_session = best[1] == trace_session_id

        if att.session_id:
            rec = load_official(att.session_id, base)
            if rec.found:
                att.agent_name = rec.agent_name
                if load_full:
                    att.official = rec
        out.append(att)
    return out
