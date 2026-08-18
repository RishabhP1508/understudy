"""InterventionRequest / InterventionResolution: the R6 escalation contract -- enough context for
a human to act on a stuck or blocked run, and a typed record of what the human then did.

Both are Pydantic v2 models because they hit disk (escalation/store.py writes one JSON file per
intervention id, always through Redactor -- ARCHITECTURE.md decision 10): an intervention carries
a screenshot path, a full Observation snapshot, and free-text application state, exactly the kind
of value this project never serializes unredacted.

`ReasonCode` is a closed, eight-value vocabulary, not a free string, for the same reason
`KnownOutcome.detector` and `RecoveryRule.trigger` are registered names rather than prose
(docs/adr/0014): a caller (the operator console, a calling agent reading `result.json`'s
`Escalated`) needs to branch on WHY a run stopped, and a free string cannot be branched on
reliably.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from understudy.models.observation import STRUCTURAL_EXTRA, VALUE_CARRYING_EXTRA, Observation


class ReasonCode(StrEnum):
    """Why a run stopped and raised an intervention, and which execution path can raise each one.

    - STUCK_NO_PROGRESS: discovery. Actions are dispatching, but the observation is not moving
      (the same no-progress signal agent/loop.py's stopping conditions already compute).
    - LOOP_DETECTED: discovery. The same tool call against the same target repeats past the
      configured stall limit.
    - LOCATOR_UNRESOLVED: either path. A recorded (replay) or proposed (discovery) target will
      not resolve to a unique element.
    - UNRECOVERABLE_CONDITION: replay. A runtime condition fired that no declared recovery rule
      knows how to clear (replay/recovery.py's `unrecovered` mapping).
    - RISKY_ACTION_REQUIRES_APPROVAL: either path. PolicyGate refused a RISKY_IRREVERSIBLE action
      because no approval (an approved+allow_risky capability in replay, a one-shot intervention
      approval in either path) authorized it.
    - POLICY_REFUSED: either path. PolicyGate refused for any other reason: the allowlist, the
      action type, the target's role, or a forbidden text pattern.
    - SESSION_EXPIRED: replay. The target application's session died mid-flow and the `reauth`
      recovery rule did not carry the run through it.
    - MAX_STEPS: discovery. The step cap was reached without the success checkpoint ever
      verifying.
    """

    STUCK_NO_PROGRESS = "stuck_no_progress"
    LOOP_DETECTED = "loop_detected"
    LOCATOR_UNRESOLVED = "locator_unresolved"
    UNRECOVERABLE_CONDITION = "unrecoverable_condition"
    RISKY_ACTION_REQUIRES_APPROVAL = "risky_action_requires_approval"
    POLICY_REFUSED = "policy_refused"
    SESSION_EXPIRED = "session_expired"
    MAX_STEPS = "max_steps"


class InterventionRequest(BaseModel):
    """Raised when a run cannot safely proceed. Carries everything a human needs to act on it
    without having to reconstruct context from run.jsonl by hand (R6): which run and capability,
    which step, why it stopped, what was tried, what the screen showed, and a screenshot.
    """

    id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    run_id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    # None: discovery has no capability yet -- the artifact is exactly what this run is trying to
    # produce, so there is nothing to name until it exists.
    capability_id: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    goal: str = Field(json_schema_extra=VALUE_CARRYING_EXTRA)
    step_id: int | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    reason_code: ReasonCode = Field(json_schema_extra=STRUCTURAL_EXTRA)
    what_it_tried: str = Field(json_schema_extra=VALUE_CARRYING_EXTRA)
    what_it_observed: str = Field(json_schema_extra=VALUE_CARRYING_EXTRA)
    # A nested BaseModel: Redactor's model-tree walk always recurses into a live BaseModel field
    # using THAT model's own field markings (safety/redact.py's `_redact_field`), regardless of
    # how this outer field is marked -- Observation's fields are already marked in
    # models/observation.py, so this marking is for a reader's benefit, not functional.
    observation: Observation = Field(json_schema_extra=STRUCTURAL_EXTRA)
    # Relative to this run's own evidence dir (evidence/logger.py's convention for every other
    # path-shaped field this project serializes), never an absolute filesystem path.
    screenshot_path: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    # Already-redacted extra detail (whatever the raising call site wants to attach beyond the
    # named fields above) -- still marked VALUE_CARRYING, not exempted from R3, because "already
    # redacted by the caller" is a property of the CONTENT, not a guarantee this schema can trust
    # blindly.
    context: dict[str, Any] = Field(default_factory=dict, json_schema_extra=VALUE_CARRYING_EXTRA)
    created_at: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    expires_at: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


class HumanAction(BaseModel):
    """One DOM-observable action a human took while holding control of the browser (R6: "record
    what the human did"). Captured by `WebSurface.install_human_action_capture()` /
    `drain_human_actions()` (surface/web.py). `role` and `name` use the SAME normalized
    vocabulary as `Observation`/`UIElement` (models/observation.py) -- surface/web.py maps a raw
    DOM tag/input-type onto it in Python -- rather than a raw HTML tag name, so the record of
    what a human did reads in the same terms as the record of what the agent did; a mixed
    handoff evidence trail in two different vocabularies would not be one trail at all.

    Does NOT see a native browser dialog: a human answering a `window.confirm` produces no DOM
    event at all, so no `HumanAction` is captured for it -- that case is evidenced instead by the
    dialog event's own `handled` value (surface/web.py's `dialog_events`, wired up in task C).
    """

    kind: Literal["click", "input", "change", "navigate"] = Field(
        json_schema_extra=STRUCTURAL_EXTRA
    )
    role: str = Field(default="", json_schema_extra=STRUCTURAL_EXTRA)
    name: str = Field(default="", json_schema_extra=STRUCTURAL_EXTRA)
    # What a person typed -- can genuinely be, or contain, a real secret or PII value, exactly
    # like UIElement.value; redacted the same way, through the same Redactor, because this field
    # is marked VALUE_CARRYING rather than left to fall back to it implicitly.
    value: str | None = Field(default=None, json_schema_extra=VALUE_CARRYING_EXTRA)
    url: str | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)
    at: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


class InterventionResolution(BaseModel):
    """What the human did, recorded so the evidence trail across a handoff is complete (R6:
    "record what the human did"), not just that a resolution eventually arrived.
    """

    resolved_by: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    action_taken: Literal["took_control", "approved", "rejected", "expired"] = Field(
        json_schema_extra=STRUCTURAL_EXTRA
    )
    human_actions: list[HumanAction] = Field(
        default_factory=list, json_schema_extra=VALUE_CARRYING_EXTRA
    )
    notes: str = Field(default="", json_schema_extra=VALUE_CARRYING_EXTRA)
    resolved_at: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
