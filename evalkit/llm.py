"""LLM call layer: invocation + ANSI stripping + JSON extraction + validation gate + retries.

The backend is Kiro ACP (``kiro-cli acp``, JSON-RPC over stdio). By default every
``ask`` starts a short-lived ACP session; when history must be preserved across asks,
maintain your own ``KiroAcpClient`` and inject it via ``caller=acp_caller_from(client)``.

Every LLM step's output must pass a validator; on failure the errors are handed back
to the LLM once, and if it still fails we raise LLMOutputError — no silent pass, no
silent drop (same principle as evalkit's validation gate).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from kiro_acp import KiroAcpClient

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMOutputError(RuntimeError):
    """LLM output failed validation twice."""


class LLMUnavailable(RuntimeError):
    """Backend unavailable (e.g., no kiro-cli)."""


def strip_ansi(s: str) -> str:
    """kiro-cli colorizes its output; strip ANSI codes before parsing JSON (DESIGN.md P11)."""
    return _ANSI.sub("", s or "")


def extract_json(text: str) -> Any:
    """Extract JSON from a reply. Prefer a ```json fenced block, fall back to the first balanced {...}."""
    t = strip_ansi(text)
    m = _FENCE.search(t)
    if m:
        cand = m.group(1)
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # Fallback: from the first { do bracket balancing (more robust than a greedy regex)
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    raise LLMOutputError(f"No parseable JSON in reply: {t[:200]!r}")


Validator = Callable[[Any], list[str]]     # Returns a list of errors; empty means pass


def _default_acp_caller(agent: str, timeout: int) -> Callable[[str], str]:
    """One-shot ACP caller: fresh session per prompt.

    Replaces the old ``kiro-cli chat --no-interactive`` route. Semantically
    identical for callers that only send one prompt per invocation
    (goal_completion's s2/s3/s4).
    """
    def _call(prompt: str) -> str:
        with KiroAcpClient(agent=agent, timeout=timeout) as c:
            return c.prompt(prompt)
    return _call


def ask(prompt: str, validate: Optional[Validator] = None, *,
        caller: Optional[Callable[[str], str]] = None,
        agent: str = "kiro-judge", timeout: int = 480,
        retries: int = 1, backend_retries: int = 2, backoff: float = 3.0,
        label: str = "llm") -> Any:
    """Run one LLM call and validate.

    Two classes of failure are retried separately:
      - Validation failure  → hand the errors back to the LLM, retry `retries` times
      - Backend exception   → retry `backend_retries` times (in practice kiro-cli occasionally
                              panics with exit 101; the same prompt succeeds on retry, so a
                              single failure should not abort)
    `caller` is injectable (tests stub it out without touching the real backend, or pass a
    caller bound to a long-lived ACP session so multiple asks share one history). The
    default backend is ACP with a short-lived session per ask.
    """
    call = caller or _default_acp_caller(agent, timeout)
    last_errs: list[str] = []
    # Some traces mix binary content with \x00 into prompts, but subprocess argv rejects NULs.
    # Strip control chars here (\x00 and other non-text control codes), keep normal whitespace.
    prompt = prompt.replace("\x00", "").translate(
        {i: None for i in range(32) if i not in (9, 10, 13)}
    )
    cur = prompt
    for attempt in range(retries + 1):
        raw = None
        backend_err: Optional[Exception] = None
        for b in range(backend_retries + 1):
            try:
                raw = call(cur)
                break
            except Exception as e:                  # backend crash / timeout
                backend_err = e
                if b < backend_retries:
                    time.sleep(backoff * (b + 1))
        if raw is None:
            raise LLMUnavailable(
                f"[{label}] backend call failed after {backend_retries} retries: {backend_err}"
            ) from backend_err
        try:
            data = extract_json(raw)
        except LLMOutputError as e:
            last_errs = [str(e)]
        else:
            errs = validate(data) if validate else []
            if not errs:
                return data
            last_errs = errs
        if attempt < retries:
            cur = (prompt + "\n\n====== Previous output failed validation, please fix and re-output ======\n"
                   + "\n".join(f"- {e}" for e in last_errs[:12]))
    raise LLMOutputError(f"[{label}] validation failed: " + "; ".join(last_errs[:8]))
