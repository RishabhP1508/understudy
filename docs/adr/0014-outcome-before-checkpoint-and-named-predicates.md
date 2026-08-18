# 0014. Outcomes before recovery before checkpoint, and predicates are named, not prose

Status: accepted (Phase 9)

## Context

R3 asks replay to distinguish three classes of ending: expected business outcomes the caller needs
to know about, recoverable conditions, and hard failures that stop with a debuggable error. The
requirements register names conflating the first with the third as the most common design mistake
in this problem, and ARCHITECTURE.md decision 8 already fixed the ordering in prose: business
outcomes first, recovery second, checkpoint last.

Prose was all it was. Phase 8 shipped `KnownOutcome.detector` as the string `"balance_check"` and
`RecoveryRule.trigger` as the sentence "a native confirm/alert/prompt dialog appears mid-flow".
Nothing evaluated either. The recorded balance-lookup capability declared `insufficient_funds`, an
outcome a read-only lookup cannot produce, because a module-level starter library was applied
wholesale to every capability. The result was a capability whose result contract looked complete
and could not fire.

## Decision 1: a detector and a trigger are named predicates, resolved at load

`KnownOutcome.detector` names a function in `replay/outcomes.py`'s `DETECTORS`, typed
`Callable[[Observation], str | None]` and returning the app text it matched, so the caller gets the
application's own field message rather than a boolean. `RecoveryRule.trigger` names a function in
`replay/recovery.py`'s `TRIGGERS`, typed `Callable[[TriggerContext], str | None]`.

Both registries are resolved when the artifact is loaded, before a browser launches. A name that
does not resolve raises `UnknownDetector`, listing the unknown name and the known ones. It is not
skipped and it does not silently never match, because a business-outcome detector that quietly
never fires is precisely how "no such member" reaches the caller as a crash. The CLI treats it as
a caller error (exit 2): a broken artifact is not a run that failed, it is a request that was never
valid.

Two registries rather than one, because they answer different questions over different inputs. A
business detector is pure over an `Observation` alone, as the brief's framing requires: whether the
record exists is a fact about the screen. A trigger also needs the step index, the login-prefix
boundary, the surface's `last_navigation`, and any native dialogs that fired since the last check,
because "the session was lost mid-flow" and "a navigation is still in flight" are not properties of
a screenshot.

The trigger registry does double duty. `recovery.unrecovered()` maps three of the same named
conditions to failure categories (`native_dialog_unhandled` to UNHANDLED_DIALOG,
`session_lost_mid_flow` to SESSION_EXPIRED, `app_error_page` to APP_ERROR) for the case where no
declared rule applied or its budget was spent. One vocabulary of conditions, read twice, rather
than a second private list inside the engine that could disagree with the first.

## Decision 2: the evaluation order, and why it is that order

Per step, after the action has run and a fresh observation has been taken:

1. **Known outcomes.** A terminal match returns `BusinessOutcome` immediately and the run ends
   with exit code 0. This is first because it is the only tier that can produce a *correct* answer
   that happens not to be the recorded happy path. Evaluating it after recovery would let a retry
   rule keep reloading a page that is correctly reporting "no member matches that search"; after
   the postcondition, the run would already have been reported as a failure.
2. **Recovery rules.** First declared rule whose trigger fires, budgeted per rule per step by
   `max_attempts` and per run by a module constant. Apply, log a `recovered` event carrying the
   rule, the trigger, the attempt number, the backoff where one applies, and the result, then
   re-evaluate the step from the top. A `reauth` re-dispatches the step's own action, because the
   session it ran under is gone; every other action does not, because the action already ran and
   only the page needed recovering.
3. **Unrecovered conditions**, mapped to a precise failure category as above.
4. **The step's postcondition.** Last, because a postcondition that does not hold is only a
   failure once we know the page is not showing a legitimate business answer and is not in a state
   we know how to recover from.

## Decision 3: seeds are gated, on two different axes

