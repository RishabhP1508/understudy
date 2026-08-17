# 0013. Replay interpolates caller parameters, and what a single recording still cannot generalize

Status: accepted (Phase 8 hotfix)

## Context

Phase 8 (docs/adr/0012) introduced `ParamRef` into `Step.value` and canonicalized route
placeholders into `Checkpoint.value`, but shipped no executor change to go with them. A live
replay of the resulting artifact was run and inspected: `replay --params
'{"password":"admin","member_id":"12345"}'` typed the literal strings `${param:password}` and
(via a stale in-memory rendering) a placeholder for `member_id` into the live form, and the
canonicalized checkpoint `.../members?f7=:member_id` was compared against a real URL that could
never contain a literal colon-prefixed placeholder. Both caller-supplied parameters were silently
discarded. R3 is "given an artifact AND input parameters, replay with no LLM in the decision
loop"; an executor that declares parameters and then ignores them is not honouring that contract,
and shipping the schema feature without its executor is not something the phase gets to defer,
unlike genuinely-new Phase 9 concerns (recovery, outcome detectors).

## Decision: one shared parameter-resolution helper, used both directions

`replay/engine.py` gains `_resolve_param(name, params) -> str`, the single place a declared input
parameter's caller-supplied value becomes a literal string. Two call sites use it:

- `_resolve_step_value(value, params)`: a `Step.value` that is a `ParamRef` resolves to
  `params[ref.name]`; a plain literal passes through unchanged. Used to build the actual `Action`
  dispatched for `navigate`/`type`/`select` steps, replacing the placeholder-rendering stopgap
  Phase 8 shipped.
- `_resolve_checkpoint(checkpoint, capability, params)`: every `:name` route placeholder
  `record/canonicalize.py`'s `canonicalize_route` embedded in a checkpoint's own value is replaced
  via the same `_resolve_param`, for every declared `InputParam` present in `params`. Called before
  both a step's own postcondition check and the capability's final success checkpoint.

Because both paths bottom out in the same function, a step's value and a checkpoint's placeholder
can never disagree about what `:member_id` means -- there is exactly one definition, not two that
happened to be written to agree today.

**Validated once, up front, never a raw crash.** `_missing_required_params(capability, params)`
runs before ANY browser is launched (not merely before "step 0" -- before the entry-point navigate,
before `WebSurface(...)` is even constructed). A required `InputParam` absent from `params` returns
a `HardFailure(category=INVALID_PARAMS)` naming which parameters were expected and which were
given, with no browser cost paid on a request that cannot possibly succeed.
`FailureCategory` grows an eleventh value for this (`INVALID_PARAMS`); the ten-value count in its
own docstring was never a fixed invariant, only a fact true at the time it was written. An
OPTIONAL parameter a step still references but the caller omitted is not pre-validated (nothing
this project records ever declares one), and `_resolve_param` raises a debuggable `KeyError` with
a named-parameter message rather than a bare one if that path is ever exercised live; the existing
per-step exception handler turns it into an `ACTION_FAILED` `HardFailure`, never a crash.

**A second, separate leak path closed at the same time.** The `replay_start` event logs the
caller's raw `params` dict verbatim. Once `params` can carry a real secret (a real password, not a
recording's own already-redacted placeholder), that dict is a new place the same leak Phase 5
already closed for a live Type action could reopen. Every declared secret/PII param's
caller-supplied value is registered with the run's `Redactor` (`register_secret`) BEFORE the
`replay_start` event is ever logged -- the identical mechanism `PolicyGate._log` already uses for
a live Type action's resolved text, just triggered one step earlier so it also covers this event.
Measured live: replaying with a distinct password value, the string appears zero times in
`run.jsonl`; the dispatched Type action's own logged text is `${param:password}`, and its
rationale (promoted verbatim from the recording) reads `Type '[REDACTED]' into Password field.` --
both exactly as they would for a live discovery run typing a real secret.

## Measured: replaying with a different member still fails, and not at the checkpoint

