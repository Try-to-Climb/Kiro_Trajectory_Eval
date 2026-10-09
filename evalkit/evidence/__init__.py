"""Evidence layer -- a restricted read-only query API over a run tree.

Everything here is deterministic; no LLM is involved. Shared by both
LLM-based evaluators: `goal` and `efficiency`. It sits on top of
`normalize` and adds what a multi-session view needs -- RunTree assembly,
an IR pickle cache, canonicalized shell payloads, and the retrieval /
hard-check primitives the judges' evidence packs are built from.

Inserts the evalkit root into sys.path so the modules here can `import
normalize` regardless of which entry point (goal.runner,
efficiency.runner, a tool under goal/tools/, or unittest) started the
process.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
