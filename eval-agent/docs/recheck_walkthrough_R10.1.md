# Recheck: Full Walkthrough of a Single Invocation — the R10.1 Case

Using the real run `archive/<example-run>_withctx` as the backdrop, we trace one requirement's entire journey into recheck: **why it triggers, how the loop controller calls tools, how the LLM decides, and how the final Finding is written back**.

**All of this happens after s8's verdict is out** — s1 through s8 have already run.

**Recheck is an optional step**, disabled by default. To turn it on you pass `--recheck` explicitly on the CLI. Without it, the flow ends `s8 → s9 aggregate` exactly as today. This document assumes `--recheck` is on.

---

## 1. Context: state of R10.1 after s1–s8

### What the user said in turn 10

> "Organize the above into a yaml file that can automatically execute UL traffic"

### The Requirement extracted by s2

```
R10.1  origin_turn=10  verifiable_by=artifact  strength=strong  status=active
text:   "Organize the above into a yaml file that can automatically execute UL traffic"
expect: "a yaml config file should be produced in the trajectory, containing steps that automatically execute UL UDP iperf"
```

### The Criterion compiled by s4

```
hard_check:     { "artifact_glob": "*.yaml" }
anchors:        [yaml, UL, UDP, iperf, auto, traffic]
residual:       "whether the yaml can truly auto-execute UL UDP depends on step content"
residual_needs: ["artifact_content"]
scope:          { "idx_gte": 61 }
```

Note the `residual` line — the LLM itself admits: **producing a yaml does not prove the yaml's content is correct**. The criterion cannot reach that layer.

### s5 retrieval result

```
status:  candidates   (hard didn't hit, retrieval has candidates)
tier:    retrieved
hard_hits:   []
candidates:  9 items (run_command, scores 7–9)
anchor_df:   {yaml:142, UL:89, UDP:31, iperf:24, auto:58, traffic:17}
```

### s7 crosscheck (artifacts on disk)

```
glob=*.yaml hit 6 files:
  example_case_001.yaml            mtime=08-12 02:29  strong
  example_case_002.yaml            mtime=08-12 09:11  strong
  example_case_003.yaml            mtime=08-12 08:06  strong
  ... total 5 strong + 1 stale
tier: artifact
```

### s8 final Finding v1

```json
{
  "req_id": "R10.1",
  "satisfied": "true",
  "tier": "artifact",
  "confidence": 0.855,
  "evidence_actions": ["<example-run>#63"],
  "evidence_files": [
    "/path/to/workspace/example_case_001.yaml",
    "/path/to/workspace/example_case_002.yaml"
  ],
  "overclaim": false,
  "reason": "Filesystem hit multiple strong UL_UDP-related yamls, artifacts generated
             by script (hook cannot record command-internal writes). Whether the yaml
             truly auto-executes UL UDP needs artifact_content verification — not handled.",
  "residual": "whether the yaml can truly auto-execute UL UDP depends on step content"
}
```

**Note the last line** — `residual` is dangling, and the `reason` explicitly says "not handled."

---

## 2. Why recheck?

**R10.1 was judged `satisfied=true`, not false** — so why recheck?

Because **the verdict is positive but the evidence chain is not fully walked**:

1. **`residual` unresolved**: the LLM concedes that the criterion only reaches "artifact produced," not "artifact content correct."
2. **`confidence` was therefore 90%-discounted**: `tier=artifact` cap is 0.95, with residual → 0.95 × 0.9 = **0.855**.
3. **The agent's real ability may be underestimated**: if the yaml content is truly complete, confidence should be 0.95.
4. **`residual` is an explicit record of "I know I don't know"** — recheck exists exactly to dissolve those honestly-flagged "don't knows."

### The three recheck triggers

```python
def needs_recheck(finding):
    if finding.synthetic:                                     return False   # control group not rechecked
    if finding.satisfied == "false" and finding.strength == "strong": return True
    if finding.satisfied == "unverifiable" and finding.residual:     return True
    if finding.overclaim:                                            return True
    if finding.satisfied == "true" and finding.residual:             return True   # ← R10.1 hits this
    return False
```

