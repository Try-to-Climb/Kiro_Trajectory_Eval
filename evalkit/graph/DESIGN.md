# graph - trajectory provenance graph

> Status: **Tasks 1-9 implemented.** Isolated-activity rate 24.2% on the
> flagship test session; see §5 for per-task notes.

Turn a run from a flat time-ordered log into a **provenance graph**: actions
are activities, outputs are entities, edges are "who produced what, who
consumed what". The purpose is **to make a run readable**, not to evaluate --
it emits no verdict and is not a fourth evaluation path.

---

## 1. The problem

Session records are flat and time-ordered. One flagship session has 277
actions across 45 turns, which is unreadable. More importantly, **time
ordering hides workflow structure**:

- Later commands reference artefacts from many steps earlier.
- Adjacent commands can be totally unrelated.
- A retry loop looks like 16 independent commands rather than one thing
  being re-tried.

Both existing visualizations are **timelines** that answer "what happened
when", not "why did this action happen, and what is it tied to":

| Existing | Shape |
|---|---|
| `normalize/viz.py` | Text swimlanes / Mermaid gantt / PNG / cross-session lanes |
| `session-viz/build.py` | Self-contained HTML with two-level zoom (session overview -> single turn) |

---

## 2. Reference: AgentTrails

**AgentTrails: Towards Trust and Reuse for Agentic Tasks** -- Wu, Castelo,
Liu, Silva, Freire (NYU), VLDB 2026 Workshop DASHSys, arXiv:2607.18816.
PDF under `ref/AgentTrails.pdf`.

The paper formalizes this approach and solves the "graph loses time ordering"
problem that stuck us.

### 2.1 Bipartite graph

```
G_T = (A_T union E_T, R_T)
  A_T   tool-call activities
  E_T   entities: inputs, outputs, artefacts, return values
  R_T subset of (A_T x E_T) union (E_T x A_T)      strictly bipartite, no same-kind edges
```

Three edge classes, **always distinguishable** (foundation of auditability):

| Edge | Semantics | Source |
|---|---|---|
| `generatedBy` | activity -> its output | always emitted, certain |
| `usedBy` | entity -> a later activity that consumed it | reconstructed from strong evidence |
| `informedBy` | weak association | weak-evidence candidate; not asserted |

### 2.2 How time ordering is preserved

The edge-construction rule itself carries a time constraint:

```
e_j -> a_i    if   K(e_j) intersect refs(c_i.args) != empty,   j < i
```

`K(e_j)` is the key set of entity `e_j` (filename, path, id, URL);
`refs(c_i.args)` are references appearing in the i-th call's arguments; and
**`j < i` is time**. Only later calls may refer to earlier outputs -> **the
graph is naturally acyclic, and time does not need its own axis; it is in
the edge direction**.

Combined with PROV-style **entity versioning** -- an activity that modifies
a file does not "mutate the node", it **produces a new entity** -- the
read/modify sequence becomes the graph's topology:

```
(control.py@v0) --usedBy-->  a[32] read
                 --usedBy-->  a[46] read
                 ...
       a[68] edit --generatedBy--> (@v1)
       a[71] edit --generatedBy--> (@v2) --usedBy--> a[72] read
       a[73] edit --generatedBy--> (@v3)
```

One file -> four entity nodes. The "read read read read read -> edit -> edit
-> read -> edit" retry convergence becomes the chain's own shape.

Agent loops need no special handling: the paper notes that "agent loops
surface as repeated activities, which the joined graph aggregates with
per-trace multiplicity" -- a retry loop is a repeated activity sequence, not
a cycle.

### 2.3 Entities are not just files

| Strong evidence (deterministic skeleton) | Weak evidence (candidate) |
|---|---|
| identifiers / paths / filenames / URLs / names / returned objects | shared values / query terms / table columns / domain objects / tokens |

This distinction matters: 188 commands have no `path` field, so a file-only
model leaves them orphaned; admitting "values that commands return" (log
paths, PIDs) connects them.

### 2.4 LLM only patches

