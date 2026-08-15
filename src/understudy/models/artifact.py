"""Capability: the recorded, agent-invocable artifact. Ordered steps, typed inputs/outputs, a
success checkpoint, and provenance -- decoupled from the raw model transcript by construction
(no field here is named messages, transcript, completion, choices, or content;
provenance.transcript_hash is a hex digest of it instead). tests/test_constraints.py
(invariant 4) enforces both properties.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from understudy.models.observation import Observation
from understudy.surface.locator import TargetDescriptor


class ParamSpec(BaseModel):
    name: str
    type: str
    description: str = ""
    required: bool = True


class OutputSpec(BaseModel):
    name: str
    type: str
    description: str = ""


class Checkpoint(BaseModel):
    kind: Literal["text_present"]
    target: str
    value: str


def checkpoint_satisfied(observation: Observation, checkpoint: Checkpoint) -> bool:
    """Pure check: does this observation satisfy this checkpoint? No Surface, no I/O.

    The one place "done" is defined. Both agent/loop.py's verify_checkpoint (discovery) and
    replay/engine.py (replay) call this instead of each keeping their own copy, so the two paths
    can never quietly diverge on what a checkpoint means.
    """
    if checkpoint.kind != "text_present":
        return False
    value = checkpoint.value
    if not value:
        return False
    for element in observation.elements:
        if value in element.name or (element.value is not None and value in element.value):
            return True
    return False


class Step(BaseModel):
    index: int
    action: str
    target: TargetDescriptor | None = None
    value: str | None = None
    rationale: str
    postcondition: Checkpoint | None = None


class Target(BaseModel):
    entry_point: str
    surface_kind: Literal["web"] = "web"


class Provenance(BaseModel):
    created_at: str
    model: str
    run_id: str
    transcript_hash: str
    understudy_version: str


class Capability(BaseModel):
    schema_version: int = 1
    capability_id: str
    version: int = 1
    name: str
    description: str
    target: Target
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    success: Checkpoint
    provenance: Provenance
    # "draft" until a human reviews it; replay only executes a RISKY_IRREVERSIBLE step when the
    # capability is "approved" AND the caller passes allow_risky=True at replay() (safety/policy.py
    # PolicyGate.dispatch). Recorded here, not passed as a bare replay() argument alone, so the
    # approval travels with the artifact a human actually reviewed.
    status: Literal["draft", "approved"] = "draft"
