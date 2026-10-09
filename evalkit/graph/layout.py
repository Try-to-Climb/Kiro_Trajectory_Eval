"""Layout from graphviz, drawing our own.

`dot -Tsvg` produces boxes with the label inside, and no amount of CSS turns a
box into the lit circle-and-halo look of `ref/web-example.html`: the shape, the
label placement and the edge curvature are all baked into the SVG graphviz emits.

`dot -Tjson` gives the part worth keeping -- node positions and the bezier
control points of every edge -- and leaves the drawing to us. The layered DAG
that makes the picture legible is graphviz's real contribution, and in timeline
mode it is what guarantees `idx` order down the page; a force-directed layout of
the kind the reference uses would throw that away.

So: circles sized by how much passes through them, radial fills, an SVG blur
filter for the halo, curved edges that fade, and labels outside the node where
they do not have to fit inside it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Optional

__all__ = ["LayoutError", "layout"]


class LayoutError(RuntimeError):
    """Raised when graphviz cannot be run."""


def _run(dot_source: str) -> dict[str, Any]:
    if not shutil.which("dot"):
        raise LayoutError(
            "graphviz is required for --format html (positions come from "
            "`dot -Tjson`); install graphviz, or use --format dot and render "
            "elsewhere")
    p = subprocess.run(["dot", "-Tjson"], input=dot_source, text=True,
                       capture_output=True)
    if p.returncode != 0:
        raise LayoutError(f"dot failed: {p.stderr.strip()[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as exc:            # pragma: no cover
        raise LayoutError(f"dot produced unreadable json: {exc}") from exc


def _points(spline: str) -> list[tuple[float, float]]:
    """The two ends of one edge: `[start, end]`.

    graphviz writes `pos` as an arrow stub followed by a bezier chain. Which end
    the stub belongs to depends on the arrow direction: `e,<pt>` is the head end,
    `s,<pt>` the tail end -- and `dir=back`, which the timeline view puts on every
    read edge, produces the `s,` form. Treating both the same overwrote the real
    endpoint with the start stub, so every read curved out towards its action and
    then came back to land three pixels from its own file.

    The chain itself is no longer used. Once a node can be dragged the curve has
    to be recomputed from positions anyway, and having two ways to draw the same
    edge meant the picture changed shape the first time anything moved. Both ends
    are all the caller needs.
    """
    head = tail = None
    pts: list[tuple[float, float]] = []
    for token in spline.split():
        if token.startswith(("e,", "s,")):
            x, y = token[2:].split(",")
            if token[0] == "e":
                head = (float(x), float(y))
            else:
                tail = (float(x), float(y))
            continue
        x, y = token.split(",")
        pts.append((float(x), float(y)))
    if not pts:
        return []
    start = tail if tail is not None else pts[0]
    end = head if head is not None else pts[-1]
    return [start, end]


def layout(dot_source: str) -> dict[str, Any]:
    """`{width, height, nodes, edges}` in SVG coordinates.

    graphviz measures in points with the origin bottom-left; SVG counts down
    from the top, so every `y` is flipped once here rather than at each use.
    """
    raw = _run(dot_source)
    x0, y0, x1, y1 = (float(v) for v in raw["bb"].split(","))
    height = y1 - y0

    def flip(y: float) -> float:
        return height - (y - y0)

    nodes = []
    by_gvid: dict[int, str] = {}
    for obj in raw.get("objects", ()):
        name = obj.get("name", "")
        by_gvid[obj.get("_gvid")] = name
        px, py = (float(v) for v in obj.get("pos", "0,0").split(","))
        nodes.append({
            "id": name,
            "x": px - x0,
            "y": flip(py),
            # graphviz reports size in inches at 72 points per inch.
            "w": float(obj.get("width", 1)) * 72,
            "h": float(obj.get("height", .5)) * 72,
            "label": obj.get("label", name),
        })

    edges = []
    for e in raw.get("edges", ()):
        if e.get("style") == "invis":
            continue                    # the ordering spine is not for drawing
        pts = [(px - x0, flip(py)) for px, py in _points(e.get("pos", ""))]
        if len(pts) < 2:
            continue
        edges.append({
            "src": by_gvid.get(e.get("tail"), ""),
            "dst": by_gvid.get(e.get("head"), ""),
            "points": pts,
        })

    return {"width": x1 - x0, "height": height, "nodes": nodes, "edges": edges}
