# 0018. Tenant overlays, app_fingerprint, and combined drift clauses

Status: accepted (Phase 12)

## Context

R7 asks how one recorded capability is reused, or safely specialized, across tenants of the same
vendor product rather than re-recorded per tenant, and how drift is detected and managed. Phase 12
measured this directly: resolving the real tenant A subaccount artifact's own descriptors against
tenant B's screens (a second tenant of the same fixture app, with two deliberate label renames and
otherwise-different routes) shows the ranked locator already absorbing some of the drift for free
(the login field's `role_ordinal` rung rescues a renamed label positionally) and failing loudly on
the rest (three descriptors -- "Member ID", "Open Subaccount", "Submit" -- have no positional
fallback and simply do not resolve, because tenant B renamed their labels, added a review step
between "Continue" and account creation, and moved the routes). Neither extreme is the answer: a
tenant with real vendor drift still needs SOMETHING to bridge it, and re-recording per tenant would
mean this project's core promise (record once, replay many times) does not survive contact with a
second tenant at all.

## Decision: an overlay is a small, separate, human-authored document, resolved at invoke time

`TenantOverlay` (models/artifact.py) names the base capability it targets (`base_capability_id`,
`base_version`), an `entry_point_override`, a `vocabulary_map`, `step_overrides` keyed by step id,
`extra_steps` (a whole additional Step inserted after a named step id), and `notes`.
`resolve_for_tenant(capability, overlay)` returns a NEW `Capability` -- the base capability, and
every `Step`/`Checkpoint`/`TargetDescriptor` it owns, is never mutated -- and that result is
**never written to `artifacts/`**. It is applied fresh, in memory, immediately before replay
(`cli.py`'s `replay --overlay PATH`, and `catalog/server.py` could do the same for a
multi-tenant catalog in the future). This is the whole answer to "reused, or safely specialized,
without re-recording": the recording stays one file, reviewed and versioned once; a second
tenant's own small, separately-reviewable overlay is what a human authors when that tenant's own
vendor version genuinely differs, and it can be thrown away and re-authored without touching the
recording at all.

## Decision: one vocabulary table, not separate label/route/checkpoint fields

`vocabulary_map` is a single `dict[str, str]`, applied as ONE substitution pass (longest key
first, one compiled regex alternation) across every step's `TargetDescriptor.name`, its
relational label, every `frame_path` segment, and every `Checkpoint.value` (a step's own
precondition/postcondition AND the capability's own success checkpoint). A renamed label
("Member ID" -> "Customer ID"), a renamed route segment ("/member/" -> "/tenantb/customer/"), and
a renamed query parameter ("?f7=" -> "?q=") are the SAME kind of fact from this project's own
point of view -- this tenant calls it something else -- and splitting them into three fields that
could each independently drift out of sync with the same underlying rename would be three places
to update instead of one. The shipped overlay (`overlays/tenant_b.json`) needs exactly seven
entries to cover every label, route, and checkpoint rename tenant B's own flow touches; a separate
route-rewrite field would have needed the identical values duplicated into a second table for no
new information.

Single-pass matters concretely: `re.sub` with one compiled alternation scans the ORIGINAL string
once, left to right, and never rescans text it has just written. A sequential `str.replace` loop
over the same dict does not have this property -- given `{"AA": "B", "B": "C"}`, replacing "AA"
first and then "B" second (or vice versa, depending on dict iteration order) can chase a lone "AA"
all the way to "C", corrupting a rename that was only ever supposed to produce "B". Sorting keys
longest-first before building the alternation also means the longest applicable key wins at any
one position, so a short key that happens to be a substring of a longer one (in vocabulary text,
not in the fixture's own case-sensitive route/label pairs, which do not collide) never fires
first and truncates the intended match.

## Decision: `step_overrides` and `extra_steps` are the escape hatch wording cannot reach

Tenant B's linked-account flow is not just reworded, it is RESHAPED: "Continue" only submits the
entered values to a review screen, and the review screen's own "Confirm" is what actually creates
the account -- a genuine extra step tenant A's single "Submit" click does not have. No amount of
vocabulary substitution produces a step that was never recorded. `step_overrides["9"]` rewrites
that step's own postcondition (tenant B's "Continue" now lands on the review screen, asserted by
`element_present button "Confirm"`, not the confirmation banner); `extra_steps` inserts a whole
new `Step` -- role button "Confirm", frame `contentframe`, postcondition
`text_present "Linked Account Opened"`, `risk_class: RISKY_IRREVERSIBLE` -- after step "9".
`resolve_for_tenant` renumbers every step's `index` after insertion (never `id`, which stays the
base recording's own stable identifier), and validates three things a human-authored overlay can
otherwise get wrong silently: a `step_overrides` key naming a step id that does not exist, an
override that changes a step's own action type (reshaping WHAT a step asserts is fine; turning a
`type` into a `click` is not something a wording table should ever be allowed to do), and an
`extra_steps` entry naming an `after_step_id` that does not exist. Each raises a typed
`OverlayError` naming the offending id, the same shape `replay/outcomes.py`'s `UnknownDetector`
already uses for an equally invalid request.

## Decision: the shipped overlay deliberately leaves two known renames undeclared

`overlays/tenant_b.json` declares "Member ID", "Subaccount", and "Submit" (all three UNRESOLVED
against tenant B without it) but deliberately omits "Username" -> "User ID" and
"Initial Deposit" -> "Opening Deposit", even though both are real, deliberate tenant B renames.
Measured: both of those two descriptors carry a `role_ordinal` fallback the recorder captured at
discovery time (an empty-named field's only real signal is its position, ARCHITECTURE.md decision
38), and that fallback rescues both positionally on tenant B -- resolving at rank 5 instead of the
recorded rank 1, but resolving. This is not an oversight; it is the overlay proving something the
other three descriptors cannot: the ranked locator strategy genuinely absorbing an undeclared
rename, with the drift visible in the evidence log (a `locator_drift` event, `understudy drift`'s
report) rather than silent. A vocabulary table that "fixed" every rename regardless of whether the
locator needed it would hide that this project's own locator strategy is doing real work.

## Decision: a rename a locator absorbs is not automatically a rename a checkpoint absorbs

Running the shipped overlay against the real subaccount recording found this the hard way: step 7's
postcondition and step 8's descriptor both name the SAME renamed field ("Initial Deposit" -> tenant
B's "Opening Deposit"), and only one of the two survived unresolved. `resolve()` (surface/locator.py)
walks six ranked strategies, so step 8's descriptor kept matching at `role_ordinal` (rank 5 instead
of rank 1) exactly as this ADR's earlier decision describes. `checkpoint_satisfied`
(models/artifact.py) has no such ladder: `element_present` matches role and name exactly, once, with
no positional or ordinal fallback at all, so step 7's postcondition -- "a textbox named 'Initial
Deposit' is present" -- failed hard the moment the field's rendered name changed, even though the
locator two steps later would have found the very same field. The fix is `step_overrides["7"]`, not
`vocabulary_map`: adding the rename to the vocabulary table would also rewrite step 8's own
descriptor to "Opening Deposit", making it resolve at rank 1 and quietly deleting the phase's only
live rank-regression case on this form. The general rule this leaves is that an overlay author has
to ask two separate questions about the same rename -- does any DESCRIPTOR need it (usually no, the
locator degrades gracefully) and does any CHECKPOINT reference it (usually yes, a checkpoint has
nothing to degrade to) -- and the shipped overlay now answers both for this field. The obvious future
option is giving `checkpoint_satisfied` the same ranked matching `resolve()` already has (fall back
to the checkpoint's own relational/ordinal signal before failing); it is not built this phase because
a checkpoint failing loudly on a genuine rename is arguably the more conservative default for an
assertion that is about to gate whether replay reports success, and R7 only asks that overlays make
graceful degradation POSSIBLE, not that every asymmetry between the two matching paths be closed.

## Decision: a single interpretation for multi-tenant vs. per-tenant drift

The same distinction this ADR's evidence makes concrete generalizes: the SAME step degrading the
SAME way across MANY tenants of one vendor product means the vendor shipped a new version of the
product itself (every tenant's overlay needs the same fix, and the fix likely belongs in the base
recording, or in a shared piece of the overlay mechanism, not repeated per tenant). ONE tenant
degrading ALONE, with sibling tenants still resolving cleanly, means a local configuration change
at that one tenant (a renamed field, a moved route specific to that deployment) -- exactly what an
overlay exists to absorb without touching anyone else's recording or overlay. `understudy drift`
(evidence/drift.py) is what makes this distinguishable in practice: it reports rank and drift
clause per run, per step, so a reviewer comparing drift reports across several tenants' replay
runs can see directly whether a given step's drift is common to all of them (vendor version) or
isolated to one (local drift).

## Decision: `app_fingerprint` warns, it never gates

`app_fingerprint` (models/observation.py) hashes the entry screen's STRUCTURAL signature (frame
count, a sorted role:count map over interactive roles, and the title/heading text) -- deliberately
never label text, so a tenant's own vocabulary rename (exactly what `vocabulary_map` exists to
absorb) does not itself register as a fingerprint mismatch. It is captured once, from the FIRST
observation after navigating to the target (`agent/loop.py`, an `app_fingerprint` event),
carried into `TargetApp.app_fingerprint` by `record/recorder.py`, and recomputed at replay time
from the entry screen (`replay/engine.py`) purely as a SIGNAL: equal means nothing further to
report, different logs an `app_fingerprint_mismatch` event naming both values, and absent on the
artifact (every capability recorded before this field existed) logs that there is nothing to
compare -- never a guess, and never a failure. This mirrors `PERCEPTION_VERSION`'s own established
stance (docs/adr/0009): a version or fingerprint mismatch CLASSIFIES a signal for a human or a
drift report to act on, and gating replay on it up front would refuse runs that would otherwise
have succeeded -- the two capabilities already in `artifacts/` predate this field entirely and
must keep replaying. `understudy fingerprint --artifact PATH` is the same shape as `understudy
approve`: a separate, human-run, out-of-band command that opens the artifact's own entry point
live and writes the computed value back through `Redactor`, because the two pre-existing artifacts'
own discovery evidence carries no `Observation` snapshot for the recorder to derive one from
retroactively.