```
deterministic evidence -> skeleton graph
semantic evidence      -> dependency candidates (not asserted)
LLM                    -> receives sketch + candidates, returns graph patch operations
                          only patches that preserve node refs, time order,
                          and the activity/entity schema are accepted
```

Rationale from the paper: `asking LLMs to infer dependencies directly from
full traces does not scale`. Retrieve candidates first (indexed artefact
keys, value sketches, schema-aware blocking, semantic matching); the LLM
only refines the ambiguous ones.

### 2.5 Three view levels

```
L1  UpSet-style tool coverage overview   trace x tool matrix, side pane with score/cost/latency/token
                                         explicitly does not infer dependencies; used only for filter/group
L2  single-trace provenance graph         the bipartite graph above
L3  joined quotient graph                 multi-trace merge, aligning repeated tools/artefacts/dependency structures
```

L3's clustering (`activity capsules`) is motivated by **cross-run alignment**,
not single-graph compression -- which also explains why it does not worry
about clustering losing time information.

### 2.6 The paper is early-stage

```
hand-annotated 10 traces with 234 gold dependency edges
"preliminary results are promising and a full staged evaluation is underway"
"provenance quality needs gold dependency benchmarks"
```

Edge-reconstruction accuracy is **not published**. So we build our own gold
set (see Task 7).

---

## 3. Measured numbers on our data

All measured on one flagship session (45 turns):

| Quantity | Value | Meaning |
|---|---|---|
| Total actions | 277 | Flat log == 277 lines |
| `run_command` | 188 | No `path` field; a file-only model orphans all of these |
| Non-empty `response` | 188 / 188 | All present when `include_responses=True` |
| Files via `path` field | 20 | |
| File-name literals in command text | 117 (unique) | **105** of them NOT in those 20 |
| Commands that mention a known file node | 94 / 188 (50%) | Baseline coverage for command-side edges |
| `purpose` coverage | **277 / 277** | One sentence; ideal node label |
| `reasoning` coverage | 70 / 277 | Only at key decision points, median 683 chars; click-to-expand |
| `Thinking.action_refs` | 70 segments all **1:1** | **Not usable as a clustering signal** |
| `script.py` | 19 occurrences (9 read / 10 edit) | Versioning -> 11 entity nodes |
| `control.py` | 9 occurrences (6 read / 3 edit) + 33 commands mentioning it | |

Two conclusions:

1. **File nodes should grow from 20 to ~120** -- many files appear only in
   command text (produced inside scripts, grepped logs), never touched by
   `read_file`/`write_file`.
2. **Use `purpose` for node labels, not `reasoning`** -- the former has
   100% coverage and is one sentence.

---

## 4. Placement and modules

Lives at `evalkit/graph/`, parallel to `normalize` / `evidence` -- it is a
**fact-layer derivative** that any of the three evaluation paths may consume
and does not belong to any one of them.

```
evalkit/graph/
  schema.py       Activity / Entity / Edge / ProvGraph        <- Task 1
  build.py        deterministic skeleton: rebuild generatedBy + usedBy   <- Task 2-3
  keys.py         entity-key extraction: command text + response         <- Task 4-5
  shell_ast.py    tree-sitter-bash command parsing with regex fallback   <- Task 7 (step 2) refactor
  export.py       mermaid / DOT / JSON exports
  overview.py     L1 overview page (one per turn + global index)
  layout.py       graphviz-driven node coordinates
  html.py         self-contained interactive HTML output
  artifacts.py    Task 9: artefact fate / isolation reasons
  cli.py          python3 -m graph.cli <sid>
  tests/
```

Reusable building blocks:

| Reused | Source |
|---|---|
| `Action.path/command/response/purpose/idx/turn/sid/ref/cwd` | `normalize` |
| `literals` (Python literals), `structure_hash`, `peeled` | `evidence/canonicalize.py` |
| `RunTree` (if we ever go cross-session) | `evidence/loader.py` |
| `to_mermaid` (add one more graph flavour) | `normalize/viz.py` |
| HTML viewer + two-level zoom (used by Task 10 only) | `session-viz/build.py` |

