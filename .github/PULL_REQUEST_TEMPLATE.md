## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- Link the issue if there is one: Fixes #123 -->

## How it was verified

<!--
Not "it works". What did you actually run, and what did it output?
Screenshots welcome for anything visual.
-->

## Checklist

- [ ] `pytest tests/` passes
- [ ] `ruff check app/ tests/` is clean
- [ ] If I changed the notebook, I ran `python3 app/build_core.py` and committed the result
- [ ] No API key, `cases.db`, or customer transcript is included
- [ ] I added or updated a test for anything I changed

## Cost

<!--
Does this add Gemini API calls to a customer or agent turn? The free tier is
rate limited, so say so plainly. "No change" is a fine answer.
-->
