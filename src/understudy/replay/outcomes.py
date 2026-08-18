"""replay/outcomes.py: business outcome detectors.

A `KnownOutcome.detector` names a registered, pure function (`DETECTORS`), never free prose. This
matters more than it looks: a business outcome ("no such member", "permission denied") is a
LEGITIMATE ANSWER the caller needs, not a failure (ARCHITECTURE.md decision 8), and the brief
names conflating the two as the single most common design mistake in this problem. That claim is
only real if a detector name on a Capability actually resolves to code that runs at replay time --
a `detector` string that silently never matches anything would make "no such member" surface as a
locator failure or a checkpoint miss instead of the business outcome it is.

`validate(capability)` is therefore called once, at artifact load (replay/engine.py, before
anything else runs), and raises loudly (`UnknownDetector`) rather than degrading: a capability
naming a detector this build does not know is a REQUEST THAT WAS NEVER VALID, not a run that
failed partway through.

No Surface, no I/O, no LLM here: nothing in this module may import understudy.llm, understudy.agent,
or understudy.config (invariant 1's neighbourhood -- tests/test_constraints.py walks the import
graph starting from src/understudy/replay/).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import BaseModel

from understudy.models.artifact import Capability
from understudy.models.observation import Observation

Detector = Callable[[Observation], str | None]


def scan_text(observation: Observation, needles: Iterable[str]) -> str | None:
    """Scan every element's `name` and `value` for any of `needles` (plain substring match); the
    FIRST FULL matching string is returned, never a bool -- the caller gets the application's own
    field message verbatim, not a synthesized one. Shared by every detector in this module AND by
    replay/recovery.py's own triggers, so there is exactly one text-scan rule, not two that could
    drift apart on what "matches" means.
    """
    needles = tuple(needles)
    for element in observation.elements:
        for text in (element.name, element.value):
            if not text:
                continue
            if any(needle in text for needle in needles):
                return text
    return None


def _member_lookup_no_match(observation: Observation) -> str | None:
    return scan_text(
        observation,
        (
            "No member matches that search",
            "No such member",
            "No matching record was found",
        ),
    )


def _permission_denied(observation: Observation) -> str | None:
    return scan_text(observation, ("do not have permission", "Permission denied"))


def _validation_rejected(observation: Observation) -> str | None:
    return scan_text(observation, ("could not be validated",))


def _balance_check(observation: Observation) -> str | None:
    return scan_text(observation, ("Insufficient funds",))


DETECTORS: dict[str, Detector] = {
    "member_lookup_no_match": _member_lookup_no_match,
    "permission_denied": _permission_denied,
    "validation_rejected": _validation_rejected,
    "balance_check": _balance_check,
}


class UnknownDetector(Exception):
    """A capability names a detector (an outcome's `detector`, or -- replay/recovery.py reuses
    this same exception -- a recovery rule's `trigger`) that this build does not have registered.
    Raised at artifact validation time, never mid-replay: see the module docstring for why this
    must be loud rather than a silent never-match."""


def resolve_detector(name: str) -> Detector:
    try:
        return DETECTORS[name]
    except KeyError:
        raise UnknownDetector(
            f"unknown outcome detector {name!r}; known detectors: {sorted(DETECTORS)}"
        ) from None


def validate(capability: Capability) -> None:
    """Resolve every `known_outcomes[].detector`, or raise UnknownDetector. Call once, at artifact
    load, before replay does anything else (see module docstring)."""
    for outcome in capability.known_outcomes:
        resolve_detector(outcome.detector)


class OutcomeMatch(BaseModel):
    code: str
    detector: str
    message: str
    observed: str


def evaluate(observation: Observation, capability: Capability) -> OutcomeMatch | None:
    """Walk `capability.known_outcomes` in declared order; the first detector that matches wins.

    `message` is the outcome's own DECLARED `message_template` -- the capability's stated meaning
    for this code, reviewable by a human or a calling agent straight off the artifact, and stable
    across tenants and vendor rewording (models/result.py's `BusinessOutcome` docstring has the
    full reasoning). `matched_text` (the app's own literal wording) is only the fallback, for the
    degenerate case of an outcome that declares no template at all -- reachable, since
    `KnownOutcome.message_template` defaults to `""`. `observed` always carries the literal app
    text verbatim, so a caller/reviewer can see exactly what the application said, alongside the
    capability's own declared meaning in `message`.
    """
    for outcome in capability.known_outcomes:
        detector = resolve_detector(outcome.detector)
        matched_text = detector(observation)
        if matched_text is not None:
            return OutcomeMatch(
                code=outcome.code,
                detector=outcome.detector,
                message=outcome.message_template or matched_text,
                observed=matched_text,
            )
    return None
