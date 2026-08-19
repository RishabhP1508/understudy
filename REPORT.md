# Report

## Architecture

Where an application exposes an API, you integrate through the API. Understudy exists for the long
tail that does not: core banking screens and servicing tools where driving the UI is the only way in.

Understudy has two execution paths sharing one contract: the `Capability` artifact
(`models/artifact.py`). Discovery runs an LLM observe-decide-act loop (`agent/loop.py`) and
records what worked; replay (`replay/engine.py`) runs it with no model in the decision path,
AST-enforced (`tests/test_constraints.py`).

The seam is the `Surface` protocol (`surface/base.py`): `observe()`/`act()`. A step names a role
and accessible name, never CSS: the artifact is surface-agnostic by construction.

Python 3.11 is pinned as a floor: the machine default outran wheel availability for
pydantic-core, Playwright, and google-genai (`docs/adr/0001`). Pydantic v2 serializes every type
reaching disk, no dataclasses. typer serves the CLI, FastAPI the mock operator console; ruff,
mypy, pytest gate the code.

Playwright drives the browser as a library, launched headed: the handoff hands a human the exact
browser window open, impossible if headless.

Perception is one `page.aria_snapshot` call per step into an accessibility tree (`docs/adr/0002`):

- never HTML or a screenshot
- the one representation a browser and Windows UI Automation share
- small enough to fit a prompt without truncation guesswork

`PolicyGate.dispatch` (`safety/policy.py`) is `Surface.act`'s only call site in `src/` (invariant
2); the model proposes, never holds a browser handle. One process, no queues: escalation blocks
the run thread synchronously via `SessionBroker.escalate`, not a broker.

The target, `fixtures/legacy_bank/`, is a deliberately hostile Flask app: an HTML 4.01 frameset,
no ARIA, no `<label for=>`, a submit control that is an `<input type="button">` with an inline
`onclick`. Built rather than pointed at a public demo site for three reasons: no public site returns a
permission denial or a session expiry on demand and the error taxonomy needs all nine; automating
someone else's site raises the terms question the brief warns about; and a second tenant of the same
vendor product was one Flask blueprint away.

Discovery calls Gemini's free Flash family (`llm/gemini.py`, `docs/adr/0003`): schema-constrained
function calling (`tool_config` mode `ANY`) at zero cost. `gemini-3.6-flash` recorded the balance
capability, `gemini-3.1-flash-lite` the subaccount capability and is the default; quota is
20/day/model, so switching `GEMINI_MODEL` opens a fresh budget.

The model addresses elements by `[index]` via validated tool calls (`agent/tools.py`), never
prose. A turn sends a diff instead of a full render only when `Observation.digest()` is unchanged
since the last turn (`docs/adr/0010`): a diff mid-change would leave the model acting on a stale
index. All eight tools require a `rationale` at the schema level (`agent/tools.py`); the system
prompt is `agent/prompts.py`.

Measured: discovering the balance capability took 22,422 tokens, 98.0 seconds, 8 turns, 0
rejected; replaying it took 0 tokens, 2.2 seconds.

## Artifact schema

A `Capability` (`models/artifact.py`) carries:

- `schema_version`/`version` (below)
- `capability_id`, `name`, `description`
- `target` (app id, entry point, tenant id, `app_fingerprint`)
- typed `inputs`/`outputs` (name, type, required, sensitivity, source step)
- ordered `steps`
- a `success` `Checkpoint`
- `known_outcomes`
- `recovery_rules`
- `provenance`
- `status`
- a read-only `stability` signal

`schema_version` is this file's own shape; `version` is which recording of this goal this is,
incrementing per re-record; `provenance.perception_version` tracks a third drift, whether the
reading of the app changed since capture (`docs/adr/0012`).

Each `Step`'s `target` is a `TargetDescriptor` (`surface/locator.py`): role, name, scope, frame
path, ordinal, relational hint, CSS fallback, confidence, `recorded_rank`, ranked rather than one
selector since a recorded target can go stale with the app unchanged (`docs/adr/0006`).

`record/recorder.py` builds a `Capability` as a separate pass over a written `run.jsonl`, not
live, so a recorder fix re-applies to a real past run with no fresh model call (`docs/adr/0012`).

The artifact is decoupled from the transcript by construction: no field is named `messages`,
`transcript`, `completion`, `choices`, or `content`; `provenance.transcript_hash` is a hex digest
(invariant 4).

Storage is one JSON file per version, `artifacts/{slug}.v{N}.json`, append-only: `discover`
once overwrote this project's first genuine discovery evidence (`docs/adr/0011`). Files on disk
diff cleanly; no database is warranted at this scale.

## Determinism & error handling

Four sources of determinism:

- no model in the loop (invariant 1)
- a ranked locator needing a unique match
- one `checkpoint_satisfied` shared by discovery and replay (`models/artifact.py`)
- explicit condition waits under `replay/`, never a fixed sleep (`docs/adr/0005`)

Per step: known business outcomes, then recovery rules, then the postcondition (`docs/adr/0014`).
The result union has four kinds on
`kind` (`models/result.py`): `Success`; `BusinessOutcome` (`code`, `message`, `observed`);
`HardFailure` (`step_id`, a 13-value `FailureCategory`, `expected`, `observed`, `evidence_refs`);
`Escalated`.

Locator resolution walks six ranked strategies (exact name, normalized name, scoped name,
relational hint, ordinal, CSS fallback), skipping a strategy matching more than one element
rather than resolving to its first hit. The one exception to "no fixed sleeps" is
`WebSurface.pause`, for retry backoff, in `surface/` as a deliberate delay, not a condition
wait. Locator rank degradation is a secondary drift signal.

Five defects, each measured live:

- Perception improved in Phase 3 and invalidated every artifact with the app unchanged, hence
  `perception_version` and `STALE_PERCEPTION` distinct from `LOCATOR_UNRESOLVED` (`docs/adr/0009`).
- `describe()` and ordinal resolution briefly indexed different pools, so a recording saying
  "Edit" would have clicked "Delete"; fixed by one shared pool helper.
- The recorder's dead-end pruning deleted the action escaping a detour, not the detour itself, on
  `[A, A, B, A, C]` (`docs/adr/0013`).
- A descriptor with no recorded ordinal rescued itself off a lone same-role survivor even when its
  name no longer matched; drift now names the mismatch, and the rescue fires only when the
  recorded name was empty (`surface/locator.py`).
- `resolve()` walks six ranked strategies but `checkpoint_satisfied` does one exact match with no
  fallback, so a checkpoint can fail on a field the locator later finds fine (`docs/adr/0018`).

## Heterogeneity & multi-tenant

The `Surface` protocol is the heterogeneity seam: artifact and engine speak only roles, names, and
typed actions, so a second `Surface` is additive. `surface/desktop_stub.py` names, per method, the
Windows UI Automation API it maps to and where the mapping fails; it does not run.

`TenantOverlay` (`models/artifact.py`) names a base capability, a vocabulary table, step
overrides, and extra steps; `resolve_for_tenant` returns a new `Capability` in memory, never
mutating the base or touching `artifacts/`; invalid overrides raise a typed `OverlayError`.
`app_fingerprint` hashes the entry screen's structure (frame count, role counts, title, never
label text) as a replay-time signal only, never a gate (`docs/adr/0018`).

The same step degrading the same way across many tenants means a vendor version; one tenant alone
means local drift the overlay should absorb (`understudy drift`, `evidence/drift.py`).

Measured: `overlays/tenant_b.json` leaves two real tenant B renames undeclared ("Username" to
"User ID," "Initial Deposit" to "Opening Deposit"), both descriptors recording a real name and a
recorded ordinal (`role=textbox`, `ordinal=0`/`1`, `recorded_rank=1`). On tenant B the name no
longer matches, so `ROLE_ORDINAL` resolves each by its recorded ordinal at rank 5, and the drift
event names both the rank regression and the mismatch (`rank_regressed+name_no_longer_matched`)
rather than staying silent; a fingerprint mismatch on tenant B warns and continues.

Two limits: (1) business-outcome detectors (`replay/outcomes.py`) match the app's own wording;
tenant B's "member not found" is caught only because it renders a string the detector already
scans for, and `TenantOverlay` has no field for detector vocabulary; (2) `app_fingerprint` hashes
the title too, so a rebranded tenant mismatches even with an identical vendor build, answering a
within-tenant question, not a cross-tenant one; the fix would be the overlay carrying its own
expected fingerprint per tenant.

## Escalation & handoff

The subaccount-opening capability could not be recorded at all until escalation worked: discovery
reaches the submit button, `PolicyGate` refuses it as `RISKY_IRREVERSIBLE` on a mutating route,
and the run ends in escalation, since discovery never auto-approves an irreversible action and
`record/recorder.py` only runs on a verified success. A human approved the refused action through
the operator console, the run resumed on the same live browser session, reached "Subaccount
Opened," and only then was `artifacts/open-a-new-savings-subaccount-...v2.json` recorded.
Escalation is the only path by which that file exists.

