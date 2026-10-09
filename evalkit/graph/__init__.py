"""Trajectory provenance graph -- turns a chronological run into a bipartite
graph of activities (tool calls) and entities (data artifacts).

Purpose is comprehension, not evaluation: this module produces no verdict and
is not a fourth evaluation path. It is a derivation of the fact layer that
`rule`, `goal` and `efficiency` may all consume.

See DESIGN.md for the plan, the AgentTrails reference (arXiv:2607.18816) and
the measurements the design rests on. Task 1 (this schema) is implemented;
graph construction starts at Task 2.
"""
from __future__ import annotations

from .schema import (
    CERTAIN,
    CONFIDENCE_LEVELS,
    EDGE_KINDS,
    GENERATED_BY,
    INFORMED_BY,
    LIKELY,
    USED_BY,
    WEAK,
    Activity,
    Edge,
    Entity,
    GraphError,
    ProvGraph,
)

__all__ = [
    "Activity",
    "Entity",
    "Edge",
    "ProvGraph",
    "GraphError",
    "GENERATED_BY",
    "USED_BY",
    "INFORMED_BY",
    "EDGE_KINDS",
    "CERTAIN",
    "LIKELY",
    "WEAK",
    "CONFIDENCE_LEVELS",
]
