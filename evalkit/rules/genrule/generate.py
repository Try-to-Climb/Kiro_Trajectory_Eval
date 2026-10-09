#!/usr/bin/env python3
"""generate.py — drive a multi-turn ACP conversation that extracts a policy IR from an agent
under test.

Adapted from IntellAgent's two-level decomposition (task types -> policies), with two gates it
does not need:
  1. policy extraction is forced through an **observability classification**
     (deterministic / judge_only / unobservable); only deterministic policies become
     deterministic checkers;
  2. severity is only a hint here — the final grade is calibrated against real runs by
     compile_ir.py.

Why ACP multi-turn instead of repeated --no-interactive calls: context accumulates inside one
ACP session, so the agent material is sent once on turn 1 and every later turn refers back to
it. Cheaper, and every turn provably sees the same material.

Usage:
    python3 rules/genrule/generate.py <agent.json | prompt.md> [--out-dir DIR]

One policy IR is emitted per task type, so each task type can get its own .checks.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("ACP_CLIENT_DIR",
                                  os.path.expanduser("~/acp-pipeline")))
from acp_client import AcpClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(HERE, "prompts")
EVALKIT = os.path.dirname(os.path.dirname(HERE))


def load_prompt(name: str) -> str:
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as f:
        return f.read()


def strip_json(text: str) -> str:
    """Pull the outermost JSON object out of a reply that may carry fences or chatter."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i != -1 and j > i else t


def ask_json(client: AcpClient, text: str, label: str, retries: int = 1) -> dict:
    """Send one turn and require JSON back; on a parse failure, re-ask inside the same session."""
    for attempt in range(retries + 1):
        reply, _tools, stop = client.prompt(text, timeout=420)
        try:
            return json.loads(strip_json(reply))
        except json.JSONDecodeError as e:
            print(f"  [{label}] JSON parse failed ({e}); stop={stop}", file=sys.stderr)
            if attempt >= retries:
                print(f"  [{label}] first 400 chars of reply:\n{reply[:400]}", file=sys.stderr)
                raise
            text = ("Your previous output was not valid JSON. Output the JSON body only: "
                    "no prose, no markdown code fences.")
    raise RuntimeError("unreachable")


def slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(s)).strip("-").lower()[:32] or "task"


def _frontmatter_digest(path: str) -> tuple[str, str] | None:
    """Pull only `name` and `description` out of a SKILL.md YAML frontmatter block.

    The body of a SKILL.md runs to hundreds of lines; only the declared purpose is useful for
    identifying task types, so the body is deliberately not read.
    """
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    block = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        block.append(ln)

    name = os.path.basename(os.path.dirname(path))
    desc_parts: list[str] = []
    i = 0
    while i < len(block):
        ln = block[i]
        if re.match(r"^name\s*:", ln):
            name = ln.split(":", 1)[1].strip() or name
        elif re.match(r"^description\s*:", ln):
            rest = ln.split(":", 1)[1].strip()
            if rest in (">", "|", ">-", "|-", ""):          # folded / literal block
                i += 1
                while i < len(block) and (block[i].startswith((" ", "\t")) or not block[i].strip()):
                    desc_parts.append(block[i].strip())
                    i += 1
                continue
            desc_parts.append(rest)
        i += 1
    desc = " ".join(x for x in desc_parts if x)
    return (name, desc) if desc else None


def _discover_skills(agent_path: str, cfg: dict | None) -> list[str]:
    """Locate SKILL.md files: `resources` entries first, else a nearby .kiro/skills directory."""
    found: list[str] = []
    for res in (cfg or {}).get("resources", []) or []:
        if isinstance(res, str) and "SKILL.md" in res:
            p = res.split("://", 1)[-1] if "://" in res else res
            if os.path.isfile(p):
                found.append(p)
    if found:
        return sorted(found)
    d = os.path.dirname(os.path.abspath(agent_path))
    for _ in range(5):                                     # walk up looking for .kiro/skills
        cand = os.path.join(d, ".kiro", "skills")
        if os.path.isdir(cand):
            for entry in sorted(os.listdir(cand)):
                p = os.path.join(cand, entry, "SKILL.md")
                if os.path.isfile(p):
                    found.append(p)
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return found


def skills_section(agent_path: str, cfg: dict | None) -> str:
    """Render one compact line per skill: name plus declared description."""
    paths = _discover_skills(agent_path, cfg)
    rows = []
    for p in paths:
        dig = _frontmatter_digest(p)
        if dig:
            rows.append(f"- **{dig[0]}**: {dig[1]}")
    if not rows:
        return ""
    print(f"  [skills] {len(rows)} skill description(s) attached", file=sys.stderr)
    return ("\n## Available skills (declared name and description only)\n"
            + "\n".join(rows) + "\n")


def known_subagents(cfg: dict | None) -> list[str]:
    """Sub-agent names this agent may dispatch to, taken from the config only.

    An empty list means the agent has no sub-agents, so no `dispatches` check can ever fire.
    Skills are NOT sub-agents and are deliberately excluded.
    """
    ts = ((cfg or {}).get("toolsSettings") or {}).get("use_subagent") or {}
    names = list(ts.get("availableAgents") or []) + list(ts.get("trustedAgents") or [])
    return sorted({n for n in names if isinstance(n, str)})


