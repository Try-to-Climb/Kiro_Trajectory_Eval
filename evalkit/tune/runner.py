"""`python3 -m tune.runner --flags ... --agent ...` -- end-to-end driver.

Loads flags + trajectory + agent definition, composes the prompt, calls
Kiro, prints the suggestions. `--no-llm` is a dry run that just writes the
prompt to stdout (or `--out`) so the caller can inspect it or feed it to a
different model.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tune.agent_loader import AgentInfo, load_agent_from_json         # noqa: E402
from tune.extractor import FlagContext, build_contexts, load_flags    # noqa: E402
from tune import prompts as prompt_mod                                # noqa: E402


# ---------------------------------------------------------------------------
# Default caller: Kiro ACP. Injected by tests to avoid spawning the client.
# ---------------------------------------------------------------------------
def default_kiro_caller(agent_name: str = "kiro_default"
                        ) -> Callable[[str], str]:
    """Return a `prompt -> reply` function that spawns a Kiro ACP client on
    first use and keeps it alive for subsequent calls. The caller is single
    use per run; closing is on process exit."""
    from kiro_acp import KiroAcpClient
    client = KiroAcpClient(agent=agent_name)
    client.__enter__()                 # keep session; closed by atexit below
    import atexit
    atexit.register(lambda: client.__exit__(None, None, None))
    return lambda text: client.prompt(text)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run(flags_path: str, agent_json: str, *,
        official_dir: Optional[str] = None,
        window: int = 2,
        with_responses: bool = False,
        caller: Optional[Callable[[str], str]] = None,
        log: Callable[..., None] = lambda *a, **k: None
        ) -> tuple[str, str]:
    """Returns (composed_prompt, model_reply). `caller=None` means dry run:
    reply is the empty string."""
    data = load_flags(flags_path)
    session_id = data.get("session") or ""
    flags = data.get("flags") or []
    log(f"[tune] flags: {len(flags)} entries from session {session_id}")

    agent = load_agent_from_json(agent_json)
    log(f"[tune] agent: {agent.name}  prompt={len(agent.prompt_text)} chars  "
        f"skills={len(agent.skills)}")

    # Trajectory is only needed when a flag has neither a self-contained
    # `step` block (new layered shape) nor a `context.flagged` snapshot
    # (older flat shape). Both of these carry everything the prompt needs,
    # so `--official-dir` can be omitted and the flag file is sufficient.
    def is_self_contained(f: dict) -> bool:
        if (f.get("step") or {}).get("ref"):
            return True
        if (f.get("context") or {}).get("flagged"):
            return True
        return False

    needs_trajectory = any(
        (f.get("step", {}).get("ref") or f.get("ref"))
        and not is_self_contained(f)
        for f in flags
    )
    actions: list[dict] = []
    if needs_trajectory:
        from normalize import load_trace_from_official
        ir = load_trace_from_official(session_id, official_dir or None,
                                      include_responses=with_responses)
        actions = [a.to_dict() for a in ir.actions]
        log(f"[tune] trajectory: {len(actions)} actions")
    else:
        log(f"[tune] trajectory: skipped (all flags self-contained)")

    contexts = build_contexts(flags, actions, window=window)
    composed = prompt_mod.compose(agent, contexts, session_id=session_id)
    log(f"[tune] prompt: {len(composed)} chars")

    if caller is None:
        return composed, ""
    reply = caller(composed)
    log(f"[tune] reply: {len(reply)} chars")
    return composed, reply


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m tune.runner",
        description="Turn user-flagged steps into agent-tuning suggestions "
                    "via Kiro.")
    p.add_argument("--flags", required=True,
                   help="path to a flags.json produced by the graph "
                        "annotation overlay")
    p.add_argument("--agent", required=True,
                   help="path to the agent definition JSON "
                        "(.kiro/agents/<name>.json)")
    p.add_argument("--official-dir", default="",
                   help="where the Kiro official session records live "
                        "(defaults to the normalize module's search)")
    p.add_argument("--context", type=int, default=2, metavar="N",
                   help="number of steps before and after each flag to "
                        "include as context (default: 2)")
    p.add_argument("--with-responses", action="store_true",
                   help="include tool responses from the trajectory "
                        "(bigger prompt, often clearer)")
    p.add_argument("--no-llm", action="store_true",
                   help="dry run: write the composed prompt to stdout / --out "
                        "and do not call Kiro")
    p.add_argument("--out", default="",
                   help="write the suggestions (or the dry-run prompt) to "
                        "this file instead of stdout")
    p.add_argument("--kiro-agent", default="kiro_default",
                   help="which Kiro agent to call (default: kiro_default)")
    p.add_argument("--save-prompt", default="",
                   help="write the exact prompt sent to Kiro to this path "
                        "(audit trail; default: `<--out>.prompt.md` sidecar "
                        "when --out is set, otherwise none)")
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress logs on stderr")
    args = p.parse_args(argv)

    log = (lambda *a, **k: None) if args.quiet \
        else lambda *a, **k: print(*a, file=sys.stderr, **k)

    try:
        caller = None if args.no_llm \
            else default_kiro_caller(args.kiro_agent)
        composed, reply = run(
            flags_path=args.flags,
            agent_json=args.agent,
            official_dir=args.official_dir or None,
            window=args.context,
            with_responses=args.with_responses,
            caller=caller,
            log=log,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    out_text = composed if args.no_llm else reply
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
        log(f"[tune] wrote {args.out}")
        # Audit sidecar: the exact prompt sent to Kiro, next to the reply.
        # Named `.prompt.md` so `*.suggestions.md` / `*.prompt.md` pair
        # obviously. Skipped on dry-run (`out_text` IS the prompt) and when
        # the caller already explicitly asked for a different sidecar path.
        if not args.no_llm:
            base, ext = os.path.splitext(args.out)
            sidecar = args.save_prompt or (base + ".prompt.md"
                                           if ext else args.out + ".prompt.md")
            with open(sidecar, "w", encoding="utf-8") as f:
                f.write(composed)
            log(f"[tune] wrote prompt sidecar {sidecar}")
    else:
        sys.stdout.write(out_text)
        if not out_text.endswith("\n"):
            sys.stdout.write("\n")
        if args.save_prompt and not args.no_llm:
            with open(args.save_prompt, "w", encoding="utf-8") as f:
                f.write(composed)
            log(f"[tune] wrote prompt sidecar {args.save_prompt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
