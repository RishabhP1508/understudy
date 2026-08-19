"""Capability: the recorded, agent-invocable artifact. Ordered steps, typed inputs/outputs, a
success checkpoint, known outcomes, recovery rules, and provenance -- decoupled from the raw
model transcript by construction (no field here is named messages, transcript, completion,
choices, or content; provenance.transcript_hash is a hex digest of it instead).
tests/test_constraints.py (invariant 4) enforces both properties.

TWO VERSION FIELDS, and they mean different things. `schema_version` is the evolution of THIS
SCHEMA's shape -- it changes when a field is added, renamed, or reinterpreted here, in
models/artifact.py, independent of any one recording. `version` is the revision of THIS
PARTICULAR recorded capability -- it changes every time `discover` records this same goal again
(artifacts are append-only, docs/adr/0011), independent of whether the schema itself changed at
all. A reviewer comparing two files under artifacts/ needs both answers separately: "were these
recorded under the same understanding of what a Capability even is" (schema_version) and "which
recording of this goal, in order, is this" (version).

`provenance.perception_version` is a THIRD, distinct kind of drift, and it exists because of a
failure that was actually measured, not a hypothetical one: Phase 3 added structural name
derivation for unlabeled controls, and the Phase 2 artifact immediately stopped replaying, because
it had recorded the login field as `role="textbox" name=""` and that same field now perceives as
`name="Username"`. The application never changed; only OUR READING of it did, and that alone
invalidated every capability recorded before the change. `code_sha` cannot stand in for this: it
moves on every commit, so it would flag drift constantly, on commits that never touched
perception at all. `PERCEPTION_VERSION` (models/observation.py) is instead an integer bumped BY
HAND only when perception semantics themselves change, and replay/engine.py compares it only to
CLASSIFY a locator failure that already happened (stale_perception vs. locator_unresolved,
docs/adr/0009), never as a pre-flight gate.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, Field, model_validator

from understudy.models.observation import STRUCTURAL_EXTRA, VALUE_CARRYING_EXTRA, Observation
from understudy.surface.locator import TargetDescriptor


class ParamRef(BaseModel):
    """A step's value, or part of a checkpoint's, deferred to a named `InputParam` the caller
    supplies per invocation, rather than the literal this recording happened to use. `name`
    references `Capability.inputs[].name`; every other detail (type, example, sensitivity) lives
    there once, not repeated at every point this same parameter is referenced.
    """

    kind: Literal["param_ref"] = Field(default="param_ref", json_schema_extra=STRUCTURAL_EXTRA)
    name: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


class InputParam(BaseModel):
    """A typed input the calling agent supplies per invocation (R2), not a bare positional value.
    `sensitivity` mirrors `UIElement.sensitivity`'s vocabulary so a catalog consumer (Phase 11)
    can treat a secret-sensitivity input the way a live UI would: never logged, never echoed back.
    """

    name: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    # A JSON-Schema-ish type name ("string", "integer", "number", "boolean"); see json_schema()
    # below, which passes this straight through as a property's "type".
    type: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    required: bool = True
    description: str = ""
    # The literal value this recording actually observed, kept for a reviewer's benefit -- never
    # populated for a secret-sensitivity param, whose real value was never even in the log to
    # begin with (record/recorder.py never sees it, only the placeholder PolicyGate already
    # substituted at discovery time). Typed to match `type` above rather than fixed to `str`: an
    # `example` of `"12345"` on a `type: "integer"` param disagrees with its own declared type,
    # which is exactly the contract Phase 11's json_schema() export hands to a calling agent.
    example: str | int | float | bool | None = Field(
        default=None, json_schema_extra=VALUE_CARRYING_EXTRA
    )
    sensitivity: Literal["none", "secret", "pii"] = Field(
        default="none", json_schema_extra=STRUCTURAL_EXTRA
    )


class OutputField(BaseModel):
    """A typed output this capability returns to its caller (R2). `source_step_id` is the
    extraction spec: which recorded Step actually produces this value, so a reviewer (or a future
    replay-time consumer) does not have to guess from name-matching alone which step to trust.
    """

    name: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    type: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    description: str = ""
    source_step_id: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)


class Checkpoint(BaseModel):
    """A verifiable condition against an Observation -- a step's postcondition, or the
    capability's own success checkpoint. Four kinds, each with its own reading of `target`/`value`
    (both plain strings, kept generic across kinds rather than four separate schemas):

    - `text_present`: `target` is descriptive only ("page"); `value` is a substring searched
      across every element's name and value (unchanged since Phase 2).
    - `url_matches`: `target` is descriptive only ("any_frame", D3); `value` is a URL that must
      equal one of `Observation.urls` (the shell or ANY child frame -- looser than matching the
      one active frame, accepted because the shell and nav URLs are constant across screens on
      this fixture and cannot themselves cause a false match).
    - `element_present`: `target` is the required role; `value` is the required accessible name
      (exact match). Asserts an element exists; does not inspect its value.
    - `value_equals`: `target` is `"role:name"` identifying one element; `value` is the exact
      string its own `.value` must equal -- a stronger check than `text_present`'s substring-
      anywhere search, for asserting a specific field holds a specific value.
    """

    kind: Literal["url_matches", "element_present", "text_present", "value_equals"] = Field(
        json_schema_extra=STRUCTURAL_EXTRA
    )
    target: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    value: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


def checkpoint_satisfied(observation: Observation, checkpoint: Checkpoint) -> bool:
    """Pure check: does this observation satisfy this checkpoint? No Surface, no I/O.

    The one place "done" is defined. Both agent/loop.py's verify_checkpoint (discovery) and
    replay/engine.py (replay) call this instead of each keeping their own copy, so the two paths
    can never quietly diverge on what a checkpoint means.
    """
    if checkpoint.kind == "text_present":
        value = checkpoint.value
        if not value:
            return False
        return any(
            value in element.name or (element.value is not None and value in element.value)
            for element in observation.elements
        )
    if checkpoint.kind == "url_matches":
        # D3: ANY currently loaded frame, not `observation.url` alone -- on this app's frameset
        # the top-level URL is constant across every screen (docs/adr/0005), so a page-level
        # check would silently pass on the wrong screen.
        return bool(checkpoint.value) and checkpoint.value in observation.urls
    if checkpoint.kind == "element_present":
        role, name = checkpoint.target, checkpoint.value
        if not role:
            return False
        return any(e.role == role and e.name == name for e in observation.elements)
    if checkpoint.kind == "value_equals":
        role, _, name = checkpoint.target.partition(":")
        if not role:
            return False
        return any(
            e.role == role and e.name == name and e.value == checkpoint.value
            for e in observation.elements
        )
    return False


def login_prefix_len(steps: list[Step], entry_point: str) -> int:
    """How many of `steps`, from the front, are "the login prefix" -- the steps that ran before
    the flow first left the entry screen. Both the recorder (record/recorder.py, deciding whether
    a `reauth` recovery rule is even executable: reauth means "re-navigate to the entry point and
    re-run these steps", which only makes sense if there ARE any) and the engine
    (replay/recovery.py's `session_lost_mid_flow` trigger, and replay/engine.py's own reauth
    callback, which re-executes exactly this many recorded steps) need the SAME answer to "how
    many steps is the login". Two independently-maintained copies of this rule already drifted
    once in this project's history (see docs/adr/0006 and ARCHITECTURE.md decision 37 for the
    general pattern), so it lives in exactly one place, next to `checkpoint_satisfied` -- the
    established precedent (ARCHITECTURE.md decision 27) for a definition both paths must share.

    The rule: the count of leading steps up to AND INCLUDING the first step whose postcondition is
    a `url_matches` whose URL PATH differs from the entry point's own path -- i.e. the step whose
    own action is what first took the flow off the entry screen. 0 when no step ever does that
    (there is nothing to re-run, so `reauth` is not a capability this flow can offer at all).
    """
    entry_path = urlsplit(entry_point).path
    for index, step in enumerate(steps):
        postcondition = step.postcondition
        if postcondition is not None and postcondition.kind == "url_matches":
            if urlsplit(postcondition.value).path != entry_path:
                return index + 1
    return 0


class Step(BaseModel):
    """One recorded, replayable action.

    `id` is a stable identifier for this step's POSITION across recordings of the same goal
    (record/recorder.py currently assigns it as `str(index)`); `index` is this step's CURRENT
    ordinal in `Capability.steps`, which pruning or reordering can change even when `id` would
    not. `value` is either the literal this recording used, or a `ParamRef` when
    record/canonicalize.py recognized it as standing in for a named input. `risk_class` is one of
    `safety.risk.RiskClass`'s string values, duplicated here as a plain `str` (never importing
    safety/ from models/, which would invert this project's layering) rather than the enum type
    itself. `on_failure` is a per-step override of which RecoveryRule-style action applies if THIS
    step's own action raises; `None` (the common case) means fall through to
    `Capability.recovery_rules` instead.
    """

    id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    index: int
    action: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    target: TargetDescriptor | None = None
    value: str | int | float | bool | ParamRef | None = Field(
        default=None, json_schema_extra=VALUE_CARRYING_EXTRA
    )
    # The state a step needs BEFORE it runs. Not populated by record/recorder.py this phase (a
    # true "state before" derivation is symmetric with postcondition's "state after" and needs
    # the same per-turn Observation snapshot postcondition derivation itself does not yet have --
    # see Checkpoint's own docstring and docs/adr/0012). The field exists now because it is part
    # of this schema's deliverable shape (R2); populating it is future work, not a placeholder cut.
    precondition: Checkpoint | None = None
    postcondition: Checkpoint | None = None
    # Duplicated from safety.risk.RiskClass.SAFE_REVERSIBLE.value rather than importing the enum
    # (see class docstring); this is the risk PolicyGate actually classified this action as, at
    # record time, off the same act event's own policy_decision.risk.
    risk_class: str = Field(default="SAFE_REVERSIBLE", json_schema_extra=STRUCTURAL_EXTRA)
    rationale: str
    on_failure: Literal["dismiss", "dismiss_dialog", "retry", "reauth", "wait"] | None = Field(
        default=None, json_schema_extra=STRUCTURAL_EXTRA
    )

    @model_validator(mode="before")
    @classmethod
    def _backfill_id_from_index(cls, data: Any) -> Any:
        """The one artifact recorded before `id` existed (artifacts/*.v1.json) has no `id` key at
        all. `str(index)` is exactly what record/recorder.py itself assigns for a fresh recording
        today, so this is not a fallback value invented for this validator -- it is the same rule
        applied retroactively to data that predates the field.
        """
        if isinstance(data, dict) and not data.get("id") and "index" in data:
            data = {**data, "id": str(data["index"])}
        return data


class KnownOutcome(BaseModel):
    """A legitimate business answer this capability's replay can end in (models/result.py's
    BusinessOutcome) -- "no such member", never a failure (ARCHITECTURE.md decision 8). Seeded by
    record/recorder.py from a starter library plus anything this specific run actually observed;
    Phase 9's replay/outcomes.py owns the detector that recognizes one at replay time -- `detector`
    names which one, not code itself.
    """

    code: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    detector: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    terminal: bool = True
    message_template: str = ""


class RecoveryRule(BaseModel):
    """A declarative recovery action (Phase 9's replay/recovery.py): what to attempt when a
    transient condition -- a slow load, an unexpected dialog, an expired session -- is detected
    mid-replay, before falling through to a hard failure.

    `dismiss` and `dismiss_dialog` are deliberately two separate values, not one: dismissing a
    NATIVE browser dialog (window.confirm/alert/prompt) goes through Playwright's
    `page.on("dialog")` handler, a genuinely different mechanism from clicking a DOM control on
    an HTML interstitial (`dismiss`). An executor cannot re-derive which mechanism was meant from
    a single shared value, so the two get their own action rather than collapsing into one.

    `trigger` names a registered condition predicate (replay/recovery.py's registry, Phase 9), not
    free prose -- record/recorder.py populates it with the predicate's name, and replay looks it
    up by that name rather than parsing a sentence.
    """

    id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    trigger: str = ""
    action: Literal["dismiss", "dismiss_dialog", "retry", "reauth", "wait"] = Field(
        json_schema_extra=STRUCTURAL_EXTRA
    )
    max_attempts: int = 1


class TargetApp(BaseModel):
    """Which application, and which tenant of it, this capability was recorded against."""

    # "" (not None) so the old Phase-2-shaped artifact (which never had this field at all)
    # still validates: an unknown app_id is a real, honestly-representable fact about that file,
    # not a crash.
    app_id: str = Field(default="", json_schema_extra=STRUCTURAL_EXTRA)
    entry_point: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    # None until Phase 12 makes multi-tenant real; a single-tenant recording has no tenant to name.
    tenant_id: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    # A vendor-version signal for drift detection across tenants running the same product
    # (R7) -- not computed by this phase's recorder; stays None until something does.
    app_fingerprint: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)


class Provenance(BaseModel):
    run_id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    model: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    # AliasChoices accepts the legacy key name `created_at` too: the one artifact recorded
    # before this rename (artifacts/*.v1.json) carries that key, and this is the same fact under
    # a clearer name, not a different one -- so the real historical timestamp loads correctly
    # rather than silently defaulting away.
    timestamp: str = Field(
        validation_alias=AliasChoices("timestamp", "created_at"),
        json_schema_extra=STRUCTURAL_EXTRA,
    )
    # Best-effort only, and never computed by invoking git (this project never runs git or gh
    # from tooling, by policy -- CLAUDE.md). Populated from CI's own environment when present
    # (e.g. GITHUB_SHA); None locally is an honest gap, not a placeholder.
    code_sha: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    # Defaults to 1, meaning "recorded before this field existed" -- literally true of the one
    # artifact in artifacts/ recorded under Phase 2 perception. Never bumped or backfilled on that
    # artifact: it is real evidence, not a value to edit. See docs/adr/0009 for how replay uses
    # this to CLASSIFY a locator failure (stale_perception vs locator_unresolved), never to gate
    # replay up front.
    perception_version: int = 1
    transcript_hash: str = Field(json_schema_extra=STRUCTURAL_EXTRA)

    model_config = {"populate_by_name": True}


class StabilitySignal(BaseModel):
    """A READ-ONLY OBSERVATION of replay reliability across repeated runs, written by Phase 9's
    own five-run replay check -- never a gate. Do not build approval gating on this; `status`
    (below) is the seam a human review deliberately controls instead."""

    runs: int
    successes: int
    last_n_outcomes: list[str] = Field(default_factory=list, json_schema_extra=STRUCTURAL_EXTRA)
    computed_at: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


_JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class Capability(BaseModel):
    schema_version: int = 2
    version: int = 1
    capability_id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    name: str
    description: str
    target: TargetApp
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step]
    success: Checkpoint
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recovery_rules: list[RecoveryRule] = Field(default_factory=list)
    provenance: Provenance
    # "draft" until a human reviews it; replay only executes a RISKY_IRREVERSIBLE step when the
    # capability is "approved" AND the caller passes allow_risky=True at replay() (safety/policy.py
    # PolicyGate.dispatch). Recorded here, not passed as a bare replay() argument alone, so the
    # approval travels with the artifact a human actually reviewed.
    status: Literal["draft", "approved"] = Field(
        default="draft", json_schema_extra=STRUCTURAL_EXTRA
    )
    # A read-only observation (StabilitySignal's own docstring); None until Phase 9 writes one.
    stability: StabilitySignal | None = None

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema for this capability's INPUT parameters only, not the whole model -- what
        Phase 11's catalog exposes so a calling agent can invoke this capability as a tool, the
        same shape agent/tools.py's own tool schemas already use.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.inputs:
            json_type = param.type if param.type in _JSON_SCHEMA_TYPES else "string"
            prop: dict[str, Any] = {"type": json_type, "description": param.description}
            if param.example is not None:
                prop["examples"] = [param.example]
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema


# --------------------------------------------------------------------------------------
# Phase 12: tenant overlays. A recorded Capability targets ONE tenant's rendering of a vendor
# product; a TenantOverlay is a separate, small, human-authored document that reconciles it
# against a DIFFERENT tenant of the same product -- a vocabulary substitution for renamed labels
# and routes, plus a handful of explicit step overrides and insertions for the places wording
# alone cannot bridge (a workflow step this tenant genuinely inserts). `resolve_for_tenant` is the
# only consumer; its output is a Capability like any other, built at INVOKE time and NEVER
# written to disk as a recorded artifact -- an overlay is reviewed and versioned on its own, not
# baked back into someone else's recording.
# --------------------------------------------------------------------------------------


class OverlayError(ValueError):
    """Raised by resolve_for_tenant() when an overlay disagrees with the capability it targets: a
    step id that does not exist, an attempt to change a step's action type, an after_step_id with
    nothing to insert after, or a capability_id/version mismatch. Always names the offending step
    id or field in its message, the same shape as replay/outcomes.py's UnknownDetector."""


class StepOverride(BaseModel):
    """A partial, per-step replacement for one recorded Step, keyed by that step's own `id` in
    TenantOverlay.step_overrides. Every field defaults to "not set" (Pydantic's own
    `model_fields_set`, not a sentinel value), so only the fields an overlay actually names are
    applied; `resolve_for_tenant` copies them onto the base Step verbatim, with NO further
    vocabulary substitution -- an override's own values are already this tenant's literal truth,
    authored by a human who read the base capability and this tenant's screen side by side.

    `action` exists for VALIDATION only (it must equal the base step's own action, or
    resolve_for_tenant refuses): an override can reshape what a step asserts or targets, never
    what KIND of action it performs -- that would silently turn a `type` step into a `click`.
    """

    action: str | None = None
    target: TargetDescriptor | None = None
    value: str | int | float | bool | ParamRef | None = None
    precondition: Checkpoint | None = None
    postcondition: Checkpoint | None = None
    risk_class: str | None = None
    rationale: str | None = None
    on_failure: Literal["dismiss", "dismiss_dialog", "retry", "reauth", "wait"] | None = None


class ExtraStep(BaseModel):
    """A whole additional Step this tenant's flow needs that the base recording never went
    through (e.g. an extra confirmation screen). `after_step_id` names the BASE capability's own
    step id to insert after; `resolve_for_tenant` renumbers every step's `index` once every
    override and insertion has been applied, so `step.id` here only needs to be unique, never
    sequential with the base capability's own ids."""

    after_step_id: str
    step: Step


class TenantOverlay(BaseModel):
    """A small, reviewable document reconciling one recorded Capability against a DIFFERENT
    tenant of the same vendor product (R7): which vocabulary this tenant renamed, which steps
    needed a genuinely different assertion, and which steps this tenant's own flow inserts.
    `resolve_for_tenant` is the only consumer; the result is a Capability like any other, built at
    invoke time, never itself written to artifacts/.
    """

    tenant_id: str
    base_capability_id: str
    base_version: int
    entry_point_override: str | None = None
    # Applied as ONE substitution pass, longest key first (see _vocabulary_substituter): a
    # renamed label, a renamed route segment, and a renamed query parameter are all the SAME kind
    # of fact -- this tenant calls it something else -- told once in this one table, rather than
    # split across a label field and a separate route field that could disagree about the same
    # rename.
    vocabulary_map: dict[str, str] = Field(default_factory=dict)
    step_overrides: dict[str, StepOverride] = Field(default_factory=dict)
    extra_steps: list[ExtraStep] = Field(default_factory=list)
    notes: str = ""


def _vocabulary_substituter(vocabulary_map: dict[str, str]) -> Any:
    """One compiled alternation, longest key first, so a replacement's OWN output can never be
    re-matched by a shorter key later in the same table: `re.sub` scans the ORIGINAL string once,
    left to right, and never rescans text it has just written -- a single pass by construction,
    not a rule this function has to separately enforce. Sorting keys longest-first also means the
    longest applicable key wins at any one position, never a shorter key that happens to be a
    prefix of it.
    """
    if not vocabulary_map:
        return lambda text: text
    ordered = sorted(vocabulary_map, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(key) for key in ordered))
    return lambda text: pattern.sub(lambda match: vocabulary_map[match.group(0)], text)


def _apply_vocabulary_to_target(target: TargetDescriptor, substitute: Any) -> TargetDescriptor:
    updates: dict[str, Any] = {}
    new_name = substitute(target.name)
    if new_name != target.name:
        updates["name"] = new_name
    new_frame_path = [substitute(segment) for segment in target.frame_path]
    if new_frame_path != target.frame_path:
        updates["frame_path"] = new_frame_path
    if target.relational is not None:
        new_label = substitute(target.relational.label)
        if new_label != target.relational.label:
            updates["relational"] = target.relational.model_copy(update={"label": new_label})
    if not updates:
        return target
    return target.model_copy(update=updates)


def _apply_vocabulary_to_checkpoint(checkpoint: Checkpoint, substitute: Any) -> Checkpoint:
    new_value = substitute(checkpoint.value)
    if new_value == checkpoint.value:
        return checkpoint
    return checkpoint.model_copy(update={"value": new_value})


def _apply_vocabulary_to_step(step: Step, substitute: Any) -> Step:
    updates: dict[str, Any] = {}
    if step.target is not None:
        new_target = _apply_vocabulary_to_target(step.target, substitute)
        if new_target is not step.target:
            updates["target"] = new_target
    if step.precondition is not None:
        new_pre = _apply_vocabulary_to_checkpoint(step.precondition, substitute)
        if new_pre is not step.precondition:
            updates["precondition"] = new_pre
    if step.postcondition is not None:
        new_post = _apply_vocabulary_to_checkpoint(step.postcondition, substitute)
        if new_post is not step.postcondition:
            updates["postcondition"] = new_post
    if not updates:
        return step
    return step.model_copy(update=updates)


def resolve_for_tenant(capability: Capability, overlay: TenantOverlay) -> Capability:
    """Apply `overlay` to `capability`, returning a NEW Capability -- `capability` itself, and
    every Step/Checkpoint/TargetDescriptor it holds, is never mutated. Meant to be called at
    invoke time, immediately before replay; the result is never written to artifacts/ (it is not
    a new recording, it is the same recording read through a tenant's own dictionary).

    Order: (1) validate the overlay actually targets this capability and names only real steps;
    (2) substitute vocabulary across every step's target/checkpoints and the capability's own
    success checkpoint; (3) apply step_overrides (a full, literal field replacement -- no further
    substitution); (4) insert extra_steps after their named step; (5) renumber `index` (never
    `id`) across the final step list.
    """
    if overlay.base_capability_id != capability.capability_id:
        raise OverlayError(
            f"overlay base_capability_id {overlay.base_capability_id!r} does not match this "
            f"capability's capability_id {capability.capability_id!r}"
        )
    if overlay.base_version != capability.version:
        raise OverlayError(
            f"overlay base_version {overlay.base_version} does not match this capability's "
            f"version {capability.version}"
        )

    steps_by_id = {step.id: step for step in capability.steps}
    for step_id, declared_override in overlay.step_overrides.items():
        base_step = steps_by_id.get(step_id)
        if base_step is None:
            raise OverlayError(
                f"step_overrides names step id {step_id!r}, which does not exist in this "
                "capability"
            )
        if declared_override.action is not None and declared_override.action != base_step.action:
            raise OverlayError(
                f"step_overrides for step {step_id!r} would change its action from "
                f"{base_step.action!r} to {declared_override.action!r}, which is not allowed"
            )
    for extra in overlay.extra_steps:
        if extra.after_step_id not in steps_by_id:
            raise OverlayError(
                f"extra_steps names after_step_id {extra.after_step_id!r}, which does not exist "
                "in this capability"
            )

    substitute = _vocabulary_substituter(overlay.vocabulary_map)
    new_steps = [_apply_vocabulary_to_step(step, substitute) for step in capability.steps]

    if overlay.step_overrides:
        overridden: list[Step] = []
        for step in new_steps:
            override = overlay.step_overrides.get(step.id)
            if override is None:
                overridden.append(step)
                continue
            # Live attribute values (a TargetDescriptor/Checkpoint/ParamRef instance), never a
            # `model_dump()`'d plain dict -- `Step.model_copy(update=...)` assigns straight into
            # the model with no revalidation, so a dumped dict here would silently leave
            # `step.target` holding a plain dict instead of a TargetDescriptor.
            fields = {
                name: getattr(override, name)
                for name in override.model_fields_set
                if name != "action"
            }
            overridden.append(step.model_copy(update=fields) if fields else step)
        new_steps = overridden

    if overlay.extra_steps:
        extra_by_after: dict[str, list[Step]] = {}
        for extra in overlay.extra_steps:
            extra_by_after.setdefault(extra.after_step_id, []).append(extra.step)
        expanded: list[Step] = []
        for step in new_steps:
            expanded.append(step)
            expanded.extend(extra_by_after.get(step.id, []))
        new_steps = expanded

    new_steps = [
        step.model_copy(update={"index": position}) for position, step in enumerate(new_steps)
    ]

    new_success = _apply_vocabulary_to_checkpoint(capability.success, substitute)
    target_updates: dict[str, Any] = {"tenant_id": overlay.tenant_id}
    if overlay.entry_point_override is not None:
        target_updates["entry_point"] = overlay.entry_point_override
    new_target = capability.target.model_copy(update=target_updates)

    return capability.model_copy(
        update={"steps": new_steps, "success": new_success, "target": new_target}
    )
