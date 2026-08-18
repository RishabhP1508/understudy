# 0016. Reinstating `insufficient_funds`

Status: accepted (Phase 11)

## Context

Phase 9 (docs/adr/0014) shipped `KnownOutcome.detector` and `RecoveryRule.trigger` as named,
resolved predicates rather than prose, and gated seeding on what a recorded flow could actually
produce. `insufficient_funds` failed both tests at the time: no flow in the project spent value at
all (the only capability recorded was a read-only balance lookup), and `replay/outcomes.py` defined
no `balance_check` detector for it to name. Keeping the seed anyway would have failed
`outcomes.validate()` the moment any capability ever emitted it, so Phase 9 dropped it entirely
rather than merely gating it, and said so in `record/recorder.py`'s own comment.

Phase 10 changed the facts on the ground. Recording the subaccount-opening capability required
building the escalation-and-handoff mechanism in the first place, precisely because its "Submit"
click is `RISKY_IRREVERSIBLE` and discovery will not auto-approve it. That capability's step 8
types an amount into "Initial Deposit" and step 9 clicks "Submit" -- a flow that spends value and
can genuinely be told no. The Phase 9 reasoning was correct when it was written and became false
the moment that recording existed.

## Decision

`insufficient_funds` is reinstated end to end, on the same two pieces Phase 9's gate checks:

1. **The app can say no.** `fixtures/legacy_bank/app.py`'s `subaccount_new` gained a business rule,
   placed after the existing `validation` injection branch and before the redirect: if the member
   has a balance on file and the submitted deposit parses as a number exceeding it, the route
   re-renders `subaccount_new.html` with `deposit_error` set to an insufficient-funds message and
   status 200, reusing the template's existing error slot rather than adding a new one. A member
   with no balance, or a deposit that does not parse, skips the check and behaves exactly as
   before -- there was no reachable condition to test before this change, and an inert mechanism
   that never fires is a correctness bug in this project, not tidy defensive code.
2. **A detector exists.** `replay/outcomes.py` gained `balance_check`, using the same `scan_text`
   helper every other detector uses, matching the fixture's own "Insufficient funds" wording.
3. **The seed is earned, not applied wholesale.** `record/recorder.py` gained
   `_earns_insufficient_funds`, mirroring `_earns_validation_rejected`'s shape: earned when a `type`
   step's target name, lowercased, reads as monetary ("deposit", "amount", "transfer") and a later
   `click` step submits it. The read-only balance lookup still does not earn it, and still cannot,
   because it never types into anything -- the PRODUCE axis from docs/adr/0014 is unchanged; the
   fact underneath it moved.

Re-running `understudy.cli record --run-dir evidence/discovery-idc6c0778a1d81` (the real Phase 10
subaccount discovery run, never re-recorded by a fresh live run) against the updated recorder
produced `artifacts/open-a-new-savings-subaccount-...v2.json`, identical to v1 in every field
except `version`, `name`/`description` (both proposed fresh by the same optional metadata call),
`known_outcomes` (now four, `insufficient_funds` last), and `provenance.timestamp`.
`provenance.transcript_hash` is byte-identical to v1's, because both were built from the same real
transcript file -- confirming this is the same recording read again, not a new one.

## Tradeoff

The fixture's balance is a plain string ("$1,204.55") and the new check reparses it on every POST
rather than storing a numeric balance alongside it. That duplicates a `$1,204.55` -> `1204.55`
parse `replay/engine.py`'s own checkpoint matching never needed before. Accepted because the
fixture is a test target (ARCHITECTURE.md's "the fixture app is a fixture"), the parse is four
lines, and a stored numeric field would be one more piece of seed data to keep in sync with a
string another route already prints verbatim.

## Alternatives considered

Adding the business rule to a different route or gating it on a fixed threshold instead of the
member's own balance was rejected: the goal is a genuine business condition a real deposit-taking
flow would enforce, not a second injectable failure mode alongside `INJECT_MODES` (that dispatch
already exists for the orthogonal recoverable-condition catalog Phase 9 built against, and
insufficient funds is a business outcome, not a recoverable one).

## Superseded

This partially supersedes docs/adr/0014's decision 3, which stated `insufficient_funds` "is earned
by none of them and is no longer emitted." That was accurate for the one capability recorded at the
time. It is no longer accurate for the subaccount-opening capability, whose flow does earn it. The
gating axis and its reasoning (docs/adr/0014, decision 3's first paragraph) are otherwise unchanged.
