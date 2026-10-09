"""Ask a language model to audit the splits, and tabulate what it says.

The split is the foundation every provenance edge rests on, and it has no gold
set: a command broken apart wrongly produces an edge that is quietly absent, and
nothing in the totals shows it. The only judge available is a reader who knows
shell. This sends the dump to one, over ACP, and puts the answers in a table
beside the split they are about.

    python3 -m graph.tests.judge_splits ~/graph-viz/splits.jsonl --out audit
    python3 -m graph.tests.judge_splits <dump.jsonl> --batch 8 --limit 40
    python3 -m graph.tests.judge_splits <dump.jsonl> --replay audit.raw.jsonl

Three properties are deliberate.

**Batched, not one prompt.** 188 commands is 290KB of markdown; a single request
would be answered by a model that had stopped reading. Batches are small enough
that every command in one can be looked at.

**A fresh session per batch.** A judge that remembers its last answer defends it.
It also accumulates context it does not need, and the later batches get worse.

**The raw reply is kept.** The verdicts are parsed out of it, and a parser is a
thing that can be wrong -- `audit.raw.jsonl` holds what was actually said, so a
strange table entry can be traced to a strange answer rather than guessed at.
`--replay` re-parses a stored run without spending another call.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# The verdicts a batch may come back with. A closed set, because "the model said
# something worrying" is not a result anyone can act on; and `ok` and `unsure` are
# both present so that a judge with nothing to report and a judge that cannot tell
# are not forced into the same answer.
VERDICTS = ("ok", "missing_command", "wrong_boundary", "lost_redirect",
            "wrong_substitution", "wrong_claim", "wrong_edge", "unsure")

BRIEF = """\
You are auditing a shell parser, one command at a time. For each command below
you are given the command as it actually ran and the way our extractor broke it
apart. Judge the split. Nothing else -- not whether the command was wise, not
whether paths were read or written, only whether the pieces are right.

A split lists, for every program the command runs:
  runs        the program, after wrappers (nohup, timeout, env, sudo, exec,
              stdbuf, setsid, ionice, nice, time, xargs, command, unbuffer)
              have been stepped past
  through     those wrappers, in order
  positional  its arguments that do not begin with `-`
  options     its arguments that do
  redirects   `> f`, `>> f`, `< f`, `<<'EOF'`, `2>&1`, attached to the command
              the shell attaches them to
  resolved    a `$VAR` that was substituted, and the value used
  in          nesting we track: currently only a heredoc body, which is
              re-parsed when the body is written to a file that is then executed
  ignored     found on purpose and not turned into an edge: rm, ls, mkdir,
              touch, stat, test
  claims      paths read out of a script fed to an interpreter's stdin -- the body
              of `python3 <<PY`. These are not arguments of the interpreter; they
              are literals found inside the program, with a guess at direction

You are given, for each command: the command text, the split, and the edges the
graph was built from. Judge all of it -- a split can be right while an edge is
wrong, and that combination is the one a reader of the split alone cannot catch.

Answer with one JSON object per line and nothing else. No prose before or after,
no markdown fences.

  {"id": "a[12]", "verdict": "<one of the words below>", "reason": "<one sentence>"}

  ok                  the split is right
  missing_command     a program is run that the split does not list
  wrong_boundary      `runs` names a wrapper, or the arguments belong to another
                      command in the line
  lost_redirect       a redirection in the command is absent, or attached to the
                      wrong command
  wrong_substitution  a `resolved` value is not what the variable held there
  wrong_claim         a path under `claims` is not what the script reads or
                      writes -- a fragment of a longer path, something built from
                      a variable, a string that is not a path at all, or the wrong
                      direction
  wrong_edge          an entry under `edges` is not a file this command reads or
                      writes, or the direction is wrong. These are what the graph
                      was built from, so they matter most
  unsure              the command is genuinely ambiguous, or you cannot tell

One line per id, every id, in the order given. For `ok`, leave `reason` empty.
For anything else, quote the part of the command you mean.

Do not report these; they are known and recorded:
  * a `$(...)` inside an assignment is not entered at all, so nothing inside
    `LOG=$(ls -t *.log | head -1)` appears in the split;
  * `~` is not expanded, because the command text does not carry HOME;
  * a variable whose value is produced at run time is left as written;
  * a `cd` part-way through a command does not move the directory that later paths
    resolve against: after `cd /app && cd sub`, a `> out.txt` is recorded under
    `/app` rather than `/app/sub`;
  * `cp` and `mv` produce no edge at all, in either direction;
  * a URL is never a node, so `curl http://host/x.txt -o x.txt` records the local
    file and not where it came from;
  * `mkdir`, `rm`, `ls`, `touch`, `stat` and `test` are found and deliberately
    dropped, because they move no content.
