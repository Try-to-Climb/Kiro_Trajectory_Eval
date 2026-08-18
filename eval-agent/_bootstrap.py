"""Let eval-agent import evalkit's normalize / trajectory packages.

The fact layer (normalization) is shared with evalkit and does not fork — the
two evaluation paths must produce the same action sequence and idx numbering
for the same run, otherwise the hotspot-guided and dogfooding join points
become invalid. See DESIGN.md §5.2 and "fact layer shared, verdict layer
separate".
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EVALKIT = os.environ.get("EVALKIT_DIR") or os.path.join(os.path.dirname(_HERE), "evalkit")


def ensure_evalkit() -> str:
    """Insert the evalkit directory at the front of sys.path and return it."""
    if not os.path.isdir(_EVALKIT):
        raise RuntimeError(
            f"evalkit directory not found: {_EVALKIT} (override via EVALKIT_DIR env var)")
    if _EVALKIT not in sys.path:
        sys.path.insert(0, _EVALKIT)
    return _EVALKIT


ensure_evalkit()
