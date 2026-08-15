# 0006. Target elements with a ranked list of descriptors, not a selector

Status: accepted (Phase 4)

## Context

Replay has to find the same control again on a page that may have changed since it was recorded: a
new row above it, a renamed caption, a different tenant's build of the same vendor product, a version
upgrade. A CSS selector answers "where was it", which is the least durable thing about an element. A
single accessible name answers "what is it", which is better but not always available, and this app
proves it: eight controls have no accessible name at all and only got one because Phase 3 derived it
from a neighbouring table cell.

Two measured facts from the captured observations shaped the design.

**Derived names systematically collide with their source.** Phase 3 gives a control the caption from
the cell beside it, so the control and the caption now share a name. Measured across the two captured
observations:

    'Savings Balance'  x3   cell, iframe, generic          (member detail)
    'Account Type'     x2   cell, combobox                 (subaccount form)
    'Nickname'         x2   cell, textbox                  (subaccount form)
    'Initial Deposit'  x2   cell, textbox                  (subaccount form)
    'Savings'          x2   cell, option                   (subaccount form)

**Role separates every one of them.** There are zero duplicate `(role, name)` pairs in either
observation. So the first rung, role plus exact name, is unique in every real case on this target,
and the lower rungs exist for the cases this fixture does not currently contain and for drift.

**A recorded descriptor can go stale even when the app does not change.** The Phase 2 artifact
recorded the login field as `role=textbox, name=""`. Phase 3's name derivation now perceives that
same field as `name="Username"`. The application is byte-identical; our reading of it improved, and
that alone was enough to break a recorded step. Any design where a step carries exactly one way to
find its target is one perception change away from being worthless.

## Decision

A recorded target is a `TargetDescriptor` carrying several independent signals, and resolution walks
them in a fixed priority order:

    1 ROLE_NAME_EXACT       role + exact accessible name
    2 ROLE_NAME_NORMALIZED  role + name compared case-folded and whitespace-collapsed
    3 ROLE_NAME_SCOPED      role + name within an ancestor scope (and frame_path)
    4 RELATIONAL            "the control in the row whose label cell reads X"
    5 ROLE_ORDINAL          the Nth element of this role, optionally within a scope
    6 DOM_FALLBACK          a CSS selector, recorded and explicitly marked brittle

A strategy that matches EXACTLY ONE element wins and reports its rank. A strategy that matches more
than one is recorded as ambiguous and SKIPPED, never resolved to the first hit. If no strategy
resolves uniquely, resolution returns no element together with the per-strategy candidate counts, so
a failure says which signals were tried and how many things each one saw.

`ROLE_ORDINAL` deliberately keys on role alone, not on role plus name. If it filtered by name first
it would inherit the failure of whatever name-based strategy already failed, and could never act as a
fallback. Keying on role is what lets it recover a descriptor whose recorded name no longer matches
anything.

## Tradeoff

Six strategies is more machinery than one selector, and a descriptor is bigger on disk than a CSS
string. The cost is paid once at record time and read by a human reviewing an artifact, which is
exactly the audience the brief cares about. The real risk is a lower rung resolving to the WRONG
element confidently: `ROLE_ORDINAL` in particular is a positional bet, and it is ranked fifth for
that reason, above only the CSS fallback. That is why the rank that actually resolved is returned to
the caller and recorded, so replay can report that it succeeded on a weaker signal than the one it
recorded. Phase 9 turns that into a drift signal.

## Alternatives considered

- **A single CSS or XPath selector.** Rejected. It encodes position and markup, both of which this
  target changes freely, and it would tie the artifact to the web, which kills the desktop story.
- **A single best strategy chosen at record time.** Rejected. It is the design that just failed:
  Phase 2 recorded one signal per step and a perception improvement invalidated it.
- **Try every strategy and vote.** Rejected. Disagreement between strategies has no principled
  resolution, and it hides which signal was actually trusted. A strict order with the winning rank
  reported is auditable; a vote is not.
- **Let the model re-find the element at replay time.** Rejected outright. That puts a model in the
  replay decision loop, which the whole design forbids.