A known outcome is gated on **what the flow can produce**, because a declared outcome the flow
cannot reach is a false contract in the file the brief treats as a focal point, and a dead detector
in replay's hot path. `member_not_found` is earned by a flow that types into a record-lookup field
and clicks; `permission_denied` by a flow that opens a protected record; `validation_rejected` by a
flow that submits typed input. `insufficient_funds` is earned by none of them and is no longer
emitted: it required a flow that spends value, and its `balance_check` detector was deleted rather
than left as a name with no implementation, which would now fail `validate()` on sight.

A recovery rule is gated on **whether replay can actually perform it for this flow**. A slow load,
a dialog, or a transient 503 can happen to any flow regardless of what one recording happened to
hit, so gating those on "did this run hit one" would strip every rule from every clean recording,
which is every recording worth keeping. What can genuinely be absent is the ability to recover:
`reauth` needs recorded login steps to re-run, so it is emitted only when
`login_prefix_len(steps, entry_point) > 0`.

## Decision 4: drift is reported without inventing a baseline

`TargetDescriptor.recorded_rank` is new, set by `describe()` resolving the descriptor it just built
against the observation it was built from. It is `None` on every artifact recorded before the field
existed, including the one this project actually shipped, because the recorder reads descriptors
out of an already-written event log and has no observation to resolve against.

An earlier version assumed rank 1 for those. That was rejected: it is a guess standing in for a
measurement, and it emits a spurious warning for any step that legitimately resolved at a weaker
rung when it was recorded. A drift signal that cries wolf is worse than one that stays quiet.

`_drift_reason` instead fires on either of two things, neither assumed. `rank_regressed` when a
measured `recorded_rank` exists and the winning rank is weaker. `name_no_longer_matched` when the
descriptor recorded a non-empty name and the strategy that actually won is not one of the three
name-matching rungs, which needs no baseline at all. The second clause is what closes the case
found in Phase 6: a descriptor carrying an ordinal or a relational hint still resolves
positionally when its recorded name matches nothing, and did so in total silence. Measured: with
step 0's name mutated to `NoSuchControlAnywhere`, the run still succeeds (correctly, via the
relational rung) and now emits a `locator_drift` event naming the name that stopped matching.

## Decision 5: the declared template is the caller's message, the app's words are evidence

`KnownOutcome.message_template` was write-only when this phase's first cut landed: the recorder
wrote it on all three seeded outcomes and the only code reading it was
`message = matched_text or outcome.message_template`, whose `or` is unreachable because the text
scan never returns an empty string. Measured: the artifact seeds "No member found for the given
id." and a live replay of member 99999 returned the application's "No member matches that search."

Deleting the dead branch would have made the field unreachable rather than removing dead code, so
the direction was reversed instead. `message` is now `message_template or matched_text`, and
`BusinessOutcome` gained an `observed` field carrying the application's literal text.

Three fields, three jobs. `code` is what a caller branches on. `message` is the capability's own
declared meaning, which a human or a calling agent can read straight off the artifact before the
capability has ever been invoked, which is what R2's "reviewable by both" asks for. `observed` is
what the application actually said. The split deliberately mirrors `HardFailure`'s existing
`expected`/`observed` rather than inventing a second shape for the same idea.

The deciding argument is Phase 12. Two tenants of the same vendor product will render different
strings for the same outcome, and a caller whose `message` changes per tenant, or when a vendor
rewords a page, has a contract the artifact was supposed to absorb that drift for. The remaining
fallback to `matched_text` is reachable and real, because `message_template` defaults to `""`.

## Tradeoffs and alternatives

Detectors and triggers are application-shaped: `member_lookup_no_match` matches this fixture's own
message text. That is inherent, not accidental, and it is why the artifact names a detector by
string rather than embedding a pattern. A real deployment attaches the registry to the vendor
product or `app_id`; the seam for that is the same one Phase 12 uses for tenant overlays.

An alternative to a registry was letting the artifact carry the predicate itself, as a text pattern
or an expression. Rejected: an artifact that carries executable matching logic is an artifact that
has to be trusted, and the whole point of resolving by name is that a reviewer can read the
capability and a maintainer can see every detector the build implements in one file.

A `transient_error_page` that survives its retry budget deliberately has no category of its own.
It falls through to whatever the step's own postcondition reports, because the ten-category
taxonomy (docs/adr/0009) has no more honest name for it than the ordinary failure the step already
produces.
