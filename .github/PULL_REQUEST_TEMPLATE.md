## Summary

<!-- One-line description of the change. -->

## Motivation

<!-- Why is this needed? Link related issues with "Fixes #N" if applicable. -->

## Changes

- <!-- bullet list of concrete changes -->

## Testing

<!-- What did you run? Attach output snippets. -->

- [ ] `evalkit`: `python -m unittest discover -s normalize/tests -t .` passes
- [ ] `evalkit`: `python -m unittest discover -s trajectory/tests -t .` passes
- [ ] `eval-agent`: `python -m unittest discover -s tests -t .` passes
- [ ] Added new tests covering the change

## Checklist

- [ ] Updated `CHANGELOG.md` under `[Unreleased]`
- [ ] No real user data (paths, session IDs, prompts) added to test fixtures or docs
- [ ] Docs updated if user-facing behavior changed
