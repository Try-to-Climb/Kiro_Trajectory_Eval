Continuing from the previous turns. You have extracted every stage group and its policies.
This turn does one last thing for task type `{task_name}`: pick the **backbone order**.

From the policies already marked `deterministic`, select the 3 to 5 steps that any legitimate run
of this task type must perform, in order, and emit them as a pipeline.

Requirements:
- Each step is written as `"<verb> <glob>"`, where verb is one of reads / runs / write / dispatches.
- Include only steps that happen on **every** run. Optional or conditional steps must be left out.
- The order must reflect real causal or phase precedence, not a mere listing.
- If one precedence relation matters more than the rest (e.g. "must probe reachability before
  dispatching"), state it separately under `before`.

Output JSON only. No prose, no markdown code fences.

{"pipeline": ["<verb> <glob>", "..."],
 "before": [["<earlier>", "<later>"]],
 "rationale": "<one sentence on why these steps form the backbone>"}

If this task type has no stable backbone, return an empty `pipeline` array.
