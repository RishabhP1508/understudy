"""The replay result contract. Phase 2 has the two-way split; Phase 6 adds business outcomes and
recoverable conditions between them (ARCHITECTURE.md decision 8: business outcomes are not
failures, and detectors run business-outcome first, recovery second, checkpoint last).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class Success(BaseModel):
    kind: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    steps_executed: int
    checkpoint_verified: bool


class HardFailure(BaseModel):
    kind: Literal["hard_failure"] = "hard_failure"
    step_index: int
    expected: str
    observed: str
    message: str


ReplayResult = Annotated[Success | HardFailure, Field(discriminator="kind")]