Replaying the same capability with `member_id=22222` (a real member in the fixture, balance
$532.10) was expected to reach the balance screen and fail the SUCCESS checkpoint, since that
checkpoint is `text_present` pinned to `$1,204.55`, the literal value the one recording happened
to observe. That is not what happened. It failed earlier, at step 5 (clicking the search result
link), with `LOCATOR_UNRESOLVED`:

    expected: a unique element matching role='link' name='12345 - Testuser Alpha'
    observed: role_ordinal: 1 candidate(s) (descriptor recorded name='12345 - Testuser Alpha',
              which matched no element; the sole remaining role='link' candidate is not used to
              rescue a descriptor that once had a meaningful name)

The captured `a11y/005.json` shows exactly why: member 22222's own search-results page renders a
link named `22222 - Sample Bravo`, not `12345 - Testuser Alpha`. The recorded `TargetDescriptor`
for that step carries `name="12345 - Testuser Alpha"` verbatim, and Phase 8's parameterization
never touches it -- `record/canonicalize.py` only rewrites a STEP'S OWN typed/selected VALUE and a
CHECKPOINT'S URL, both matched against a literal run of digits in the goal text. A `describe()`-
derived accessible NAME that happens to also embed that same digit run is a third place the
literal leaks into the artifact, and this recorder does not look there.

**This is a real, additional limitation, not a variant of the one already documented, and it is
more fundamental:** even if the success checkpoint were fixed to generalize (below), THIS
capability would still be single-member-only, because step 5 can never resolve for any member
other than 12345. Locator (docs/adr/0006) resolution deliberately refuses to rescue a descriptor
that once had a meaningful, now-unmatched name by falling back to the sole same-role candidate --
correctly, since that rescue is exactly how a wrong-element click happens (docs/adr/0006 measured
this directly: "Confirm Transfer" must never resolve to "Log out" just because it is the only
button left). The correct fix is not to weaken that rule; it is to recognize that this specific
link's name is ITSELF parameter-shaped and canonicalize it the same way a checkpoint's URL is.

**What a better success condition would have to look like.** The current checkpoint answers "was
the string `$1,204.55` present," which only a single specific member's balance ever satisfies. The
goal here is really "was A balance value displayed" -- the SHAPE of an answer, not one instance of
it. Two changes would be needed together, and neither exists in this schema today:
1. A checkpoint kind (or a mode of `value_equals`) that asserts a PATTERN, not a literal --
   e.g. `\$[\d,]+\.\d{2}` for a currency value, so the checkpoint holds regardless of which member
   was looked up.
2. Recognizing that for a capability whose goal is "read a value" (as opposed to "reach a state"),
   the more honest success condition is "the declared OUTPUT was extracted at all" -- the checkpoint
   and the output are answering the same question twice, and the checkpoint is the one that got it
   wrong, since the output's OWN presence is already the real signal. Neither the goal text nor any
   step's typed value ever mentions the balance, so canonicalization's literal-matching mechanism
   (matching a goal-text digit run) has structurally no way to discover this on its own -- it would
   need a difference kind of signal, e.g. "this value came from an `extract` step, therefore it is
   an output to check for PRESENCE, not a literal to pin."

Both of the above, and the target-descriptor-name gap, are consequences of the same root limit:
**a capability recorded from ONE run can only parameterize what that one run's literal-matching
rule happened to recognize.** Fixing all three needs either more than one recording per capability
(to see which literals vary and which are structural) or a genuinely different mechanism for
outputs and derived accessible names, not a bigger regex. Neither is attempted here; Phase 8's own
scope is the schema and the (now-repaired) executor contract, not a generalization strategy this
project has not designed yet.

## Code-review round: four more correctness holes, found by running the reviewer, not by a test

A code-review pass against the params-interpolation fix above found four genuine correctness
holes the test suite did not catch, because no existing test's synthetic data had the right
shape to expose them. All four are fixed here.

