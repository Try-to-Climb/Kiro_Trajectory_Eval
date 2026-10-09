#!/usr/bin/env python3
"""extract_tasks.py — run turn 1 only: identify the agent's **task types** (parallel) and the
ordered stages inside each.

Task types are mutually exclusive: one run lands on exactly one. Single-purpose agents yield
exactly one. Each task type will later get its own .checks.json, so that a rule file written for
one task type is never applied to a run of another.

Usage:
    python3 rules/genrule/extract_tasks.py <agent.json | prompt.md> [more paths...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("ACP_CLIENT_DIR",
                                  os.path.expanduser("~/acp-pipeline")))

from acp_client import AcpClient                                     # noqa: E402
from generate import agent_material, ask_json, load_prompt, EVALKIT  # noqa: E402


def run_one(path: str, out_dir: str) -> dict:
    name, material, _subs = agent_material(path)
    print(f"\n{'=' * 72}\n{name}  <-  {path}")
    print(f"material: {len(material)} chars / {len(material.splitlines())} lines")

    client = AcpClient(cwd=EVALKIT)
    client.start()
    client.initialize()
    client.new_session()
    try:
        p = load_prompt("01_tasks.md").replace("{agent_material}", material)
        doc = ask_json(client, p, f"tasks:{name}")
    finally:
        client.shutdown()

    doc["_agent"] = name
    doc["_source"] = os.path.abspath(path)
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, f"{name}.tasks.json")
    json.dump(doc, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    tasks = doc.get("tasks", [])
    print(f"single_task={doc.get('single_task')}   task types: {len(tasks)}")
    for t in tasks:
        stages = t.get("stages") or []
        tag = "" if t.get("mandatory", True) else "  (peripheral)"
        print(f"  [{t.get('id')}] {t.get('name')}{tag}")
        print(f"        trigger: {str(t.get('trigger'))[:100]}")
        if stages:
            print(f"        {len(stages)} stages: " + " -> ".join(
                s.get("name", "?") + ("" if s.get("mandatory", True) else " (optional)")
                for s in stages))
        else:
            print("        stages: none (not a pipeline-shaped task)")
    if doc.get("split_rationale"):
        print(f"  split rationale: {doc['split_rationale'][:300]}")
    print(f"  -> {dst}")
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("agents", nargs="+")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "out"))
    args = ap.parse_args()
    for p in args.agents:
        try:
            run_one(p, args.out_dir)
        except Exception as e:
            print(f"[error] {p}: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
