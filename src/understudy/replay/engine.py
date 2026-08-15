"""Deterministic replay: no model in the decision loop.

tests/test_constraints.py (invariant 1) walks this module's import graph and fails if anything
here transitively reaches understudy.llm or a provider SDK. Imports here are limited to
models/, surface/, safety/, and evidence/ -- not even understudy.agent or understudy.config --
so that boundary is obviously true by inspection, not just true today.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import Capability, Step, checkpoint_satisfied
from understudy.models.result import HardFailure, ReplayResult, Success
from understudy.safety.policy import PolicyGate
from understudy.surface.base import Action, Click, Navigate, ReadText, Select, Type
from understudy.surface.locator import AmbiguousTarget, TargetNotFound, resolve
from understudy.surface.web import WebSurface


def _action_for_step(step: Step, node_id: str | None) -> Action:
    if step.action == "navigate":
        return Navigate(url=step.value or "")
    if step.action == "click":
        return Click(node_id=node_id or "")
    if step.action == "type":
        return Type(node_id=node_id or "", text=step.value or "")
    if step.action == "select":
        return Select(node_id=node_id or "", value=step.value or "")
    if step.action in ("read_text", "extract"):
        return ReadText(node_id=node_id or "")
    raise ValueError(f"unsupported step action: {step.action!r}")


def _screenshot_on_failure(logger: EvidenceLogger, surface: WebSurface, step_index: int) -> None:
    # A screenshot failure (e.g. the page already crashed, which is exactly when a hard failure
    # is likely) must never replace the real hard failure with an unrelated exception.
    try:
        logger.screenshot(surface, step_index)
    except Exception as exc:
        logger.event("screenshot_failed", step=step_index, reason=str(exc))


def replay(artifact_path: Path, params: dict[str, Any], policy_path: Path) -> ReplayResult:
    # params and policy_path are accepted for parity with the Phase 8/5 contract. Phase 2 has no
    # step parameterization and no allowlist enforcement yet, so neither is used below.
    capability = Capability.model_validate_json(artifact_path.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    logger = EvidenceLogger("replay", run_id)
    gate = PolicyGate(logger)
    surface = WebSurface(headless=False)
    outputs: dict[str, str] = {}

    logger.event(
        "replay_start",
        capability_id=capability.capability_id,
        version=capability.version,
        params=params,
    )
    try:
        gate.dispatch(
            surface,
            Navigate(url=capability.target.entry_point),
            context={"tool": "navigate", "rationale": "open the capability's recorded entry point"},
        )

        for step in capability.steps:
            observation = surface.observe()
            node_id: str | None = None
            if step.target is not None:
                try:
                    node_id = resolve(observation, step.target)
                except (AmbiguousTarget, TargetNotFound) as exc:
                    logger.event("hard_failure", step_index=step.index, reason=str(exc))
                    _screenshot_on_failure(logger, surface, step.index)
                    return HardFailure(
                        step_index=step.index,
                        expected=f"a unique element matching role={step.target.role!r} "
                        f"name={step.target.name!r}",
                        observed=str(exc),
                        message=f"could not resolve the target for step {step.index}",
                    )

            try:
                action = _action_for_step(step, node_id)
                result_text = gate.dispatch(
                    surface, action, context={"tool": step.action, "rationale": step.rationale}
                )
            except Exception as exc:
                # Phase 2 has only Success/HardFailure; Phase 9 adds a real recovery taxonomy
                # for slow loads, dialogs, and transient failures. An unexpected runtime
                # condition here becomes a structured, debuggable failure rather than a raw
                # traceback (R3: report what step, what was expected, what was observed).
                logger.event("hard_failure", step_index=step.index, reason=str(exc))
                _screenshot_on_failure(logger, surface, step.index)
                return HardFailure(
                    step_index=step.index,
                    expected=f"step {step.index} ({step.action}) to execute",
                    observed=str(exc),
                    message=f"step {step.index} failed to execute",
                )

            if step.action == "extract":
                outputs[step.value or f"output_{step.index}"] = result_text or ""

            postcondition = step.postcondition
            if postcondition is not None and not checkpoint_satisfied(
                surface.observe(), postcondition
            ):
                logger.event(
                    "hard_failure", step_index=step.index, reason="postcondition not satisfied"
                )
                _screenshot_on_failure(logger, surface, step.index)
                return HardFailure(
                    step_index=step.index,
                    expected=postcondition.value,
                    observed="postcondition not satisfied",
                    message=f"postcondition failed at step {step.index}",
                )

        checkpoint_verified = checkpoint_satisfied(surface.observe(), capability.success)
        logger.event("replay_end", checkpoint_verified=checkpoint_verified, outputs=outputs)
        return Success(
            outputs=outputs,
            steps_executed=len(capability.steps),
            checkpoint_verified=checkpoint_verified,
        )
    finally:
        surface.close()
