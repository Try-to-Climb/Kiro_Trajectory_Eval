"""Step-level annotation view: a sequential list of activities with label
controls, independent from `graph/html.py`'s spatial graph view.

Conventions follow `html.py`: self-contained HTML, vanilla JS, dark theme,
data embedded as JSON in a `<script>` block. No framework, no CDN.

Persistence is browser-local: labels auto-save to `localStorage`, so the page
survives a reload. Sharing is explicit: an **Export** button downloads a JSON
file, an **Import** button loads one. This matches evalkit's "offline tool"
ethos and lets annotations live beside session data without any backend.

Phase 1 (this file) ships the starter codebook read-only and only humans
answer. Phase 2 will add codebook editing and LLM-assisted labelling.
"""
from __future__ import annotations

import html as html_lib
import json
from typing import Any, Iterable, Mapping, Optional

from .schema import ProvGraph
from .annotate_schema import Codebook, STARTER_CODEBOOK, codebook_to_dict

__all__ = ["to_annotate_html"]


# ---------------------------------------------------------------------------
# Payload: everything the page needs, pre-computed server-side
# ---------------------------------------------------------------------------
def _step_payload(g: ProvGraph,
                  prompts: Mapping[int, str],
                  responses_by_turn: Mapping[int, Iterable[str]]) -> list[dict]:
    """One row per activity, in idx order.

    Fields are chosen so the annotator does not have to leave the page to
    label: purpose and reasoning are the agent's own words about the step, the
    command preview says what it actually did, and `edges_summary` is a
    one-liner about reads/writes so the context is on screen.
    """
    rows: list[dict] = []
    sorted_activities = sorted(g.activities.values(), key=lambda a: a.idx)
    for a in sorted_activities:
        reads = [e for e in g.edges
                 if e.src in g.entities and e.dst == f"a:{a.ref}"]
        writes = [e for e in g.edges
                  if e.src == f"a:{a.ref}" and e.dst in g.entities]
        edges_summary = {
            "reads": [g.entities[e.src].key for e in reads],
            "writes": [g.entities[e.dst].key for e in writes],
        }
        rows.append({
            "ref": a.ref,
            "idx": a.idx,
            "turn": a.turn,
            "action": a.action,
            "purpose": a.purpose or "",
            "reasoning": a.reasoning or "",
            "command": a.command or "",
            "path": a.path or "",
            "duration_ms": a.duration_ms,
            "edges": edges_summary,
        })
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def to_annotate_html(g: ProvGraph,
                     codebook: Codebook = STARTER_CODEBOOK,
                     prompts: Optional[Iterable[str]] = None,
                     responses_by_turn: Optional[Mapping[int, Any]] = None,
                     **_ignored) -> str:
    """Render the annotation list page as a single self-contained HTML string.

    Extra kwargs are accepted so the function slots into `graph.cli`'s
    uniform `fn(g, ...)` call site without a special case.
    """
    prompts_by_turn: dict[int, str] = {}
    for i, p in enumerate(prompts or (), start=1):
        prompts_by_turn[i] = p or ""

    steps = _step_payload(g, prompts_by_turn, responses_by_turn or {})
    data = {
        "session": g.session_id,
        "codebook": codebook_to_dict(codebook),
        "steps": steps,
        "prompts_by_turn": prompts_by_turn,
    }

    payload = json.dumps(data, ensure_ascii=False)
    # The JSON goes into a <script type="application/json"> block, which is
    # browser-safe for `</script>` escaping as long as `/` is escaped. json.dumps
    # does not escape it by default; a replace is cheaper than custom encoder.
    payload = payload.replace("</", "<\\/")

    css = _CSS
    js = _JS
    return _HTML_TEMPLATE.format(
        session=html_lib.escape(g.session_id),
        n_steps=len(steps),
        n_questions=len(codebook.questions),
        css=css,
        payload=payload,
        js=js,
    )


