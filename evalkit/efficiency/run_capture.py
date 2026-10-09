"""Run the full efficiency pipeline for a session, dumping every step's
output (including s8's evidence pack, prompt, and raw LLM reply) into a
subfolder of the given output directory.

Usage:
    python3 run_capture.py <sid> --official-dir <dir> --out-root <dir>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path


from evidence.loader import load_run_tree
from efficiency import steps
from efficiency import prompts as eff_prompts
from kiro_acp import KiroAcpClient


def _dump(obj, path: Path) -> None:
    def default(o):
        if hasattr(o, "to_dict"):
            return o.to_dict()
        return str(o)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=default)


def _serialize_segstep(d: dict) -> dict:
    """Convert dataclass values inside s4/s5/s6/s7 outputs to plain dicts."""
    out = {}
    for seg_id, r in d.items():
        out2 = {}
        for k, v in r.items():
            if isinstance(v, list):
                out2[k] = [x.to_dict() if hasattr(x, "to_dict") else x for x in v]
            else:
                out2[k] = v
        out[str(seg_id)] = out2
    return out


def run(sid: str, official_dir: str, out_root: Path) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = out_root / sid / f"run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[capture] session={sid}  out={out_dir}")

    # ---- s1
    print("[s1] map ...")
    ov = steps.s1_map(sid, official_dir)
    _dump(ov, out_dir / "s1_overview.json")

    # tree for s2..s7
    tree = load_run_tree(sid, official_dir=official_dir,
                         with_children=False, include_responses=True,
                         use_cache=True)

    # ---- s2 (LLM)
    print("[s2] segment ...")
    segs = steps.s2_segment(ov, tree, use_llm=True)
    _dump({"segments": [s.to_dict() for s in segs]}, out_dir / "s2_segments.json")

    # ---- s3
    print("[s3] cost stats ...")
    seg_stats = steps.s3_cost_stats(ov, segs)
    _dump([s.to_dict() for s in seg_stats], out_dir / "s3_cost_stats.json")

    # ---- s4
    print("[s4] duplicates ...")
    dup = steps.s4_detect_duplicates(sid, tree, segs)
    _dump(_serialize_segstep(dup), out_dir / "s4_duplicates.json")

    # ---- s5
    print("[s5] wasted reads ...")
    wasted = steps.s5_detect_wasted_reads(sid, tree, segs)
    _dump(_serialize_segstep(wasted), out_dir / "s5_wasted_reads.json")

    # ---- s6
    print("[s6] multi-writes ...")
    undo = steps.s6_detect_undo(tree, segs)
    _dump(_serialize_segstep(undo), out_dir / "s6_undo.json")

    # ---- s7
    print("[s7] stuck ...")
    stuck = steps.s7_detect_stuck(ov, tree, segs)
    _dump(_serialize_segstep(stuck), out_dir / "s7_stuck.json")

    # ---- s8: capture evidence pack + prompt + raw reply + tool trace
    print("[s8] judge (ACP session; tool-use loop; capturing everything) ...")
    pack = steps._build_judge_pack(ov, seg_stats, dup, wasted, undo, stuck, tree)
    judge_prompt = eff_prompts.JUDGE_PROMPT.format(pack=pack, budget=20)
    (out_dir / "s8_evidence_pack.txt").write_text(pack, encoding="utf-8")
    (out_dir / "s8_judge_prompt.txt").write_text(judge_prompt, encoding="utf-8")

    # Persistent ACP session; log every ACP event for auditing.
    acp_log: list = []
    def _log_hook(event, payload):
        try:
            entry = {"event": event}
            if isinstance(payload, (dict, list)):
                entry["payload"] = payload
            else:
                entry["payload"] = str(payload)[:2000]
            acp_log.append(entry)
        except Exception:
            pass

    captured = {"turns": []}
    tool_trace: list = []

    with KiroAcpClient(agent="kiro-judge", timeout=480,
                       log_hook=_log_hook) as client:
        def capture_caller(prompt: str) -> str:
            reply = client.prompt(prompt)
            captured["turns"].append({"prompt_len": len(prompt),
                                      "reply_len": len(reply),
                                      "reply": reply})
            return reply

        judgment = steps.s8_judge(ov, seg_stats, dup, wasted, undo, stuck, tree,
                                  caller=capture_caller, use_llm=True,
                                  trace_sink=tool_trace)
        # remember the sessionId so the transcript can be tied back to
        # ~/.kiro/sessions/cli/<id>.jsonl if needed.
        captured["acp_session_id"] = client.session_id

    _dump(judgment.to_dict(), out_dir / "s8_judgment.json")
    _dump(captured, out_dir / "s8_raw_replies.json")
    _dump(tool_trace, out_dir / "s8_tool_trace.json")
    _dump(acp_log, out_dir / "s8_acp_log.json")

    # ---- s9 narrative: objective overview + cost hotspots (LLM, but
    # fed only exact numbers; validator forbids grades/advice).
    print("[s9] narrative ...")
    narrative_facts = steps._build_narrative_facts(
        ov, seg_stats, dup, wasted, undo, stuck, tree)
    (out_dir / "s9_narrative_facts.txt").write_text(narrative_facts,
                                                    encoding="utf-8")
    narrative = steps.s9_narrative(ov, seg_stats, dup, wasted, undo, stuck,
                                   tree=tree, use_llm=True)
    _dump(narrative, out_dir / "s9_narrative.json")

    # ---- s9 aggregate
    print("[s9] aggregate ...")
    report = steps.s9_aggregate(ov, segs, seg_stats, dup, wasted, undo,
                                stuck, judgment, narrative=narrative)
    _dump(report, out_dir / "s9_final_report.json")

    # ---- s9 markdown render
    print("[s9] markdown render ...")
    md = steps.render_report_markdown(report, tree)
    (out_dir / "s9_final_report.md").write_text(md, encoding="utf-8")

    # brief console summary
    print(f"[done] verdict={report['verdict']}  "
          f"turns={ov['turns']}  credits={ov['totals']['credits']:.1f}")
    print(f"       output dir: {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sids", nargs="+")
    ap.add_argument("--official-dir", required=True)
    ap.add_argument("--out-root", required=True)
    args = ap.parse_args()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    for sid in args.sids:
        try:
            run(sid, args.official_dir, out_root)
        except Exception as e:
            print(f"[error] {sid}: {e!r}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
