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

from typing import Any, Literal

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
    # substituted at discovery time).
    example: str | None = Field(default=None, json_schema_extra=VALUE_CARRYING_EXTRA)
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
    on_failure: Literal["dismiss", "retry", "reauth", "wait"] | None = Field(
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
    """

    id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    trigger: str = ""
    action: Literal["dismiss", "retry", "reauth", "wait"] = Field(
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
