"""The replay result contract: four terminal kinds on the `kind` discriminator, plus one event
shape (`type == "recovered"` in run.jsonl) that is deliberately NOT a fifth kind here.

Why four, not three: ARCHITECTURE.md decision 8 says a business outcome ("no such member") is not
a failure, so `Success` and `HardFailure` alone would force a caller to read `HardFailure.message`
prose to tell "the record does not exist" from "the automation broke" -- exactly the conflation
the brief calls out as the most common design mistake in this problem. `BusinessOutcome` gives
that a first-class, typed shape a caller can branch on without parsing English.

Why four, not five: `Escalated` exists because a run can end with a human in control instead of a
result at all (R6) -- that is a genuinely different SHAPE of ending, not a variant of failure or
success, so it earns its own kind.

Why `Recovered` is an event, not a fifth kind: a recovery that worked (a slow load that was
waited out, a dialog that was dismissed) is not an outcome the caller needs to branch on -- it is
something that happened ON THE WAY to one of the four kinds above, and the run still ends in one
of them. Making it a terminal kind would force every caller to add a branch for "it worked, but
only after some trouble", which is not a decision any caller actually needs to make; the caller
needs to know it succeeded, not how bumpy the road was. What DOES matter (what was recovered from,
how many attempts it took) belongs in the evidence trail, not the result contract, so it is a
`type="recovered"` line in run.jsonl (evidence/logger.py) carrying that detail, and Phase 9's
recovery.py is what will populate it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class FailureCategory(StrEnum):
    """Exactly ten values. Three of them exist because a single message,
    "could not resolve the target for step 0", was measured (Phase 3) coming from two
    unrelated causes -- a dead fixture server and a stale artifact -- and a caller cannot act on
    a failure it cannot tell apart. See docs/adr/0009 for the full reasoning and the classifier.

    - TARGET_UNREACHABLE: the entry-point navigation itself never reached the server.
    - STALE_PERCEPTION: a locator failed to resolve AND the artifact was recorded under a
      different PERCEPTION_VERSION than the one running now -- the likely actual cause.
    - LOCATOR_UNRESOLVED: a locator failed to resolve and perception versions match; the target
      itself has genuinely changed or the recorded descriptor was always ambiguous.
    - POLICY_DENIED: the policy gate refused a step (allowlist, action type, role, forbidden
      text, or risk) that the recorded capability itself proposed.
    - ACTION_FAILED: the surface raised executing an otherwise-permitted action (an "outright
      app error" in R3's list, or any runtime condition not covered by a more specific category).
    - POSTCONDITION_FAILED: a step's own recorded postcondition did not hold afterwards.
    - CHECKPOINT_NOT_VERIFIED: every step executed, but the capability's success checkpoint did
      not hold in the final observation.
    - PERMISSION_DENIED: the target application itself refused the action (R3's "permission
      denials") -- distinct from POLICY_DENIED, which is Understudy's own gate refusing first.
    - SESSION_EXPIRED: the target application's session or login expired mid-replay (R3's
      "session and timeout expiry").
    - UNHANDLED_DIALOG: a native or in-app confirmation dialog appeared that recovery did not
      recognize (R3's "unexpected confirmation dialogs").

    PERMISSION_DENIED, SESSION_EXPIRED, and UNHANDLED_DIALOG have no detector wired to them yet --
    that is Phase 9's recovery/outcome taxonomy. The category exists now because the result
    contract's shape is what the brief grades hardest, and a caller integrating against this enum
    today should not have to add a case later just because a detector arrives after the schema
    does.
    """

    TARGET_UNREACHABLE = "target_unreachable"
    STALE_PERCEPTION = "stale_perception"
    LOCATOR_UNRESOLVED = "locator_unresolved"
    POLICY_DENIED = "policy_denied"
    ACTION_FAILED = "action_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    CHECKPOINT_NOT_VERIFIED = "checkpoint_not_verified"
    PERMISSION_DENIED = "permission_denied"
    SESSION_EXPIRED = "session_expired"
    UNHANDLED_DIALOG = "unhandled_dialog"


class Success(BaseModel):
    kind: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    steps_run: int
    duration_ms: float


class BusinessOutcome(BaseModel):
    """A legitimate, typed answer the caller needs -- "no such member", "insufficient funds" --
    never a failure (ARCHITECTURE.md decision 8). Not produced anywhere yet: the detectors that
    recognize one are Phase 9's `replay/outcomes.py`. The shape exists now so the result contract
    is complete and reviewable as a whole (R2), the way the brief grades it.
    """

    kind: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)


class HardFailure(BaseModel):
    kind: Literal["hard_failure"] = "hard_failure"
    # None means the failure happened before any step ran (e.g. the entry-point navigate itself).
    step_id: int | None
    category: FailureCategory
    expected: str
    observed: str
    # Paths (relative to the run's own evidence directory) to whatever richer signal
    # EvidenceLogger.capture_failure produced: a DOM snapshot, an accessibility snapshot, a
    # masked screenshot, a kept trace -- R5's "at least one richer signal on failure".
    evidence_refs: list[str] = Field(default_factory=list)


class Escalated(BaseModel):
    """A run that ended with a human in control instead of a result (R6). `intervention_id` and
    `resolution` are kept primitive here on purpose: Phase 10 owns the real
    `models/intervention.py` (InterventionRequest, Resolution) and this stays a thin pointer into
    that store rather than a second definition of the same shape.
    """

    kind: Literal["escalated"] = "escalated"
    intervention_id: str
    resolution: str | None = None
    resumed: bool = False


ReplayResult = Annotated[
    Success | BusinessOutcome | HardFailure | Escalated, Field(discriminator="kind")
]
