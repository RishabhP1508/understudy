# 0012. Schema richness, the recorder as a separate pass, field marking, and two version fields

Status: accepted (Phase 8)

## Context

Phase 8 is the schema phase: the brief calls the artifact the focal point of the evaluation, and
several gaps had accumulated since Phase 2 that a richer schema needed to close at once rather than
patch individually. `Capability` had 7 steps with `postcondition` null on every one, `inputs: []`
even though step 1's own value was already a parameter reference, confidences serializing as
`0.49999999999999994`, and a redaction rule (R3, docs/adr/0008) that destroyed a checkpoint value
`DONE_TOKEN` and a URL path `/secret-flow` purely because both happen to contain a credential
token as a substring. This ADR covers the decisions that closed those gaps: the recorder as a
separate pass, postcondition derivation and its real limitation, the `url_matches` frame decision,
field marking and its residual limit, and the two version fields the schema now carries.

## Decision: the recorder stays a separate pass over `run.jsonl`, not in-loop recording

`record/recorder.py` reads the written evidence log rather than any live object. This was already
true before Phase 8 and stays true: recording never depends on the discovery process still being
in memory, and the recorder never runs during the loop itself. Phase 8 extends the pass with
pruning, canonicalization, and parameterization, but the shape -- read `run.jsonl`, produce a
`Capability` -- is unchanged.

