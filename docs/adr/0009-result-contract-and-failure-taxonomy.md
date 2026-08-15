# 0009. Four terminal result kinds, a ten-category hard-failure taxonomy, and Recovered as an event

Status: accepted (Phase 6)

## Context

Phase 2 shipped a two-way `Success | HardFailure` result. That is not enough to answer the
question a caller of a replayed capability actually asks: not just "did it work", but "what kind
of thing happened, and can I act on it without reading English prose". Three gaps showed up once
the contract had to carry a real failure taxonomy instead of a single `message` string.

## Decision

**Four terminal kinds, not three.** `Success`, `BusinessOutcome`, `HardFailure`, `Escalated`.

- `Success` vs `BusinessOutcome` exist as separate kinds because ARCHITECTURE.md decision 8 is
  explicit that a business outcome ("no such member") is not a failure, and the brief's glossary
  names conflating the two as the most common design mistake in this problem. A caller that has to
  parse `HardFailure.observed` prose to tell "the record does not exist" from "the automation
  broke" will retry a correct answer -- exactly the failure mode decision 8 exists to prevent. Two
  kinds cannot express this distinction with a typed field; three can.
- `Escalated` is a fourth kind, not a variant of failure, because a run that ends with a human in
  control (R6) is not describing an outcome of the target application at all -- it is describing
  who is holding the session. Folding it into `HardFailure` would force a caller to distinguish
  "the automation is stuck, debug it" from "a human took over and is mid-task" by string-matching
  a category, when the two need entirely different next actions from a caller (retry later vs.
  read `resolution`).

**Not five: `Recovered` is an event, never a terminal kind.** A recovery that worked (a slow load
waited out, a dialog dismissed) is not a decision a caller needs to make; the caller needs to know
whether the RUN succeeded, not how bumpy the road to that success was. Making "it worked, but only
after some trouble" a fifth kind would force every caller to add a branch for a distinction with no
action attached to it. What is genuinely useful -- what was recovered from, how many attempts it
took -- belongs in the evidence trail a human or a later analysis can read, not in the contract a
calling agent branches on. It is a `type="recovered"` line in run.jsonl (`evidence/logger.py`)
instead, and the run still ends in one of the four kinds above. Phase 9's `replay/recovery.py` is
what will populate it; the result contract does not wait on that to be complete.

**`HardFailure.category` is a ten-value StrEnum, not a free-text field.** Three of the ten exist
because of one measured collision (Phase 3): a dead fixture server and a stale, perception-drifted
artifact both produced the byte-identical failure message "could not resolve the target for step
0". A caller cannot act on a failure it cannot tell apart, and no amount of prose in `message`
fixes that if the prose is the same for two different root causes.

- `TARGET_UNREACHABLE`: the entry-point navigate itself never reached the server. Classified by
  the exception's own text containing `net::ERR_`, not by a separate reachability probe, because
  the entry navigate already runs first, before any locator work -- it IS the check. Adding a probe
  would duplicate a request the replay was going to make anyway and could itself time out
  differently than the real navigate does.
- `STALE_PERCEPTION` vs `LOCATOR_UNRESOLVED`: both fire only once a locator has already failed to
  resolve; the classifier's only job is to say why. It compares the artifact's
  `provenance.perception_version` against the running `PERCEPTION_VERSION`
  (`models/observation.py`) and reports `STALE_PERCEPTION` on a mismatch, `LOCATOR_UNRESOLVED`
  otherwise. Priority matters: a version mismatch is checked first because it names the actual,
  fixable cause (re-record against current perception) rather than leaving a human staring at a
  locator that looks reasonable and wondering why it stopped matching.

The remaining seven cover the rest of R3's list directly: `POLICY_DENIED` (the gate itself refused
a step the capability proposed), `ACTION_FAILED` (an "outright app error", or anything not covered
by a more specific category), `POSTCONDITION_FAILED`, `CHECKPOINT_NOT_VERIFIED`,
`PERMISSION_DENIED`, `SESSION_EXPIRED`, `UNHANDLED_DIALOG`. The last three have no detector wired
to them yet -- that is Phase 9's `replay/outcomes.py` and `replay/recovery.py` -- but the category
exists now because the result contract's shape is the part of this submission that gets read
hardest, and a caller integrating against the enum today should not have to add a case tomorrow
just because a detector arrives after the schema does.

**`stale_perception` is a classification, never a pre-flight gate.** It is tempting to read a
version mismatch as a reason to refuse the whole replay before it starts. That would be wrong: the
one artifact in `artifacts/` has `perception_version=1` (it predates the field's own existence, by
definition, since default 1 means "recorded before this field existed") and still replays
successfully end to end against the live fixture. A version mismatch says nothing about whether a
particular locator will actually fail; most locators in a drifted artifact still resolve fine, the
same way most of a legacy app's UI does not change between two perception implementations. Gating
on the version would refuse runs that would have succeeded, which is a worse failure mode than the
one this taxonomy exists to fix. `test_phase6.py` pins this directly against the real artifact.

## The measured limit

`stale_perception` is a heuristic, not a certainty, and the limit is worth stating rather than
implying the classifier is exact. A version bump can be released for a reason that never touches
the specific element a given step targets -- the artifact and the running perception code
genuinely disagree in general, but this ONE locator would have failed anyway, or would have
resolved fine either way. The classifier cannot distinguish "this failure is explained by the
version bump" from "this artifact merely happens to predate a version bump that is irrelevant to
it"; it only knows the two numbers differ. That is an accepted, stated cost: a `stale_perception`
result is a strong hint to re-record, not a proof that re-recording is the fix, and a human reading
`evidence_refs` still has to look.

## Alternatives considered

- **A single `message: str` on `HardFailure`, no `category` at all.** Rejected: this is exactly
  what produced the identical Phase 3 message for two unrelated root causes, and it is the
  contract a calling agent has the least to act on programmatically.
- **Recovered as a fifth terminal kind.** Rejected above: it has no action a caller needs to take
  that `Success` does not already cover, and it would force every integration to add a branch for
  a distinction with no decision attached to it.
- **One combined `LOCATOR_FAILED` category instead of splitting stale_perception /
  locator_unresolved.** Rejected: this is the one change the Phase 3 measurement exists to argue
  against. A combined category throws away the one piece of information (did perception itself
  change) that tells a human where to look first.