Seven discovery stopping conditions name distinct stuck shapes (`goal_verified`, `max_steps`,
`timeout`, `no_progress`, `loop_detected`, `dead_end`, `escalation`); eight `ReasonCode` values
(`models/intervention.py`) name why a run escalated. A `ControlToken` has four states,
`AUTOMATION`, `PENDING_HANDOFF`, `HUMAN`, `PENDING_RESUME`; only `AUTOMATION` dispatches, and the
two transient states refuse everything, since nobody has looked at the page in that window
(`docs/adr/0015`). Enforcement sits in `PolicyGate.dispatch` as check 0, before the allowlist or
risk classification, not duplicated in the two runners.

The handoff is the same session: the operator uses the exact browser window open, same cookies,
login, half-filled form. Resume re-observes and, in order, skips the step if its postcondition
already holds (`step_skipped_after_handoff`, measured on `evidence/escalation/run.jsonl`), retries
from the top if the precondition holds, or escalates again as `unrecoverable_condition`. Every
intervention carries `expires_at`; past it with no resolution, the run terminates as
`HardFailure(escalation_unresolved)` rather than hanging.

## Safety

The allowlist (`safety/policy.py`) checks every loaded frame URL (`Surface.urls()`), not one
value, against permitted origins, routes, and action types from policy YAML.

Risk classification runs two independent layers, label then route, first match wins
(`safety/risk.py`); a `RISKY_IRREVERSIBLE` action is blocked, never confirmed inline, since that
would ask the actor proposing the action to grade its own work (`docs/adr/0007`). Redaction is
one path, `Redactor.dumps` (`safety/redact.py`), walking the live pydantic model tree so a field's
`STRUCTURAL`/`VALUE_CARRYING` marking decides whether the credential-shaped-literal rule applies;
a screenshot is masked before the PNG bytes are written, never after.

Label-based classification will miss an unlabeled irreversible control, which is why route
scoping is a second layer. Only synthetic data reaches the model.

Admission: `PolicyGate` read `page.url`, always the frameset shell (`/app`), never the
content frame a click acts against; `mutating_routes` never fired for a real click, inert from
Phase 5 until the Phase 7 live discovery run found it, completing `goal_verified` in 12 rounds
when it should have been refused. The rule's own test passed throughout, calling `classify()`
with a hand-built URL and never going through a real `Surface`. The fix reads every loaded frame
URL. Measured proof, `evidence/discovery-subaccount/run.jsonl` seq 22: `'Submit'` matches no entry
in `risky_labels`, and the refusal instead names a loaded URL (`/member/12345/subaccount/new`)
matching `mutating_routes`, checking all three: shell, nav frame, content frame.

## Cuts

Cut:

- streamed co-browsing viewport (the real window is open)
- tenant overlay store (overlays are plain files)
- assisted LLM recovery on replay failure (breaks invariant 1)
- approval gating on `stability` (a measured, read-only signal, 5 runs, 5 successes)
- drift dashboard (`understudy drift`'s text report covers 14 runs)
- code generation from artifact (cannot carry executable logic, `docs/adr/0014`)

Next: `docs/adr/0013`'s single-recording generalization gap: a recording can only parameterize
what one run's literal-matching rule recognized.

Four deliberate shortcuts:

- `safety/risk.py:132`: `Key` classifies `SAFE_REVERSIBLE` unconditionally; no code path emits
  one today.
- `escalation/store.py:158`: no lease on the lockfile, so a crashed holder's lock blocks that id
  (bounded by a `TimeoutError`); the upgrade is a lease.
- `replay/recovery.py:219`: the retry rule reloads the whole page, discarding unsubmitted form
  state; the upgrade is a frame-scoped reload, needing `Surface` to expose it.
- `surface/locator.py:35`: `RelationalHint` has one kind, `row_label`, since only one was
  measured on this target.

Dead code the repo audit found, deleted this phase: `AmbiguousTarget`/`TargetNotFound`
(`surface/locator.py`, never raised);
`Redactor.redact_model` (`safety/redact.py`, no callers); `Policy.max_action_retries`
(`safety/policy.py`, unread; retry limits come from `RecoveryRule.max_attempts`); a stray 60KB
fixture log at the repo root.

Tests are weakest here: most defects were found by replaying something real or by code review,
not the suite: the ordinal pool mismatch, the inert route check, the pruning bug, an unvaried
parameter, silent ordinal drift, an unwired human-action capture, a drain that crashed the
handoff it was meant to record. Two (the inert route check, the unwired capture) had a passing
test over the broken behavior: each exercised the mechanism in isolation, not the real call path.
Fix: sever the one line wiring a mechanism to its caller and confirm a test goes red. Newest
example: `understudy drift` omitted the only two runs carrying drift, since curation nested them
a level deeper than `iterdir()` looked.

`.github/workflows/ci.yml` runs `pytest` on Linux with no browser and no fixture server, so every
live test skips there; a green badge proves the offline half. Every live artifact under
`evidence/` was produced locally, against the fixture, with a headed browser.
