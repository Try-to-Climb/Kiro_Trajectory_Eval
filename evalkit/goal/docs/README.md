# evalkit/goal/docs/ — Deep-dive documents

`goal`'s top-level [`README.md`](../README.md) covers usage; `../DESIGN.md` covers architecture and tradeoffs. This directory holds **topic-specific deep-dive documents**.

## Index

### [`R10.1_walkthrough.md`](R10.1_walkthrough.md)

Traces one real requirement `R10.1` ("produce a yaml file") from **the user's original phrasing** through all 9 steps, ending as a scored, cited `Finding`.

After reading, you'll understand:
- How s2 turns a phrase like "make a yaml" into a structured `Requirement`
- How s4 compiles it into `hard_check` + `anchors`
- Why s5 emits `candidates` when the hard check is empty but retrieval hit
- Why s7 must go to the filesystem for evidence (the hook can't see what a script writes internally)
- How s8's `build_pack` is assembled, how the LLM judges, how `confidence` is computed (not by the LLM)
- How the s9 control group backs the final verdict

Good material for **teaching** or **onboarding new contributors**.

### [`recheck_design.md`](recheck_design.md)

Design proposal (**not yet implemented**) for `s8b_recheck`: upgrade s8 from a "one-shot mapping" to "tool-assisted iterative evidence gathering" for the few requirements judged `false` / `unverifiable` / with residual.

After reading, you'll understand:
- What triggers recheck, what doesn't
- Input/output/validation of the 4 read-only tools (`search_actions` / `read_action` / `list_files` / `read_file`)
- How dynamic `allowed_refs` / `allowed_files` whitelists extend without breaking the anti-hallucination gate
- The specific prompt wording that keeps the LLM neutral (avoiding confirmation bias)
- The 3-phase rollout plan with success metrics

This is the **next-phase roadmap**; reading it clarifies the boundaries of the current implementation and where things are headed.

## Adding a new document

Put it here and add an entry to the index above. Naming convention:
- **Walkthroughs**: `R<req-id>_walkthrough.md`
- **Design proposals**: `<feature>_design.md`
- **Deep analyses**: `<topic>_analysis.md`
