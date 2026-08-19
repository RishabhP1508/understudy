"""Deterministic replay: no model in the decision loop.

tests/test_constraints.py (invariant 1) walks this module's import graph and fails if anything
here transitively reaches understudy.llm or a provider SDK. Imports here are limited to
models/, surface/, safety/, evidence/, and replay/ itself (outcomes.py, recovery.py) -- not even
understudy.agent or understudy.config -- so that boundary is obviously true by inspection, not
just true today.

EVALUATION ORDER, every step, non-negotiable: a business outcome (replay/outcomes.py) is checked
FIRST, because "no such member" is a legitimate answer the caller needs, never a failure. A
recoverable condition (replay/recovery.py) is checked SECOND, and re-evaluates the step once
applied. Only once neither applies does the step's own postcondition get the final word.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from understudy.escalation.control import ControlState, SessionBroker
from understudy.escalation.store import InterventionStore
from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    ParamRef,
    Step,
    checkpoint_satisfied,
    login_prefix_len,
)
from understudy.models.intervention import InterventionRequest, InterventionResolution, ReasonCode
from understudy.models.observation import (
    PERCEPTION_VERSION,
    Observation,
    UIElement,
    app_fingerprint,
)
from understudy.models.result import (
    BusinessOutcome,
    Escalated,
    FailureCategory,
    HardFailure,
    ReplayResult,
    Success,
)
from understudy.replay import outcomes, recovery
from understudy.replay.recovery import TriggerContext
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    PolicyDenied,
    PolicyGate,
    decision_context,
    load_policy,
    reason_code_for_decision,
)
from understudy.safety.redact import mint_safe_id
from understudy.surface.base import Action, Click, Navigate, ReadText, Select, Type
from understudy.surface.locator import (
    Resolution,
    ResolutionStrategy,
    TargetDescriptor,
    drift_delta,
    resolve,
)
from understudy.surface.web import WebSurface

_PolicyException = (PolicyDenied, EscalationRequired, NavigationBlocked)

# A generous run-level ceiling on TOTAL recovery attempts (across every step, every rule),
# independent of any one rule's own max_attempts. This guards against a genuinely runaway trigger
# (one that never stops firing), not against a legitimate multi-step recovery: measured against
# the fixture's own transient_failure injection, a single step recovering from a fresh, never-
# before-visited path can plausibly need on the order of 3-4 retries, and a multi-step flow can
# hit more than one fresh path, so this needs real headroom above any single rule's own cap.
_MAX_RECOVERY_ATTEMPTS_PER_RUN = 40


def _with_expiry_note(observed: str, request_id: str) -> str:
    """Append an escalation's expiry to an ALREADY-BUILT HardFailure's `observed`, never replace
    it (F1, Phase 11 round 2). `observed` at the two call sites below is the original refusal
    reason -- e.g. "risk_replay: RISKY_IRREVERSIBLE refused ... requires capability status
    'approved' and allow_risky=True" -- which is exactly the "why" a calling agent needs to decide
    what to do next. That reason survives in the intervention record and in run.jsonl, but the
    RETURNED RESULT is the only thing the caller actually sees; overwriting it with just "nobody
    answered" threw the why away at the one place it mattered most.
    """
    return (
        f"{observed} -- escalated as intervention {request_id!r}, which expired with no "
        "operator resolution"
    )


def _missing_required_params(capability: Capability, params: dict[str, Any]) -> list[str]:
    """Every declared `InputParam` the capability requires that the caller's own `params` did not
    supply. Checked once, up front, before anything else runs (R3: replay is given "an artifact
    AND input parameters"; a required one missing is not a runtime condition to discover mid-run,
    it is an invalid request)."""
    return sorted(
        param.name for param in capability.inputs if param.required and param.name not in params
    )


def _is_integer(value: Any) -> bool:
    # `bool` is a subclass of `int` in Python, so `True == 1` would otherwise silently pass an
    # integer check it should fail -- excluded first, deliberately, on every numeric predicate.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and re.fullmatch(r"-?\d+", value) is not None


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
}


def _param_type_error(name: str, declared_type: str, value: Any) -> str | None:
    """Does `value` satisfy `declared_type` (a JSON-Schema-ish type name)? `integer`/`number` also
    accept a numeric STRING (a JSON caller may legitimately send either); `bool` is deliberately
    never accepted where an int/float is declared (`_is_integer`/`_is_number` above). An
    undeclared/unknown JSON-schema type name has no registered check and is left alone (returns
    None: nothing to check against)."""
    check = _TYPE_CHECKS.get(declared_type)
    if check is None or check(value):
        return None
    return f"parameter {name!r} declared type {declared_type!r} but got {value!r}"


def _param_type_errors(capability: Capability, params: dict[str, Any]) -> list[str]:
    """Type errors for every SUPPLIED parameter (a missing one is `_missing_required_params`'s job,
    checked separately). A bad type is a CALLER ERROR (INVALID_PARAMS), never a run failure --
    checked before any browser launches, same as the missing-required check."""
    errors: list[str] = []
    for param in capability.inputs:
        if param.name not in params:
            continue
        error = _param_type_error(param.name, param.type, params[param.name])
        if error:
            errors.append(error)
    return errors


def _resolve_param(name: str, params: dict[str, Any]) -> str:
    """The one place a declared input parameter's caller-supplied value becomes a literal string
    -- used by `_resolve_step_value` (a Step's own `ParamRef`) and by `_interpolate` (every `:name`
    placeholder, wherever it appears), so nothing in this module can disagree about what a given
    name means. Raises with a debuggable message (never a bare `KeyError`) rather than crashing if
    `name` was never validated present -- reachable only for an OPTIONAL param a step still
    references but the caller omitted, since every REQUIRED param is already checked present
    before any step runs (`_missing_required_params`).
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


def _interpolate(text: str, capability: Capability, params: dict[str, Any]) -> str:
    """ONE SHARED INTERPOLATION PATH: replace every `:name` placeholder record/canonicalize.py and
    record/recorder.py's own descriptor-parameterization stage embed -- in a Checkpoint's value, a
    TargetDescriptor's `name`, or a TargetDescriptor's `frame_path` segment -- with the caller's
    resolved parameter. Phase 8 shipped two interpolation paths that disagreed (docs/adr/0013); a
    single regex pass, reused by every caller in this module, is what makes that impossible again.

    ONE regex pass over `:(\\w+)`, never a sequence of `str.replace` calls keyed off each declared
    param name: an ordered sequence is prefix-unsafe (with params `id` and `id_long`, replacing
    `:id` first also corrupts `:id_long`'s own leading `:id`). `\\w+` always matches the full
    identifier greedily, so `:id_long` resolves as one name in one step. A declared param absent
    from `params` (only possible for an optional one; every required one is validated present
    before any step runs) is left un-interpolated, so the placeholder correctly fails to match a
    real value at the checkpoint/locator stage instead of crashing here.
    """
    declared = {param.name for param in capability.inputs}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in declared and name in params:
            return _resolve_param(name, params)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def _resolve_checkpoint(
    checkpoint: Checkpoint, capability: Capability, params: dict[str, Any]
) -> Checkpoint:
    """A checkpoint's `value`, interpolated via `_interpolate` -- kept as its own named function
    (rather than inlined at every call site) because tests/test_phase8.py already depends on this
    exact name."""
    value = _interpolate(checkpoint.value, capability, params)
    if value == checkpoint.value:
        return checkpoint
    return checkpoint.model_copy(update={"value": value})