# ---------------------------------------------------------------------------
# The page: HTML + CSS + JS, each as its own constant for grepping
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>annotate · {session}</title>
<style>{css}</style>
</head>
<body>
<header id="hdr">
  <div class="title">
    <strong>annotate</strong>
    <span class="sid">{session}</span>
    <span class="stat"><span id="progress">0</span> / {n_steps} steps labelled
      (across {n_questions} questions)</span>
  </div>
  <div class="actions">
    <button id="btn-export">Export JSON</button>
    <label class="imp">Import JSON<input type="file" id="btn-import"
                                        accept=".json" hidden></label>
    <button id="btn-clear" title="Discard all labels on this page">Clear</button>
  </div>
</header>

<section id="codebook"></section>
<section id="steps"></section>

<script type="application/json" id="data">{payload}</script>
<script>{js}</script>
</body>
</html>
"""


_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #0f1115; color: #e6e8ee;
       font: 14px/1.5 -apple-system, "Segoe UI", "Helvetica Neue", Arial,
             "PingFang SC", "Microsoft YaHei", sans-serif; }

#hdr { position: sticky; top: 0; z-index: 10;
       background: #171a21; border-bottom: 1px solid #2a303c;
       padding: 12px 20px; display: flex; align-items: center;
       justify-content: space-between; gap: 12px; flex-wrap: wrap; }
#hdr .title strong { color: #f3b454; margin-right: 8px; }
#hdr .sid { color: #9aa3b2; font-family: ui-monospace, Menlo, monospace;
            font-size: 12px; margin-right: 12px; }
#hdr .stat { color: #9aa3b2; font-size: 12.5px; }
#hdr #progress { color: #7bd389; font-weight: 700; }
#hdr .actions { display: flex; gap: 8px; }
#hdr button, #hdr .imp {
  background: #1d2129; color: #e6e8ee;
  border: 1px solid #2a303c; border-radius: 6px;
  padding: 6px 12px; font-size: 12.5px; cursor: pointer; }
#hdr button:hover, #hdr .imp:hover { background: #262c38; border-color: #3a4152; }

#codebook { padding: 14px 20px; background: #131620;
            border-bottom: 1px solid #2a303c; }
#codebook h2 { margin: 0 0 10px; font-size: 13px; color: #9aa3b2;
               text-transform: uppercase; letter-spacing: 1px; }
.q-card { margin: 8px 0; padding: 8px 12px; background: #1a1e28;
          border-left: 3px solid #5aa9ff; border-radius: 4px; }
.q-card .qp { font-weight: 600; margin-bottom: 2px; }
.q-card .qlabels { color: #9aa3b2; font-size: 12.5px; }
.q-card .qlabels code {
  background: #0b0d12; border: 1px solid #2a303c;
  padding: 1px 6px; border-radius: 4px; margin-right: 4px; font-size: 12px; }
.q-card .qrules { color: #7a8294; font-size: 12px; font-style: italic;
                  margin-top: 4px; }

#steps { padding: 10px 20px 60px; }
.step { background: #171a21; border: 1px solid #2a303c; border-radius: 8px;
        padding: 10px 14px; margin: 10px 0; }
.step.labelled { border-left: 4px solid #7bd389; }
.step.partial  { border-left: 4px solid #f3b454; }
.step .row1 { display: flex; align-items: baseline; gap: 10px;
              flex-wrap: wrap; }
.step .idx { color: #5aa9ff; font-family: ui-monospace, Menlo, monospace;
             font-size: 12px; }
.step .act { color: #f3b454; font-weight: 600; font-size: 13px; }
.step .turn { color: #9aa3b2; font-size: 11.5px; }
.step .purpose { color: #e6e8ee; margin-top: 2px; }
.step .detail { color: #9aa3b2; font-size: 12.5px; margin-top: 6px;
                background: #0b0d12; border: 1px solid #2a303c;
                border-radius: 4px; padding: 6px 10px;
                font-family: ui-monospace, Menlo, Consolas, monospace;
                white-space: pre-wrap; word-break: break-word;
                max-height: 4.5em; overflow: hidden; cursor: pointer; }
.step .detail.open { max-height: none; }
.step .detail .lab { color: #5aa9ff; font-weight: 700; font-size: 10.5px;
                     letter-spacing: .5px; text-transform: uppercase;
                     margin-right: 6px; }
.step .edges { color: #7a8294; font-size: 11.5px; margin-top: 4px;
               font-family: ui-monospace, Menlo, monospace; }
.step .edges .ar { color: #5aa9ff; margin: 0 4px; }

.labels { display: flex; gap: 14px; margin-top: 10px; flex-wrap: wrap;
          padding-top: 8px; border-top: 1px dashed #2a303c; }
.labels .lctl { display: flex; flex-direction: column; gap: 3px; }
.labels .lctl .lq { color: #9aa3b2; font-size: 11px;
                    text-transform: uppercase; letter-spacing: .5px; }
.labels select { background: #1d2129; color: #e6e8ee;
                 border: 1px solid #2a303c; border-radius: 4px;
                 padding: 4px 8px; font-size: 13px; min-width: 110px; }
.labels select:focus { border-color: #5aa9ff; outline: none; }
.labels select.answered { background: #1a2a1a; border-color: #3a5a3a; }
"""


