"""Kiro ACP (Agent Client Protocol) client.

Replaces the previous ``--no-interactive`` mode that had to be re-invoked for
every LLM call and could not carry context across turns.

ACP is JSON-RPC 2.0 over stdio (newline-delimited on stdout, one JSON object
per line). The agent keeps its own conversation history keyed by
``sessionId``, so callers only send the *new* prompt each turn — no manual
concatenation needed.

Usage::

    with KiroAcpClient(agent="kiro-judge") as c:
        reply1 = c.prompt("hi")            # first turn
        reply2 = c.prompt("what did I just say?")  # remembers turn 1

Design notes:
- ``prompt`` blocks until ``session/prompt`` returns a ``stopReason``,
  concatenating any ``agent_message_chunk`` text along the way.
- ``session/update`` notifications with other update kinds (``tool_call``,
  ``tool_call_update``, ``plan``, ``available_commands_update``, ...) are
  captured into ``self.updates`` for optional inspection but do not affect
  the return value.
- The subprocess is killed on ``close``; the ACP protocol has no explicit
  shutdown RPC.
- Errors (non-zero exit, JSON-RPC error field, stopReason=="error") raise
  ``KiroAcpError``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional


class KiroAcpError(RuntimeError):
    """Any failure in the ACP transport or agent-side error response."""


class KiroAcpClient:
    """Persistent Kiro ACP session.

    One instance == one ``kiro-cli acp`` subprocess == one ACP session
    (created lazily on first ``prompt`` call, or eagerly via
    ``start_session``).
    """

    def __init__(self, agent: str = "kiro-judge", *,
                 trust_tools: str = "",
                 cwd: Optional[str] = None,
                 effort: Optional[str] = None,
                 timeout: float = 300.0,
                 log_hook: Optional[Callable[[str, Any], None]] = None):
        """
        Args:
            agent:       ``--agent`` value; also becomes the initial mode.
            trust_tools: forwarded as ``--trust-tools=<value>``. Empty
                         string means "trust no tools" (safe default for
                         judge agents).
            cwd:         working directory reported to the agent in
                         ``session/new`` (default: current working dir).
            effort:      forwarded as ``--effort`` if set.
            timeout:     per-``prompt`` wall-clock cap in seconds.
            log_hook:    optional ``(event, payload) -> None`` callback for
                         auditing. Events: ``send``, ``recv``, ``update``,
                         ``stop``, ``spawn``, ``close``.
        """
        self.agent = agent
        self.trust_tools = trust_tools
        self.cwd = cwd or os.getcwd()
        self.effort = effort
        self.timeout = timeout
        self.log_hook = log_hook

        self._proc: Optional[subprocess.Popen] = None
        self._msgs: "queue.Queue[dict]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_buf: list[bytes] = []
        self._rid: int = 0
        self.session_id: Optional[str] = None
        # Collected non-response updates from the most recent prompt turn.
        self.updates: list[dict] = []

    # ------------------------------------------------------------------
    # subprocess plumbing
    # ------------------------------------------------------------------
    def _log(self, event: str, payload: Any) -> None:
        if self.log_hook:
            try: self.log_hook(event, payload)
            except Exception: pass

    def _spawn(self) -> None:
        cmd = ["kiro-cli", "acp", f"--trust-tools={self.trust_tools}",
               "--agent", self.agent]
        if self.effort:
            cmd += ["--effort", self.effort]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._log("spawn", {"pid": self._proc.pid, "cmd": cmd})

        def stdout_reader():
            assert self._proc and self._proc.stdout
            for line in iter(self._proc.stdout.readline, b""):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                self._msgs.put(obj)

        def stderr_reader():
            assert self._proc and self._proc.stderr
            for line in iter(self._proc.stderr.readline, b""):
                self._stderr_buf.append(line)

        self._reader_thread = threading.Thread(target=stdout_reader, daemon=True)
        self._reader_thread.start()
        threading.Thread(target=stderr_reader, daemon=True).start()

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin, "client not started"
        data = (json.dumps(obj) + "\n").encode()
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except BrokenPipeError as e:
            raise KiroAcpError(f"kiro-cli stdin closed: {e}; "
                               f"stderr={self._stderr_tail()}") from e
        self._log("send", obj)

    def _wait_response(self, rid: int, timeout: float) -> dict:
        """Pull messages until a response with matching id arrives.

        Non-matching messages (notifications, other-id responses) are
        preserved in ``self.updates`` (notifications) or re-queued so we
        don't drop the ordering.
        """
        deadline = time.time() + timeout
        pending: list[dict] = []
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise KiroAcpError(f"timed out waiting for response id={rid}")
                try:
                    m = self._msgs.get(timeout=remaining)
                except queue.Empty:
                    raise KiroAcpError(f"timed out waiting for response id={rid}")
                if m.get("id") == rid and ("result" in m or "error" in m):
                    self._log("recv", m)
                    return m
                # It's a notification or a response we don't own — remember.
                pending.append(m)
        finally:
            for x in pending:
                self._msgs.put(x)

    def _stderr_tail(self, n: int = 500) -> str:
        blob = b"".join(self._stderr_buf)[-n:]
        return blob.decode(errors="replace")

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def start_session(self) -> str:
        """Start subprocess + initialize + session/new. Returns session_id."""
        if self._proc is None:
            self._spawn()

        # initialize
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        resp = self._wait_response(rid, timeout=30)
        if "error" in resp:
            raise KiroAcpError(f"initialize failed: {resp['error']}")

        # session/new
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": "session/new",
                    "params": {"cwd": self.cwd, "mcpServers": []}})
        resp = self._wait_response(rid, timeout=60)
        if "error" in resp:
            raise KiroAcpError(f"session/new failed: {resp['error']}")
        self.session_id = resp["result"]["sessionId"]
        return self.session_id

    def prompt(self, text: str) -> str:
        """Send one prompt turn, return the full agent text reply.

        Also populates ``self.updates`` with any session/update notifications
        that arrived during this turn (tool_call, plan, etc.), so callers
        that want the raw stream can inspect them.
        """
        if self.session_id is None:
            self.start_session()

        rid = self._next_id()
        # kiro-cli argv path rejects NUL and some control chars; ACP is
        # JSON-encoded so \x00 is fine, but keep the belt-and-braces cleanup
        # aligned with llm.ask's behavior.
        text = text.replace("\x00", "").translate(
            {i: None for i in range(32) if i not in (9, 10, 13)}
        )
        self._send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
                    "params": {"sessionId": self.session_id,
                               "prompt": [{"type": "text", "text": text}]}})

        chunks: list[str] = []
        self.updates = []
        deadline = time.time() + self.timeout
        pending: list[dict] = []
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise KiroAcpError(
                        f"prompt timeout after {self.timeout}s; "
                        f"chunks_so_far={len(chunks)}; "
                        f"stderr={self._stderr_tail()}")
                try:
                    m = self._msgs.get(timeout=remaining)
                except queue.Empty:
                    raise KiroAcpError(f"prompt timeout after {self.timeout}s")
                if m.get("id") == rid and ("result" in m or "error" in m):
                    self._log("recv", m)
                    if "error" in m:
                        raise KiroAcpError(f"session/prompt error: {m['error']}")
                    stop = (m.get("result") or {}).get("stopReason")
                    self._log("stop", stop)
                    if stop and stop not in ("end_turn", "max_tokens",
                                             "max_turn_requests"):
                        # error stopReasons: refusal, cancelled, ...
                        # still return whatever text we got, but signal it
                        # in an exception so the caller can decide.
                        raise KiroAcpError(
                            f"unexpected stopReason={stop!r}; "
                            f"text={''.join(chunks)[:500]!r}")
                    return "".join(chunks)
                if m.get("method") == "session/update":
                    u = (m.get("params") or {}).get("update") or {}
                    self.updates.append(u)
                    self._log("update", u)
                    kind = u.get("sessionUpdate")
                    if kind == "agent_message_chunk":
                        content = u.get("content") or {}
                        if isinstance(content, dict) and "text" in content:
                            chunks.append(content["text"])
                    continue
                # Unknown message — keep it, might be a delayed response.
                pending.append(m)
        finally:
            for x in pending:
                self._msgs.put(x)

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        except Exception:
            pass
        self._log("close", {"stderr_tail": self._stderr_tail()})
        self._proc = None

    # context manager
    def __enter__(self) -> "KiroAcpClient":
        self.start_session()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Adapter: make an ACP client look like the old `kiro_caller(prompt) -> str`.
# Lets us plug the ACP client into `llm.ask(prompt, validator, caller=...)`
# without changing llm.ask's contract.
# ---------------------------------------------------------------------------
def acp_caller_from(client: KiroAcpClient) -> Callable[[str], str]:
    def _call(prompt: str) -> str:
        return client.prompt(prompt)
    return _call


# ---------------------------------------------------------------------------
# CLI smoke test:  python3 -m kiro_acp "hi"  or  python3 kiro_acp.py "hi"
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Reply with the JSON {\"ok\":true}."
    with KiroAcpClient(agent="kiro-judge",
                       log_hook=lambda ev, p: print(f"[{ev}]",
                            (json.dumps(p)[:200] if not isinstance(p, str) else p[:200]),
                            file=sys.stderr)) as c:
        print("SESSION:", c.session_id, file=sys.stderr)
        r1 = c.prompt(q)
        print("---REPLY 1---"); print(r1)
        r2 = c.prompt("What was my previous message? Answer verbatim in one line.")
        print("---REPLY 2 (context test)---"); print(r2)
