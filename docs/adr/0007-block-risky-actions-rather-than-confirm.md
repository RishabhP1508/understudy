# 0007. Block a RISKY_IRREVERSIBLE action and escalate, rather than confirm inline

Status: accepted (Phase 5)

## Context

The gate has to do something more than log when it sees an action it classifies as irreversible. Two
shapes were on the table: prompt the model (or a human) for a confirmation right there in the loop and
proceed if it says yes, or refuse the action outright and hand control to a human through a separate
channel.

An inline confirmation puts the decision back in the hands of the same actor whose action is in
question. In discovery that actor is the model: asking it to confirm its own risky action is asking it
to grade its own homework, and a model under instruction to complete a goal has every incentive to say
yes. In replay there is no model to ask at all, so "confirm inline" would have to mean pausing a
production run mid-step for a human who was not expecting to be paged, with no recorded reason why.

## Decision

`PolicyGate.dispatch` refuses a `RISKY_IRREVERSIBLE` action outright rather than asking anyone inline:

- Discovery: always refused. `EscalationRequired` is raised, carrying the `PolicyDecision` that
  explains which rule fired and why. The escalation and handoff mechanism (Phase 10) is the only path
  back to that action, not a follow-up prompt in the same turn.
- Replay: refused unless the capability's own recorded `status` is `"approved"` **and** the caller
  passed `allow_risky=True`. Both have to hold. `status` travels with the artifact a human actually
  reviewed; `allow_risky` is a separate, explicit opt-in per invocation, so approving a capability once
  does not silently arm every future replay of it forever.

Risk classification itself runs two independent layers, in order, first match wins:

1. **Label heuristic** (`safety/risk.py`): does the clicked element's own accessible name match an
   entry in `policy.risky_labels` (`transfer`, `delete`, `approve`, `submit payment`, `close account`,
   `wire`)? Whole-word matching, so "Transfer Funds" matches `transfer` but "Transferable" would not.
2. **Route heuristic**: does the current URL's path match `policy.mutating_routes`? This is the
   layer that exists specifically because the label heuristic is a heuristic.

## The measured limit

The label heuristic has a real, demonstrated blind spot: this fixture's own subaccount submit
control (`fixtures/legacy_bank/templates/subaccount_new.html`) is `<input type="button"
value="Submit" onclick="this.form.submit()">`. Its accessible name is "Submit", which matches no entry
in `risky_labels`. A gate that trusted the label alone would classify opening a new subaccount as
`SAFE_REVERSIBLE`, when it is exactly the kind of state-committing action the risk classification
exists to catch. `tests/test_phase5.py::test_classify_submit_on_mutating_route_is_risky_via_route_not_label`
exercises this directly: the same `Click(name="Submit")` element classifies `SAFE_REVERSIBLE` on a
non-mutating route and `RISKY_IRREVERSIBLE` on `/member/*/subaccount/new`, with the reason naming
that the label heuristic did not fire and the route rule did.

This is why route scoping is a second, independent layer rather than a refinement of the label list: no
finite list of risky-sounding words can cover every legacy app's own naming conventions, and a
20-year-old back office is exactly the kind of surface where "Submit" is doing the work "Confirm
Transfer" would do elsewhere.

## Update (Phase 7 hotfix): the route heuristic was wired to the wrong URL and never actually fired

State this plainly: from the moment this ADR was accepted through the Phase 7 live discovery run, the
route heuristic above was **inert against the real fixture**. It was correct in design and untested
against a real surface, and the gap between those two things is exactly how it shipped broken.

The live run "open a new sub-account for member 12345 and reach the confirmation screen" completed
`goal_verified` in 12 rounds. It should not have: the final action was a click on the subaccount form's
"Submit" control, classified `SAFE_REVERSIBLE` and dispatched with no escalation, even though it commits
state and is irreversible.

**The measured cause.** `PolicyGate.dispatch` read `surface.url` -- `WebSurface`'s current top-level
`page.url` -- as "the current route" for both the allowlist check and the call into `classify()`. In
`fixtures/legacy_bank`, `/app` serves a frameset (`app_frameset.html`) whose two frames do all of the
actual navigating; the top-level document itself never reloads again for the rest of the session. This
is the identical fact `docs/adr/0005-child-frame-navigation-wait.md` already recorded for click waits,
and it applies here too: measured directly against the live fixture, mid-session,

    top-level page.url  -> http://127.0.0.1:5055/app
       frame: navframe      -> http://127.0.0.1:5055/nav
       frame: contentframe  -> http://127.0.0.1:5055/member/12345/subaccount/new

Every action after login was policy-checked against `/app`, which never matches `mutating_routes`
(`/member/*/subaccount/new`) no matter what the content frame is actually showing. The route layer
never fired once, against any real session, since it was written. `test_classify_submit_on_mutating_route_is_risky_via_route_not_label`
kept passing regardless, because it calls `classify()` directly with a hand-built URL string and never
goes through a real surface -- it asserted the rule, not the wiring, and that gap is exactly what let
this through.

**The fix.** `Surface` gained an `urls()` method: every URL currently loaded, top-level plus every
child frame (`WebSurface.urls()`; `DesktopSurface.urls()` raises, like its other methods).
`PolicyGate.dispatch` now reads `urls()`, not `.url`, for a non-Navigate action's allowlist and risk
checks: the allowlist refuses if ANY loaded URL falls outside it, and `classify()` treats the action as
mutating if ANY loaded URL matches `mutating_routes`. Both are deliberately conservative in the same
direction as the rest of this ADR: with several frames loaded there is no way to always know which one
an action actually commits against, and over-triggering is the safe failure mode, not under-triggering.
`surface.url` is kept on `PolicyDecision` for a readable single value; a new `checked_urls` field
records the full set, so a reviewer can see what was actually checked instead of trusting the label.

The regression coverage this gap needed is a test that goes through `PolicyGate.dispatch` and a real
(fake, or live) `Surface`, not `classify()` in isolation:
`tests/test_phase5.py::test_mutating_route_detected_via_content_frame_urls_not_shell_url` (a fake
surface whose `.url` is a shell and whose `.urls()` includes a mutating content-frame URL) and
`tests/test_phase5.py::test_live_mutating_route_is_detected_via_content_frame_not_frameset_shell` (the
real fixture, driven directly through the gate, no LLM: log in, search for member 12345, open the
subaccount form, and assert the gate raises `EscalationRequired` on the Submit click).

## Tradeoff

Route scoping buys coverage at the cost of precision: `mutating_routes` flags a whole route, not a
specific control, so **every** click on a mutating route is treated as irreversible, including one that
turns out to be harmless (a "Cancel" link on the same page, for instance). That is a deliberate
false-positive cost, accepted on purpose: a refused safe action costs a rejected turn and, at worst, an
escalation a human clears in seconds. A missed irreversible action costs a state change nothing in this
design can undo. Conservative and occasionally wrong in the safe direction is the entire point of
ARCHITECTURE.md decision 11.

## Alternatives considered

- **Confirm inline with the model.** Rejected for the reason above: the same actor proposing the
  action is not a credible check on it.
- **A single, larger risky-labels list instead of a route layer.** Rejected: no fixed vocabulary
  generalizes to every legacy app's own button captions, and the fixture proves it does not even
  generalize to this one app's own subaccount form.
- **Ask a human for every action, safe or risky.** Rejected outright: it defeats the purpose of
  unattended replay and would make discovery unusable at the rate a model proposes actions.