**FIX 1 -- `record/recorder.py`'s pruning deleted the action that actually escapes a detour.**
The reviewer executed `_prune_dead_ends` against a synthetic `[A, A, B, A, C]` state sequence (5
events) and it returned only the first two `A` events plus `C` -- dropping the THIRD event (the
one whose own action is what finally leaves the `A`/`B` loop toward `C`). The root cause was the
"group consecutive same-signature events into a run, then prune across runs" design from Phase
8's own first pass: grouping is correct for ordinary sequential progress (two actions dispatched
from an unchanging page, like a login form's username then password), but it is the wrong
granularity for a genuine detour, because it cannot separately drop the ONE action (of a
multi-action leading run) whose own effect is what starts the excursion away from a state, while
keeping the rest of that same run. The fix drops the grouping pass entirely and tracks, per
event, the MOST RECENT prior position of each state signature directly: a NON-adjacent repeat (at
least one different state ran in between) erases every event from that prior position up to (but
not including) the current one -- including the prior position itself, since it is precisely the
action whose effect started the detour; an ADJACENT repeat (nothing ran in between) erases
nothing, preserving ordinary sequential progress exactly as before. This did not change the
shipped artifact: verified by rebuilding it in memory from the same evidence log with the fixed
recorder and diffing against `artifacts/*.v2.json` field by field -- identical except
`outputs[0].description`, which differs only because that comparison used `llm=None` (D5's
deterministic fallback) against a shipped file the live structured call had actually named; every
step, postcondition, input, and the success checkpoint match exactly. Both the original failing
case and the coordinator's own `[A, A, B, A, C]` construction are now direct tests.

**FIX 2 -- a pii-sensitivity Type was recorded as the hardcoded literal string `"[REDACTED]"`.**
`safety/policy.py`'s `PolicyGate._log` redacted a pii Type action's own text to a bare mask,
never a parameter reference, so `record/recorder.py` had nothing to bind a declared, replayable
`InputParam(sensitivity="pii")` to -- the exact defect class `ParamRef` already fixed for
secrets, just for the other sensitivity. The fix logs `${pii:<slug>}` instead of `[REDACTED]`, a
DIFFERENT prefix than the secret form's `${param:<slug>}` (not the same one), specifically so the
recorder can recover which sensitivity produced a given placeholder from the placeholder's own
text, rather than re-guessing sensitivity from the field's accessible name via
`classify_field_sensitivity` -- a guess that can genuinely disagree with what perception already
determined live (a field policy.sensitive_fields.pii matches via a multi-word pattern like "date
of birth" would not be recognized by `classify_field_sensitivity`'s narrower vocabulary, which
looks for substrings like "dob"). Not reachable on the shipped fixture flow (no pii field in the
member-lookup path), so this did not change the shipped artifact either.

**FIX 3 -- `InputParam.example` could hold a raw pii literal.** A goal-literal-matched parameter
the recorder itself classifies as `pii` (via `classify_field_sensitivity`) still had the observed
value stored in `example`. `example` is VALUE_CARRYING (D4), so R3 applied, but R3 only catches
credential-SHAPED strings and R2 only catches its own named patterns -- neither is a general "is
this pii" test, so a short, unpatterned pii value survived into the artifact's `example` field. A
field the recorder has just labelled sensitive now never gets the observed literal as its
example at all (omitted, not replaced with a shape hint -- the simpler of the two options the fix
allowed, and there is no consumer yet that would need more than "no value" here).

**FIX 4 -- checkpoint placeholder interpolation was prefix-unsafe.** `_resolve_checkpoint` did an
ordered sequence of bare `str.replace(f":{name}", value)` calls, one per declared param, in
whatever order `capability.inputs` happened to list them. With params `id` and `id_long`,
replacing `:id` first also corrupts `:id_long`'s own leading `:id` before `id_long`'s own turn
ever comes. Fixed with one `re.sub(r":(\w+)", ...)` pass instead of N sequential replacements:
`\w+` always matches the full identifier greedily, so `:id_long` resolves as one name in one
step, never as `:id` plus a leftover `_long`, regardless of declaration order. Directly tested
with both colliding names present.

## Accepted as documented limitations (measured, not fixed this round)

Five real limitations, none papered over:

1. **The extract postcondition and `success` freeze the one observed value** (`$1,204.55`), so a
   correct replay reading a different member's balance is reported as a failure. See "What a
   better success condition would have to look like" above.
