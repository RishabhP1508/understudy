"""Deterministic replay: no model in the decision loop.

tests/test_constraints.py (invariant 1) walks this module's import graph and fails if anything
here transitively reaches understudy.llm or a provider SDK. Imports here are limited to
models/, surface/, safety/, and evidence/ -- not even understudy.agent or understudy.config --
so that boundary is obviously true by inspection, not just true today.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import Capability, Checkpoint, ParamRef, Step, checkpoint_satisfied
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


def _missing_required_params(capability: Capability, params: dict[str, Any]) -> list[str]:
    """Every declared `InputParam` the capability requires that the caller's own `params` did not
    supply. Checked once, up front, before anything else runs (R3: replay is given "an artifact
    AND input parameters"; a required one missing is not a runtime condition to discover mid-run,
    it is an invalid request)."""
    return sorted(
        param.name for param in capability.inputs if param.required and param.name not in params
    )


def _resolve_param(name: str, params: dict[str, Any]) -> str:
    """The one place a declared input parameter's caller-supplied value becomes a literal string
    -- used both for a Step's own `ParamRef` value (`_resolve_step_value`) and for a `:name`
    placeholder embedded in a canonicalized Checkpoint URL (`_resolve_checkpoint`), so the two can
    never disagree about what a given name means. Raises with a debuggable message (never a bare
    `KeyError`) rather than crashing if `name` was never validated present -- reachable only for
    an OPTIONAL param a step still references but the caller omitted, since every REQUIRED param a
    capability declares is already checked present before any step runs
    (`_missing_required_params`).
    """
    if name not in params:
        raise KeyError(f"capability references parameter {name!r}, which was not supplied")
    return str(params[name])


def _resolve_step_value(
    value: str | int | float | bool | ParamRef | None, params: dict[str, Any]
) -> str:
    if value is None:
        return ""
    if isinstance(value, ParamRef):
        return _resolve_param(value.name, params)
    return str(value)


_PLACEHOLDER_RE = re.compile(r":(\w+)")


def _resolve_checkpoint(
    checkpoint: Checkpoint, capability: Capability, params: dict[str, Any]
) -> Checkpoint:
    """Replace every `:name` route placeholder `record/canonicalize.py`'s `canonicalize_route`
    embedded in this checkpoint's own value with the caller's resolved parameter, via the SAME
    `_resolve_param` a Step's own value goes through -- a checkpoint and the step whose value it
    is checking the effect of can never disagree about what `:member_id` means. A declared param
    absent from `params` (only possible for an optional one; every required one is already
    validated present) is simply left un-interpolated rather than raising: the placeholder then
    will not match a real URL, which correctly becomes a postcondition/checkpoint failure instead
    of a crash.

    ONE regex pass over `:(\\w+)`, never a sequence of `str.replace` calls keyed off each
    declared param name: an ordered sequence is prefix-unsafe (with params `id` and `id_long`,
    replacing `:id` first also corrupts `:id_long`'s own leading `:id`, regardless of which order
    the declared params happen to be iterated in). `\\w+` always matches the full identifier
    greedily, so `:id_long` resolves as one name in one step, never as `:id` plus a leftover
    `_long`.
    """
    declared = {param.name for param in capability.inputs}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in declared and name in params:
            return _resolve_param(name, params)
        return match.group(0)  # not a declared, supplied param name: leave untouched

    value = _PLACEHOLDER_RE.sub(_replace, checkpoint.value)
    if value == checkpoint.value:
        return checkpoint
    return checkpoint.model_copy(update={"value": value})


def _action_for_step(step: Step, node_id: str | None, params: dict[str, Any]) -> Action:
    if step.action == "navigate":
        return Navigate(url=_resolve_step_value(step.value, params))
    if step.action == "click":
        return Click(node_id=node_id or "")
    if step.action == "type":
        return Type(node_id=node_id or "", text=_resolve_step_value(step.value, params))
    if step.action == "select":
        return Select(node_id=node_id or "", value=_resolve_step_value(step.value, params))
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
    capability = Capability.model_validate_json(artifact_path.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    logger = EvidenceLogger(run_id, "replay", base_dir=evidence_base_dir)
    result: ReplayResult | None = None

    # Register every sensitive declared param's caller-supplied value BEFORE any logging happens
    # -- exactly what PolicyGate._log already does for a live Type action's own resolved text --
    # so a raw secret/PII value the caller passed in `params` never appears in run.jsonl, even
    # inside this run's own bootstrap "replay_start" event below.
    for param in capability.inputs:
        if param.sensitivity in ("secret", "pii") and param.name in params:
            logger.redactor.register_secret(str(params[param.name]))

    logger.event(
        "replay_start",
        capability_id=capability.capability_id,
        version=capability.version,
        params=params,
    )

    missing = _missing_required_params(capability, params)
    if missing:
        # R3/D-defect-1: a required parameter the capability declares but the caller did not
        # supply is an invalid request, checked before step 0 (indeed before a browser is even
        # launched) -- never a silently-typed empty string, never a raw crash.
        expected = sorted(param.name for param in capability.inputs if param.required)
        result = HardFailure(
            step_id=None,
            category=FailureCategory.INVALID_PARAMS,
            expected=f"required parameter(s) {expected}",
            observed=f"missing {missing}; given {sorted(params)}",
        )
        logger.event(
            "hard_failure",
            step_id=None,
            category=FailureCategory.INVALID_PARAMS.value,
            note=result.observed,
        )
        logger.write_result(result)
        return result

    policy = load_policy(policy_path)
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
                action = _action_for_step(step, node_id, params)
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
                outputs[_resolve_step_value(step.value, params) or f"output_{step.index}"] = (
                    result_text or ""
                )

            # D7 (Phase 5, extended Phase 6): the action just changed the page, so the 'after'
            # screenshot and the postcondition check both need a FRESH observation, never
            # `observation` above -- one extra observe() per step, reused for both, so it is
            # never paid twice.
            after_observation = surface.observe()
            logger.screenshot(surface, step.index, "after", after_observation)

            postcondition = step.postcondition
            if postcondition is not None:
                # D-defect-2: a canonicalized `:name` placeholder (record/canonicalize.py) is
                # resolved against the caller's own params before evaluation, through the SAME
                # `_resolve_param` a step's own value goes through, so the two can never disagree
                # about what `:member_id` means.
                resolved_postcondition = _resolve_checkpoint(postcondition, capability, params)
                if not checkpoint_satisfied(after_observation, resolved_postcondition):
                    refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                    logger.event(
                        "hard_failure",
                        step_id=step.index,
                        category=FailureCategory.POSTCONDITION_FAILED.value,
                        note="postcondition not satisfied",
                    )
                    result = HardFailure(
                        step_id=step.index,
                        category=FailureCategory.POSTCONDITION_FAILED,
                        expected=resolved_postcondition.value,
                        observed="postcondition not satisfied",
                        evidence_refs=refs,
                    )
                    return result

        # The last step's own 'after' observation (if any step ran) is already fresh; reusing it
        # avoids a third observe() call this step never needed to make on top of the two above.
        final_observation = (
            after_observation if after_observation is not None else surface.observe()
        )
        resolved_success = _resolve_checkpoint(capability.success, capability, params)
        checkpoint_verified = checkpoint_satisfied(final_observation, resolved_success)
        logger.event(
            "replay_end",
            phase="verify",
            checkpoint_eval=resolved_success.model_dump(),
            outcome_match=str(checkpoint_verified),
            outputs=outputs,
        )
        steps_run = len(capability.steps)
        duration_ms = (time.monotonic() - start) * 1000
        result = _finish_result(
            checkpoint_verified, resolved_success, outputs, steps_run, duration_ms
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