def _interpolate_descriptor(
    target: TargetDescriptor, capability: Capability, params: dict[str, Any]
) -> TargetDescriptor:
    """A TargetDescriptor's `name` and every `frame_path` segment, both interpolated via the SAME
    `_interpolate` a checkpoint's value goes through -- a descriptor whose recorded name embeds a
    goal literal (record/recorder.py's own name-parameterization stage) becomes ":member_id.*",
    and here `:member_id` becomes the caller's actual value, e.g. "22222.*"."""
    name = _interpolate(target.name, capability, params)
    frame_path = [_interpolate(segment, capability, params) for segment in target.frame_path]
    if name == target.name and frame_path == target.frame_path:
        return target
    return target.model_copy(update={"name": name, "frame_path": frame_path})


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


def _stale_perception_note(capability: Capability) -> str:
    """Only appended to a locator failure's `observed` message when `_classify_locator_failure`
    returned STALE_PERCEPTION -- says plainly which two versions disagree and what to do about it,
    rather than leaving a caller to go work that out from the bare category name."""
    return (
        f"this artifact was recorded under perception_version="
        f"{capability.provenance.perception_version}, but this build runs perception_version="
        f"{PERCEPTION_VERSION}; re-record this capability"
    )


_DRIFT_NAME_STRATEGIES = frozenset(
    {
        ResolutionStrategy.ROLE_NAME_EXACT,
        ResolutionStrategy.ROLE_NAME_NORMALIZED,
        ResolutionStrategy.ROLE_NAME_SCOPED,
    }
)


def _drift_reason(descriptor: TargetDescriptor, resolution: Resolution) -> str | None:
    """Both clauses below describe one concept: this target resolved on weaker evidence than it
    was recorded with. Neither assumes a baseline that was never actually measured, and BOTH can
    apply to the same resolution at once -- on a tenant whose vocabulary renamed a field the
    recorder also gave a positional fallback, the strategy that wins both ranks weaker AND stops
    matching by name, and reporting only the first would hide the second, which is the whole
    tenant-vocabulary case this phase exists to surface. Every applicable clause is returned,
    joined with "+", in the precedence order below; a resolution where only one clause applies
    still returns that one clause alone, unchanged from before this phase.

    Clause 1 ("rank_regressed"): `descriptor.recorded_rank` is not None (every capability built by
    the current recorder carries it) and the strategy that actually won this time ranks weaker
    (`drift_delta(recorded_rank, resolution.rank) > 0`). The precise signal, but only available
    once a descriptor actually carries a measured `recorded_rank`.

    Clause 2 ("name_no_longer_matched"): the descriptor recorded a non-empty name, and the
    strategy that actually won is not one of the three name-matching rungs (ROLE_NAME_EXACT/
    ROLE_NAME_NORMALIZED/ROLE_NAME_SCOPED) -- the descriptor resolved on weaker evidence than it
    was recorded with, with no baseline required at all. `recorded_rank` is None on every artifact
    recorded before that field existed, so clause 1 can never fire for those; clause 2 is what
    makes drift reachable for them, instead of inventing a guessed baseline (rank 1) that would
    cry wolf on any step that legitimately resolved at rank > 1 when it was recorded. It is also
    exactly the case that matters regardless of `recorded_rank`: a name that matched at record
    time and matches nothing now is the definition of drift, and a descriptor carrying an ordinal
    or a relational hint would otherwise resolve POSITIONALLY in total silence without it.
    """
    clauses: list[str] = []
    if descriptor.recorded_rank is not None and resolution.rank is not None:
        if drift_delta(descriptor.recorded_rank, resolution.rank) > 0:
            clauses.append("rank_regressed")
    if descriptor.name and resolution.strategy_used not in _DRIFT_NAME_STRATEGIES:
        clauses.append("name_no_longer_matched")
    return "+".join(clauses) if clauses else None


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


_STEP_ACTION_VERBS: dict[str, str] = {
    "click": "click",
    "type": "type into",
    "select": "select in",
    "read_text": "extract from",
    "extract": "extract from",
}


def _describe_step(step: Step, interpolated_name: str | None) -> str:
    """Render `step` as something a person acting on a failure can use -- e.g.
    `step 2 (click 'Login')` or `step 6 (extract from generic 'Savings Balance')` -- rather than a
    bare step number, which by itself tells a reader nothing about what the automation was doing.

    `interpolated_name` is the caller's own already-interpolated descriptor name (e.g. `'22222.*'`
    once `:member_id` has been substituted), used in preference to `step.target`'s recorded,
    pre-interpolation name (`':member_id.*'`): a reader needs what was actually looked for, not the
    placeholder. Degrades to a bare `step N (action)` when `step.target` is None (a navigate has no
    target to name).
    """
    if step.target is None:
        return f"step {step.index} ({step.action})"
    name = interpolated_name if interpolated_name is not None else step.target.name
    verb = _STEP_ACTION_VERBS.get(step.action, step.action)
    if step.action in ("read_text", "extract"):
        return f"step {step.index} ({verb} {step.target.role} {name!r})"
    return f"step {step.index} ({verb} {name!r})"