2. **`TargetDescriptor.name` and `frame_path` keep the recording run's own literals** (e.g.
   `"12345 - Testuser Alpha"`), which is why member 22222 fails at step 5 before the checkpoint
   is ever reached. Locator-side canonicalization is a distinct piece of design work from
   route/value canonicalization. See "Measured" above.
3. **`checked_urls` does not mean the same thing for a `navigate` action as for every other
   action.** Verified directly against `safety/policy.py`'s `PolicyGate.dispatch`: for a
   `Navigate`, `checked_urls` is `[action.url]` -- the DESTINATION, recorded before that navigate
   has actually run -- and for every other action kind it is the URLs currently loaded.
   `record/recorder.py`'s `_derive_postcondition` assumes `next_urls` (the next event's own
   `checked_urls`) means "the state THIS step's action produced"; if the NEXT event is itself a
   `navigate`, that assumption is false -- `next_urls` would be the navigate's own upcoming
   destination, not yet loaded at the point this step's postcondition is actually checked. No
   capability recorded so far has an interior `navigate` step (the only one, the harness's own
   bootstrap, is excluded before postcondition derivation ever runs), so this is verified-correct
   but latent. A prominent comment at the derivation site (`record/recorder.py`,
   `_derive_postcondition`) now states this plainly so the next person extending it does not walk
   into it unknowingly; no functional guard is added for a path nothing exercises yet.
4. **`canonicalize_route` is never applied to a `navigate` step's own URL value.**
   `_parameterize` only turns a step's `value` into a `ParamRef` when it is a bare literal
   matching a goal-literal or a pre-redacted secret/pii placeholder; a `Navigate` step's value is
   a full URL, which never equals a bare goal literal on its own, so it is never parameterized or
   canonicalized by anything in this recorder. Latent for the same reason as (3): no recorded
   capability has an interior navigate step yet.
5. **Two different literals typed into equally-named fields collapse into one `InputParam`.**
   `_parameterize` keys a parameter's identity on its derived FIELD NAME
   (`record/canonicalize.py`'s `infer_param_name`); if two different steps type different values
   into two fields that happen to share a name (e.g. the same "Amount" field on two different
   pages of one flow), the second occurrence's value is silently bound to the same parameter as
   the first, and one caller-supplied value would be replayed into both. A field-name-keyed
   identity is what makes an artifact recorded from steps 3 and 4 above both callable as
   `member_id`, and telling apart "the same conceptual field, reused" from "two unrelated fields
   with the same caption" needs more context than a single recording's own field names give it.

## Alternatives considered

- **Substitute an empty string for a missing parameter and let the run fail naturally later.**
  Rejected outright, and explicitly forbidden by the fix request: a blank password typed into a
  live form is a wrong action taken with a caller's real credential field, not a safe default.
- **Validate params lazily, per step, as each `ParamRef` is encountered.** Rejected: a capability
  with a missing required parameter would still launch a real browser and dispatch several
  actions (typing a resolved but not-actually-supplied blank, or crashing mid-form) before the
  problem surfaces, which is both slower and a worse failure mode than refusing the request before
  any of that happens.
- **Rescue the step-5 locator failure by falling back to the sole remaining link.** Rejected: this
  is precisely the confident-wrong-action rescue docs/adr/0006 already refuses to perform, and
  weakening it to make one capability generalize would reopen exactly the failure mode that rule
  exists to prevent.
- **Encode pii vs. secret sensitivity by re-deriving it from the field name at record time,
  rather than a distinct placeholder prefix.** Rejected: `classify_field_sensitivity` is a
  narrower, string-pattern-based heuristic than `policy.sensitive_fields`'s own configured
  patterns (see FIX 2), so the two can genuinely disagree; encoding the answer perception already
  computed live, in the placeholder itself, cannot disagree with itself.
- **Keep the sequential `str.replace` chain in `_resolve_checkpoint` and just sort param names
  longest-first (FIX 4's own suggested minimum).** A single regex pass was not more code than
  sorting a list first and looping, and it removes the prefix hazard structurally (by construction
  of `\w+`'s greedy match) rather than by an ordering invariant a future edit could quietly break.