**The real limitation this exposed.** Work item 3 describes building each step's `TargetDescriptor`
via `locator.describe()` against that step's observation. In practice, the descriptor is already
computed live, at discovery time, by `agent/loop.py` (which calls `describe()` and serializes the
result into the event's own `context.target`), and `run.jsonl` carries no per-turn `Observation`
snapshot for the recorder to independently re-derive it from (an `a11y/` snapshot is written on
FAILURE only). The recorder therefore parses the already-serialized descriptor rather than
recomputing it, and "the resolution rank achieved at record time" cannot be verified from this log
format at all -- it is implicitly rank 1 by construction, never independently confirmed. This is
the same evidence gap the postcondition derivation below has to work around, and D2 (below) is the
first half of closing it for future recordings, not this one.

## Decision: postcondition derivation, in precedence order, and why a true diff was not possible

A true "diff the observation before and after this step" derivation needs a per-turn `Observation`
snapshot, which `run.jsonl` does not carry (only `policy_decision.checked_urls`, the URL state
across every frame, is present on every event). Postconditions are derived from that instead, in
this precedence:

1. The LAST step has no next event to derive a produced-state from at all; its postcondition is
   the run's own success checkpoint (`goal_verified`).
2. Otherwise, if the URL state the NEXT event observed differs from this event's own, the
   postcondition is `url_matches` against the deepest (most nested child-frame) URL of the new
   state.
3. Otherwise, for a read/extract step, `text_present` with the value it actually extracted.
4. Otherwise (the URL state did not change -- the three steps on the login page), `element_present`
   against the NEXT step's own recorded target role+name: the page reached a state where the next
   action's target actually exists, which is exactly what replay needs true before proceeding.

Every one of the real recording's 7 steps gets a non-null postcondition this way, verified against
the measured URL progression in `evidence/discovery-b2405e162ba4/run.jsonl`.

**A pruning correctness fix found along the way.** Pruning ("drop a subsequence that returns to a
state already visited without progressing") first used the state signature per RAW event. That
broke on the real log: typing a username and then a password both dispatch while the URL state is
still the login page, so they share an identical signature, and a naive "seen this signature before
-> drop everything since" rule discarded the password step, the login click, and the search click
-- 3 of 7 real steps -- as if they were a detour, when they are ordinary sequential form-filling.
The fix groups CONSECUTIVE same-signature events into one run first, and prunes across runs, not
raw events: several actions taken in a row while the signature does not change are forward
progress, never a detour, and only a run whose signature repeats a NON-adjacent earlier run's is
dropped.

## Decision: `Observation.urls`, and `url_matches` checks it, not `url` alone

`Surface.urls()` already existed (Phase 5, for the policy gate's own allowlist and risk checks).
Phase 8 adds `urls: list[str]` to `Observation` itself, populated by `WebSurface.observe()`, and
`checkpoint_satisfied`'s `url_matches` kind checks membership in that list, not `observation.url`
alone. On this app's frameset the top-level URL is `/app` on every screen (measured, Phase 3 and
Phase 7); a page-level `url_matches` would therefore pass on the wrong screen every time. The
accepted tradeoff: matching ANY loaded frame is looser than matching only the one active frame, and
that is accepted because the shell and nav frame URLs are constant across every screen on this
fixture and can never themselves cause a false match -- only the content frame's URL actually
varies, so looseness here costs nothing observed.

## Decision: field marking (D4), and its residual limit

Every schema field is marked `STRUCTURAL` or `VALUE_CARRYING` via `json_schema_extra`
(`models/observation.py`'s `FIELD_MARKING_KEY`/`STRUCTURAL_EXTRA`/`VALUE_CARRYING_EXTRA`, imported
by every module that declares a marked field so there is one vocabulary, not several). `Redactor`
walks the actual pydantic model tree (not a pre-dumped plain dict, which would already have lost
this information) to read each field's own marking, and applies R3 (the whole-string
credential-shaped-literal rule) only to a `VALUE_CARRYING` field. R1 (registered secret values) and
R2 (named PII patterns) still apply to every field regardless of marking, because a real secret or
PII value landing in a structural field must still be caught.

This fixes both measured regressions: `Checkpoint.value` and `TargetApp.entry_point` are marked
`STRUCTURAL`, so `DONE_TOKEN` (which contains "token") and `/secret-flow` (which contains "secret")
both now survive serialization unredacted, because they never reach R3 at all.

**Why the walk reads values from `model_dump(mode="json")`, not from a live attribute directly.**
`TargetDescriptor.confidence` has its own `field_serializer` (D6, below) that rounds only at
serialization time. Reading `getattr(obj, "confidence")` directly would bypass that serializer
entirely and re-introduce the float noise the rounding exists to remove. The redactor therefore
reads structure (which field is a nested `BaseModel`, worth recursing into for its OWN markings)
from the live attribute, but reads VALUES from `obj.model_dump(mode="json")`, so no pydantic-level
serialization is ever silently skipped.

**The residual limit, stated plainly.** Marking is only as good as the schema author's
classification: a field nobody marked defaults to `VALUE_CARRYING` (today's behaviour, unchanged),
and a value living inside an untyped `dict[str, Any]` field -- `RunEvent`'s own
`context`/`proposed_action`/`policy_decision`/`checkpoint_eval` fields, deliberately loose so one
event schema covers every event type -- has no schema left to consult at all once the walk reaches
it, and falls back to the same rule a plain, model-less dict always got. This means `run.jsonl`'s
own redaction behaviour is UNCHANGED by this phase; only `Capability`/`Observation` serialization
(`artifacts/*.json`, `a11y/*.json`) gained field-level precision, which is exactly where the two
measured regressions actually happened.

## Decision: two version fields, and what each one means

`schema_version` is the evolution of the SCHEMA's shape (this file, `models/artifact.py`); it
changes when a field is added, renamed, or reinterpreted, independent of any one recording.
`version` is the revision of THIS PARTICULAR recorded capability; it changes every time `discover`
records this same goal again (docs/adr/0011), independent of whether the schema itself changed at
all. A reviewer comparing two files under `artifacts/` needs both answers separately: "were these
recorded under the same understanding of what a `Capability` even is" versus "which recording of
this goal, in order, is this." `provenance.perception_version` is a third, distinct kind of drift
(models/artifact.py's own docstring covers it against the measured Phase 3 failure); `code_sha` is
explicitly not a substitute for it, because `code_sha` moves on every commit and would flag drift
on commits that never touched perception at all.

`Provenance.timestamp` replaces `created_at` (a plain rename, both mean "when this run happened"),
and accepts the legacy key via `AliasChoices` so the one artifact recorded before this rename still
loads with its real historical value intact rather than silently defaulting away. `code_sha` is
best-effort and never computed by invoking git (this project never runs git or gh from tooling, by
policy): it reads a CI-provided environment variable (`GITHUB_SHA`) when present, and stays `None`
locally, which is an honest gap, not a placeholder.

## Decision: D5's structured metadata call degrades gracefully, and this project's regeneration used the real one

One structured model call proposes a name, description, and per-output descriptions
(`record/recorder.py`'s `_propose_metadata`). It is optional: `llm=None`, a raised exception, or a
response naming an output no step extracts (rejected and retried once, then given up on) all fall
back to a deterministic name/description equal to the goal string -- the same thing `discover` has
always recorded, so an offline recording is never blocked on this call. Regenerating this project's
own artifact for this phase (`artifacts/*.v2.json`) had a real `GEMINI_API_KEY` available, so that
one real call fired live and its answer is what the shipped v2 artifact carries
(`name: "get_member_savings_balance"`); the tests in `tests/test_phase8.py` exercise the fallback
path instead (`llm=None`), since the suite must run with no network and no key.

## Alternatives considered

- **A single, path-keyed field-marking registry, walked against the dumped dict directly.**
  Considered and rejected in favour of walking the live model tree: a path-keyed registry cannot
  tell a `Checkpoint.value` living inside `Capability.steps[3].postcondition` from an unrelated
  dict that happens to have a `"value"` key with no model behind it at all, which is precisely the
  distinction D4's residual limit needs to draw.
- **Fix the pruning regression by special-casing "consecutive events of the same tool".** Rejected:
  the real failure is about STATE (the URL did not change), not about which tool fired: a click and
  a type dispatched back to back on an unchanged page are exactly as much "not a detour" as two
  types are, and a tool-keyed special case would not generalize to that.
- **Compute `code_sha` by shelling out to `git rev-parse HEAD`.** Rejected outright: this project
  never runs git or the `gh` CLI from tooling, by explicit policy, and a provenance field is not an
  exception worth carving one out for.
