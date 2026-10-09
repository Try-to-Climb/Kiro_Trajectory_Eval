"""Per-Action multi-dimensional embedding cache (pickle on disk).

Design:
- Each Action's fields (command / path / purpose / reasoning / response /
  error) are embedded independently. Missing fields yield None (not embedded).
- Invalidation is per-field via sha1 hash of the source text: only fields
  whose text has changed get recomputed.
- Cache lives at ``~/.eval-agent/embed_cache/v1/{sid}.pkl``.
- The SentenceTransformer model is a lazily loaded process-level singleton
  (saves memory and startup time).

Model: all-MiniLM-L6-v2 (384-dim, strong on English, ~90MB).
"""
from __future__ import annotations

import hashlib
import os
import pickle
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MODEL_NAME = "all-MiniLM-L6-v2"
_MODEL_DIM = 384
_CACHE_VERSION = "v1"
_CACHE_ROOT = Path.home() / ".eval-agent" / "embed_cache" / _CACHE_VERSION

# Fields to embed (empty ones are skipped per action).
_DIMS = ("command", "path", "purpose", "reasoning", "response", "error")

# Cap per-text length (responses can be tens of kilobytes; truncate to avoid
# blowing up encoder throughput).
_MAX_TEXT_CHARS = 2000

# Model singleton.
_MODEL: Any = None


def get_model():
    """Lazily load the SentenceTransformer model as a singleton."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        t0 = time.time()
        _MODEL = SentenceTransformer(_MODEL_NAME)
        print(f"[embed] model {_MODEL_NAME} loaded in {time.time()-t0:.1f}s")
    return _MODEL


# ---------------------------------------------------------------------------
# Text prep + hashing
# ---------------------------------------------------------------------------
def _prep(text: Any) -> Optional[str]:
    """Clean and truncate. Returns None for empty/None (won't be embedded)."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if len(s) > _MAX_TEXT_CHARS:
        s = s[:_MAX_TEXT_CHARS]
    return s


def _hash(text: str) -> str:
    """Short hash for invalidation checks."""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_dim(action: dict, dim: str) -> Optional[str]:
    """Pull the source text for one dimension out of an action dict."""
    if dim == "command":
        return _prep(action.get("command"))
    if dim == "path":
        return _prep(action.get("path") or action.get("root"))
    if dim == "purpose":
        return _prep(action.get("purpose"))
    if dim == "reasoning":
        return _prep(action.get("reasoning"))
    if dim == "response":
        return _prep(action.get("response"))
    if dim == "error":
        return _prep(action.get("error"))
    return None


# ---------------------------------------------------------------------------
# Cache load / save
# ---------------------------------------------------------------------------
def _cache_path(sid: str) -> Path:
    return _CACHE_ROOT / f"{sid}.pkl"


def _load_cache(sid: str) -> dict:
    p = _cache_path(sid)
    if not p.exists():
        return {"sid": sid, "model": _MODEL_NAME, "model_dim": _MODEL_DIM,
                "entries": {}}
    try:
        with open(p, "rb") as f:
            data = pickle.load(f)
        # Discard on model / schema mismatch.
        if data.get("model") != _MODEL_NAME:
            return {"sid": sid, "model": _MODEL_NAME, "model_dim": _MODEL_DIM,
                    "entries": {}}
        return data
    except Exception:
        return {"sid": sid, "model": _MODEL_NAME, "model_dim": _MODEL_DIM,
                "entries": {}}


def _save_cache(sid: str, data: dict) -> None:
    """Atomic write: tmp file + rename, so a partial write on a full disk
    doesn't corrupt an existing cache."""
    p = _cache_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".pkl.tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception as e:
        # Do not let a cache-write failure kill the pipeline: the vectors are
        # already in memory and usable.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(f"embed cache write failed: {e}")


# ---------------------------------------------------------------------------
# Main entry point: compute_embeds
# ---------------------------------------------------------------------------
def compute_embeds(sid: str, actions: list[dict],
                   verbose: bool = True) -> dict:
    """Compute multi-dim embeddings for a list of actions, reusing the cache
    and only recomputing fields whose source text has changed.

    Returns::

        {
          "cache": {sid, model, model_dim,
                    entries: {ref: {text_hashes, vectors}}},
          "stats": {reused, computed, dim_counts, elapsed_s}
        }
    """
    cache = _load_cache(sid)
    entries = cache["entries"]

    to_compute = []   # [(ref, dim, text, hash)]
    reused = 0
    dim_counts = {d: 0 for d in _DIMS}

    for a in actions:
        ref = a.get("ref")
        if not ref:
            continue
        entry = entries.setdefault(ref, {"text_hashes": {}, "vectors": {}})

        for dim in _DIMS:
            text = _extract_dim(a, dim)
            if text is None:
                # Empty field -> not embedded; if a prior value existed we
                # leave it alone (harmless).
                continue

            h = _hash(text)
            dim_counts[dim] += 1

            if entry["text_hashes"].get(dim) == h and dim in entry["vectors"]:
                reused += 1
                continue

            to_compute.append((ref, dim, text, h))

    if verbose:
        total_seen = sum(dim_counts.values())
        print(f"[embed] {sid[:8]}: {total_seen} fields total, "
              f"{reused} reused, {len(to_compute)} to compute")

    # Batch encode.
    t0 = time.time()
    if to_compute:
        model = get_model()
        texts = [x[2] for x in to_compute]
        vecs = model.encode(texts, batch_size=32,
                            show_progress_bar=verbose,
                            convert_to_numpy=True)
        for (ref, dim, _text, h), vec in zip(to_compute, vecs):
            entries[ref]["text_hashes"][dim] = h
            entries[ref]["vectors"][dim] = vec.astype(np.float32)
    elapsed = time.time() - t0

    if verbose:
        rate = len(to_compute) / elapsed if elapsed > 0 else 0
        print(f"[embed] encode done in {elapsed:.1f}s ({rate:.1f} texts/s)")

    # Persist.
    if to_compute:
        _save_cache(sid, cache)
        if verbose:
            size_kb = _cache_path(sid).stat().st_size / 1024
            print(f"[embed] cache saved to {_cache_path(sid).name}: {size_kb:.0f} KB")

    return {
        "cache": cache,
        "stats": {
            "reused": reused,
            "computed": len(to_compute),
            "dim_counts": dim_counts,
            "elapsed_s": elapsed,
        }
    }


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------
def get_entry(cache: dict, ref: str) -> Optional[dict]:
    """Return the multi-dim vector record for an action by ref."""
    return cache["entries"].get(ref)


def get_vector(cache: dict, ref: str, dim: str) -> Optional[np.ndarray]:
    """Return one vector by (ref, dim). None if that field wasn't embedded."""
    entry = cache["entries"].get(ref)
    if not entry:
        return None
    return entry["vectors"].get(dim)
