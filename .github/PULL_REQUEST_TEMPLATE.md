<!--
Thanks for the pull request. Keep it small and focused: one change per PR is much
easier to review than five.

Before you open it, please run:

    python3 -m pytest
    ruff check src tests

If the diff touches the interface, start the app once as well: several layout and
style problems only appear with a real desktop style.
-->

## What this changes

<!-- One paragraph, present tense: "Adds ...", "Fixes ..." -->

## Why

<!-- The problem it solves. Link the issue if there is one: Fixes #123 -->

## How it was tested

<!--
Be concrete: which tests were added, which commands were run, what was verified
by hand. "It compiles" is not a test result.
-->

- [ ] `python3 -m pytest` passes
- [ ] `ruff check src tests` is clean
- [ ] New behaviour is covered by a test, or this PR explains why it cannot be
- [ ] The interface was started once if QML changed
- [ ] Documentation updated when configuration, flags or behaviour changed

## Checklist

- [ ] `src/dcs/core` still imports no Qt
- [ ] Detection rules cite the evidence they rely on
- [ ] A non-clean medium is still never archived and never uploaded
- [ ] No secrets, personal paths or live malware samples are included
- [ ] I wrote this myself, or I disclosed AI assistance below

## AI assistance

<!--
Disclosure, not disqualification: say what was generated and how it was verified.
See CONTRIBUTING.md, section "AI-assisted contributions".
-->