def _describe_checkpoint(checkpoint: Checkpoint) -> str:
    """Render one Checkpoint readably -- what it required, in plain words -- so a step's
    postcondition or the capability's own success checkpoint reads as a sentence rather than a bare
    `kind`/`target`/`value` dump. `target` is only surfaced for the two kinds where it actually
    carries information (`element_present`'s role, `value_equals`'s `"role:name"`); for
    `text_present`/`url_matches` it is documentation-only (Checkpoint's own docstring: `"page"` /
    `"any_frame"`), so showing it would add noise, not information.
    """
    if checkpoint.kind == "text_present":
        return f"text {checkpoint.value!r} to be present"
    if checkpoint.kind == "url_matches":
        return f"the URL {checkpoint.value!r} to be loaded"
    if checkpoint.kind == "element_present":
        return f"a {checkpoint.target} named {checkpoint.value!r} to be present"
    if checkpoint.kind == "value_equals":
        role, _, name = checkpoint.target.partition(":")
        return f"{role} {name!r} to equal {checkpoint.value!r}"
    return f"{checkpoint.kind} ({checkpoint.target!r} = {checkpoint.value!r})"  # pragma: no cover


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
    `evidence_refs` is attached by the caller (only it has a `surface` to capture evidence from).
    """
    if not checkpoint_verified:
        return HardFailure(
            step_id=steps_run,
            category=FailureCategory.CHECKPOINT_NOT_VERIFIED,
            expected=f"success checkpoint: {_describe_checkpoint(success_checkpoint)}",
            observed="the recorded success checkpoint was not satisfied in the final observation",
        )
    return Success(outputs=outputs, steps_run=steps_run, duration_ms=duration_ms)


def _resolve_escalation(
    broker: SessionBroker,
    logger: EvidenceLogger,
    surface: WebSurface,
    capability: Capability,
    reason_code: ReasonCode,
    step_id: int | None,
    what_it_tried: str,
    what_it_observed: str,
    context: dict[str, str],
    ttl_seconds: float,
) -> tuple[str, InterventionResolution | None]:
    """Build one InterventionRequest and hand it to `SessionBroker.escalate()`
    (escalation/control.py), the ONE shared entry point both execution paths raise an
    intervention through. Returns `(request.id, resolution)`; `resolution` is None on expiry.

    F1: the screenshot is taken from THIS SAME `observation` (never a second, fresher one), so
    the request's own observation and its screenshot describe the same moment -- see
    agent/loop.py's own `_resolve_escalation` docstring for the full argument. F2: `context` is
    the caller's own flat, already-plain-string detail for the reason code being raised --
    required, not defaulted, so a call site cannot silently fall back to an empty dict.
    """
    observation = surface.observe()
    screenshot_path = logger.escalation_screenshot(surface, observation)
    now = datetime.now(UTC)
    request = InterventionRequest(
        # mint_safe_id (safety/redact.py) -- a bare hex slice can randomly come out all-digit
        # and get silently rewritten to "[REDACTED]" by R2 the first time it is serialized,
        # breaking every later store lookup by this id. See its docstring for the measured
        # probability and why a fixed prefix is the whole-class fix, not a per-field exemption.
        id=mint_safe_id(prefix="esc", length=10),
        run_id=logger.run_id,
        capability_id=capability.capability_id,
        goal=capability.description or capability.name,
        step_id=step_id,
        reason_code=reason_code,
        what_it_tried=what_it_tried,
        what_it_observed=what_it_observed,
        observation=observation,
        screenshot_path=(
            screenshot_path.relative_to(logger.dir).as_posix()
            if screenshot_path is not None
            else None
        ),
        context=context,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    return request.id, broker.escalate(request, logger)


class _RunState:
    """Mutable, in-memory-only recovery bookkeeping shared across every step's own recovery loop
    -- never serialized, unlike everything else this module touches. Two independent things:
    which of `surface.dialog_events` are "new" since the last check (a run-long cursor, because a
    native dialog can fire mid-action, asynchronously, not aligned to any one step's own
    evaluation), and how many recovery attempts this WHOLE RUN has spent (the run-level cap,
    independent of any one rule's own per-step max_attempts).

    Task C2 adds three more fields, all escalation bookkeeping for the WHOLE run rather than one
    step: `resumed` (has ANY escalation this run raised been rescued via `approved`/`took_control`
    at least once -- what `Escalated.resumed` reports), and the id/decision of the LAST
    intervention raised, for the same reason discovery's `RunOutcome` carries them (a caller needs
    to see a human was involved even in a run that goes on to finish normally afterward).
    """

    def __init__(self) -> None:
        self._dialog_cursor = 0
        self._total_recovery_attempts = 0
        self.resumed = False
        self.last_intervention_id: str | None = None
        self.last_resolution: str | None = None

    def new_dialogs(self, surface: WebSurface) -> list[dict[str, Any]]:
        events = list(getattr(surface, "dialog_events", []))
        fresh = events[self._dialog_cursor :]
        self._dialog_cursor = len(events)
        return fresh

    def budget_left(self) -> bool:
        return self._total_recovery_attempts < _MAX_RECOVERY_ATTEMPTS_PER_RUN

    def record_attempt(self) -> None:
        self._total_recovery_attempts += 1


class _DialogPolicy:
    """Installed on the surface BEFORE the first navigate (`WebSurface.dialog_policy`): consults
    the remaining budget of the capability's own `dismiss_dialog`-action rule (if any) and returns
    "dismiss" while budget remains, "none" once it is spent -- so spending the budget genuinely
    stops dismissing, and the NEXT native dialog really blocks and becomes UNHANDLED_DIALOG. No
    `dismiss_dialog` rule at all means budget 0: never a blanket auto-dismiss.

    `max_attempts` on a RecoveryRule is a PER-STEP budget everywhere else in this engine
    (`attempts_by_rule` in `_run_step` is a local, reset for every step) -- this was the one place
    that disagreed, because the policy was installed once, before the first navigate, with no
    step boundary for a dialog (which fires asynchronously) to hang off. `reset()` gives it one:
    `_run_step` calls it at the top of every step, so `max_attempts` means "per step" here too, the
    same as everywhere else. The run-level cap (`_MAX_RECOVERY_ATTEMPTS_PER_RUN`) is untouched by
    this and is still what stops a genuinely runaway page.

    Task C3: during a handoff (the control token is anything other than AUTOMATION) this stands
    down BEFORE the budget is even consulted, so standing down does not silently spend it either.
    A `window.confirm` a human was escalated to DECIDE must reach that human, not be dismissed out
    from under them by this rule. A human answering the confirm produces NO captured `HumanAction`
    (surface/web.py's `install_human_action_capture` cannot see a native dialog at all -- it is not
    a DOM event); the evidence for it is this same dialog event's own `handled` value being
    `"none"` rather than `"dismiss"`.
    """

    def __init__(self, max_attempts: int, broker: SessionBroker | None = None) -> None:
        self._max_attempts = max_attempts
        self._remaining = max_attempts
        self._broker = broker

    def __call__(self, event: dict[str, Any]) -> str:
        if self._broker is not None and self._broker.state().state != ControlState.AUTOMATION:
            return "none"
        if self._remaining > 0:
            self._remaining -= 1
            return "dismiss"
        return "none"

    def reset(self) -> None:
        self._remaining = self._max_attempts


def _make_dialog_policy(
    capability: Capability, broker: SessionBroker | None = None
) -> _DialogPolicy:
    rule = next((r for r in capability.recovery_rules if r.action == "dismiss_dialog"), None)
    return _DialogPolicy(rule.max_attempts if rule is not None else 0, broker=broker)


def _make_reauth(
    capability: Capability,
    params: dict[str, Any],
    surface: WebSurface,
    gate: PolicyGate,
    prefix_len: int,
) -> Callable[[], str]:
    """The engine supplies THIS callback to replay/recovery.py's `reauth` action, because only the
    engine can re-execute recorded steps: re-navigate to the entry point, then re-run
    `capability.steps[:prefix_len]` (the login prefix, models/artifact.py's `login_prefix_len`)
    through the ordinary resolve+dispatch path -- never a fork of it. Every re-executed step's own
    `act` event is logged as usual (it carries the recorded rationale, so the log still explains
    itself), plus the reauth's own navigate.
    """

    def _reauth() -> str:
        gate.dispatch(
            surface,
            Navigate(url=capability.target.entry_point),
            context={
                "tool": "navigate",
                "rationale": (
                    "recovery: session was lost mid-flow; re-navigating to the capability's "
                    "recorded entry point to log back in"
                ),
            },
        )
        for step in capability.steps[:prefix_len]:
            if step.target is None:
                continue
            observation = surface.observe()
            target = _interpolate_descriptor(step.target, capability, params)
            resolution = resolve(target, observation)
            if resolution.element is None:
                raise RuntimeError(
                    f"reauth: could not re-resolve step {step.index}'s target while re-running "
                    "the recorded login prefix"
                )
            action = _action_for_step(step, resolution.element.node_id, params)
            gate.dispatch(
                surface,
                action,
                context={
                    "tool": step.action,
                    "rationale": (
                        f"recovery: re-executing recorded step {step.index} ({step.rationale}) "
                        "to log back in after session loss"
                    ),
                    "step_id": step.index,
                    "resolution_strategy": (
                        resolution.strategy_used.value
                        if resolution.strategy_used is not None
                        else None
                    ),
                    "actual_rank": resolution.rank,
                    "recorded_rank": target.recorded_rank,
                },
                element=resolution.element,
            )
        return f"re-navigated to the entry point and re-ran the first {prefix_len} recorded step(s)"

    return _reauth


def _outcome_is_terminal(capability: Capability, code: str) -> bool:
    return next((o.terminal for o in capability.known_outcomes if o.code == code), True)


def _run_step(
    step: Step,
    capability: Capability,
    params: dict[str, Any],
    surface: WebSurface,
    gate: PolicyGate,
    logger: EvidenceLogger,
    run_state: _RunState,
    reauth: Callable[[], str],
    prefix_len: int,
    dialog_policy: _DialogPolicy,
    broker: SessionBroker | None = None,
    intervention_ttl_s: float = 900,
) -> HardFailure | BusinessOutcome | Escalated | tuple[Observation, str | None]:
    """Run one recorded step to a terminal answer: a HardFailure, BusinessOutcome, or Escalated
    that ends the whole replay, or `(after_observation, extracted_value)` once this step is
    genuinely done (extracted_value is None for every non-"extract" step).

    A recovery loop lives here: `should_dispatch` starts True (the recorded action has not run
    yet) and stays True only immediately after a `reauth` recovery (the session died, so the
    step's own action must run again in the fresh session); every OTHER recovery action
    (`retry`/`wait`/`dismiss`/`dismiss_dialog`) leaves it False, because the action already ran --
    only the PAGE needed recovering, so only (e)/(f) below repeat, never the locator resolve or
    the dispatch itself.

    Task C2: escalation is enabled by the presence of `broker`, nothing else -- a call with
    broker=None (every test predating this task) is byte-for-byte the failure this function
    already produced. `_rescue` and `_resume` (below) are the escalate-then-decide machinery
    shared by the three call sites with no specific refused action to re-dispatch on approval (a
    locator failure, an unrecovered condition); `_handle_dispatch_policy_exception` is the same
    idea for the two call sites that DO have one (this step's own dispatch raising
    PolicyDenied/EscalationRequired), where "approved" means something concrete: re-dispatch the
    SAME action.
    """
    # The native-dialog budget is per STEP (same as every other rule's max_attempts,
    # attempts_by_rule below): reset it here, once, before this step's own action or any of its
    # recovery attempts can raise a dialog.
    dialog_policy.reset()

    observation = surface.observe()
    before_path = logger.screenshot(surface, step.index, "before", observation)

    should_dispatch = True
    attempts_by_rule: dict[str, int] = {}
    result_text: str | None = None
    # Set the first time (b) below resolves this step's target, and never reset on later passes
    # through the loop (a retry/wait/dismiss pass leaves `should_dispatch` False and so never
    # re-enters (b)) -- carried forward so a later branch ((f)'s unrecovered-condition check, or
    # the postcondition check) can still describe THIS step by its actual, interpolated name.
    # Stays None for a navigate step (step.target is None), which `_describe_step` degrades for.
    interpolated_target: TargetDescriptor | None = None
    # Set by (b) below the first time this step's target actually resolves; never reset on a
    # later pass through the loop, same as `interpolated_target` above. `_build_step_context()`
    # reads it so the SAME rank data (the strategy that actually won, its rank, and the rank this
    # descriptor was recorded at) rides into every dispatch of this step's own action -- for every
    # step that resolves a target, not only one that drifted (B5, Phase 12): a `locator_drift`
    # event alone gives no signal for the steps that did NOT drift, and a rank distribution needs
    # both.
    resolution: Resolution | None = None

    def _build_step_context() -> dict[str, Any]:
        context: dict[str, Any] = {
            "tool": step.action,
            "rationale": step.rationale,
            "step_id": step.index,
        }
        if resolution is not None:
            context["resolution_strategy"] = (
                resolution.strategy_used.value if resolution.strategy_used is not None else None
            )
            context["actual_rank"] = resolution.rank
            context["recorded_rank"] = (
                interpolated_target.recorded_rank if interpolated_target is not None else None
            )
        return context

    def _resume() -> HardFailure | BusinessOutcome | Escalated | tuple[Observation, str | None]:
        """RESUME IS NOT BLIND (task C2). Called once a human has taken control of THIS step and
        handed it back (or approved with nothing concrete to re-dispatch). Re-observe, then: if
        the step's own postcondition NOW holds, the human already did this step's work -- skip
        it. Otherwise, if the step's precondition holds (or it has none), it is still safe to
        attempt from the top -- retry the whole step. Otherwise resuming would be a guess neither
        checkpoint can justify: escalate ONE more time as unrecoverable_condition, and whatever
        THAT resolves to is final -- not a third round of this same decision.
        """
        assert broker is not None  # only ever called after a broker has already resolved once
        after_observation = surface.observe()
        if step.postcondition is not None:
            resolved_post = _resolve_checkpoint(step.postcondition, capability, params)
            if checkpoint_satisfied(after_observation, resolved_post):
                logger.event(
                    "step_skipped_after_handoff",
                    step_id=step.index,
                    note="the step's own postcondition was already satisfied when control returned",
                )
                return after_observation, None

        precondition_ok = step.precondition is None or checkpoint_satisfied(
            after_observation, _resolve_checkpoint(step.precondition, capability, params)
        )
        if precondition_ok:
            return _run_step(
                step,
                capability,
                params,
                surface,
                gate,
                logger,
                run_state,
                reauth,
                prefix_len,
                dialog_policy,
                broker,
                intervention_ttl_s,
            )

        request_id, resolution = _resolve_escalation(
            broker,
            logger,
            surface,
            capability,
            ReasonCode.UNRECOVERABLE_CONDITION,
            step.index,
            what_it_tried=f"resumed after a handoff at {_describe_step(step, None)}",
            what_it_observed=(
                "neither the step's postcondition nor its precondition holds after the handoff; "
                "automation cannot safely guess whether to skip or retry this step"
            ),
            context={
                "capability_id": capability.capability_id,
                "step_index": str(step.index),
            },
            ttl_seconds=intervention_ttl_s,
        )
        run_state.last_intervention_id = request_id
        if resolution is None:
            run_state.last_resolution = "expired"
            return HardFailure(
                step_id=step.index,
                category=FailureCategory.ESCALATION_UNRESOLVED,
                expected=f"a human to resolve intervention {request_id!r} before it expired",
                observed=f"intervention {request_id!r} expired with no operator resolution",
            )
        run_state.last_resolution = resolution.action_taken
        return Escalated(
            intervention_id=request_id, resolution=resolution.action_taken, resumed=True
        )

    def _rescue(
        hard_failure: HardFailure,
        reason_code: ReasonCode,
        what_it_tried: str,
        what_it_observed: str,
        context: dict[str, str],
    ) -> HardFailure | BusinessOutcome | Escalated | tuple[Observation, str | None]:
        """Try one human escalation before accepting `hard_failure` (already fully built, with its
        own evidence already captured and logged) as this step's final answer. Used by the call
        sites with no specific refused action to re-dispatch on approval (a locator failure, an
        unrecovered condition) -- "approved" there is treated the same as "took_control": resume,
        not repeat, is the honest answer when there is nothing concrete to redo.

        `context` (F2) is the caller's own flat detail -- at minimum the capability id and this
        step's index, plus `trigger_reason` when the caller has one (recovery's own `why`).
        """
        if broker is None:
            return hard_failure
        request_id, resolution = _resolve_escalation(
            broker, logger, surface, capability, reason_code, step.index, what_it_tried,
            what_it_observed, context, intervention_ttl_s,
        )
        run_state.last_intervention_id = request_id
        if resolution is None:
            run_state.last_resolution = "expired"
            return hard_failure.model_copy(
                update={
                    "category": FailureCategory.ESCALATION_UNRESOLVED,
                    "observed": _with_expiry_note(hard_failure.observed, request_id),
                }
            )
        run_state.last_resolution = resolution.action_taken
        if resolution.action_taken == "rejected":
            return Escalated(
                intervention_id=request_id, resolution="rejected", resumed=run_state.resumed
            )
        run_state.resumed = True
        return _resume()

    def _handle_dispatch_policy_exception(
        exc: PolicyDenied | EscalationRequired, reason_code: ReasonCode
    ) -> Literal["fallthrough"] | HardFailure | BusinessOutcome | Escalated | tuple[
        Observation, str | None
    ]:
        """Task C2: escalate a PolicyDenied/EscalationRequired raised by THIS step's own dispatch
        (step (d), below) before accepting it as a hard failure. "approved" re-dispatches the
        SAME action (`nonlocal result_text`) and returns "fallthrough" so the caller's own while
        loop proceeds to steps (e)/(f) exactly as it would have on an ordinary successful dispatch
        -- there is exactly one post-dispatch bookkeeping path in this function, not a second copy
        of it. Every other outcome (no broker, rejected, expired, took_control) ends the step here.
        """
        nonlocal result_text
        reason = _policy_exception_reason(exc)
        refs = _capture_failure_evidence(logger, surface, step.index, before_path)
        logger.event(
            "hard_failure", step_id=step.index, category=FailureCategory.POLICY_DENIED.value,
            note=reason,
        )
        hard_failure = HardFailure(
            step_id=step.index,
            category=FailureCategory.POLICY_DENIED,
            expected=f"step {step.index} ({step.action}) to be permitted by policy",
            observed=reason,
            evidence_refs=refs,
        )
        if broker is None:
            return hard_failure
        request_id, resolution = _resolve_escalation(
            broker,
            logger,
            surface,
            capability,
            reason_code,
            step.index,
            what_it_tried=f"attempted step {step.index} ({step.action}): {reason}",
            what_it_observed="the policy gate refused it pending human input",
            context=decision_context(exc.decision),
            ttl_seconds=intervention_ttl_s,
        )
        run_state.last_intervention_id = request_id
        if resolution is None:
            run_state.last_resolution = "expired"
            return hard_failure.model_copy(
                update={
                    "category": FailureCategory.ESCALATION_UNRESOLVED,
                    "observed": _with_expiry_note(hard_failure.observed, request_id),
                }
            )
        run_state.last_resolution = resolution.action_taken
        if resolution.action_taken == "rejected":
            return Escalated(
                intervention_id=request_id, resolution="rejected", resumed=run_state.resumed
            )
        run_state.resumed = True
        if resolution.action_taken == "approved":
            # `broker.escalate()` (via `_resolve_escalation` above) already logged
            # `handoff_resumed` itself (round H) -- nothing left to log here.
            try:
                action = _action_for_step(step, node_id, params)
                result_text = gate.dispatch(
                    surface,
                    action,
                    context=_build_step_context(),
                    element=element,
                )
            except Exception as exc2:  # noqa: BLE001 - reported as a hard failure, never swallowed
                return HardFailure(
                    step_id=step.index,
                    category=FailureCategory.ACTION_FAILED,
                    expected=f"the approved retry of step {step.index} ({step.action}) to succeed",
                    observed=str(exc2),
                )
            return "fallthrough"
        # took_control
        return _resume()

    while True:
        node_id: str | None = None
        element: UIElement | None = None
        dispatch_exception: Exception | None = None

        if should_dispatch:
            if step.precondition is not None:
                # (a) An explicit condition check against the CURRENT observation -- never a
                # polling loop. No recorded step sets a precondition yet (models/artifact.py's own
                # docstring: populating it is future work), so this is unreached today; it exists
                # because the schema declares the field, and a check that never blocks is the
                # honest behaviour for a condition nothing has ever populated.
                resolved_pre = _resolve_checkpoint(step.precondition, capability, params)
                if not checkpoint_satisfied(observation, resolved_pre):
                    logger.event(
                        "precondition_not_satisfied",
                        step_id=step.index,
                        note="recorded precondition was not satisfied at this step's own start",
                    )

            if step.target is not None:
                # (b) interpolate + resolve
                interpolated_target = _interpolate_descriptor(step.target, capability, params)
                resolution = resolve(interpolated_target, observation)
                if resolution.element is None:
                    # docs/adr/0006: a failed resolution reports every rung's candidate count,
                    # not just "not found", so the failure is debuggable (R3).
                    observed = "; ".join(
                        f"{attempt.strategy.value}: {attempt.candidate_count} candidate(s)"
                        + (f" ({attempt.skipped_reason})" if attempt.skipped_reason else "")
                        for attempt in resolution.attempts
                    )
                    category = _classify_locator_failure(capability)
                    if category == FailureCategory.STALE_PERCEPTION:
                        observed = f"{_stale_perception_note(capability)}; {observed}"
                    refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                    logger.event(
                        "hard_failure",
                        step_id=step.index,
                        category=category.value,
                        note=observed,
                        resolution=resolution.model_dump(),
                    )
                    hard_failure = HardFailure(
                        step_id=step.index,
                        category=category,
                        expected=f"a unique element matching role={step.target.role!r} "
                        f"name={interpolated_target.name!r}",
                        observed=observed,
                        evidence_refs=refs,
                    )
                    return _rescue(
                        hard_failure,
                        ReasonCode.LOCATOR_UNRESOLVED,
                        what_it_tried=(
                            f"tried to resolve the target for step {step.index} ({step.action})"
                        ),
                        what_it_observed=observed,
                        context={
                            "capability_id": capability.capability_id,
                            "step_index": str(step.index),
                        },
                    )
                node_id = resolution.element.node_id
                element = resolution.element

                # (c) DRIFT: a signal, never a failure -- log and continue.
                drift_clause = _drift_reason(interpolated_target, resolution)
                if drift_clause is not None:
                    logger.event(
                        "locator_drift",
                        step_id=step.index,
                        recorded_name=step.target.name,
                        interpolated_name=interpolated_target.name,
                        # None (never a substituted guess) when this descriptor predates
                        # recorded_rank -- clause 2 is what still makes drift reachable for it.
                        recorded_rank=step.target.recorded_rank,
                        actual_rank=resolution.rank,
                        strategy=(
                            resolution.strategy_used.value
                            if resolution.strategy_used is not None
                            else None
                        ),
                        clause=drift_clause,
                    )

            # (d) dispatch
            try:
                action = _action_for_step(step, node_id, params)
                result_text = gate.dispatch(
                    surface,
                    action,
                    context=_build_step_context(),
                    element=element,
                )
            except PolicyDenied as exc:
                # Task C2, fixed by G1: this step's own dispatch was refused. The reason code is
                # derived from the refusing DECISION's own rule (reason_code_for_decision,
                # safety/policy.py), not hardcoded off the exception type -- replay's own risk
                # refusal ("risk_replay") raises this exact exception type too, and must reach the
                # operator console's per-action approve/reject flow the same as
                # EscalationRequired below does, not the plain "policy_refused" every other rule
                # (allowlist/action_type/role/forbidden_text) gets.
                outcome = _handle_dispatch_policy_exception(
                    exc, reason_code_for_decision(exc.decision)
                )
                if not isinstance(outcome, str):
                    return outcome
                # "fallthrough": _handle_dispatch_policy_exception already re-dispatched the same
                # action (an approved one-shot) and updated `result_text` itself -- proceed to
                # (e)/(f) below exactly as an ordinary successful dispatch would.
            except EscalationRequired as exc:
                # Task C2: this step's own RISKY_IRREVERSIBLE dispatch was refused pending human
                # approval (unreachable in replay today -- PolicyGate only raises this in
                # mode="discovery" -- but goes through the SAME mapping as PolicyDenied above so
                # the two paths cannot disagree if that ever changes).
                outcome = _handle_dispatch_policy_exception(
                    exc, reason_code_for_decision(exc.decision)
                )
                if not isinstance(outcome, str):
                    return outcome
            except NavigationBlocked as exc:
                # A navigation that left the allowlist means the session state is no longer
                # trustworthy (decision 59) -- not one of task C2's five escalation triggers, so
                # this stays exactly as it was: a POLICY_DENIED hard failure, no escalation.
                reason = _policy_exception_reason(exc)
                refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                logger.event(
                    "hard_failure",
                    step_id=step.index,
                    category=FailureCategory.POLICY_DENIED.value,
                    note=reason,
                )
                return HardFailure(
                    step_id=step.index,
                    category=FailureCategory.POLICY_DENIED,
                    expected=f"step {step.index} ({step.action}) to be permitted by policy",
                    observed=reason,
                    evidence_refs=refs,
                )
            except Exception as exc:
                # Goes through the SAME (f) machinery below before becoming ACTION_FAILED, so a
                # transient failure that raises is retried rather than reported as a hard failure.
                dispatch_exception = exc

        # (e) observe fresh, every pass. A native dialog Playwright's own dialog handler left
        # OPEN (native_dialog_unhandled: the run's dismiss budget was already spent) blocks the
        # renderer entirely -- measured live: `observe()` itself hangs for Playwright's own
        # action timeout (30s) and then raises, rather than returning a page that merely mentions
        # a dialog. That IS the UNHANDLED_DIALOG condition, just discovered by a different signal
        # (an observe() failure) than the ordinary path (a trigger over a successfully-observed
        # page) -- `surface.dialog_events` is updated by Playwright's own background event loop
        # the instant the dialog fires, independently of whether THIS foreground call is stuck, so
        # it is still readable even though observe() itself never returned.
        try:
            after_observation = surface.observe()
        except Exception as exc:
            blocking = [d for d in run_state.new_dialogs(surface) if d.get("handled") == "none"]
            if blocking:
                dialog = blocking[-1]
                refs = (
                    [before_path.relative_to(logger.dir).as_posix()]
                    if before_path is not None
                    else []
                )
                observed = f"{dialog.get('dialog_type')}: {dialog.get('message')} (unhandled)"
                logger.event(
                    "hard_failure",
                    step_id=step.index,
                    category=FailureCategory.UNHANDLED_DIALOG.value,
                    note=f"{observed}; the dialog also blocked further observation",
                )
                return HardFailure(
                    step_id=step.index,
                    category=FailureCategory.UNHANDLED_DIALOG,
                    expected="no unrecovered condition after this step's action",
                    observed=observed,
                    evidence_refs=refs,
                )
            refs = _capture_failure_evidence(logger, surface, step.index, before_path)
            logger.event(
                "hard_failure",
                step_id=step.index,
                category=FailureCategory.ACTION_FAILED.value,
                note=f"could not observe the surface after step {step.index}: {exc}",
            )
            return HardFailure(
                step_id=step.index,
                category=FailureCategory.ACTION_FAILED,
                expected=f"a fresh observation after step {step.index} ({step.action})",
                observed=str(exc),
                evidence_refs=refs,
            )

        # (f) EVALUATION ORDER: outcomes, then recovery, then the step's own postcondition.
        outcome_match = outcomes.evaluate(after_observation, capability)
        if outcome_match is not None:
            terminal = _outcome_is_terminal(capability, outcome_match.code)
            logger.event(
                "business_outcome",
                step_id=step.index,
                code=outcome_match.code,
                detector=outcome_match.detector,
                message=outcome_match.message,
                observed=outcome_match.observed,
                terminal=terminal,
            )
            if terminal:
                logger.screenshot(surface, step.index, "after", after_observation)
                return BusinessOutcome(
                    code=outcome_match.code,
                    message=outcome_match.message,
                    observed=outcome_match.observed,
                    outputs={},
                )
            # non-terminal: logged, and falls through to recovery/postcondition below.

        ctx = TriggerContext(
            observation=after_observation,
            step_index=step.index,
            login_prefix_len=prefix_len,
            last_navigation=getattr(surface, "last_navigation", "none"),
            new_dialogs=run_state.new_dialogs(surface),
        )

        recovered = False
        matched = recovery.match(capability, ctx)
        if matched is not None:
            rule, reason = matched
            step_attempts = attempts_by_rule.get(rule.id, 0)
            if step_attempts < rule.max_attempts and run_state.budget_left():
                attempt_number = step_attempts + 1
                attempts_by_rule[rule.id] = attempt_number
                run_state.record_attempt()
                try:
                    report = recovery.apply(
                        rule,
                        ctx,
                        surface=surface,
                        gate=gate,
                        attempt=attempt_number,
                        reauth=reauth,
                    )
                except _PolicyException as exc:
                    reason2 = _policy_exception_reason(exc)
                    refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                    logger.event(
                        "hard_failure",
                        step_id=step.index,
                        category=FailureCategory.POLICY_DENIED.value,
                        note=reason2,
                    )
                    return HardFailure(
                        step_id=step.index,
                        category=FailureCategory.POLICY_DENIED,
                        expected=f"recovery rule {rule.id!r} to be permitted by policy",
                        observed=reason2,
                        evidence_refs=refs,
                    )
                except Exception as exc:
                    refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                    logger.event(
                        "hard_failure",
                        step_id=step.index,
                        category=FailureCategory.ACTION_FAILED.value,
                        note=f"recovery rule {rule.id!r} itself raised: {exc}",
                    )
                    return HardFailure(
                        step_id=step.index,
                        category=FailureCategory.ACTION_FAILED,
                        expected=f"recovery rule {rule.id!r} to complete",
                        observed=str(exc),
                        evidence_refs=refs,
                    )
                # dismiss_dialog's own "recovery" already happened synchronously, in the browser's
                # dialog callback, before this code ever ran; if none of this batch was actually
                # dismissed (the run-level dialog budget was already spent), nothing was recovered
                # and this must fall through to unrecovered() below, not loop as if it had.
                actually_recovered = True
                if rule.action == "dismiss_dialog":
                    actually_recovered = any(
                        d.get("handled") == "dismiss" for d in ctx.new_dialogs
                    )
                if actually_recovered:
                    backoff_ms = (
                        recovery.backoff_ms_for_attempt(attempt_number)
                        if rule.action == "retry"
                        else None
                    )
                    logger.event(
                        "recovered",
                        step_id=step.index,
                        rule_id=rule.id,
                        trigger=rule.trigger,
                        attempt=attempt_number,
                        backoff_ms=backoff_ms,
                        result=report,
                        reason=reason,
                    )
                    should_dispatch = rule.action == "reauth"
                    if should_dispatch:
                        observation = surface.observe()
                    recovered = True

        if recovered:
            continue

        if dispatch_exception is not None:
            refs = _capture_failure_evidence(logger, surface, step.index, before_path)
            logger.event(
                "hard_failure",
                step_id=step.index,
                category=FailureCategory.ACTION_FAILED.value,
                note=str(dispatch_exception),
            )
            return HardFailure(
                step_id=step.index,
                category=FailureCategory.ACTION_FAILED,
                expected=f"step {step.index} ({step.action}) to execute",
                observed=str(dispatch_exception),
                evidence_refs=refs,
            )

        unrecovered_result = recovery.unrecovered(ctx)
        if unrecovered_result is not None:
            category, why = unrecovered_result
            refs = _capture_failure_evidence(logger, surface, step.index, before_path)
            logger.event(
                "hard_failure", step_id=step.index, category=category.value, note=why
            )
            step_desc = _describe_step(
                step, interpolated_target.name if interpolated_target is not None else None
            )
            resolved_postcondition = (
                _resolve_checkpoint(step.postcondition, capability, params)
                if step.postcondition is not None
                else None
            )
            expected = (
                f"{step_desc}: {_describe_checkpoint(resolved_postcondition)}"
                if resolved_postcondition is not None
                else f"{step_desc} to complete with no unrecovered condition"
            )
            hard_failure = HardFailure(
                step_id=step.index,
                category=category,
                expected=expected,
                observed=f"{category.value} detected: {why}",
                evidence_refs=refs,
            )
            # Task C2: SESSION_EXPIRED gets its own reason code; every other unrecovered
            # category (UNHANDLED_DIALOG, APP_ERROR) is the generic unrecoverable_condition.
            reason_code = (
                ReasonCode.SESSION_EXPIRED
                if category == FailureCategory.SESSION_EXPIRED
                else ReasonCode.UNRECOVERABLE_CONDITION
            )
            return _rescue(
                hard_failure,
                reason_code,
                what_it_tried=f"ran {step_desc}",
                what_it_observed=f"{category.value} detected: {why}",
                context={
                    "capability_id": capability.capability_id,
                    "step_index": str(step.index),
                    "trigger_reason": why,
                },
            )

        # D7 (Phase 5, extended Phase 6): the action just changed the page, so the 'after'
        # screenshot needs a FRESH observation -- already have one.
        logger.screenshot(surface, step.index, "after", after_observation)

        if step.postcondition is not None:
            resolved_postcondition = _resolve_checkpoint(step.postcondition, capability, params)
            if not checkpoint_satisfied(after_observation, resolved_postcondition):
                refs = _capture_failure_evidence(logger, surface, step.index, before_path)
                logger.event(
                    "hard_failure",
                    step_id=step.index,
                    category=FailureCategory.POSTCONDITION_FAILED.value,
                    note="postcondition not satisfied",
                )
                step_desc = _describe_step(
                    step, interpolated_target.name if interpolated_target is not None else None
                )
                missing_note = ""
                if resolved_postcondition.kind == "text_present":
                    missing_note = f"; required text {resolved_postcondition.value!r} not found"
                elif resolved_postcondition.kind == "element_present":
                    missing_note = (
                        f"; no {resolved_postcondition.target} named "
                        f"{resolved_postcondition.value!r} was present"
                    )
                observed = f"current URL(s): {', '.join(after_observation.urls)}{missing_note}"
                return HardFailure(
                    step_id=step.index,
                    category=FailureCategory.POSTCONDITION_FAILED,
                    expected=f"{step_desc}: {_describe_checkpoint(resolved_postcondition)}",
                    observed=observed,
                    evidence_refs=refs,
                )

        extracted = result_text if step.action == "extract" else None
        return after_observation, extracted


def replay(
    artifact_path: Path,
    params: dict[str, Any],
    policy_path: Path,
    allow_risky: bool = False,
    evidence_base_dir: str | Path = "evidence",
    intervention_store: InterventionStore | None = None,
    intervention_ttl_s: float = 900,
) -> ReplayResult:
    """Thin wrapper: load the recorded artifact from disk, then hand off to replay_resolved()
    below -- the one core every caller shares, including cli.py's `--overlay` path, which
    resolves a TenantOverlay against an in-memory Capability (models/artifact.py's
    resolve_for_tenant) and never writes the result back to disk.

    Named `replay`, not `replay_capability`, because `catalog/server.py` imports this exact
    function under the local alias `replay_capability` (`from understudy.replay.engine import
    replay as replay_capability`) -- keeping the two names apart avoids a reader mistaking one
    for the other.
    """
    capability = Capability.model_validate_json(artifact_path.read_text(encoding="utf-8"))
    return replay_resolved(
        capability,
        params,
        policy_path,
        allow_risky=allow_risky,
        evidence_base_dir=evidence_base_dir,
        intervention_store=intervention_store,
        intervention_ttl_s=intervention_ttl_s,
    )


def replay_resolved(
    capability: Capability,
    params: dict[str, Any],
    policy_path: Path,
    allow_risky: bool = False,
    evidence_base_dir: str | Path = "evidence",
    intervention_store: InterventionStore | None = None,
    intervention_ttl_s: float = 900,
) -> ReplayResult:
    """The core replay loop, over an ALREADY-RESOLVED, already-loaded Capability -- `replay()`
    above is the only caller that reads one from disk. Splitting this out (Phase 12) is what lets
    a tenant-resolved Capability (never itself a file under artifacts/) replay through the exact
    same engine a recorded artifact does, with no second implementation to keep in sync.
    """
    # ORDER OF OPERATIONS point 1: let UnknownDetector propagate UNCAUGHT. A capability naming a
    # detector or trigger this build does not know is a request that was never valid, not a run
    # that failed partway through -- there is no logger yet, on purpose, because there is nothing
    # to write evidence about for a request this invalid.
    outcomes.validate(capability)
    recovery.validate(capability)

    run_id = mint_safe_id()
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

    type_errors = _param_type_errors(capability, params)
    if type_errors:
        # A bad parameter is a CALLER ERROR, not a run failure -- checked before any browser
        # launches, same as the missing-required check above.
        result = HardFailure(
            step_id=None,
            category=FailureCategory.INVALID_PARAMS,
            expected="every supplied parameter to match its declared type",
            observed="; ".join(type_errors),
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
    surface = WebSurface(policy=policy, headless=False)
    # Escalation is enabled by the presence of a broker, nothing else (task C0): no
    # intervention_store given means broker=None, and every existing call to replay() (which
    # never passes one) behaves exactly as it did before this task.
    broker: SessionBroker | None = None
    if intervention_store is not None:
        broker = SessionBroker(surface, intervention_store, run_id=run_id, logger=logger)
    gate = PolicyGate(
        policy,
        logger,
        mode="replay",
        allow_risky=allow_risky,
        capability_status=capability.status,
        broker=broker,
    )
    # Install the dialog policy BEFORE the first navigate -- never a blanket auto-dismiss, only a
    # budgeted one (see _DialogPolicy). Kept as a typed local (not read back off surface.dialog_
    # policy, which is a bare Callable) so _run_step can call its own .reset() every step.
    # Task C3: the SAME broker also stands the dialog policy down during a handoff (any state
    # other than AUTOMATION), before its own per-step budget is even consulted.
    dialog_policy = _make_dialog_policy(capability, broker=broker)
    surface.dialog_policy = dialog_policy
    prefix_len = login_prefix_len(capability.steps, capability.target.entry_point)
    reauth = _make_reauth(capability, params, surface, gate, prefix_len)
    run_state = _RunState()
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
                "hard_failure",
                step_id=None,
                category=FailureCategory.POLICY_DENIED.value,
                note=reason,
            )
            result = HardFailure(
                step_id=None,
                category=FailureCategory.POLICY_DENIED,
                expected=(
                    f"the capability's recorded entry point ({capability.target.entry_point!r}) "
                    "to be permitted by policy"
                ),
                observed=reason,
                evidence_refs=refs,
            )
            return result
        except Exception as exc:
            # A dead app must never surface as a locator problem: name the entry point URL in
            # both `expected` and `observed`, not just "the recorded entry point".
            category = _classify_entry_navigate_failure(exc)
            reason = str(exc)
            refs = _capture_failure_evidence(logger, surface, 0, None)
            logger.event("hard_failure", step_id=None, category=category.value, note=reason)
            result = HardFailure(
                step_id=None,
                category=category,
                expected=(
                    f"the capability's recorded entry point ({capability.target.entry_point!r}) "
                    "to be reachable"
                ),
                observed=f"navigating to {capability.target.entry_point!r} failed: {reason}",
                evidence_refs=refs,
            )
            return result

        # Phase 12 (B3): a coarse vendor-version drift signal, recomputed from the entry screen
        # and compared against what this artifact recorded. A mismatch WARNS -- it never changes
        # the result kind and never gates replay, the same non-gating stance PERCEPTION_VERSION
        # already takes. Best-effort: a failure here must never replace the real replay outcome
        # with an unrelated exception, so it is caught and logged, not raised.
        try:
            entry_fingerprint = app_fingerprint(surface.observe())
        except Exception as exc:
            logger.event("app_fingerprint_check_failed", note=str(exc))
        else:
            recorded_fingerprint = capability.target.app_fingerprint
            if recorded_fingerprint is None:
                # Absent on the artifact -- nothing to compare, never a guess.
                logger.event(
                    "app_fingerprint_check", status="no_baseline", actual=entry_fingerprint
                )
            elif recorded_fingerprint == entry_fingerprint:
                logger.event("app_fingerprint_check", status="match", actual=entry_fingerprint)
            else:
                logger.event(
                    "app_fingerprint_mismatch",
                    status="mismatch",
                    recorded=recorded_fingerprint,
                    actual=entry_fingerprint,
                    note=(
                        "the entry screen's structure differs from the recording "
                        "(frame count, control mix, or title/heading text changed); "
                        "this is a warning only and the run continued"
                    ),
                )

        after_observation: Observation | None = None
        for step in capability.steps:
            step_result = _run_step(
                step,
                capability,
                params,
                surface,
                gate,
                logger,
                run_state,
                reauth,
                prefix_len,
                dialog_policy,
                broker,
                intervention_ttl_s,
            )
            if isinstance(step_result, tuple):
                after_observation, extracted = step_result
                if step.action == "extract":
                    outputs[_resolve_step_value(step.value, params) or f"output_{step.index}"] = (
                        extracted or ""
                    )
                continue
            result = step_result
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
