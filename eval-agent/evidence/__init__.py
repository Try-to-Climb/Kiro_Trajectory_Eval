"""Evidence layer: restricted read-only query API over the run tree
(fully deterministic, no LLM)."""

from __future__ import annotations

import os
import sys

# Put the eval-agent root on sys.path so that `import _bootstrap` / `import schema` work.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import _bootstrap  # noqa: E402,F401  (also inserts evalkit)
