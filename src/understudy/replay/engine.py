"""Deterministic replay: no model in the decision loop.

tests/test_constraints.py (invariant 1) walks this module's import graph and fails if anything
here transitively reaches understudy.llm or a provider SDK. Imports here are limited to
models/, surface/, safety/, and evidence/ -- not even understudy.agent or understudy.config --
so that boundary is obviously true by inspection, not just true today.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import Capability, Checkpoint, Step, checkpoint_satisfied
from understudy.models.observation import PERCEPTION_VERSION, Observation, UIElement
from understudy.models.result import FailureCategory, HardFailure, ReplayResult, Success
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    PolicyDenied,
    PolicyGate,
    load_policy,
)
from understudy.surface.base import Action, Click, Navigate, ReadText, Select, Type
from understudy.surface.locator import resolve
from understudy.surface.web import WebSurface

_PolicyException = (PolicyDenied, EscalationRequired, NavigationBlocked)


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


def _policy_exception_reason(exc: Exception) -> str:
    if isinstance(exc, NavigationBlocked):
        return f"navigation left the allowlist: {exc.urls}"
    decision = getattr(exc, "decision", None)
    if decision is not None:
        return f"{decision.rule}: {decision.reason}"
    return str(exc)


def _classify_entry_navigate_failure(exc: Exception) -> FailureCategory:
    """The entry-point navigate is the first thing replay does, before any locator work, so a
    connection-level failure here IS the "is the target reachable" check (docs/adr/0009) -- no
    separate HTTP probe is needed. Playwright's own navigation errors carry Chromium's net-error
    code in their message (e.g. "net::ERR_CONNECTION_REFUSED"); anything else at this point is
    some other kind of action failure, not specifically an unreachable target.
    """
    if "net::ERR_" in str(exc):
        return FailureCategory.TARGET_UNREACHABLE
    return FailureCategory.ACTION_FAILED


def _classify_locator_failure(capability: Capability) -> FailureCategory:
    """Measured (Phase 3): a dead fixture server and a stale artifact both produced the identical
    message "could not resolve the target for step 0" -- a caller cannot act on a failure it
    cannot tell apart. STALE_PERCEPTION takes priority over LOCATOR_UNRESOLVED when both could
    apply, because it names the actual cause: the artifact was recorded under different
    perception logic than the one running now. See docs/adr/0009 for the full reasoning.

    This is a CLASSIFICATION of a failure that already happened, never a pre-flight gate: the
    real artifact in artifacts/ has perception_version=1 (it predates PERCEPTION_VERSION
    entirely) and still replays successfully end to end, because this function is only ever
    called once a locator has ALREADY failed to resolve.
    """
    if capability.provenance.perception_version != PERCEPTION_VERSION:
        return FailureCategory.STALE_PERCEPTION
    return FailureCategory.LOCATOR_UNRESOLVED


def _capture_failure_evidence(
    logger: EvidenceLogger, surface: WebSurface, step_id: int, before_path: Path | None
) -> list[str]:
    """Evidence for one HardFailure: the already-taken 'before' screenshot (if any), a fresh
    'after' screenshot, the DOM, the accessibility snapshot, and the kept trace.

    Re-observes rather than reusing `before_path`'s own observation (D7, Phase 5/6): the page may
    have changed since 'before' was masked (an action may have partially executed, a dialog may
    have appeared), and a stale observation would position a mask over pixels that no longer show
    what it thinks they show -- a leak, not merely wrong. A screenshot failure must never replace
    the real hard failure with an unrelated exception, so this is best-effort throughout.
    """
    # evidence_refs is a result-contract field, not a local filesystem path: always "/"-separated
    # regardless of host OS (Path.as_posix()), so result.json is identical content on Windows and
    # Linux and a consumer can join a ref against a POSIX base path with no translation.
    refs: list[str] = (
        [before_path.relative_to(logger.dir).as_posix()] if before_path is not None else []
    )
    try:
        observation = surface.observe()
    except Exception as exc:
        logger.event("screenshot_failed", step_id=step_id, note=str(exc))
        return refs
    after_path = logger.screenshot(surface, step_id, "after", observation)
    if after_path is not None:
        refs.append(after_path.relative_to(logger.dir).as_posix())
    refs.extend(logger.capture_failure(surface, step_id, observation))
    return refs


def _finish_result(
    checkpoint_verified: bool,
    success_checkpoint: Checkpoint,
    outputs: dict[str, str],
    steps_run: int,
    duration_ms: float = 0.0,
) -> ReplayResult:
    """The one decision replay's terminal step makes: did the recorded success checkpoint hold?

    Pulled out as a pure function (no Surface, no logger) so the false branch -- every step
    executed without raising, but the goal was never actually reached -- has a direct unit test.
    Before this fix, replay returned Success(checkpoint_verified=False) here and cli.py only
    exits non-zero on hard_failure, so a replay that did not achieve its goal printed "success"
    and exited 0. The checkpoint is the one place "done" is decided (models/artifact.py), never
    the model and never optimism, so a checkpoint that does not hold is a failed run, full stop.
    `evidence_refs` is attached by the caller (only it has a `surface` to capture evidence from).
    """
    if not checkpoint_verified:
        return HardFailure(
            step_id=steps_run,
            category=FailureCategory.CHECKPOINT_NOT_VERIFIED,
            expected=f"success checkpoint: text {success_checkpoint.value!r} present "
            f"on {success_checkpoint.target!r}",
            observed="checkpoint text was not present in the final observation",
        )
    return Success(outputs=outputs, steps_run=steps_run, duration_ms=duration_ms)


def replay(
    artifact_path: Path,
    params: dict[str, Any],
    policy_path: Path,
    allow_risky: bool = False,
    evidence_base_dir: str | Path = "evidence",
) -> ReplayResult:
    # params is accepted for parity with the Phase 8 contract; this phase has no step
    # parameterization yet, so it is unused below.
    capability = Capability.model_validate_json(artifact_path.read_text(encoding="utf-8"))
    policy = load_policy(policy_path)
    run_id = uuid.uuid4().hex[:12]
    logger = EvidenceLogger(run_id, "replay", base_dir=evidence_base_dir)
    gate = PolicyGate(
        policy,
        logger,
        mode="replay",
        allow_risky=allow_risky,
        capability_status=capability.status,
    )
    surface = WebSurface(policy=policy, headless=False)
    outputs: dict[str, str] = {}
    start = time.monotonic()
    result: ReplayResult | None = None

    logger.event(
        "replay_start",
        capability_id=capability.capability_id,
        version=capability.version,
        params=params,
    )
    logger.start_trace(surface)
    try:
        try:
            gate.dispatch(
                surface,
                Navigate(url=capability.target.entry_point),
                context={
                    "tool": "navigate",
                    "rationale": "open the capability's recorded entry point",
                },
            )
        except _PolicyException as exc:
            # Replay's contract is to return a result, never raise -- a policy denial anywhere,
            # including the entry-point navigate itself, becomes a HardFailure like any other
            # runtime condition replay cannot proceed past.
            reason = _policy_exception_reason(exc)
            refs = _capture_failure_evidence(logger, surface, 0, None)
            logger.event(
                "hard_failure", step_id=None, category=FailureCategory.POLICY_DENIED.value,
                note=reason,
            )
            result = HardFailure(
                step_id=None,
                category=FailureCategory.POLICY_DENIED,
                expected="the capability's recorded entry-point navigation to be permitted "
                "by policy",
                observed=reason,
                evidence_refs=refs,
            )
            return result
        except Exception as exc:
            category = _classify_entry_navigate_failure(exc)
            reason = str(exc)
            refs = _capture_failure_evidence(logger, surface, 0, None)
            logger.event("hard_failure", step_id=None, category=category.value, note=reason)
            result = HardFailure(
                step_id=None,
                category=category,
                expected="the capability's recorded entry point to be reachable",
                observed=reason,
                evidence_refs=refs,
            )
            return result

        after_observation: Observation | None = None
        for step in capability.steps:
            observation = surface.observe()
            before_path = logger.screenshot(surface, step.index, "before", observation)
            node_id: str | None = None
            element: UIElement | None = None
            if step.target is not None:
                resolution = resolve(step.target, observation)
                if resolution.element is None:
                    # docs/adr/0006: a failed resolution reports every rung's candidate count,
                    # not just "not found", so the failure is debuggable (R3).
                    observed = "; ".join(
                        f"{attempt.strategy.value}: {attempt.candidate_count} candidate(s)"
                        + (f" ({attempt.skipped_reason})" if attempt.skipped_reason else "")
                        for attempt in resolution.attempts
                    )
                    category = _classify_locator_failure(capability)
                    refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                    logger.event(
                        "hard_failure",
                        step_id=step.index,
                        category=category.value,
                        note=observed,
                        resolution=resolution.model_dump(),
                    )
                    result = HardFailure(
                        step_id=step.index,
                        category=category,
                        expected=f"a unique element matching role={step.target.role!r} "
                        f"name={step.target.name!r}",
                        observed=observed,
                        evidence_refs=refs,
                    )
                    return result
                node_id = resolution.element.node_id
                element = resolution.element

            try:
                action = _action_for_step(step, node_id)
                result_text = gate.dispatch(
                    surface,
                    action,
                    context={
                        "tool": step.action,
                        "rationale": step.rationale,
                        "step_id": step.index,
                    },
                    element=element,
                )
            except _PolicyException as exc:
                reason = _policy_exception_reason(exc)
                refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                logger.event(
                    "hard_failure",
                    step_id=step.index,
                    category=FailureCategory.POLICY_DENIED.value,
                    note=reason,
                )
                result = HardFailure(
                    step_id=step.index,
                    category=FailureCategory.POLICY_DENIED,
                    expected=f"step {step.index} ({step.action}) to be permitted by policy",
                    observed=reason,
                    evidence_refs=refs,
                )
                return result
            except Exception as exc:
                # An unexpected runtime condition becomes a structured, debuggable failure
                # rather than a raw traceback (R3: report what step, what was expected, what was
                # observed). Phase 9 adds a real recovery taxonomy ahead of this for slow loads,
                # dialogs, and transient failures.
                refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                logger.event(
                    "hard_failure",
                    step_id=step.index,
                    category=FailureCategory.ACTION_FAILED.value,
                    note=str(exc),
                )
                result = HardFailure(
                    step_id=step.index,
                    category=FailureCategory.ACTION_FAILED,
                    expected=f"step {step.index} ({step.action}) to execute",
                    observed=str(exc),
                    evidence_refs=refs,
                )
                return result

            if step.action == "extract":
                outputs[step.value or f"output_{step.index}"] = result_text or ""

            # D7 (Phase 5, extended Phase 6): the action just changed the page, so the 'after'
            # screenshot and the postcondition check both need a FRESH observation, never
            # `observation` above -- one extra observe() per step, reused for both, so it is
            # never paid twice.
            after_observation = surface.observe()
            logger.screenshot(surface, step.index, "after", after_observation)

            postcondition = step.postcondition
            if postcondition is not None and not checkpoint_satisfied(
                after_observation, postcondition
            ):
                refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                logger.event(
                    "hard_failure", step_id=step.index,
                    category=FailureCategory.POSTCONDITION_FAILED.value,
                    note="postcondition not satisfied",
                )
                result = HardFailure(
                    step_id=step.index,
                    category=FailureCategory.POSTCONDITION_FAILED,
                    expected=postcondition.value,
                    observed="postcondition not satisfied",
                    evidence_refs=refs,
                )
                return result

        # The last step's own 'after' observation (if any step ran) is already fresh; reusing it
        # avoids a third observe() call this step never needed to make on top of the two above.
        final_observation = (
            after_observation if after_observation is not None else surface.observe()
        )
        checkpoint_verified = checkpoint_satisfied(final_observation, capability.success)
        logger.event(
            "replay_end",
            phase="verify",
            checkpoint_eval=capability.success.model_dump(),
            outcome_match=str(checkpoint_verified),
            outputs=outputs,
        )
        steps_run = len(capability.steps)
        duration_ms = (time.monotonic() - start) * 1000
        result = _finish_result(
            checkpoint_verified, capability.success, outputs, steps_run, duration_ms
        )
        if isinstance(result, HardFailure):
            # _finish_result has no Surface to capture evidence from; attach it here.
            refs = _capture_failure_evidence(logger, surface, steps_run, None)
            result = result.model_copy(update={"evidence_refs": refs})
        return result
    finally:
        try:
            if result is not None:
                logger.write_result(result)
            logger.stop_trace(surface, keep=(result is None or isinstance(result, HardFailure)))
        finally:
            surface.close()
