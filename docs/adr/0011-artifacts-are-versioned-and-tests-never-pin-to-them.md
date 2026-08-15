# 0011. Artifacts are append-only on write, and tests never pin to generated artifact content

Status: accepted (Phase 7 hotfix)

## Context

`cli.py discover` always wrote `{slug}.v1.json`, with no check for a file already there. A live
discovery run of a goal whose text slugified to the same string as an earlier one overwrote the
earlier recording outright. That is how this project lost its own non-negotiable evidence: the
original Phase 2 discovery run's artifact, recorded before name derivation, redaction, and
relational hints existed, was silently replaced by a later, real run of the same goal text. It is
not recoverable: rebuilding it from `evidence/discovery-3348784c8a88/run.jsonl` now raises
`KeyError: 'checkpoint_eval'`, because `record/recorder.py` moved to the Phase 6 event schema and
that run predates it.

The immediate trigger was incidental (a live verification run during Phase 7 happened to reuse an
earlier goal's exact wording), but the root cause is structural: `capability.version` existed on
the schema and meant nothing, and five tests across two phase files asserted on the frozen
CONTENT of a file under `artifacts/`, which is a generated directory (`artifacts/ # recorded
capabilities [P2 onward, generated]`) that any real, successful run of the same goal legitimately
rewrites.

## Decision

**Artifacts are append-only.** `discover` now checks for existing `{slug}.v<N>.json` files before
writing, writes `{slug}.v<N+1>.json`, and sets `capability.version` to match. A second successful
run of the same goal text can never again destroy the first recording; it produces a new version
alongside it. `capability.version` now means something: which recording of this goal, in order,
this file is.

**A test must never depend on the frozen content of a file under `artifacts/`.** Five tests did:
two in `test_phase4.py` read a specific recorded `TargetDescriptor` (an empty accessible name, a
specific ordinal) off the file on disk to demonstrate a locator-drift case; three in
`test_phase6.py` mutated the real artifact's step 0 target to make it deliberately unresolvable,
or asserted the file's own recorded `perception_version` was a specific number. All five broke the
moment the file's content changed, because their premise never was really "this specific recorded
descriptor exists" -- it was "a descriptor shaped like this exists", which does not need reading
from disk to be true. The fix follows a pattern this codebase already uses elsewhere:
`test_phase6.py`'s own T4 builds a broken artifact PROGRAMMATICALLY from the real one in memory,
and `test_phase4.py`'s own module docstring says ambiguity and cross-frame cases are built as
CONSTRUCTED `Observation`s by hand, because the captured fixtures do not contain every case worth
testing. The same discipline now applies to `TargetDescriptor`s and to `perception_version`:

- The two `test_phase4.py` locator-drift tests construct `TargetDescriptor(role="textbox",
  name="", ordinal=...)` directly. They were never really testing "what Phase 2 recorded"; they
  were testing how `resolve()` behaves given a descriptor with an empty name, which needs no
  artifact at all to state.
- The two `test_phase6.py` perception-version tests derive the mismatch from
  `PERCEPTION_VERSION - 1` in memory, never from a hardcoded number and never from whatever the
  file on disk happens to record right now. This also means the tests survive the next perception
  version bump with no edit required.
- The remaining `test_phase6.py` test (the live forced-failure case) mutates the real artifact's
  step 0 target's `role` to a value no element can have, rather than its `name`/`ordinal`. This is
  the one genuinely load-bearing consequence of the artifact's content changing: the newer
  recording carries a relational hint (`docs/adr/0004`'s row-label derivation, which the original
  Phase 2 recording predates) that a name+ordinal-only mutation left intact, and `RELATIONAL` found
  the labelled row and resolved anyway -- silently turning a test meant to force a hard failure into
  one that no longer did. A bad `role` defeats every one of the six resolution strategies at once,
  RELATIONAL included, because all six filter their candidate pool by role before anything else
  (`surface/locator.py`'s `_STRATEGY_FUNCS`). This is the more conservative mutation, not merely a
  different one: it does not depend on which other signals a given recording happens to carry.

**The artifact stays; the new content is the one kept.** The replacement recording is strictly
better evidence than the one it replaced: derived accessible names instead of empty strings, a
proper `${param:password}` reference instead of a destroyed `[REDACTED]` value, a readable
rationale, `perception_version=2`, and relational hints throughout. Nothing under `artifacts/` or
`evidence/` was edited or reverted to produce this fix; only the tests that wrongly depended on one
file's specific frozen content changed, and their assertions are exactly as strong as before --
only the source of their input did.

## Alternatives considered

- **Restore the original artifact from evidence.** Tried; not possible. `evidence/discovery-3348784c8a88/run.jsonl`
  predates the Phase 6 event schema `record/recorder.py` now requires, so rebuilding from it raises
  `KeyError: 'checkpoint_eval'` rather than reconstructing anything.
- **Freeze a copy of the artifact under `tests/fixtures/` instead of constructing inputs by
  hand.** Rejected: a frozen copy is exactly the same failure mode one level removed -- it is still
  content a future change could legitimately need to move past, and the two `test_phase4.py` cases
  in particular are not really about one specific artifact at all, only about a descriptor shape.
  Constructing the input directly says what the test actually depends on, in the test itself.
- **Leave the tests reading `artifacts/` and just re-record before every test run.** Rejected
  outright: it would spend the free-tier LLM quota (measured at 8-9 calls per discovery run, 20/day
  total) on every CI run, for a check that has nothing to do with a live model.
