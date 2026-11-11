"""Resolve each flag against the original trajectory to produce a context
block the tuning LLM can reason about.

The flags.json produced by the graph annotation overlay has enough information
to look up a step (`ref`, `idx`, `action`, `purpose`), but the agent's own
reasoning + command text + what happened right before / after is only in the
normalised trajectory. This module reads flags, loads the session once, and
emits one `FlagContext` per flag with that information already stitched in.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class StepSnippet:
    """One action's record, condensed for the prompt."""
    idx: int
    turn: int
    action: str
    purpose: str = ""
    reasoning: str = ""
    command: str = ""
    path: str = ""
    response_head: str = ""   # first N chars of the tool response, if any

    def to_block(self, max_cmd: int = 400, max_think: int = 400,
                 max_resp: int = 200) -> str:
        parts = [f"#{self.idx} · turn {self.turn} · {self.action}"]
        if self.purpose:
            parts.append(f"  purpose: {self.purpose}")
        if self.path:
            parts.append(f"  path: {self.path}")
        if self.command:
            cmd = self.command.replace("\n", "\n    ")
            if len(cmd) > max_cmd:
                cmd = cmd[:max_cmd] + "…"
            parts.append(f"  command: {cmd}")
        if self.reasoning:
            th = self.reasoning
            if len(th) > max_think:
                th = th[:max_think] + "…"
            parts.append(f"  think: {th}")
        if self.response_head:
            r = self.response_head
            if len(r) > max_resp:
                r = r[:max_resp] + "…"
            parts.append(f"  response: {r}")
        return "\n".join(parts)


@dataclass
class FlagContext:
    """A single flag together with the surrounding window of steps."""
    ref: str
    tag: str
    note: str
    before: list[StepSnippet] = field(default_factory=list)
    flagged: Optional[StepSnippet] = None
    after: list[StepSnippet] = field(default_factory=list)

    def to_block(self, before_label: str = "before",
                 after_label: str = "after") -> str:
        blocks = [f"<flag ref=\"{self.ref}\" tag=\"{self.tag}\">"]
        if self.note:
            blocks.append(f"  user note: {self.note}")
        if self.before:
            blocks.append(f"  --- {before_label} ---")
            blocks.extend("  " + s.to_block().replace("\n", "\n  ")
                          for s in self.before)
        if self.flagged:
            blocks.append("  --- FLAGGED STEP ---")
            blocks.append("  " + self.flagged.to_block().replace("\n", "\n  "))
        if self.after:
            blocks.append(f"  --- {after_label} ---")
            blocks.extend("  " + s.to_block().replace("\n", "\n  ")
                          for s in self.after)
        blocks.append("</flag>")
        return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_flags(path: str) -> dict:
    """Parse a flags file. Raises on anything that is not shape-valid, so a
    caller does not accidentally pretend it worked."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "flags" not in data:
        raise ValueError(f"{path}: not a flags file (missing 'flags')")
    if not isinstance(data["flags"], list):
        raise ValueError(f"{path}: 'flags' is not a list")
    return data


def _snip(action: dict) -> StepSnippet:
    """Turn a normalised Action dict into a StepSnippet."""
    resp = action.get("response") or ""
    if isinstance(resp, dict):
        resp = resp.get("text") or json.dumps(resp)[:400]
    return StepSnippet(
        idx=int(action.get("idx", -1)),
        turn=int(action.get("turn", 0)),
        action=str(action.get("action") or ""),
        purpose=str(action.get("purpose") or ""),
        reasoning=str(action.get("reasoning") or ""),
        command=str(action.get("command") or ""),
        path=str(action.get("path") or ""),
        response_head=str(resp)[:800],
    )


def _snip_embedded(snap: dict) -> StepSnippet:
    """Turn an embedded context snapshot (written by the graph annotation
    overlay) into a StepSnippet. Same shape as `_snip` but no response field:
    the HTML export does not embed tool responses, by design -- they can be
    large and are already in the trajectory for anyone who wants them."""
    return StepSnippet(
        idx=int(snap.get("idx", -1)),
        turn=int(snap.get("turn", 0)),
        action=str(snap.get("action") or ""),
        purpose=str(snap.get("purpose") or ""),
        reasoning=str(snap.get("reasoning") or ""),
        command=str(snap.get("command") or ""),
        path=str(snap.get("path") or ""),
        response_head="",
    )


def build_contexts(flags: list[dict], actions: list[dict],
                   window: int = 2) -> list[FlagContext]:
    """Match each flag to its action and attach a `window` of neighbours.

    Three input shapes are accepted:

    * **Layered, self-contained** (current graph export):
      ``{tag, note, ts, step: {ref, idx, action, ...},
         context: {before, after}}``

    * **Flat, self-contained** (earlier graph export):
      ``{ref, idx, action, ..., tag, note,
         context: {flagged, before, after}}``

    * **Reference-only** (pre-context era): just enough fields to look up
      the step in the trajectory. `actions` is required for this path.

    Matching prefers `ref` (session-unique) and falls back to `idx`.
    """
    by_ref: dict[str, int] = {}
    by_idx: dict[int, int] = {}
    for i, a in enumerate(actions):
        if a.get("ref"):
            by_ref[a["ref"]] = i
        try:
            by_idx[int(a.get("idx", -1))] = i
        except (TypeError, ValueError):
            pass

    out: list[FlagContext] = []
    for f in flags:
        if not isinstance(f, dict):
            continue
        tag = str(f.get("tag") or "")
        note = str(f.get("note") or "")
        step = f.get("step") if isinstance(f.get("step"), dict) else None
        ref = str((step or {}).get("ref") or f.get("ref") or "")
        if not ref or not tag:
            continue

        # Path 1: new layered shape -- `step` carries the flagged snapshot,
        # `context.before/after` carry the neighbours. No trajectory needed.
        if step:
            emb_ctx = f.get("context") or {}
            out.append(FlagContext(
                ref=ref, tag=tag, note=note,
                before=[_snip_embedded(s)
                        for s in emb_ctx.get("before") or [] if isinstance(s, dict)],
                flagged=_snip_embedded(step),
                after=[_snip_embedded(s)
                       for s in emb_ctx.get("after") or [] if isinstance(s, dict)],
            ))
            continue

        # Path 2: older flat shape -- `context.flagged` carries the snapshot.
        emb = f.get("context")
        if isinstance(emb, dict) and emb.get("flagged"):
            out.append(FlagContext(
                ref=ref, tag=tag, note=note,
                before=[_snip_embedded(s)
                        for s in emb.get("before") or [] if isinstance(s, dict)],
                flagged=_snip_embedded(emb["flagged"]),
                after=[_snip_embedded(s)
                       for s in emb.get("after") or [] if isinstance(s, dict)],
            ))
            continue

        # Path 3: look up the action in the trajectory.
        pos = by_ref.get(ref)
        if pos is None:
            try:
                pos = by_idx.get(int(f.get("idx", -1)))
            except (TypeError, ValueError):
                pos = None
        if pos is None:
            # Flag refers to a step that is not in the trajectory. Keep the
            # record so the caller can still show the user's note, but with
            # no snippet attached.
            out.append(FlagContext(ref=ref, tag=tag, note=note))
            continue

        lo = max(0, pos - window)
        hi = min(len(actions), pos + window + 1)
        ctx = FlagContext(
            ref=ref, tag=tag, note=note,
            before=[_snip(actions[i]) for i in range(lo, pos)],
            flagged=_snip(actions[pos]),
            after=[_snip(actions[i]) for i in range(pos + 1, hi)],
        )
        out.append(ctx)
    return out
