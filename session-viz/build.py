#!/usr/bin/env python3
"""Kiro session → self-contained HTML timeline viewer.

Reads a Kiro session's `.json` (metadata + per-turn usage) and `.jsonl`
(full conversation) files, normalizes them into a turn/event tree, then
emits a single HTML file that renders a zoomable timeline.

Usage:
    python3 build.py <session-id> [--dir SESSIONS_DIR] [--out FILE]
    python3 build.py --list [--dir SESSIONS_DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _resolve_sessions_dir(sessions_dir):
    """Resolve the sessions directory. If not given, fall back to the Kiro
    default: $KIRO_HOME/sessions/cli, else ~/.kiro/sessions/cli."""
    if sessions_dir:
        return sessions_dir
    kiro_home = os.environ.get("KIRO_HOME")
    if kiro_home:
        return os.path.join(kiro_home, "sessions", "cli")
    return os.path.expanduser("~/.kiro/sessions/cli")


def load_session(session_id: str, sessions_dir: str) -> dict[str, Any]:
    sessions_dir = _resolve_sessions_dir(sessions_dir)
    meta_p = os.path.join(sessions_dir, f"{session_id}.json")
    jl_p = os.path.join(sessions_dir, f"{session_id}.jsonl")
    if not os.path.isfile(meta_p):
        raise FileNotFoundError(f"missing metadata: {meta_p}")

    meta = json.load(open(meta_p, encoding="utf-8"))
    state = meta.get("session_state") or {}
    conv_meta = state.get("conversation_metadata") or {}
    tms = conv_meta.get("user_turn_metadatas") or []

    # turn index (0-based turn number) built from message_ids
    mid2turn: dict[str, int] = {}
    turns: list[dict[str, Any]] = []
    for i, tm in enumerate(tms):
        loop = (tm.get("loop_id") or {}).get("agent_id") or {}
        turns.append({
            "turn": i + 1,
            "agent": loop.get("name"),
            "parent_agent": loop.get("parent_id"),
            "duration_s": (tm.get("turn_duration") or {}).get("secs"),
            "end_reason": tm.get("end_reason"),
            "tool_uses": tm.get("builtin_tool_uses"),
            "request_count": tm.get("total_request_count"),
            "input_tokens": tm.get("input_token_count"),
            "output_tokens": tm.get("output_token_count"),
            "context_pct": tm.get("context_usage_percentage"),
            "credits": round(sum(v.get("value", 0.0)
                                 for v in (tm.get("metering_usage") or [])), 6) or None,
            "prompt_len": tm.get("user_prompt_length"),
            "start_ts": None,
            "events": [],
        })
        for mid in (tm.get("message_ids") or []):
            mid2turn[mid] = i

    tool_use_index: dict[str, tuple[int, int]] = {}  # tid → (turn_idx, evt_idx)

    if os.path.isfile(jl_p):
        for line in open(jl_p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = r.get("kind")
            data = r.get("data") or {}
            mid = data.get("message_id")

            if kind == "Prompt":
                idx = mid2turn.get(mid)
                if idx is None or idx >= len(turns):
                    continue
                ts = (data.get("meta") or {}).get("timestamp")
                if ts and not turns[idx]["start_ts"]:
                    turns[idx]["start_ts"] = ts
                text_parts = []
                for c in (data.get("content") or []):
                    if c.get("kind") == "text":
                        d = c.get("data")
                        text_parts.append(d if isinstance(d, str) else "")
                turns[idx]["events"].append({
                    "kind": "prompt",
                    "text": "\n".join(text_parts),
                })

            elif kind == "AssistantMessage":
                idx = mid2turn.get(mid)
                if idx is None or idx >= len(turns):
                    continue
                for c in (data.get("content") or []):
                    ck = c.get("kind")
                    d = c.get("data")
                    if ck == "text":
                        txt = d if isinstance(d, str) else (d or {}).get("text", "")
                        if txt:
                            turns[idx]["events"].append({"kind": "assistant_text", "text": txt})
                    elif ck == "thinking":
                        txt = d.get("text", "") if isinstance(d, dict) else (d or "")
                        if txt:
                            turns[idx]["events"].append({"kind": "thinking", "text": txt})
                    elif ck == "toolUse":
                        td = d or {}
                        name = td.get("name")
                        tid = td.get("toolUseId")
                        args = td.get("input") or td.get("args") or {}
                        evt = {
                            "kind": "tool_use",
                            "name": name,
                            "id": tid,
                            "args": args,
                            "result_status": None,
                            "result_summary": None,
                        }
                        turns[idx]["events"].append(evt)
                        if tid:
                            tool_use_index[tid] = (idx, len(turns[idx]["events"]) - 1)

            elif kind == "ToolResults":
                rs = data.get("results") or {}
                if not isinstance(rs, dict):
                    continue
                for tid, res in rs.items():
                    loc = tool_use_index.get(tid)
                    if loc is None:
                        continue
                    ti, ei = loc
                    rr = res.get("result")
                    status = "unknown"
                    summary = ""
                    if isinstance(rr, dict):
                        if "Success" in rr:
                            status = "success"
                            items = (rr.get("Success") or {}).get("items") or []
                            # Grab the first text/json blob short summary
                            for it in items:
                                if isinstance(it, dict):
                                    for k, v in it.items():
                                        if isinstance(v, dict):
                                            for kk in ("stdout", "text", "content"):
                                                if kk in v and v[kk]:
                                                    summary = str(v[kk])
                                                    break
                                            if not summary:
                                                summary = json.dumps(v)[:400]
                                        else:
                                            summary = str(v)[:400]
                                        break
                                if summary:
                                    break
                        else:
                            status = "error"
                            summary = json.dumps(rr, ensure_ascii=False)[:600]
                    else:
                        status = str(rr)[:40] if rr else "unknown"
                    turns[ti]["events"][ei]["result_status"] = status
                    turns[ti]["events"][ei]["result_summary"] = summary[:1500]

    # Derive end_ts and gap-fill start_ts if a turn had no prompt event
    prev_end = None
    for t in turns:
        if t["start_ts"] is None and prev_end is not None:
            t["start_ts"] = prev_end
        if t["start_ts"] is not None and t["duration_s"] is not None:
            t["end_ts"] = t["start_ts"] + t["duration_s"]
        else:
            t["end_ts"] = None
        prev_end = t["end_ts"] or prev_end

    return {
        "session_id": session_id,
        "agent_name": state.get("agent_name"),
        "cwd": meta.get("cwd"),
        "created_at": meta.get("created_at"),
        "created_reason": meta.get("session_created_reason"),
        "title": meta.get("title"),
        "total_turns": len(turns),
        "total_credits": round(sum(t.get("credits") or 0 for t in turns), 6),
        "total_input_tokens": sum(t.get("input_tokens") or 0 for t in turns),
        "total_output_tokens": sum(t.get("output_tokens") or 0 for t in turns),
        "total_duration_s": sum(t.get("duration_s") or 0 for t in turns),
        "turns": turns,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kiro Session · __TITLE__</title>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a2028;
    --panel2: #232c37;
    --fg: #e6edf3;
    --muted: #8b95a5;
    --accent: #4fc3f7;
    --border: #2d3843;
    --user: #ffb74d;
    --assistant: #81c784;
    --thinking: #ba68c8;
    --tool: #4fc3f7;
    --tool-err: #e57373;
    --tool-ok: #64d6a3;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--fg); font-size: 13px;
    display: flex; flex-direction: column; height: 100vh;
  }
  header {
    background: var(--panel); padding: 10px 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  }
  header .title { font-weight: 600; font-size: 15px; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .stat { color: var(--muted); font-size: 12px; }
  header .stat b { color: var(--fg); }

  .breadcrumb { padding: 8px 16px; background: var(--panel2); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .breadcrumb a { color: var(--accent); cursor: pointer; text-decoration: none; }
  .breadcrumb a:hover { text-decoration: underline; }
  .breadcrumb .sep { color: var(--muted); }

  #view { flex: 1; overflow: auto; padding: 16px; }

  /* Session overview: horizontal timeline with turn bars */
  .overview .lane {
    position: relative;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px;
    min-height: 100px;
  }
  .overview .axis {
    height: 20px; position: relative; color: var(--muted); font-size: 10px;
    border-bottom: 1px solid var(--border); margin-bottom: 6px;
  }
  .overview .axis .tick {
    position: absolute; top: 0; height: 100%;
    border-left: 1px dashed var(--border);
    padding-left: 3px;
  }
  .overview .bars { position: relative; height: 60px; }
  .overview .gap {
    position: absolute; top: 8px; height: 40px;
    background: repeating-linear-gradient(45deg, #1a2028, #1a2028 4px, #151a20 4px, #151a20 8px);
    border-radius: 3px;
    pointer-events: none;
    color: var(--muted); font-size: 9px;
    display: flex; align-items: center; justify-content: center;
  }
  .overview .gap span { opacity: 0.6; padding: 0 4px; }

  /* ===== Turn cards overview (default view) ===== */
  .mini-timeline-wrap {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; margin-bottom: 16px; position: sticky; top: 0; z-index: 10;
  }
  .mini-timeline-hd { display:flex; justify-content: space-between; margin-bottom: 6px; color: var(--fg); font-size: 12px; }
  .mini-timeline { position: relative; height: 22px; background: #10161c; border-radius: 3px; }
  .mini-bar {
    position: absolute; top: 3px; height: 16px;
    background: linear-gradient(to bottom, #305066, #223546);
    border: 1px solid #3d5568; border-radius: 2px;
    cursor: pointer; overflow: hidden;
  }
  .mini-bar.subagent { background: linear-gradient(to bottom, #4a3f66, #322a46); border-color: #5a4d80; }
  .mini-bar:hover { filter: brightness(1.4); }
  .mini-bar .mini-lbl { color: var(--fg); font-size: 9px; padding: 0 3px; line-height: 16px; }

  .turn-cards { display: flex; flex-direction: column; gap: 12px; }
  .tcard {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 14px; cursor: pointer; transition: border-color 0.15s, background 0.15s;
    border-left: 4px solid var(--tool);
  }
  .tcard.subagent { border-left-color: var(--thinking); }
  .tcard:hover { border-color: var(--accent); background: var(--panel2); }
  .tcard.flash { animation: flash 0.8s; }
  @keyframes flash {
    0% { background: #2a3a4a; }
    100% { background: var(--panel); }
  }
  .tcard-hd { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
  .tnum {
    font-weight: 700; font-size: 15px; color: var(--accent);
    background: var(--panel2); padding: 2px 10px; border-radius: 4px;
    min-width: 44px; text-align: center;
  }
  .tmeta { flex: 1; color: var(--muted); font-size: 12px; }
  .tmeta b { color: var(--fg); font-weight: 600; }
  .tmeta .parent { color: var(--thinking); margin-left: 4px; }
  .tmeta .endreason { color: var(--tool-err); margin-left: 8px; padding: 1px 6px; background: #331515; border-radius: 3px; font-size: 10px; }
  .tmeta .errchip { color: #fff; background: var(--tool-err); margin-left: 6px; padding: 1px 6px; border-radius: 3px; font-size: 10px; }
  .tstats { display: flex; gap: 10px; color: var(--muted); font-size: 12px; }

  .ev-strip {
    display: flex; height: 4px; gap: 1px; margin-bottom: 10px;
    background: #10161c; border-radius: 2px; padding: 1px;
  }
  .ev-tick { flex: 1; min-width: 2px; height: 100%; border-radius: 1px; }
  .ev-tick.k-prompt { background: var(--user); }
  .ev-tick.k-thinking { background: var(--thinking); opacity: 0.7; }
  .ev-tick.k-assistant_text { background: var(--assistant); }
  .ev-tick.k-tool_use { background: var(--tool); }
  .ev-tick.k-tool_use.err { background: var(--tool-err); }

  .snip {
    padding: 6px 10px; border-radius: 4px; margin: 6px 0;
    font-family: ui-monospace, monospace; font-size: 11.5px; line-height: 1.5;
    max-height: 5em; overflow: hidden; position: relative;
    color: var(--fg);
    background: #10161c;
  }
  .snip .lbl {
    display: inline-block; padding: 0 6px; margin-right: 6px;
    background: var(--panel2); border-radius: 3px; font-size: 10px;
    color: var(--muted); font-family: -apple-system, sans-serif;
  }
  .snip-prompt { border-left: 2px solid var(--user); }
  .snip-asst { border-left: 2px solid var(--assistant); }

  .tools-block { margin: 6px 0; }
  .tools-line { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .tool-badge {
    padding: 2px 8px; border-radius: 10px; background: #1c2a38;
    border: 1px solid #2d4256; font-size: 11px; color: var(--tool);
  }
  .tool-badge .c { color: var(--muted); margin-left: 4px; font-size: 10px; }
  .tools-samples { display: flex; flex-direction: column; gap: 2px; margin-left: 4px; }
  .sample {
    font-family: ui-monospace, monospace; font-size: 11px; color: var(--muted);
    padding: 2px 6px; border-left: 2px solid #2d3843;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .sample.err { border-left-color: var(--tool-err); color: #f0a0a0; }
  .sample.more { color: var(--accent); }
  .sample .tn { color: var(--tool); font-weight: 600; margin-right: 6px; }
  .overview .bar {
    position: absolute; top: 8px; height: 40px;
    background: linear-gradient(to bottom, #305066, #223546);
    border: 1px solid #3d5568;
    border-radius: 3px; cursor: pointer;
    overflow: hidden; padding: 3px 5px;
    font-size: 10px; color: var(--fg);
    transition: transform 0.1s;
  }
  .overview .bar:hover { transform: translateY(-2px); border-color: var(--accent); }
  .overview .bar.subagent { background: linear-gradient(to bottom, #4a3f66, #322a46); border-color:#5a4d80; }
  .overview .bar .n { font-weight: 600; }
  .overview .bar .a { color: var(--muted); font-size: 9px; }
  .zoom-controls { margin-bottom: 10px; display:flex; gap:8px; align-items:center; }
  .zoom-controls button {
    background: var(--panel2); color: var(--fg); border: 1px solid var(--border);
    padding: 4px 10px; border-radius: 4px; cursor: pointer;
  }
  .zoom-controls button:hover { border-color: var(--accent); }
  .zoom-controls input[type=range] { width: 200px; }

  /* Turn detail: vertical list of events */
  .turn-detail .turn-header {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 14px; margin-bottom: 12px;
    display: flex; gap: 16px; flex-wrap: wrap;
  }
  .turn-detail .turn-header .k { color: var(--muted); font-size: 11px; }
  .turn-detail .turn-header .v { color: var(--fg); }

  .events { display: flex; flex-direction: column; gap: 6px; }
  .event {
    background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
    padding: 8px 12px; cursor: pointer; position: relative;
    border-left: 3px solid var(--muted);
  }
  .event:hover { background: var(--panel2); }
  .event.prompt { border-left-color: var(--user); }
  .event.assistant_text { border-left-color: var(--assistant); }
  .event.thinking { border-left-color: var(--thinking); opacity: 0.85; }
  .event.tool_use { border-left-color: var(--tool); }
  .event.tool_use.err { border-left-color: var(--tool-err); }
  .event .head { display: flex; align-items: center; gap: 8px; }
  .event .badge {
    padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;
    background: var(--panel2); color: var(--muted);
  }
  .event.prompt .badge { color: var(--user); }
  .event.assistant_text .badge { color: var(--assistant); }
  .event.thinking .badge { color: var(--thinking); }
  .event.tool_use .badge { color: var(--tool); }
  .event.tool_use.err .badge { color: var(--tool-err); }
  .event .title { flex: 1; color: var(--muted); }
  .event .preview {
    color: var(--fg); margin-top: 4px; white-space: pre-wrap;
    max-height: 3em; overflow: hidden;
    text-overflow: ellipsis; font-family: ui-monospace, monospace; font-size: 11px;
    line-height: 1.4;
  }
  .event.expanded .preview { max-height: none; }
  .event .tool-args {
    margin-top: 6px; font-family: ui-monospace, monospace; font-size: 11px;
    color: var(--muted); background: #10161c; padding: 6px 8px; border-radius: 3px;
    white-space: pre-wrap; max-height: 200px; overflow: auto;
  }
  .event .tool-result {
    margin-top: 4px; font-family: ui-monospace, monospace; font-size: 11px;
    color: var(--tool-ok); background: #0d1a12; padding: 6px 8px; border-radius: 3px;
    white-space: pre-wrap; max-height: 300px; overflow: auto;
    border-left: 2px solid var(--tool-ok);
  }
  .event .tool-result.err { color: var(--tool-err); background: #1c0d0d; border-left-color: var(--tool-err); }

  .filter-bar { margin-bottom: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .filter-bar label {
    background: var(--panel2); padding: 3px 8px; border-radius: 3px;
    border: 1px solid var(--border); cursor: pointer; font-size: 11px;
  }
  .filter-bar label input { margin-right: 4px; vertical-align: middle; }

  /* Detail modal */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6);
    display: none; align-items: center; justify-content: center; z-index: 100;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    max-width: 90vw; max-height: 90vh; width: 900px; display: flex; flex-direction: column;
  }
  .modal .modal-head {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }
  .modal .modal-body {
    padding: 12px 16px; overflow: auto; font-family: ui-monospace, monospace;
    white-space: pre-wrap; font-size: 12px; line-height: 1.5;
  }
  .modal .close { cursor: pointer; color: var(--muted); font-size: 20px; }
  .modal .close:hover { color: var(--fg); }
</style>
</head>
<body>
<header>
  <div>
    <div class="title">__SESSION_TITLE__</div>
    <div class="meta">__SESSION_ID__ · <span id="agent-name"></span></div>
  </div>
  <div class="stat">Turns: <b id="s-turns"></b></div>
  <div class="stat">Duration: <b id="s-dur"></b></div>
  <div class="stat">Tokens: <b id="s-tokens"></b></div>
  <div class="stat">Credits: <b id="s-credits"></b></div>
  <div class="stat">CWD: <b id="s-cwd" style="font-family:monospace;font-size:11px"></b></div>
</header>
<div class="breadcrumb" id="breadcrumb"></div>
<div id="view"></div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <div class="modal-head">
      <div id="modal-title"></div>
      <div class="close" onclick="document.getElementById('modal').classList.remove('show')">×</div>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const DATA = __DATA__;

// ---------- utilities ----------
function fmtDur(s) {
  if (s == null) return "—";
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return m + "m" + (sec ? " " + sec + "s" : "");
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}
function fmtNum(n) { return (n||0).toLocaleString(); }
function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}
function escape(s) {
  return String(s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function shortArgs(args) {
  if (!args) return "";
  if (typeof args !== "object") return String(args);
  // Prefer semantic fields
  for (const k of ["command", "path", "pattern", "query", "task"]) {
    if (args[k]) return k + ": " + String(args[k]).replace(/\n/g," ⏎ ").slice(0, 200);
  }
  const keys = Object.keys(args).filter(k => !k.startsWith("__"));
  return keys.map(k => k+": "+JSON.stringify(args[k]).slice(0,60)).join(", ").slice(0,200);
}
function eventTitle(evt) {
  switch (evt.kind) {
    case "prompt": return "user prompt";
    case "assistant_text": return "assistant text";
    case "thinking": return "thinking";
    case "tool_use": return evt.name + (evt.args && evt.args.__tool_use_purpose ? " · " + evt.args.__tool_use_purpose : "");
  }
  return evt.kind;
}
function eventPreview(evt) {
  if (evt.kind === "tool_use") return shortArgs(evt.args);
  return (evt.text||"").slice(0, 400);
}

// ---------- header ----------
// Flags derived from data — must be declared BEFORE header setup uses them
const HAS_TOKENS = DATA.turns.some(t => (t.input_tokens || t.output_tokens));

document.getElementById("agent-name").textContent = DATA.agent_name || "(unknown agent)";
document.getElementById("s-turns").textContent = DATA.total_turns;
document.getElementById("s-dur").textContent = fmtDur(DATA.total_duration_s);
document.getElementById("s-tokens").textContent = fmtNum(DATA.total_input_tokens) + " in / " + fmtNum(DATA.total_output_tokens) + " out";
if (!HAS_TOKENS) document.getElementById("s-tokens").parentElement.style.display = "none";
document.getElementById("s-credits").textContent = (DATA.total_credits || 0).toFixed(4);
document.getElementById("s-cwd").textContent = DATA.cwd || "—";

// ---------- state / router ----------
let state = { view: "overview", turnIdx: null, zoom: 1.0, filter: {}, packed: false };

function setBreadcrumb() {
  const bc = document.getElementById("breadcrumb");
  if (state.view === "overview") {
    bc.innerHTML = '<span>Session overview</span>';
  } else {
    bc.innerHTML = '<a onclick="go(\'overview\')">Session overview</a>' +
      ' <span class="sep">›</span> <span>Turn ' + (state.turnIdx + 1) + '</span>';
  }
}
function go(view, turnIdx) {
  state.view = view;
  state.turnIdx = turnIdx == null ? null : turnIdx;
  render();
}

// ---------- overview ----------
function renderOverview() {
  const view = document.getElementById("view");
  const turns = DATA.turns;
  // For the mini timeline strip at top
  let t0 = null, t1 = null;
  for (const t of turns) {
    if (t.start_ts) t0 = t0 == null ? t.start_ts : Math.min(t0, t.start_ts);
    if (t.end_ts) t1 = t1 == null ? t.end_ts : Math.max(t1, t.end_ts);
  }
  const useTime = t0 != null && t1 != null && t1 > t0;
  const totalSpan = useTime ? (t1 - t0) : turns.length;

  // ---- Mini timeline strip (compact overview) ----
  const miniW = 900;
  const miniBars = turns.map((t, i) => {
    let left, width;
    if (useTime && t.start_ts && t.end_ts) {
      left = (t.start_ts - t0) / totalSpan * miniW;
      width = Math.max(6, (t.end_ts - t.start_ts) / totalSpan * miniW);
    } else {
      left = i / turns.length * miniW;
      width = miniW / turns.length - 1;
    }
    const cls = t.parent_agent ? "mini-bar subagent" : "mini-bar";
    return `<div class="${cls}" style="left:${left}px;width:${width}px"
      onclick="scrollToTurn(${i})" title="T${t.turn} · ${fmtDur(t.duration_s)} · ${(t.events||[]).filter(e=>e.kind==='tool_use').length} tools">
      <span class="mini-lbl">T${t.turn}</span></div>`;
  }).join("");

  // ---- Turn cards ----
  const cards = turns.map((t, i) => renderTurnCard(t, i)).join("");

  view.innerHTML = `
    <div class="mini-timeline-wrap">
      <div class="mini-timeline-hd">
        <span>Timeline</span>
        <span style="color:var(--muted);font-size:11px">
          ${useTime ? fmtTs(t0) + " → " + fmtTs(t1) + " · " + fmtDur(t1-t0) : "no wall-clock"}
        </span>
      </div>
      <div class="mini-timeline" style="width:${miniW+10}px">${miniBars}</div>
    </div>
    <div class="turn-cards">${cards}</div>`;
}

function scrollToTurn(i) {
  const el = document.getElementById("card-" + i);
  if (el) { el.scrollIntoView({behavior:"smooth", block:"center"}); el.classList.add("flash"); setTimeout(()=>el.classList.remove("flash"), 800); }
}

function summarizeToolUse(evt) {
  // Compact one-liner of what this tool call did
  const a = evt.args || {};
  const purpose = a.__tool_use_purpose;
  if (purpose) return purpose;
  const name = evt.name || "?";
  if (a.command) return String(a.command).replace(/\n/g," ↵ ").slice(0, 90);
  if (a.path)    return a.path;
  if (a.pattern) return "pattern: " + a.pattern;
  if (a.query)   return "query: " + a.query;
  if (a.task)    return String(a.task).slice(0, 90);
  if (a.operations && Array.isArray(a.operations)) {
    return a.operations.map(o => o.path || o.mode || "").filter(Boolean).slice(0,3).join(", ");
  }
  return name;
}

function renderTurnCard(t, i) {
  const events = t.events || [];
  const toolEvts = events.filter(e => e.kind === "tool_use");
  const promptEvt = events.find(e => e.kind === "prompt");
  const lastAsst = [...events].reverse().find(e => e.kind === "assistant_text");
  const errorCount = toolEvts.filter(e => e.result_status && e.result_status !== "success").length;

  // Tally by tool name
  const byName = {};
  for (const e of toolEvts) byName[e.name] = (byName[e.name] || 0) + 1;
  const nameBadges = Object.entries(byName)
    .sort((a,b) => b[1]-a[1])
    .map(([n,c]) => `<span class="tool-badge">${escape(n)}<span class="c">×${c}</span></span>`)
    .join("");

  // Sample tool actions (up to 4 most informative)
  const samples = toolEvts.slice(0, 4).map(e => {
    const s = summarizeToolUse(e);
    const cls = e.result_status && e.result_status !== "success" ? "sample err" : "sample";
    return `<div class="${cls}"><span class="tn">${escape(e.name)}</span> ${escape(s)}</div>`;
  }).join("");
  const moreSamples = toolEvts.length > 4 ? `<div class="sample more">… +${toolEvts.length - 4} more</div>` : "";

  // Prompt / assistant snippets
  const promptText = promptEvt ? (promptEvt.text || "").slice(0, 300) : "";
  const asstText = lastAsst ? (lastAsst.text || "").slice(0, 300) : "";

  // Event kind density strip
  const kinds = ["prompt","thinking","assistant_text","tool_use"];
  const kindCounts = kinds.map(k => events.filter(e=>e.kind===k).length);
  const strip = events.map(e => `<span class="ev-tick k-${e.kind}${e.kind==='tool_use'&&e.result_status&&e.result_status!=='success'?' err':''}"></span>`).join("");

  return `<div class="tcard ${t.parent_agent?'subagent':''}" id="card-${i}" onclick="go('turn', ${i})">
    <div class="tcard-hd">
      <div class="tnum">T${t.turn}</div>
      <div class="tmeta">
        <b>${escape(t.agent || '?')}</b>
        ${t.parent_agent ? `<span class="parent">← ${escape(t.parent_agent)}</span>` : ''}
        · ${fmtDur(t.duration_s)}
        · ${fmtTs(t.start_ts)}
        ${t.end_reason && t.end_reason !== 'UserTurnEnd' ? `<span class="endreason">${escape(t.end_reason)}</span>` : ''}
        ${errorCount ? `<span class="errchip">${errorCount} error${errorCount>1?'s':''}</span>` : ''}
      </div>
      <div class="tstats">
        <span title="tool calls">🔧 ${toolEvts.length}</span>
        ${kindCounts[1] ? `<span title="thinking blocks">💭 ${kindCounts[1]}</span>` : ''}
        ${kindCounts[2] ? `<span title="assistant text blocks">💬 ${kindCounts[2]}</span>` : ''}
        <span title="credits">💰 ${(t.credits||0).toFixed(3)}</span>
      </div>
    </div>
    <div class="ev-strip" title="event density (${events.length} events)">${strip}</div>
    ${promptText ? `<div class="snip snip-prompt"><span class="lbl">📥 user</span> ${escape(promptText)}${promptEvt&&(promptEvt.text||'').length>300?'…':''}</div>` : ''}
    ${toolEvts.length ? `<div class="tools-block">
      <div class="tools-line">${nameBadges}</div>
      <div class="tools-samples">${samples}${moreSamples}</div>
    </div>` : ''}
    ${asstText ? `<div class="snip snip-asst"><span class="lbl">📤 assistant</span> ${escape(asstText)}${lastAsst&&(lastAsst.text||'').length>300?'…':''}</div>` : ''}
  </div>`;
}

// ---------- turn detail ----------
function renderTurn() {
  const view = document.getElementById("view");
  const t = DATA.turns[state.turnIdx];
  if (!t) { view.innerHTML = "Turn not found."; return; }

  const filter = state.filter;
  const events = (t.events || []).map((e, i) => ({...e, _i: i}))
    .filter(e => !filter[e.kind]);

  const header = `
    <div class="turn-header">
      <div><span class="k">Turn</span> <span class="v"><b>${t.turn}</b> / ${DATA.total_turns}</span></div>
      <div><span class="k">Agent</span> <span class="v">${escape(t.agent||'?')}</span></div>
      ${t.parent_agent ? `<div><span class="k">Parent</span> <span class="v">${escape(t.parent_agent)}</span></div>` : ''}
      <div><span class="k">Start</span> <span class="v">${fmtTs(t.start_ts)}</span></div>
      <div><span class="k">Duration</span> <span class="v">${fmtDur(t.duration_s)}</span></div>
      ${HAS_TOKENS ? `<div><span class="k">Tokens</span> <span class="v">${fmtNum(t.input_tokens)} in / ${fmtNum(t.output_tokens)} out</span></div>` : ''}
      ${t.context_pct != null ? `<div><span class="k">Context</span> <span class="v">${t.context_pct.toFixed(1)}%</span></div>` : ''}
      <div><span class="k">Credits</span> <span class="v">${(t.credits||0).toFixed(4)}</span></div>
      <div><span class="k">End</span> <span class="v">${escape(t.end_reason||'')}</span></div>
      <div style="margin-left:auto"><button onclick="prevTurn()" style="background:var(--panel2);color:var(--fg);border:1px solid var(--border);padding:4px 10px;border-radius:4px;cursor:pointer">← Prev</button>
      <button onclick="nextTurn()" style="background:var(--panel2);color:var(--fg);border:1px solid var(--border);padding:4px 10px;border-radius:4px;cursor:pointer">Next →</button></div>
    </div>`;

  const kinds = ["prompt", "thinking", "assistant_text", "tool_use"];
  const filterBar = `<div class="filter-bar">
    <span style="color:var(--muted)">Show:</span>
    ${kinds.map(k => `<label><input type="checkbox" ${!filter[k]?'checked':''}
      onchange="toggleFilter('${k}')">${k}</label>`).join("")}
    <span style="margin-left:auto;color:var(--muted)">${events.length} of ${t.events.length} events</span>
    <button onclick="expandAll(true)" style="background:var(--panel2);color:var(--fg);border:1px solid var(--border);padding:2px 8px;border-radius:3px;cursor:pointer">Expand all</button>
    <button onclick="expandAll(false)" style="background:var(--panel2);color:var(--fg);border:1px solid var(--border);padding:2px 8px;border-radius:3px;cursor:pointer">Collapse all</button>
  </div>`;

  const evtHtml = events.map(e => {
    const errCls = e.kind === "tool_use" && e.result_status && e.result_status !== "success" ? " err" : "";
    const bodyExtra =
      e.kind === "tool_use"
        ? `<div class="tool-args">${escape(JSON.stringify(e.args, null, 2).slice(0, 2000))}</div>
           ${e.result_status ? `<div class="tool-result${e.result_status==='success'?'':' err'}">[${escape(e.result_status)}] ${escape(String(e.result_summary||'').slice(0, 800))}</div>` : ''}`
        : "";
    return `<div class="event ${e.kind}${errCls}" onclick="toggleEvt(this, ${e._i})" data-idx="${e._i}">
      <div class="head">
        <span class="badge">${e.kind.replace('_',' ')}</span>
        <span class="title">${escape(eventTitle(e))}</span>
        <span style="color:var(--muted);font-size:10px">#${e._i+1}</span>
      </div>
      <div class="preview">${escape(eventPreview(e))}</div>
      ${bodyExtra}
    </div>`;
  }).join("");

  view.innerHTML = `<div class="turn-detail">${header}${filterBar}<div class="events">${evtHtml}</div></div>`;
}
function toggleEvt(el, i) {
  // If click was on a link/button or scrollable, ignore
  el.classList.toggle("expanded");
  // Open modal on double-tap: show full details
}
function toggleFilter(k) {
  state.filter[k] = !state.filter[k];
  renderTurn();
}
function expandAll(on) {
  document.querySelectorAll(".event").forEach(el => {
    if (on) el.classList.add("expanded"); else el.classList.remove("expanded");
  });
}
function prevTurn() { if (state.turnIdx > 0) go('turn', state.turnIdx - 1); }
function nextTurn() { if (state.turnIdx < DATA.turns.length - 1) go('turn', state.turnIdx + 1); }

// ---------- render dispatcher ----------
function render() {
  setBreadcrumb();
  if (state.view === "overview") renderOverview();
  else renderTurn();
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (document.getElementById("modal").classList.contains("show")) {
      document.getElementById("modal").classList.remove("show");
    } else if (state.view !== "overview") go("overview");
  }
  if (state.view === "turn") {
    if (e.key === "ArrowLeft") prevTurn();
    if (e.key === "ArrowRight") nextTurn();
  }
});

render();
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    title = data.get("title") or data.get("session_id", "")
    js_data = json.dumps(data, ensure_ascii=False, default=str)
    # Escape sequences that are legal in JSON but break when embedded in <script>:
    #   - '</' → '<\/'   (prevents premature </script> tag close)
    #   - U+2028, U+2029 → \u2028, \u2029  (illegal as raw chars in JS string literals)
    js_data = js_data.replace("</", "<\\/")
    js_data = js_data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = HTML_TEMPLATE
    html = html.replace("__TITLE__", (title or "")[:80])
    html = html.replace("__SESSION_TITLE__", (title or "(untitled session)")[:120])
    html = html.replace("__SESSION_ID__", data.get("session_id", ""))
    html = html.replace("__DATA__", js_data)
    return html


def cmd_list(sessions_dir: str) -> None:
    sessions_dir = _resolve_sessions_dir(sessions_dir)
    entries = []
    for f in sorted(os.listdir(sessions_dir)):
        if not f.endswith(".json") or f.endswith(".jsonl"):
            continue
        p = os.path.join(sessions_dir, f)
        try:
            m = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        state = m.get("session_state") or {}
        tms = ((state.get("conversation_metadata") or {}).get("user_turn_metadatas")) or []
        entries.append((
            m.get("created_at") or "",
            f[:-5],
            state.get("agent_name") or "",
            len(tms),
            (m.get("title") or "")[:80],
        ))
    entries.sort(reverse=True)
    print(f"{'session_id':<40} {'agent':<25} {'turns':>5}  title")
    print("-" * 120)
    for _, sid, agent, turns, title in entries[:200]:
        print(f"{sid:<40} {agent:<25} {turns:>5}  {title}")


INDEX_TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Kiro Sessions Index</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: #0f1419; color: #e6edf3; margin: 0; padding: 20px; font-size: 13px; }
  h1 { margin: 0 0 8px; }
  .meta { color: #8b95a5; margin-bottom: 16px; }
  input[type=text] { background: #1a2028; color: #e6edf3; border: 1px solid #2d3843; padding: 6px 10px; border-radius: 4px; width: 400px; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #2d3843; }
  th { color: #8b95a5; font-weight: normal; cursor: pointer; user-select: none; position: sticky; top: 0; background: #0f1419; }
  tr:hover { background: #1a2028; }
  td.mono { font-family: ui-monospace, monospace; font-size: 11px; }
  a { color: #4fc3f7; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style></head><body>
<h1>Kiro Sessions</h1>
<div class="meta">__DIR__ · <b id="count"></b> sessions</div>
<input type="text" id="search" placeholder="Filter: agent name, title, session id...">
<table id="tbl">
<thead><tr>
  <th data-key="created_at">Created ↓</th>
  <th data-key="agent">Agent</th>
  <th data-key="turns">Turns</th>
  <th data-key="duration">Duration</th>
  <th data-key="tokens">Tokens</th>
  <th data-key="credits">Credits</th>
  <th>Session</th>
  <th>Title</th>
</tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
const SESSIONS = __SESSIONS__;
let sortKey = "created_at", sortDir = -1;
function fmtDur(s) { if (!s) return "—"; if (s<60) return s+"s"; const m=Math.floor(s/60); return m+"m"+(m<60?"":""); }
function fmtNum(n) { return (n||0).toLocaleString(); }
function render() {
  const q = document.getElementById("search").value.toLowerCase();
  const rows = SESSIONS.filter(s =>
    !q || (s.session_id+" "+s.agent+" "+s.title).toLowerCase().includes(q)
  );
  rows.sort((a,b) => (a[sortKey] > b[sortKey] ? 1 : -1) * sortDir);
  document.getElementById("count").textContent = rows.length + " / " + SESSIONS.length;
  document.getElementById("rows").innerHTML = rows.map(s => `
    <tr>
      <td class="mono">${(s.created_at||'').slice(0,19).replace('T',' ')}</td>
      <td>${s.agent||''}${s.parent?' <span style="color:#8b95a5">← '+s.parent+'</span>':''}</td>
      <td>${s.turns}</td>
      <td>${fmtDur(s.duration)}</td>
      <td style="color:#8b95a5">${fmtNum(s.input_tokens)}/${fmtNum(s.output_tokens)}</td>
      <td>${(s.credits||0).toFixed(3)}</td>
      <td class="mono"><a href="sessions/${s.session_id}.html">${s.session_id.slice(0,8)}…</a></td>
      <td>${(s.title||'').slice(0,100).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</td>
    </tr>`).join("");
}
document.querySelectorAll("th[data-key]").forEach(th => {
  th.onclick = () => {
    const k = th.dataset.key;
    sortDir = (sortKey === k) ? -sortDir : -1;
    sortKey = k;
    render();
  };
});
document.getElementById("search").oninput = render;
render();
</script></body></html>
"""