---

## 5. Task breakdown

Each task below produced measurable improvements that were tested on real
sessions before moving on. Status is as of this writeup.

### Task 1: Data structures + empty graph (done)

Defines `Activity` (wraps an Action's `ref`/`idx`/`turn`/`action`/`purpose`/
`reasoning`), `Entity` (`key`/`kind`/`version`/`producer_ref`/`born_idx`),
`Edge` (`src`/`dst`/`kind`/`confidence`/`evidence`), and `ProvGraph`
(the collection plus topology queries).

`Edge.confidence` is one of `certain | likely | weak`; `Edge.evidence`
records which key matched -- this is the auditability basis, mirroring
goal's evidence tiers (`strong`/`stale`) and efficiency's strong/weak
signals.

Implementation points:
- Node ids are namespaced (`a:<ref>` / `e:<key>@v<n>`); re-adding is idempotent.
- `add_edge` enforces five invariants at the entry point, not relying on
  callers: unknown nodes / unknown kind or confidence / bipartiteness /
  edge direction (`generatedBy` must be activity->entity, `usedBy` must be
  entity->activity) / time ordering.
- `Entity.born_idx = -1` means never-produced-this-run, read-only
  (external input); sorts before every activity.
- **2-cycle refusal, not in the design but added at implementation.** Time
  order alone does not stop it: an activity and the entity it produces
  share `idx`, so both `a->e` and `e->a` are "in time". That pair is
  absurd in provenance semantics (an activity cannot consume its own
  output) and breaks Task 9 reachability, so it is structurally rejected.
- `assert_bipartite()` re-checks every invariant (including 2-cycles),
  guarding against bypassing `add_edge` and mutating `edges` directly.

Query surface used by downstream tasks: `versions_of(key)`,
`latest_version(key, before_idx)` ("the newest version visible at this read"),
`isolated_activities()` (the headline metric for Task 7), `filter_edges`,
`stats`.

### Task 2: `generatedBy` edges from the `path` field (done)

`build.py`: every action becomes an activity (including the 211 commands
with no `path` -- otherwise `isolated_activities()` would be meaningless);
write-type actions produce a new entity version from their `path` and emit
a `generatedBy` edge. The write-action set reuses
`efficiency/steps.py::_WRITE_ACTIONS` so the two sides do not drift on
"what counts as a write".

**Version-number semantics**: `vN` counts writes in **this run** -- so
`engine.py@v3` reads as "this run rewrote it three times".

Asymmetry locked in during implementation (not in the design): `v0` is
only produced when the run has a demonstrated prior state.

```
read_file     reading requires the file already exists  -> emit v0
modify_file   modifying implies a prior state           -> emit v0, then v1
create_file   explicit "new file"                       -> emit v1 only
```

### Task 3: `usedBy` edges, completing the path-field skeleton (done)

Read-type actions with a `path` emit a `usedBy` edge from the latest visible
entity version (per `latest_version(key, before_idx)`) to the activity.
Edge `evidence` field records the key match. Baseline edge recovery on the
flagship session: 94 of 188 commands connect.

### Task 4: Entity keys parsed from shell command text (done)

Pull filename-looking tokens from `command` text, admit them as entity keys
when they appear in a known schema (extension whitelist, path shape,
quoted argument position, etc.). Expands the file-node population from
20 to ~120 on the flagship session.

### Task 5: Entity keys from `response` (done)

Mine command stdout/stderr for the same kinds of literals (log paths, PIDs,
URLs). Three post-Task-5 refinements for the three common false-match
categories: wrapper-prefix stripping, response-boundary clamping, and
same-activity read-of-just-written suppression. Each is documented in
`keys.py`.

### Task 6: Export + CLI (done)

`export.py` provides mermaid / DOT / JSON serialisations; `cli.py` exposes
`python3 -m graph.cli <sid>`. The CLI was the first demo artefact and the
mechanism by which the next few refinements got surfaced.

