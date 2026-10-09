"""What became of each artifact the run produced.

The original plan for this task was to find *dead work* -- activities whose
results nothing downstream used. A prototype showed that cannot be done on this
graph yet: it called 220 of 277 activities dead, including
`Read the full log to understand the failure`, which is what led to the fix. A
diagnostic action reads a file and writes nothing, so its output is knowledge,
and knowledge only exists on the graph as `informedBy` edges that are not built
yet. Launch actions fared no better, because the log a background process writes
is not named in the launching command.

So this module answers the question structure *can* answer: not "which effort was
wasted" but "what happened to each thing the run wrote". No inference, no new
edges.

Naming is deliberately literal. `unconsumed` says nothing read it, not that it is
a deliverable -- a final report and an abandoned scratch file look identical from
here, and telling them apart needs a reader. The one judgement worth flagging is
`superseded` with no reads in between: written, overwritten, never looked at.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .schema import USED_BY, ProvGraph

__all__ = ["Fate", "classify", "summary", "INPUT", "SUPERSEDED", "UNCONSUMED",
           "CONSUMED"]

INPUT = "input"                   # @v0: read here, produced before the run
SUPERSEDED = "superseded"         # a later version of the same key replaced it
UNCONSUMED = "unconsumed"         # newest version, nothing read it
CONSUMED = "consumed"             # newest version, something read it


@dataclass(frozen=True)
class Fate:
    """One entity version and what happened to it."""
    node_id: str
    key: str
    version: int
    kind: str
    fate: str
    producer_ref: Optional[str]
    consumers: tuple[str, ...]          # activity refs that read it
    superseded_by: Optional[str]        # node_id of the version that replaced it

    @property
    def written_and_never_read(self) -> bool:
        """Overwritten without anything reading it first."""
        return self.fate == SUPERSEDED and not self.consumers


def classify(g: ProvGraph, include_pids: bool = False) -> list[Fate]:
    """Every entity's fate, in `(key, version)` order.

    PIDs are excluded by default: a process handle is not an artifact, and every
    PID is unconsumed unless a later command happens to name it, which would
    swamp the counts.
    """
    readers: dict[str, list[str]] = {}
    for e in g.edges:
        if e.kind != USED_BY:
            continue
        act = g.activities.get(e.dst)
        if act is not None:
            readers.setdefault(e.src, []).append(act.ref)

    newest: dict[str, Any] = {}
    for ent in g.entities.values():
        cur = newest.get(ent.key)
        if cur is None or ent.version > cur.version:
            newest[ent.key] = ent

    out: list[Fate] = []
    for ent in g.entities.values():
        if ent.kind == "pid" and not include_pids:
            continue
        consumers = tuple(readers.get(ent.node_id, ()))
        top = newest[ent.key]
        if ent.version == 0 and ent.producer_ref is None:
            fate, replaced_by = INPUT, (top.node_id if top is not ent else None)
        elif ent is not top:
            fate, replaced_by = SUPERSEDED, top.node_id
        elif consumers:
            fate, replaced_by = CONSUMED, None
        else:
            fate, replaced_by = UNCONSUMED, None
        out.append(Fate(node_id=ent.node_id, key=ent.key, version=ent.version,
                        kind=ent.kind, fate=fate,
                        producer_ref=ent.producer_ref, consumers=consumers,
                        superseded_by=replaced_by))
    return sorted(out, key=lambda f: (f.key, f.version))


def summary(g: ProvGraph, include_pids: bool = False) -> dict[str, Any]:
    """Counts per fate, plus the two figures worth acting on."""
    fates = classify(g, include_pids)
    counts: dict[str, int] = {}
    for f in fates:
        counts[f.fate] = counts.get(f.fate, 0) + 1
    wasted = [f for f in fates if f.written_and_never_read]
    return {
        "entities": len(fates),
        "counts": counts,
        "produced": sum(1 for f in fates if f.fate != INPUT),
        "written_and_never_read": len(wasted),
        "rewrite_champion": max(
            ((f.key, f.version) for f in fates), key=lambda kv: kv[1],
            default=None),
    }


def to_text(g: ProvGraph, include_pids: bool = False, limit: int = 20) -> str:
    """A short report, ordered so the actionable part comes first."""
    fates = classify(g, include_pids)
    s = summary(g, include_pids)
    lines = [f"{g.session_id}  artifacts",
             f"  {s['produced']} produced by this run, "
             f"{s['counts'].get(INPUT, 0)} pre-existing inputs"]
    for name, label in ((UNCONSUMED, "nothing read them"),
                        (CONSUMED, "read downstream"),
                        (SUPERSEDED, "replaced by a later version")):
        lines.append(f"  {s['counts'].get(name, 0):4d}  {name:11s} {label}")

    wasted = [f for f in fates if f.written_and_never_read]
    if wasted:
        lines.append(f"\n  written, overwritten, never read ({len(wasted)}):")
        for f in wasted[:limit]:
            lines.append(f"    {f.key}@v{f.version}  by {f.producer_ref}")
        if len(wasted) > limit:
            lines.append(f"    … {len(wasted) - limit} more")

    left = [f for f in fates if f.fate == UNCONSUMED]
    if left:
        lines.append(f"\n  what the run left behind ({len(left)}):")
        for f in left[:limit]:
            lines.append(f"    {f.key}@v{f.version}  by {f.producer_ref}")
        if len(left) > limit:
            lines.append(f"    … {len(left) - limit} more")
    return "\n".join(lines)
