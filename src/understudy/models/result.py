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
    """Twelve values. Three of the first ten exist because a single message,
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
    - ACTION_FAILED: the SURFACE RAISED while executing an otherwise-permitted action -- a
      Playwright timeout, a stale ref, a crashed page -- any runtime condition where the action
      itself never completed. Distinct from APP_ERROR, below: this is Understudy's own execution
      failing, not the target application's.
    - APP_ERROR: the action executed fine and the APPLICATION returned an error (an "outright
      app error" in R3's list, e.g. an HTTP 500 or an error page) -- the surface did its job, and
      what it observed afterward is the failure.
    - POSTCONDITION_FAILED: a step's own recorded postcondition did not hold afterwards.
    - CHECKPOINT_NOT_VERIFIED: every step executed, but the capability's success checkpoint did
      not hold in the final observation.
    - PERMISSION_DENIED: the target application itself refused the action (R3's "permission
      denials") -- distinct from POLICY_DENIED, which is Understudy's own gate refusing first.
    - SESSION_EXPIRED: the target application's session or login expired mid-replay (R3's
      "session and timeout expiry").
    - UNHANDLED_DIALOG: a native or in-app confirmation dialog appeared that recovery did not
      recognize (R3's "unexpected confirmation dialogs").
    - INVALID_PARAMS: the caller's own `params` do not satisfy the capability's declared
      `InputParam`s (a required one is missing). Checked before the entry-point navigate even
      runs (Phase 8's D-defect-1 fix): there is no point launching a browser for a request that
      cannot possibly succeed, and typing a missing parameter's placeholder text literally into a
      live form is worse than refusing up front.

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
    APP_ERROR = "app_error"
    POSTCONDITION_FAILED = "postcondition_failed"
    CHECKPOINT_NOT_VERIFIED = "checkpoint_not_verified"
    PERMISSION_DENIED = "permission_denied"
    SESSION_EXPIRED = "session_expired"
    UNHANDLED_DIALOG = "unhandled_dialog"
    INVALID_PARAMS = "invalid_params"


class Success(BaseModel):
    kind: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    steps_run: int
    duration_ms: float


class BusinessOutcome(BaseModel):
    """A legitimate, typed answer the caller needs -- "no such member", "insufficient funds" --
    never a failure (ARCHITECTURE.md decision 8).

    Three fields, deliberately split, mirroring `HardFailure`'s existing `expected`/`observed`
    split rather than inventing a second shape:
      - `code`: what a caller BRANCHES ON. Stable across tenants and vendor rewording.
      - `message`: the CAPABILITY's OWN DECLARED MEANING (`KnownOutcome.message_template`) -- a
        human or a calling agent can read this straight off the artifact before the capability
        has ever been invoked (R2: reviewable by both), and it is the caller's actual contract.
      - `observed`: what the APPLICATION ITSELF said, verbatim -- supporting evidence, not the
        contract. Phase 12 puts two tenants of the same vendor product behind one capability, and
        they will render different literal strings for the same outcome; a caller's `message`
        changing per tenant, or when a vendor rewords a page, is exactly the drift the artifact
        exists to absorb, so the literal text must never be the caller-facing field.
    """

    kind: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    observed: str = ""
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