A demo finding: the "retry loop" we expected to be five parallel chains is
in fact five fragments pointing at different intermediate artefacts. The
graph made that visible for the first time; the time-ordered views did not.

### Task 6b: Interactive single-file HTML (done)

Self-contained HTML. One page per turn, plus an overview page to navigate
between them. Edges coloured by direction; the view has a real legend;
isolation reasons and artefact "fate" are shown in the interactive view.
Runs offline. See `evalkit/graph/tests/page-harness/README.md` for the DOM
runtime harness and its limits.

### Task 7: Coverage + isolation evaluation (done)

Three measurement rounds:

**Round 1: Multi-session statistics.** Measured on 56 real sessions
(action count >= 40). Isolation-rate median: 63.6%, range 19.6-98.9%. The
previously-cited 24.2% on one session turned out to be close to best case
-- a lesson about overfitting to one session.

**Round 2: Gold set.** Hand-annotated two turns of the flagship session,
stored under `graph/tests/gold/` (internal use; not shipped). Each edge
carries `why` (evidence) and `kind` (explicit = determinable from the
action's own text or output, inferred = requires cross-action reasoning);
plus `no_edge` (correct isolation), `unlabeled` (annotator refused to
guess), and `traps` (traps with expected outcomes).

**Round 3: Five defects pointed out by the gold set, fixed.** Documented
inline in `keys.py` and `shell_ast.py`.

### Task 7 step 2: Switch to a syntax tree (done)

Replace the regex-based command extractor with tree-sitter-bash parsing,
keeping regex as a fallback (the parser bails on some constructs). This
closed extraction gaps that the pure-regex approach could not reach,
and surfaced several upstream tree-sitter-bash defects documented under
`reported/`.

### Task 8 (optional): weak-evidence candidates + LLM patch

Not implemented in this cut. See AgentTrails §2.4 for the intended shape;
would require an LLM-backed `informedBy` recovery step over value-sketch
candidates.

### Task 9 (reduced): what became of each artefact (done)

`artifacts.py`: for each entity, derive its "fate" (final version, number of
rewrites, whether read after last write, whether orphaned). Isolation
reasons are propagated into the interactive view so a reader can see why a
node is unconnected rather than just that it is.

---

## 6. Reference graph: turn 6 retry loop

In the flagship session, turn 6 was a long retry loop over
`control.py`. In a timeline view it was 16 independent commands with no
obvious grouping. In the graph view it is a single entity (`control.py`)
with a chain of version nodes and reads interleaving, making the
convergence structure obvious. See `tests/gold/9c8ef9e5-turn6.json` for
the hand-annotation (not shipped publicly) and the demo HTML the CLI
produces.

---

## 7. Known risks

- **Isolation median is still high** (63.6% across 56 sessions). Many of
  those are expected -- a command inspecting the agent's own state
  legitimately has no file dependency -- but a sizable tail is real
  extraction misses. Weak-evidence recovery (Task 8) is the plausible fix.
- **Single-annotator gold set.** All annotations are one person's reading
  of the session; there is no independent review. The gold set is the
  design's main unmitigated weakness.
- **Scripts that write files internally remain invisible.** The hook /
  official record cannot see them. The graph infers these artefacts from
  command text and response text (Tasks 4-5), which helps but is not
  comprehensive.
- **Cross-session not supported in this cut.** `ref` is already globally
  unique and `RunTree` is available, but a parent + children tree on one
  flagship session was 786 actions -- the node count would blow up and the
  current layout engine does not scale.

---

## 8. Open decisions

1. **Weak-evidence LLM patching** (Task 8) -- worth implementing if we can
   build a cost-bounded candidate retriever first; otherwise the LLM gets
   the whole trace, which is exactly the one-shot problem the design
   avoided elsewhere.
2. **Cross-session scope.** `ref` already supports it. Pending until a
   layout approach exists that survives 500+ nodes.
3. **Gold-set multi-annotator consensus.** The single-annotator weakness
   is cheap to fix mechanically (bring in a second reader) but expensive
   in attention budget; punted.
