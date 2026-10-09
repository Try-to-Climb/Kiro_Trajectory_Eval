"""goal_completion evaluation -- rule-free, evidence-based.

Nine steps: requirements are extracted from what the user asked for, compiled
into anchors and hard checks, evidence is gathered deterministically, and only
then does a judge rule on each requirement while restricted to citing the refs
it was given. See DESIGN.md.
"""
