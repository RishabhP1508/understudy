# 0015. The control token, and why the handoff is on the same session

Status: accepted (Phase 10)

## Context

R6 asks for a human to take over a stuck run, do something by hand, and give control back so the
run continues. The requirement is specific about two things that are easy to fake: the human must
operate THE SAME live session, not a fresh one, and there must be a way to know who is or should be
in control.

Both halves are easy to stub and hard to mean. A "handoff" that opens a second browser has lost the
login, the wizard position, and the half-filled form, which is precisely the state a human was
called in to deal with. And "who holds control" written as a comment is not a mechanism.

There was also a concrete forcing function. Since Phase 7 the subaccount capability has been
impossible to record at all: the agent logs in, searches, opens the form, fills every field, and is
correctly refused at the submit because that route is in `mutating_routes` and discovery never
auto-approves an irreversible action. The run ends in escalation, and the recorder only runs on a
verified success. Escalation is therefore not a box to tick here. It is the only path by which that
capability can exist.

## Decision 1: four states, and the two transient ones are the point

`ControlState` is AUTOMATION, PENDING_HANDOFF, HUMAN, PENDING_RESUME. Only AUTOMATION permits a
dispatch.

A boolean would have covered "is a human driving". It would not cover the two windows in which
NOBODY should be acting: after the runner has raised an intervention but before the operator has
accepted it, and after the operator has handed back but before the runner has re-observed and
decided how to continue. Both are states in which an action dispatched by either side would be
acting on a page neither of them has looked at. Making them refuse is the reason there are four
values rather than two, and each is asserted refusing in the tests.

Transitions are validated against an explicit map and an illegal one raises rather than being
quietly allowed. Every transition logs `control_transition` with from, to, actor and timestamp, so
custody of the session is a readable sequence in the same evidence log as everything else.

## Decision 2: the control model is enforced at the existing choke point

`PolicyGate.dispatch` consults the broker as check 0, before anything about the proposed action is
inspected, and refuses with an ordinary `PolicyDenied` carrying `rule="control_token"`.

Two reasons for putting it there rather than in the runners. First, "only the holder may act" is
then enforced by the same mechanism as every other safety rule, and `tests/test_constraints.py`
invariant 2 already proves `Surface.act` has exactly one call site, so there is no second path an
action could take while a human holds the token. Second, a guard in the runners would be a
convention that decays the moment someone adds a call site, which is the argument ARCHITECTURE.md
decision 7 already makes for the gate's existence.

Check 0 refuses before risk classification runs, so its decision records `risk="n/a"` and
`checked_urls=[]` rather than inventing values: whether a human holds the token is not a property
of the proposed action at all.

## Decision 3: approval is external state, never a caller argument

An operator resolving a `risky_action_requires_approval` intervention with "approve" authorizes
exactly one dispatch of the action that raised it, without a full handoff. The gate consumes that
approval; it does not accept an `approved=True` parameter from its caller.

This matters more than it looks. The entire value of a single choke point is that a caller cannot
argue its way past it. An approval flag on `dispatch` would mean any call site could grant itself
permission, and the guarantee would be back to convention. Instead the approval is state a human
created, stored on disk, and consumed exactly once.

"Exactly once" is enforced in the store, not in memory. The first implementation kept granted
approvals in a `set` on the broker instance, and its test granted and consumed on the same object.
That test could not express the situation the feature exists for: the operator console is a
separate local process, so an approval it granted would never have reached the run's gate, and the
one demonstration this phase is built around would have failed silently. The grant is now derived
from the stored resolution and the consumed bit is persisted, so it crosses a process boundary and
survives a restart. The replacement test uses two brokers over one store directory.

## Decision 4: the same session, and what that costs

Chromium runs headed, which CLAUDE.md has required since Phase 2 specifically so this phase could
be real. "Take control" therefore means the operator uses the browser window the run already has
open: same cookies, same login, same half-filled form. There is no code that hands the session
over, because there is nothing to hand over. That is the strongest form of the claim R6 asks for.

The honest cost is that a Playwright sync page is bound to the thread that created it, so a real
human cannot be simulated by a test driving the same page from another thread. The automated tests
therefore act as the operator on the same thread, exercising the broker, the token, the endpoints
and the resume logic, and the genuinely-manual half is produced by a person at the keyboard. The
report says which evidence came from which.

## Decision 5: resume is not blind

On handback the runner re-observes and then, in order: if the current step's postcondition now
holds, the human already did that step's work, so the step is skipped and a
`step_skipped_after_handoff` event records why; otherwise, if the precondition holds, the step is
retried from the top; otherwise the run escalates again as `unrecoverable_condition` rather than
looping.

Blind resumption is the obvious wrong answer. A human may have completed the step, done half of it,
or navigated somewhere else entirely, and the recorded postcondition is the only thing that
actually knows which. Re-running a completed step is not harmless either: the step this phase
exists to unblock is an irreversible submit.

## Decision 6: the automatic dialog policy stands down during a handoff

Phase 9 installed a budgeted dialog policy that auto-dismisses native dialogs. While the token is
not AUTOMATION it returns "none" before the budget is consulted, so a `window.confirm` a human was
escalated to decide reaches that human instead of being dismissed out from under them, and standing
down does not silently spend the budget either.

A native dialog is not a DOM event, so a human answering one produces no captured human action. The
evidence for it is the dialog event's own `handled` value being "none".

## Decision 7: an intervention expires, and the run terminates

Every request carries `expires_at`. Past it with no resolution, the runner takes the token back to
AUTOMATION and terminates as `HardFailure(escalation_unresolved)`, a new thirteenth failure
category. Runs do not hang waiting for a human who is not coming.

The wait itself is a poll of the store, and it lives in `escalation/control.py` rather than under
`replay/`, which bans fixed sleeps (ARCHITECTURE.md decision 18). Polling a file for a human's
decision is a bounded wait on an external actor with the intervention's own deadline, not a guess
about a machine, and naming it honestly in one place beats hiding it behind a fabricated condition.

## Tradeoffs and alternatives

A file-backed store rather than a database or a queue, because the architecture is one run process
plus one local web process and CLAUDE.md rules out scaling infrastructure. The cost is a
read-modify-write race between the two processes, which is real and was measured; it is handled by
one lockfile per intervention id, with a bounded acquisition so a stale lock fails loudly instead of
hanging.

`session_lost_mid_flow` still cannot distinguish an expired session from a wrong password, both of
which land back on the login form. That limit is inherited from Phase 9 and unchanged here.
