"""Agent-tuning feature: take user-flagged steps + the agent's definition,
ask Kiro what to change in the agent to address those flags.

Input :  flags.json (produced by the graph annotation overlay)
         + agent JSON path (`.kiro/agents/<name>.json`)
Output:  concrete suggestions for the agent's prompt / skills / pipeline.

Not integrated with `graph/`. Phase 1 CLI is `python3 -m tune.runner`.
"""