## Decision: `_drift_reason` returns every applicable clause, not the first

Before this phase, `_drift_reason` returned on the first matching clause (`rank_regressed`), which
silently hid a second, independently true clause (`name_no_longer_matched`) whenever both applied
to the same resolution. Tenant B is exactly this case for a descriptor that carries a
`recorded_rank`: it both regresses in rank AND stops matching by name, and reporting only the rank
regression would hide the fact that the recorded NAME stopped matching at all -- the whole
tenant-vocabulary story this phase exists to surface. Both clauses are now gathered and joined
with `"+"`, in the SAME precedence order as before; a resolution where only one clause applies
still returns that one clause alone, so `tests/test_phase9.py`'s existing drift assertion
(`clause == "name_no_longer_matched"`, a case where only clause 2 applies) keeps passing unchanged.

## Decision: per-step rank data rides in `context`, not only in a drift event

`replay/engine.py`'s `_build_step_context()` adds `resolution_strategy`, `actual_rank`, and
`recorded_rank` to the context every dispatched, target-resolving step passes to
`PolicyGate.dispatch` -- not only a step that drifted. A `locator_drift` event only exists for a
step whose resolution was WEAKER than expected; without this, there is no per-step rank signal at
all for a step that resolved exactly as recorded, and a rank distribution (`understudy drift`)
would have no denominator to report a rate against. `evidence/drift.py` reads both: the `act`
event's own context for every step that resolved a target, and the separate `locator_drift` event
for the subset that drifted, joined by `step_id`. A run recorded before this phase carries neither
key on its `act` events; `evidence/drift.py` counts and names those explicitly as "no rank data",
never imputing a rank the run never measured.

## Alternatives considered

Re-recording per tenant (running `discover` again against each tenant's own screens) was
rejected: it is what R7 explicitly asks this project NOT to fall back to, and it would produce
`base_capability_id` values with no relationship to each other at all, closing off exactly the
"is this the same underlying flow, drifted, or a genuinely different flow" question a reviewer
needs to answer across tenants.

A single flat "renames" dict keyed by field NAME, applied only to `TargetDescriptor.name`, was
considered and rejected in favor of the broader vocabulary substitution: it would have missed the
checkpoint and route drift entirely, needing a second mechanism for those, which is the exact
duplication this ADR's vocabulary-table decision avoids.

Gating replay on an `app_fingerprint` mismatch (refuse to run at all, the way a version pin might)
was rejected for the same reason `PERCEPTION_VERSION` never gates: a coarse structural hash is a
signal for a human to look at, not a promise precise enough to justify refusing a run that might
otherwise complete correctly -- and the two capabilities already shipped would stop replaying the
moment this field existed, for no genuine change in either the app or the recording.