def subagents_section(names: list[str]) -> str:
    if names:
        return ("\n## Sub-agents that may be dispatched to (the ONLY valid `dispatches` targets)\n"
                + "\n".join(f"- {n}" for n in names) + "\n")
    return ("\n## Sub-agents that may be dispatched to\n"
            "None. This agent has no sub-agents, so `dispatches` and `never_dispatches` are "
            "never valid for it.\n")


def agent_material(path: str) -> tuple[str, str, list[str]]:
    """Read an agent definition and return (agent name, material text, sub-agent names).

    Accepts either an agent config .json (its `prompt` may be inline or a file:// reference,
    relative references resolved against the config's directory) or a raw .md / .txt prompt.
    Skill descriptions are appended when SKILL.md files can be located.
    """
    if not path.endswith(".json"):
        text = open(path, encoding="utf-8").read()
        name = os.path.splitext(os.path.basename(path))[0]
        if name.lower() == "prompt":                      # prompt.md -> use parent dir name
            name = os.path.basename(os.path.dirname(os.path.abspath(path)))
        return name, ("## Agent system prompt\n```\n" + text + "\n```\n"
                      + skills_section(path, None)
                      + subagents_section([])), []

    cfg = json.load(open(path, encoding="utf-8"))
    name = cfg.get("name") or os.path.splitext(os.path.basename(path))[0]
    prompt = cfg.get("prompt") or ""
    if isinstance(prompt, str) and prompt.startswith("file://"):
        ref = prompt[len("file://"):]
        cand = ref if os.path.isabs(ref) else os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(path)), ref))
        if os.path.isfile(cand):
            prompt = open(cand, encoding="utf-8").read()
        else:
            print(f"[warn] cannot resolve prompt reference: {ref}", file=sys.stderr)
    slim = {k: cfg.get(k) for k in
            ("name", "description", "tools", "allowedTools", "resources", "toolsSettings")
            if cfg.get(k) is not None}
    subs = known_subagents(cfg)
    material = ("## Agent configuration (excerpt)\n```json\n"
                + json.dumps(slim, ensure_ascii=False, indent=2)
                + "\n```\n\n## Agent system prompt\n```\n" + str(prompt) + "\n```\n"
                + skills_section(path, cfg)
                + subagents_section(subs))
    return name, material, subs


def scopes_of(task: dict) -> list[tuple[str, str]]:
    """Stage groups to walk for one task type: its stages, or the task itself when it has none."""
    stages = task.get("stages") or []
    if stages:
        return [(s.get("id") or f"{task['id']}S{i+1}", s.get("name", "?"))
                for i, s in enumerate(stages)]
    return [(task["id"], task.get("name", "?"))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "out"))
    args = ap.parse_args()

    name, material, subagents = agent_material(args.agent)
    os.makedirs(args.out_dir, exist_ok=True)

    client = AcpClient(cwd=EVALKIT)
    client.start()
    client.initialize()
    client.new_session()
    print(f"ACP session={client.session_id}")

    written = []
    try:
        # ---- turn 1: task types (material is sent only here) ----
        print("turn 1: task types ...")
        p1 = load_prompt("01_tasks.md").replace("{agent_material}", material)
        tasks_doc = ask_json(client, p1, "tasks")
        tasks = tasks_doc.get("tasks", [])
        print(f"  -> single_task={tasks_doc.get('single_task')}, {len(tasks)} task type(s)")

        pol_tmpl = load_prompt("02_policies.md")
        bb_tmpl = load_prompt("03_backbone.md")
        turn = 1

        for task in tasks:
            print(f"task {task.get('id')} — {task.get('name')}")
            policies = []
            for sid, sname in scopes_of(task):
                turn += 1
                print(f"  turn {turn}: policies for {sname} ...")
                p = pol_tmpl.replace("{scope_name}", sname).replace("{scope_id}", sid)
                doc = ask_json(client, p, f"policies:{sid}")
                pols = doc.get("policies", [])
                for po in pols:
                    po["task_id"] = task["id"]
                    po["scope_id"] = sid
                    po["scope_name"] = sname
                policies.extend(pols)
                obs: dict[str, int] = {}
                for po in pols:
                    k = po.get("observability", "?")
                    obs[k] = obs.get(k, 0) + 1
                print(f"    -> {len(pols)} policies {obs}")

            turn += 1
            print(f"  turn {turn}: backbone ...")
            backbone = ask_json(client, bb_tmpl.replace("{task_name}", task.get("name", "?")),
                                f"backbone:{task['id']}")

            ir = {"target_agent": name,
                  "known_subagents": subagents,
                  "task_id": task["id"],
                  "task_name": task.get("name"),
                  "task_trigger": task.get("trigger"),
                  "single_task": tasks_doc.get("single_task"),
                  "source_config": os.path.abspath(args.agent),
                  "stages": task.get("stages") or [],
                  "policies": policies,
                  "backbone": backbone}
            suffix = "" if tasks_doc.get("single_task") else "." + slug(task.get("name"))
            dst = os.path.join(args.out_dir, f"{name}{suffix}.policy_ir.json")
            json.dump(ir, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            written.append((dst, policies))
    finally:
        client.shutdown()

    print()
    for dst, policies in written:
        det = sum(1 for p in policies if p.get("observability") == "deterministic")
        jud = sum(1 for p in policies if p.get("observability") == "judge_only")
        print(f"{dst}\n  {len(policies)} policies: {det} deterministic / {jud} judge-only / "
              f"{len(policies) - det - jud} unobservable")


if __name__ == "__main__":
    main()