def cmd_build_all(sessions_dir: str, out_dir: str, limit: int = 0) -> None:
    sessions_dir = _resolve_sessions_dir(sessions_dir)
    os.makedirs(os.path.join(out_dir, "sessions"), exist_ok=True)
    entries = []
    files = sorted([f for f in os.listdir(sessions_dir)
                    if f.endswith(".json") and not f.endswith(".jsonl")])
    for i, f in enumerate(files):
        if limit and i >= limit:
            break
        sid = f[:-5]
        try:
            data = load_session(sid, sessions_dir)
        except Exception as e:
            print(f"skip {sid}: {e}", file=sys.stderr)
            continue
        try:
            meta_p = os.path.join(sessions_dir, f)
            meta = json.load(open(meta_p, encoding="utf-8"))
        except Exception:
            meta = {}
        state = meta.get("session_state") or {}
        parent = None
        # Prefer parent agent from first turn if any
        for t in data["turns"]:
            if t.get("parent_agent"):
                parent = t["parent_agent"]
                break
        entries.append({
            "session_id": sid,
            "agent": data.get("agent_name") or "",
            "parent": parent,
            "created_at": meta.get("created_at") or "",
            "title": meta.get("title") or "",
            "turns": data["total_turns"],
            "duration": data["total_duration_s"],
            "input_tokens": data["total_input_tokens"],
            "output_tokens": data["total_output_tokens"],
            "credits": data["total_credits"],
        })
        html = render_html(data)
        with open(os.path.join(out_dir, "sessions", f"{sid}.html"), "w", encoding="utf-8") as fh:
            fh.write(html)
        if (i + 1) % 50 == 0:
            print(f"built {i+1}/{len(files)}...", file=sys.stderr)
    idx = INDEX_TEMPLATE.replace("__DIR__", sessions_dir)
    sessions_js = json.dumps(entries, ensure_ascii=False)
    sessions_js = sessions_js.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    idx = idx.replace("__SESSIONS__", sessions_js)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(idx)
    print(f"wrote {out_dir}/index.html ({len(entries)} sessions)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id", nargs="?", help="session id (basename without .json)")
    ap.add_argument("--dir", default=None,
                    help="sessions directory")
    ap.add_argument("--out", help="output html file (default: <session_id>.html)")
    ap.add_argument("--list", action="store_true", help="list available sessions")
    ap.add_argument("--json", action="store_true", help="dump normalized JSON to stdout instead of HTML")
    ap.add_argument("--all", action="store_true", help="build viewer for all sessions + index.html")
    ap.add_argument("--out-dir", default="site", help="output dir for --all")
    ap.add_argument("--limit", type=int, default=0, help="limit number of sessions in --all (0 = all)")
    args = ap.parse_args()

    if args.all:
        cmd_build_all(args.dir, args.out_dir, args.limit)
        return 0

    if args.list:
        cmd_list(args.dir)
        return 0

    if not args.session_id:
        ap.print_help()
        return 2

    data = load_session(args.session_id, args.dir)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0

    html = render_html(data)
    out = args.out or f"{args.session_id}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out} ({len(html):,} bytes, {data['total_turns']} turns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
