"""One node per turn, joined by the artifacts they hand to each other.

A run of 36 turns and 437 nodes cannot be read at once, and most of what is in it
is local: of 160 entities in the reference session, 24 are touched by more than one
turn. Those 24 are the only ones that say anything about how the turns relate, so
the overview keeps them and drops the rest.

The result is deliberately shaped like the graph it summarises -- turns where
activities were, the shared files between them -- so the same renderer draws both
and the two views read the same way.
"""
from __future__ import annotations

import collections
from typing import Any, Optional, Sequence

from .schema import (CERTAIN, CONNECTED_BY, GENERATED_BY, LIKELY, USED_BY,
                     Activity, Entity,
                     ProvGraph)

__all__ = ["build_overview", "turn_summary"]


def turn_summary(g: ProvGraph) -> dict[int, dict[str, Any]]:
    """Per turn: how many actions, and of what kinds."""
    out: dict[int, dict[str, Any]] = {}
    for a in g.activities.values():
        t = out.setdefault(a.turn, {"actions": 0, "kinds": collections.Counter(),
                                    "first_idx": a.idx, "last_idx": a.idx,
                                    "purposes": [], "thinking": ""})
        t["actions"] += 1
        t["kinds"][a.action] += 1
        t["first_idx"] = min(t["first_idx"], a.idx)
        t["last_idx"] = max(t["last_idx"], a.idx)
        if a.purpose and len(t["purposes"]) < 3:
            t["purposes"].append(a.purpose)
        # The turn's own words about what it was doing. Only 70/277 actions carry
        # thinking and it is absent from the first third of the reference session
        # entirely, so the longest one in the turn is taken -- the alternative,
        # the first, is often a one-line aside about the previous tool result.
        if a.reasoning and len(a.reasoning) > len(t["thinking"]):
            t["thinking"] = a.reasoning
    return out


def build_overview(g: ProvGraph, prompts: Sequence[str] = ()
                   ) -> tuple[ProvGraph, dict[int, dict[str, Any]]]:
    """`(overview graph, per-turn summary)`.

    Turns become activities so that everything downstream -- `select`, `to_dot`,
    the layout, the renderer -- works unchanged. `idx` is the turn number, which
    keeps the time-order invariant true: an artifact cannot be read in a turn
    earlier than the one that wrote it.

    An entity is kept when at least two different turns touch it. A file written
    and read inside one turn is real but says nothing about the shape of the run.

    `prompts` is what the user typed, indexed by turn number. It is the label on a
    turn card: what was asked identifies a turn to the person reading, where the
    agent's own thinking describes only how it answered -- and is missing from the
    first third of the reference session anyway. The thinking is kept in the panel.
    """
    summary = turn_summary(g)
    turn_of = {a.node_id: a.turn for a in g.activities.values()}

    producer_turn: dict[str, int] = {}
    consumer_turns: dict[str, set[int]] = collections.defaultdict(set)
    for e in g.edges:
        if e.kind == GENERATED_BY:
            producer_turn.setdefault(e.dst, turn_of[e.src])
        elif e.kind == USED_BY:
            consumer_turns[e.src].add(turn_of[e.dst])
        elif e.kind == CONNECTED_BY:
            # A host counts for "which turns touched this entity" the same way
            # a read does: it is a dependency, with the same direction (entity
            # → activity). Without this the overview sees a host as touched by
            # zero turns and drops it, so a session whose only cross-turn
            # artifact is a host (`example-session-x`) ends up with no overview at all
            # and lands the reader straight into a turn.
            consumer_turns[e.src].add(turn_of[e.dst])

    ov = ProvGraph(session_id=g.session_id)

    shared: dict[str, tuple[Optional[int], list[int]]] = {}
    for node_id, ent in g.entities.items():
        produced_in = producer_turn.get(node_id)
        read_in = sorted(consumer_turns.get(node_id, ()))
        touching = set(read_in) | ({produced_in} if produced_in is not None else set())
        if len(touching) < 2:
            continue
        shared[node_id] = (produced_in, read_in)

    # Every turn that has any activity gets a card. The "entities shared by
    # ≥2 turns" rule still shapes *which entity nodes* are drawn, but it no
    # longer decides whether a turn exists on the overview. A session like
    # `example-session-x` has early turns of pure probes (grep / knowledge_search) and
    # later turns that reach a host; the earlier ones should not vanish from
    # the overview just because they produced no cross-turn hand-off.
    used_turns = sorted({a.turn for a in g.activities.values()})
    # A one-turn session has nothing cross-turn to summarise -- `to_html`
    # treats an empty overview as "skip the overview view", so the single
    # turn is the whole page and no stray "overview with one card" shows up.
    if len(used_turns) < 2:
        return ov, {}
    for t in used_turns:
        s = summary.get(t, {"actions": 0, "kinds": collections.Counter()})
        kinds = ", ".join(f"{n}×{k.replace('_', ' ')}"
                          for k, n in s["kinds"].most_common(2))
        # `command` is what the card body shows for an action; for a turn it is
        # what the user asked, so the renderer needs no special case.
        asked = prompts[t - 1] if 0 < t <= len(prompts) else ""
        think = s.get("thinking", "")
        ov.add_activity(Activity(
            ref=f"turn{t}", idx=t, turn=t, action="turn",
            purpose=f"{s['actions']} actions · {kinds}" if kinds else "",
            command=asked or "(no prompt recorded for this turn)",
            reasoning=think or "\n".join(s.get("purposes", ())),
        ))

    for node_id, (produced_in, read_in) in shared.items():
        ent = g.entities[node_id]
        e_id = ov.add_entity(Entity(
            key=ent.key, version=ent.version, kind=ent.kind,
            producer_ref=f"turn{produced_in}" if produced_in is not None else None,
            born_idx=produced_in if produced_in is not None else -1))
        if produced_in is not None:
            ov.add_edge(f"a:turn{produced_in}", e_id, GENERATED_BY, CERTAIN,
                        f"written during turn {produced_in}")
        # For a host, the overview edge is `connectedBy` -- same direction as
        # `usedBy` but the label on the picture should say "contacted", not
        # "read", because the far-side file wasn't the point.
        edge_kind = CONNECTED_BY if ent.kind == "host" else USED_BY
        verb = "contacted" if ent.kind == "host" else "read"
        for t in read_in:
            if t == produced_in:
                continue          # within one turn: not what this view is about
            try:
                ov.add_edge(e_id, f"a:turn{t}", edge_kind, CERTAIN,
                            f"{verb} during turn {t}")
            except Exception:
                # A read in the same turn as the write, or before it, is not a
                # hand-off between turns; the invariants reject it and that is
                # the right answer here.
                continue

    return ov, summary