R10.1 hits the last one: **judged true but has residual** — sufficient evidence for satisfaction, insufficient evidence to dissolve the residual; recheck aims to close the gap.

---

## 3. Kicking off recheck: build the index first

After s8 and before s9, `s8b_recheck` is invoked. Its first act is to **build a TreeIndex**:

```python
class TreeIndex:
    actions_by_ref:    {ref → complete action dict}     # O(1) fetch action by ref
    searchable_texts:  {ref → concatenated searchable text}  # for substring retrieval
    written_dirs:      [list of directories the agent wrote to]  # baseline for read_file whitelist
    written_files:     [(path, mtime, ref) ...]         # search space for list_files
    time_window:       (lo, hi)                         # used to judge strong/stale
```

**All four are derived from the in-memory `tree`** — no session re-scan needed. Built in tens of milliseconds; used and thrown away (not persisted).

---

## 4. Wiring up the tools

Four read-only tools, all Python functions holding the index via closure:

```python
def build_tools(idx):
    def search_actions(query, k=8): ...        # substring search over actions
    def read_action(ref):            ...       # read a single action's full detail
    def list_files(glob_pattern):    ...       # glob under written_dirs
    def read_file(path):             ...       # read an artifact file's content
    return {...}
```

**The LLM never sees `idx`**. It only knows there are four callable tools.

---

## 5. Composing the initial prompt (neutral, not steering)

```
You are performing recheck for agent trajectory evaluation. The following requirement
was judged as below in the previous round; please investigate its true state.

Requirement R10.1: Organize the above into a yaml file that can automatically execute UL traffic
expect:    a yaml config file should be produced in the trajectory, containing steps that
           automatically execute UL UDP iperf

Previous Finding:
  satisfied: true
  tier:      artifact
  reason:    "Filesystem hit multiple strong UL_UDP yamls..."
  residual:  "whether the yaml can truly auto-execute UL UDP depends on step content"

Evidence cited in the previous round:
  evidence_files: [
    "/path/to/workspace/example_case_001.yaml",
    "/path/to/workspace/example_case_002.yaml"
  ]

Available tools:
  search_actions(query: str, k: int = 8)
      substring search in the trajectory; query supports | to separate terms
  read_action(ref: str)
      read one action's full detail (with complete command)
  list_files(glob_pattern: str)
      glob files under the directories the agent wrote to
  read_file(path: str, max_bytes: int = 4000)
      read an artifact file's content; path must be under written_dirs

To call a tool, output a single JSON code block:
  {"tool_call": {"name": "...", "args": {...}}}
To emit the final verdict, output:
  {"final": {"req_id": "...", "satisfied": "...", ...}}

Budget: at most 7 tool calls. If evidence is still insufficient at 7 calls, you may
emit `unverifiable` and describe where you got stuck, for human review —
do not force-emit an unreliable verdict.
```

**Key**: tell the LLM "last round judged X"; do not say "you were right / wrong last time." Let the evidence speak.

---

## 6. The actual run: three tool-loop iterations

### Iteration 1: LLM decides to first locate relevant artifacts

**Question**: the evidence bundle already lists two yamls; why not read them directly?

Because it wants **to first confirm the candidate set is complete** — maybe there are other UL_UDP yamls produced in this run that we should look at. So `list_files` first.

```
[LLM iteration 1]
{"tool_call": {"name": "list_files",
               "args": {"glob_pattern": "*UL_UDP*.yaml"}}}
```

**Loop controller takes over**:

```python
# 1. Parse JSON, identify tool_call
# 2. Validate args: pattern length OK, no braces
# 3. Call the real tool function:
result = tools["list_files"]("*UL_UDP*.yaml")

# 4. Inside the tool:
for root in written_dirs:
    for p in glob.glob(root/**/UL_UDP*.yaml, recursive=True):
        found.append({path, strength})

# 5. Result:
```