"""


def _one(rec: dict) -> str:
    """One command, as the judge sees it."""
    lines = [f"### {rec['id']}"]
    fence = "```"
    while fence in rec["command"]:
        fence += "`"
    lines.append("command:")
    lines.append(fence)
    lines.append(rec["command"])
    lines.append(fence)
    if rec.get("assignments"):
        lines.append("assignments:")
        for a in rec["assignments"]:
            know = "knowable" if a["knowable"] else "built at run time"
            lines.append(f"  {a['name']}={a['value']}   [{know}]")
    lines.append("split:")
    if not rec["split"]:
        lines.append("  (the extractor found no command here)")
    for i, c in enumerate(rec["split"], 1):
        bits = [f"runs={c['runs']}"]
        for key in ("through", "positional", "options", "redirects"):
            if c.get(key):
                bits.append(f"{key}={c[key]}")
        if c.get("resolved"):
            bits.append(f"resolved={c['resolved']}")
        if c.get("in"):
            bits.append(f"in={c['in']}")
        if c.get("ignored"):
            bits.append("ignored=true")
        if c.get("claims"):
            bits.append(f"claims={c['claims']}")
        lines.append(f"  {i}. " + "  ".join(bits))
    # The edges the graph was actually built from, via `build._command_paths` --
    # the same call the graph makes, with the same working directory and carried
    # variables. Shown because a split that looks right and an edge that is wrong
    # is the combination a reader of the split alone cannot catch.
    lines.append(f"edges ({rec.get('via', '?')}):")
    if not rec.get("edges"):
        lines.append("  (none)")
    for path, direction, why in rec.get("edges", ()):
        lines.append(f"  {direction:5} {path}   [{why}]")
    if rec.get("skipped"):
        lines.append("seen but not claimed:")
        for what, why in rec["skipped"]:
            lines.append(f"  {what}" + (f"   [{why}]" if why else ""))
    return "\n".join(lines)


def ask(prompt: str, timeout: int = 900) -> str:
    """One question to a fresh kiro session over ACP; returns what it said.

    `acptest/acp_client.py` counts `agent_message_chunk` notifications without
    keeping their text, which is right for its purpose and useless here, so the
    protocol is spoken directly: initialize, session/new, session/prompt, and
    collect the chunks.
    """
    proc = subprocess.Popen(["kiro-cli", "acp", "-a"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    said: list[str] = []
    nid = [0]

    def send(method, params):
        nid[0] += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": nid[0],
                                     "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return nid[0]

    def pump(until_id, deadline):
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "session/update":
                u = msg.get("params", {}).get("update", {})
                if u.get("sessionUpdate") == "agent_message_chunk":
                    said.append(u.get("content", {}).get("text", ""))
            elif msg.get("method") == "session/request_permission":
                # A judge only reads. Nothing it asks for is granted, and the
                # question is answered rather than left to time out.
                proc.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": msg["id"],
                     "result": {"outcome": {"outcome": "cancelled"}}}) + "\n")
                proc.stdin.flush()
            if msg.get("id") == until_id and ("result" in msg or "error" in msg):
                return msg
        return None

    try:
        end = time.time() + timeout
        i = send("initialize", {"protocolVersion": 1,
                                "clientCapabilities": {
                                    "fs": {"readTextFile": False,
                                           "writeTextFile": False}}})
        pump(i, end)
        i = send("session/new", {"cwd": REPO, "mcpServers": []})
        r = pump(i, end)
        sess = (r or {}).get("result", {}).get("sessionId")
        if not sess:
            return ""
        i = send("session/prompt", {
            "sessionId": sess,
            "prompt": [{"type": "text", "text": prompt}]})
        pump(i, end)
    finally:
        try:
            proc.stdin.close()
        except Exception:                                  # noqa: BLE001
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return "".join(said)


def parse(reply: str, wanted: list) -> dict:
    """`{id: (verdict, reason)}` from a reply that was asked for JSON lines.

    Tolerant on purpose -- fences, stray prose and a missing id are all things a
    model does -- but never inventive: an id that was not asked about is dropped,
    and a verdict outside the set becomes `unsure` with the original kept in the
    reason, so a table entry can always be traced to something that was said.
    """
    out = {}
    for m in re.finditer(r"\{[^{}]*\}", reply):
        try:
            got = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        rid = str(got.get("id", "")).strip()
        if rid not in wanted:
            continue
        verdict = str(got.get("verdict", "")).strip()
        reason = str(got.get("reason", "")).strip()
        if verdict not in VERDICTS:
            reason = f"[unrecognised verdict {verdict!r}] {reason}".strip()
            verdict = "unsure"
        out[rid] = (verdict, reason)
    return out


def table(recs: list, verdicts: dict) -> str:
    """id, command, split, verdict, reason -- one row each, widest column last."""
    def cell(text: str, width: int) -> str:
        flat = " ".join(str(text).split())
        return flat if len(flat) <= width else flat[:width - 1] + "…"

    head = ("| id | command | split | verdict | reason |\n"
            "|---|---|---|---|---|\n")
    rows = []
    for r in recs:
        v, why = verdicts.get(r["id"], ("(no answer)", ""))
        split = "; ".join(
            (c["runs"] or "?") + (f"({', '.join(c['positional'])})"
                                  if c["positional"] else "")
            for c in r["split"]) or "(none)"
        rows.append("| `{}` | `{}` | {} | **{}** | {} |".format(
            r["id"], cell(r["command"], 90).replace("|", "\\|"),
            cell(split, 70).replace("|", "\\|"), v,
            cell(why, 160).replace("|", "\\|")))
    return head + "\n".join(rows) + "\n"


def summary(recs: list, verdicts: dict) -> str:
    counts = {}
    for r in recs:
        v = verdicts.get(r["id"], ("(no answer)", ""))[0]
        counts[v] = counts.get(v, 0) + 1
    lines = [f"{len(recs)} command(s) judged, "
             f"{sum(1 for r in recs if r['id'] in verdicts)} answered.", ""]
    for v, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {v}: {n}")
    unanswered = [r["id"] for r in recs if r["id"] not in verdicts]
    if unanswered:
        lines.append("")
        lines.append("Not answered, so not judged -- absence here is our problem, "
                     "not a verdict: " + ", ".join(unanswered[:30]))
    return "\n".join(lines) + "\n"


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m graph.tests.judge_splits",
        description="Send a split dump to a language model and tabulate its "
                    "verdicts.")
    p.add_argument("dump", help="a .jsonl produced by graph.tests.dump_splits")
    p.add_argument("--out", default="split-audit",
                   help="basename: writes <out>.md and <out>.raw.jsonl")
    p.add_argument("--batch", type=int, default=20,
                   help="commands per request (default 20; 188 in one prompt "
                        "is answered by a model that stopped reading)")
    p.add_argument("--limit", type=int, default=0, help="stop after N commands")
    p.add_argument("--skip", default="echo,cd",
                   help="comma-separated: commands whose split is only these are "
                        "not sent (they carry no paths and cost a judge's "
                        "attention). Empty string to send everything")
    p.add_argument("--replay", default="",
                   help="re-parse a stored .raw.jsonl instead of asking again")
    p.add_argument("--parallel", type=int, default=4,
                   help="batches in flight at once (default 4). Each is its own "
                        "kiro process and session, so they do not interfere; "
                        "raise it until the answers start timing out")
    p.add_argument("--timeout", type=int, default=900,
                   help="seconds per request")
    args = p.parse_args(argv)

    with open(args.dump, encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    if skip:
        kept = []
        for r in recs:
            runs = {c["runs"] for c in r["split"]}
            if r["split"] and runs and runs <= skip:
                continue
            kept.append(r)
        dropped = len(recs) - len(kept)
        recs = kept
    else:
        dropped = 0
    if args.limit:
        recs = recs[:args.limit]

    verdicts: dict = {}
    raw_path = f"{args.out}.raw.jsonl"

    if args.replay:
        with open(args.replay, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                got = json.loads(line)
                verdicts.update(parse(got["reply"], got["ids"]))
        print(f"replayed {len(verdicts)} verdict(s) from {args.replay}",
              file=sys.stderr)
    else:
        batches = [recs[i:i + args.batch]
                   for i in range(0, len(recs), args.batch)]
        total = len(batches)
        # Each batch is its own kiro process and its own session, so they are
        # independent and can run at once. The work is waiting on a subprocess, so
        # threads are the right shape; the lock is only so two replies cannot
        # interleave in the file that has to be readable afterwards.
        lock = threading.Lock()
        done = [0]

        def one(job):
            n, batch = job
            ids = [r["id"] for r in batch]
            prompt = BRIEF + "\n\n" + "\n\n".join(_one(r) for r in batch)
            reply = ask(prompt, args.timeout)
            got = parse(reply, ids)
            with lock:
                done[0] += 1
                raw.write(json.dumps({"batch": n, "ids": ids, "reply": reply},
                                     ensure_ascii=False) + "\n")
                raw.flush()
                verdicts.update(got)
                missing = [i for i in ids if i not in got]
                print(f"[{done[0]}/{total}] batch {n}: {len(got)}/{len(ids)} "
                      f"judged" + (f", no verdict for {', '.join(missing)}"
                                   if missing else ""), file=sys.stderr)

        with open(raw_path, "w", encoding="utf-8") as raw:
            jobs = list(enumerate(batches, 1))
            if args.parallel <= 1:
                for job in jobs:
                    one(job)
            else:
                with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                    list(pool.map(one, jobs))

    body = [
        "# split audit — judged\n",
        f"dump: `{args.dump}`  ·  batch size {args.batch}"
        + (f"  ·  {args.parallel} in parallel" if args.parallel > 1 else "")
        + (f"  ·  {dropped} command(s) skipped as {sorted(skip)}-only"
           if dropped else "") + "\n",
        summary(recs, verdicts),
        "\n## verdicts\n",
        table(recs, verdicts),
        "\n## what a verdict means\n",
        "\n".join(f"- `{v}`" for v in VERDICTS) + "\n",
        "\nThe raw replies are in `" + os.path.basename(raw_path) +
        "`. Every row above was parsed out of one of them; a row that reads "
        "oddly should be checked against what was actually said before it is "
        "believed.\n",
    ]
    out_md = f"{args.out}.md"
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body))
    print(f"{len(verdicts)}/{len(recs)} judged; table in {out_md}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