_JS = """
(function () {
  'use strict';
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const STORE_KEY = 'evalkit:annotate:' + DATA.session;
  const qs = DATA.codebook.questions;
  const qById = Object.fromEntries(qs.map(q => [q.id, q]));

  // state: { "a1b2c3d4-xxxx#1": { "q1_correctness": "Yes", ... } }
  let state = loadState();

  function loadState() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed.annotations || {};
    } catch (e) { return {}; }
  }

  function saveState() {
    const payload = {
      session: DATA.session,
      codebook_id: DATA.codebook.id,
      annotations: state,
      updated_at: new Date().toISOString(),
    };
    try { localStorage.setItem(STORE_KEY, JSON.stringify(payload)); }
    catch (e) { console.warn('localStorage write failed', e); }
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function appliesTo(q, step) {
    return q.applies_to.length === 0 || q.applies_to.includes(step.action);
  }

  function renderCodebook() {
    const box = document.getElementById('codebook');
    const header = '<h2>Codebook: ' + esc(DATA.codebook.description) + '</h2>';
    const items = qs.map(q =>
      '<div class="q-card">'
      + '<div class="qp">' + esc(q.prompt) + '</div>'
      + '<div class="qlabels">'
      + q.labels.map(L => '<code>' + esc(L) + '</code>').join('')
      + '</div>'
      + (q.house_rules
          ? '<div class="qrules">' + esc(q.house_rules) + '</div>'
          : '')
      + '</div>'
    ).join('');
    box.innerHTML = header + items;
  }

  function renderStep(step) {
    const anns = state[step.ref] || {};
    const applicableQs = qs.filter(q => appliesTo(q, step));
    const answered = applicableQs.filter(q => anns[q.id]).length;
    const cls =
      answered === 0 ? '' :
      answered < applicableQs.length ? ' partial' : ' labelled';

    const reads = step.edges.reads;
    const writes = step.edges.writes;
    const edgesLine = (reads.length || writes.length)
      ? '<div class="edges">'
        + reads.map(k => '<span class="ar">←</span>' + esc(k)).join(' ')
        + ' '
        + writes.map(k => '<span class="ar">→</span>' + esc(k)).join(' ')
        + '</div>'
      : '';

    const cmdBlock = step.command
      ? '<div class="detail" data-expand><span class="lab">cmd</span>'
        + esc(step.command) + '</div>'
      : '';
    const thinkBlock = step.reasoning
      ? '<div class="detail" data-expand><span class="lab">think</span>'
        + esc(step.reasoning) + '</div>'
      : '';

    const labels = applicableQs.map(q => {
      const sel = anns[q.id] || '';
      const options = ['<option value="">—</option>']
        .concat(q.labels.map(L =>
          '<option value="' + esc(L) + '"'
          + (sel === L ? ' selected' : '') + '>' + esc(L) + '</option>'
        )).join('');
      const sc = sel ? ' answered' : '';
      return '<div class="lctl">'
        + '<label class="lq" for="s-' + esc(step.ref) + '-' + esc(q.id) + '">'
        + esc(q.id) + '</label>'
        + '<select class="' + sc.trim() + '" id="s-' + esc(step.ref) + '-'
        + esc(q.id) + '" data-ref="' + esc(step.ref)
        + '" data-qid="' + esc(q.id) + '">' + options + '</select>'
        + '</div>';
    }).join('');

    return '<div class="step' + cls + '" data-ref="' + esc(step.ref) + '">'
      + '<div class="row1">'
      + '<span class="idx">#' + step.idx + '</span>'
      + '<span class="act">' + esc(step.action) + '</span>'
      + '<span class="turn">turn ' + step.turn + '</span>'
      + '</div>'
      + (step.purpose
          ? '<div class="purpose">' + esc(step.purpose) + '</div>' : '')
      + cmdBlock + thinkBlock + edgesLine
      + '<div class="labels">' + labels + '</div>'
      + '</div>';
  }

  function renderSteps() {
    document.getElementById('steps').innerHTML =
      DATA.steps.map(renderStep).join('');
    updateProgress();
  }

  function updateProgress() {
    let answered = 0;
    for (const step of DATA.steps) {
      const anns = state[step.ref] || {};
      const applicable = qs.filter(q => appliesTo(q, step));
      if (applicable.length && applicable.every(q => anns[q.id])) answered++;
    }
    document.getElementById('progress').textContent = answered;
  }

  function onSelectChange(ev) {
    const sel = ev.target;
    if (!sel.dataset || !sel.dataset.ref) return;
    const ref = sel.dataset.ref, qid = sel.dataset.qid;
    state[ref] = state[ref] || {};
    if (sel.value) state[ref][qid] = sel.value;
    else delete state[ref][qid];
    if (Object.keys(state[ref]).length === 0) delete state[ref];
    saveState();
    sel.classList.toggle('answered', !!sel.value);
    const stepEl = sel.closest('.step');
    const step = DATA.steps.find(s => s.ref === ref);
    if (stepEl && step) {
      const anns = state[ref] || {};
      const applicable = qs.filter(q => appliesTo(q, step));
      const n = applicable.filter(q => anns[q.id]).length;
      stepEl.classList.remove('labelled', 'partial');
      if (n === applicable.length && n > 0) stepEl.classList.add('labelled');
      else if (n > 0) stepEl.classList.add('partial');
    }
    updateProgress();
  }

  function onDetailClick(ev) {
    const el = ev.target.closest('[data-expand]');
    if (el) el.classList.toggle('open');
  }

  function exportJSON() {
    const payload = {
      session: DATA.session,
      codebook_id: DATA.codebook.id,
      annotations: state,
      exported_at: new Date().toISOString(),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)],
                         { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = DATA.session + '.annotations.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function importJSON(ev) {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(reader.result);
        if (!parsed.annotations || typeof parsed.annotations !== 'object') {
          alert('Not a valid annotations file (missing "annotations").');
          return;
        }
        if (parsed.session && parsed.session !== DATA.session) {
          const ok = confirm(
            'This file is for session ' + parsed.session
            + '\\nCurrent page is ' + DATA.session
            + '\\nImport anyway? Labels for unknown steps will be ignored.');
          if (!ok) return;
        }
        state = parsed.annotations;
        saveState();
        renderSteps();
      } catch (e) {
        alert('Could not parse JSON: ' + e.message);
      }
    };
    reader.readAsText(f);
    ev.target.value = '';   // let the same file be picked again
  }

  function clearAll() {
    if (!confirm('Discard all labels on this page?')) return;
    state = {};
    saveState();
    renderSteps();
  }

  renderCodebook();
  renderSteps();
  document.addEventListener('change', onSelectChange);
  document.addEventListener('click', onDetailClick);
  document.getElementById('btn-export').addEventListener('click', exportJSON);
  document.getElementById('btn-import').addEventListener('change', importJSON);
  document.getElementById('btn-clear').addEventListener('click', clearAll);
})();
"""