```json
{
  "pattern": "*UL_UDP*.yaml",
  "found": [
    {"path": "/path/to/workspace/suites/example_case_001.yaml",           "strength": "strong"},
    {"path": "/path/to/workspace/suites/example_case_002.yaml",           "strength": "strong"},
    {"path": "/path/to/workspace/suites/example_case_003.yaml",           "strength": "strong"},
    {"path": "/path/to/workspace/test_parameters/example_case_001.yaml",  "strength": "strong"},
    {"path": "/path/to/workspace/test_parameters/example_case_002.yaml",  "strength": "strong"},
    {"path": "/path/to/workspace/test_parameters/example_case_003.yaml",  "strength": "strong"}
  ]
}
```

**Post-processing by controller**:
```python
allowed_files.update(6 paths)     # whitelist expanded from 2 to 8
budget: 1/7
```

**Payoff**: the LLM discovers there is a split between `suites/` and `test_parameters/`. That layer of relationship was not made explicit by s7 crosscheck. **This is exactly the information-density gain from recheck.**

### Iteration 2: LLM decides to read one yaml's content

**Question**: why not read directly? why list first?

Because `read_file` has a **path whitelist check** — a path not under `written_dirs` cannot be read. The two paths in the evidence bundle are certainly in the whitelist, but the LLM wants to confirm more candidates; listing first is the cautious approach.

**Secondary question**: why doesn't the LLM just read one of the paths already in the evidence bundle rather than list-then-read?

It **could** read directly. There are two strategies:
- **Aggressive**: directly `read_file(evidence_files[0])`
- **Cautious**: first `list_files` to see if there are more complete candidates, then pick one to read

Aggressive saves a tool call; cautious covers artifacts that s7 might have missed. Under a neutral prompt, the LLM will choose autonomously; here we illustrate the cautious variant.

```
[LLM iteration 2]
{"tool_call": {"name": "read_file",
               "args": {"path": "/path/to/workspace/suites/example_case_001.yaml"}}}
```

**Loop controller**:
```python
# Check: path ∈ allowed_files ✓
# Check: os.path.commonpath([path, some entry in written_dirs]) == that entry ✓
# Check: not binary ✓
result = tools["read_file"](path)
```

**What was actually read from disk** (this demo reads a **real file** directly from the archive):

```yaml
# example suite: UL UDP iperf on <site-id>
# UE: <device> (<serial>), auto-attach + UL 60s 200M
# Usage: python -u run.py suites/example_case_001.yaml
suite:
  name: "example_case_001"
  stp: ../assets/stp_configs/<site-id>.yaml
  numberOfRepetitions: 1
  tests:
    - testParameters: ../assets/test_parameters/example_case_001.yaml
      steadyStateDurationMinutes: 0
```

**What information the LLM extracts**:
- ✅ suite name is clearly "example_case_001"
- ✅ complete `tests` structure — auto-executable
- ✅ the comment explicitly says "auto-attach + UL 60s 200M" — **this is direct evidence of "automatically execute UL"**
- ⚠️ concrete iperf parameters are in the referenced `testParameters` file; this file doesn't spell them out
- ⚠️ `steadyStateDurationMinutes: 0` (uncertain whether this is a problem)

**Decision point**: the LLM has two choices:
- **A**: evidence is sufficient (has "auto UL" comment + complete suite structure) — emit final
- **B**: read the `testParameters` yaml once more and confirm the iperf parameters

The demo picks A (evidence is sufficient for the "UL UDP iperf auto-execute" expect). B would be stricter but costs another tool call.

### Iteration 3: LLM emits final verdict

```
[LLM iteration 3]
{"final": {
  "req_id": "R10.1",
  "satisfied": "true",
  "tier": "artifact",
  "evidence_actions": [],
  "evidence_files": [
    "/path/to/workspace/suites/example_case_001.yaml"
  ],
  "overclaim": false,
  "reason": "Reading yaml content: the comment explicitly says 'auto-attach + UL 60s 200M',
             suite structure is complete (has stp/testParameters/tests), the intent to
             auto-execute UL lands directly in the artifact. Concrete iperf parameters
             in the testParameters reference file were not read, but the main criterion
             (a yaml that auto-executes UL is produced) is confirmed by content; residual dissolved."
}}
```

**Final validation by the controller**:

```
evidence_files ⊆ allowed_files          ✓ (that path was returned by list_files)
satisfied / tier enum compliance        ✓
reason non-empty                        ✓
```

**Confidence recomputed**:

```python
base = 1.0                    # satisfied=true
tier = "artifact"             # cap 0.95
has_residual = False          # ← dissolved, no longer True

confidence = cap_confidence("artifact", 1.0, has_residual=False)
           = 0.95            # no longer × 0.9
```

---

## 7. The final Finding v2

```json
{
  "req_id": "R10.1",
  "satisfied": "true",
  "tier": "artifact",
  "confidence": 0.95,
  "evidence_actions": [],
  "evidence_files": [
    "/path/to/workspace/suites/example_case_001.yaml"
  ],
  "overclaim": false,
  "reason": "Reading yaml content...auto-attach + UL 60s 200M...residual dissolved",
  "residual": null,

  "rechecked": true,
  "recheck_delta": {
    "prior_satisfied":  "true",
    "prior_tier":       "artifact",
    "prior_confidence": 0.855,
    "prior_residual":   "whether the yaml can truly auto-execute UL UDP depends on step content",
    "changed":          false,
    "residual_resolved": true
  },
  "tool_trace": [
    {"tool": "list_files",  "args": {"glob_pattern": "*UL_UDP*.yaml"},
     "returned_paths": [ ... 6 paths ... ]},
    {"tool": "read_file",   "args": {"path": "..."},
     "returned_size":  479}
  ]
}
```

**What changed**:
- `satisfied`: unchanged (already true)
- `tier`: unchanged (artifact)
- `residual`: went from a string → `null`, `residual_resolved=true` marked
- **`confidence`: 0.855 → 0.95** — the quantifiable benefit
- New `rechecked=true` + `recheck_delta` + `tool_trace` for audit

---

## 8. Effect at s9 aggregation

R10.1 was already counted as `satisfied=12/12` in s9 (v1 was already true). After recheck:
- Verdict conclusion unchanged (PASS)
- But **the report can display the confidence lift**, showing this verdict is "harder"
- `tool_trace` is fully preserved for review

---

## 9. What this example teaches

### A typical recheck benefit pattern

> **s8 already judged true; recheck is not here to overturn the verdict** — it's here to **dissolve the "I judged true but did not fully investigate" residuals**. The verdict is unchanged, but **confidence rises + evidence chain becomes complete** — an auditable plus.

### Three key design decisions are validated

1. **The LLM does not act, the controller acts**: both `list_files` / `read_file` runs are Python-side `glob.glob` / `open()`; the LLM only emits JSON.

2. **The dynamic whitelist holds the hallucination boundary**: the path the LLM ultimately cites must come from a tool return. If it fabricates a path, validation rejects.

3. **Code computes confidence; the LLM does not score it**: the LLM emits tier and satisfied; confidence is computed by `cap_confidence(tier, base, has_residual)`. Dissolving the residual automatically lifts 0.855 → 0.95 — no subjective LLM bump needed.

### A real edge case

In the demo, the LLM judged true mainly on the yaml comment "auto-attach + UL 60s 200M." A **stricter** verdict would have read the `testParameters` yaml and confirmed iperf parameters. This exposes a real trait of recheck:

> **Recheck is only as strict as the LLM is strict.** You can raise recall by adding to the prompt: "if the evidence chain requires multiple reads to fully close, continue calling tools."

---

## 10. Summarizing R10.1's recheck in one sentence

> **s8 judged R10.1 true but the residual is dangling** (yaml content not verified), so
> confidence was discounted from 0.95 to 0.855.
>
> Recheck triggers: the LLM says "list yaml artifacts," the Python controller runs `glob.glob`
> and returns 6 candidates; the LLM says "read this one," the controller runs `open().read()`
> and returns real content; the LLM sees "auto-attach + UL 60s 200M" in the comment and
> concludes the residual is dissolved — emit final.
>
> The controller validates the path is in the dynamic whitelist, enum compliance holds,
> and recomputes confidence (no discount after dissolution).
> Conclusion: **satisfied=true unchanged, confidence 0.855 → 0.95**, full tool_trace kept for audit.
>
> Start to finish, the LLM emitted only 3 pieces of JSON; the real work — reading disk,
> globbing, computing whitelists, computing confidence — **is all done by the Python controller**.
