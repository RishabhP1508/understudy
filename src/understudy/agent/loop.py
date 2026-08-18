"""observe -> decide -> act: the discovery agent's loop.

The model never declares its own success. verify_checkpoint() is a deterministic re-observe and
check; it is called from the one 'finish' branch below, before the loop can return a verified
outcome, and there is no early return in that branch before the call.
tests/test_constraints.py (invariant 5) enforces that shape by walking loop.py's AST.

Seven stopping conditions (RunStatus below) replace a single step cap. `no_progress` and
`dead_end` are deliberately different: `no_progress` fires while actions ARE dispatching and the
observation's structure is not moving; `dead_end` fires when actions are NOT dispatching at all,
because the proposed target cannot be resolved and the model offers no working alternative. See
docs/adr/0010-diff-observations-and-stopping-conditions.md.
"""

from __future__ import annotations

import difflib
import json
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from understudy.agent.prompts import SYSTEM_PROMPT
from understudy.agent.tools import ALL_TOOLS
from understudy.escalation.control import SessionBroker
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMClient
from understudy.models.artifact import Checkpoint, checkpoint_satisfied
from understudy.models.intervention import InterventionRequest, InterventionResolution, ReasonCode
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    PolicyDenied,
    PolicyGate,
    decision_context,
    reason_code_for_decision,
)
from understudy.safety.redact import mint_safe_id
from understudy.surface.base import Action, Click, Navigate, ReadText, Select, Surface, Type
from understudy.surface.locator import describe


class RunStatus(StrEnum):
    """Every way a discovery run can end. This is documentation order, not the priority a single
    round checks in -- see the per-round checks in run() for the actual evaluation order."""

    GOAL_VERIFIED = "goal_verified"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    NO_PROGRESS = "no_progress"
    LOOP_DETECTED = "loop_detected"
    DEAD_END = "dead_end"
    ESCALATION = "escalation"


class RunOutcome(BaseModel):
    status: RunStatus
    run_id: str
    steps_executed: int
    rounds: int
    rejected_turns: int
    outputs: dict[str, str] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    # None on a run that never escalated. Otherwise the LAST intervention this run raised and
    # what the operator decided (Phase 10, task C) -- so cli.py and record/recorder.py can see a
    # human was involved even on a run that went on to complete normally afterward.
    intervention_id: str | None = None
    resolution: str | None = None


def verify_checkpoint(surface: Surface, checkpoint: dict[str, Any] | Checkpoint) -> bool:
    """Deterministic, no model: re-observe and check via the shared checkpoint_satisfied().

    Accepts the raw dict the model's finish call returns, or an already-typed Checkpoint.
    """
    if isinstance(checkpoint, dict):
        try:
            checkpoint = Checkpoint.model_validate(checkpoint)
        except ValidationError:
            return False
    return checkpoint_satisfied(surface.observe(), checkpoint)


def _index_to_node_id(observation: Observation, index: Any) -> str:
    is_plain_int = isinstance(index, int) and not isinstance(index, bool)
    if not is_plain_int or not 0 <= index < len(observation.elements):
        raise ValueError(f"index {index!r} is out of range for this observation")
    return observation.elements[index].node_id


def _screenshot_safely(
    logger: EvidenceLogger,
    surface: Surface,
    step: int,
    when: Literal["before", "after"],
    observation: Observation,
) -> None:
    # R5: at least one richer signal per step. A screenshot failure (browser closed mid-run,
    # a fake surface with no screenshot_bytes() in tests, etc.) must never abort a real discovery
    # run. `before` must describe the observation the decision was made from; `after` must
    # describe a FRESH observation taken once the action has run: masking `after` from the
    # pre-action observation would paint a box over pixels that no longer match what changed.
    try:
        logger.screenshot(surface, step, when, observation=observation)
    except Exception as exc:
        logger.event("screenshot_failed", step_id=step, note=str(exc))


def _build_action(name: str, args: dict[str, Any], observation: Observation) -> Action:
    if name == "navigate":
        return Navigate(url=args["url"])
    if name == "click":
        return Click(node_id=_index_to_node_id(observation, args["index"]))
    if name == "type":
        return Type(node_id=_index_to_node_id(observation, args["index"]), text=args["text"])
    if name == "select":
        return Select(node_id=_index_to_node_id(observation, args["index"]), value=args["value"])
    if name in ("read", "extract"):
        return ReadText(node_id=_index_to_node_id(observation, args["index"]))
    raise ValueError(f"unknown tool: {name}")


def _rationale_is_malformed(args: dict[str, Any]) -> bool:
    """D1: every tool argument named `rationale` is required, on all eight tools alike. A
    missing, non-string, or whitespace-only rationale is malformed; so is the redaction sentinel
    itself, which a live model must never be allowed to pass off as its own reasoning."""
    rationale = args.get("rationale")
    return not isinstance(rationale, str) or not rationale.strip() or rationale == "[REDACTED]"


def _build_diff(previous_render: str, current_render: str) -> str:
    """A unified diff (stdlib difflib) of two full render() outputs, labeled so the model knows
    it is seeing a delta and that lines it does not see are unchanged and still hold. Only ever
    called when observation.digest() proves the element list itself is identical to last turn
    (see the digest_unchanged check in run()), so every [index] the model already has is still
    valid -- the diff never has to re-teach indices, only what values moved.
    """
    diff_lines = list(
        difflib.unified_diff(
            previous_render.splitlines(),
            current_render.splitlines(),
            lineterm="",
        )
    )
    body = "\n".join(diff_lines) if diff_lines else "(no visible change since your last turn)"
    return (
        "The element list is UNCHANGED since your last turn: every [index] you saw before still "
        "addresses the same element now. Lines not shown below are unchanged and still hold; "
        "only the values on the lines below have moved. Diff against your last turn's listing:\n"
        f"{body}"
    )


def _target_key(name: str, action: Action, context: dict[str, Any]) -> tuple[str, str | None]:
    """The (tool, resolved target) identity loop_detected repeats on. Deliberately keyed on the
    resolved TargetDescriptor (context["target"]), never on the action's own node_id: a node_id
    is a live ref that is regenerated on every snapshot (docs/adr/0002 measured this), so it would
    almost never repeat across turns even when the model keeps acting on the same logical element.
    """
    if "target" in context:
        return name, json.dumps(context["target"], sort_keys=True)
    if isinstance(action, Navigate):
        return name, action.url
    return name, None


def _resolve_escalation(
    broker: SessionBroker,
    logger: EvidenceLogger,
    surface: Surface,
    goal: str,
    reason_code: ReasonCode,
    what_it_tried: str,
    what_it_observed: str,
    context: dict[str, str],
    ttl_seconds: float,
) -> tuple[str, InterventionResolution | None]:
    """Build one InterventionRequest and hand it to `SessionBroker.escalate()`
    (escalation/control.py), the ONE shared entry point both execution paths raise an
    intervention through. Returns `(request.id, resolution)`; `resolution` is None on expiry, so
    the CALLER -- which alone knows what each `action_taken` means for the stopping condition it
    is rescuing -- decides what happens next.

    F1: the screenshot is taken from THIS SAME `observation` (never a second, fresher one), so
    the request's own observation and its screenshot describe the same moment -- a stale
    observation would position a mask over pixels the screenshot no longer shows
    (ARCHITECTURE.md decision 47 makes the identical argument for the ordinary step
    screenshots). None (never a broken image reference) when the surface has no
    `screenshot_bytes` or the mask was refused; the operator page already renders that
    honestly as "none captured".

    F2: `context` is the caller's own flat, already-plain-string detail for the reason code it
    is raising -- the refusing PolicyDecision's fields for a risk/policy refusal, or the streak
    and its limit for a stall. Required, not defaulted, so a call site cannot silently fall back
    to an empty dict.
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
        capability_id=None,
        goal=goal,
        step_id=None,
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


def run(
    goal: str,
    target: str,
    surface: Surface,
    llm: LLMClient,
    gate: PolicyGate,
    logger: EvidenceLogger,
    max_steps: int,
    timeout_s: float,
    stall_limit: int = 3,
    full_render_every: int = 5,
    broker: SessionBroker | None = None,
    intervention_ttl_s: float = 900,
) -> RunOutcome:
    start = time.monotonic()
    rounds = 0
    steps_executed = 0
    rejected_turns = 0
    outputs: dict[str, str] = {}
    seen_dialogs = 0
    usage_totals: dict[str, int] = {}

    # D3: the model addresses elements by [index] from `enumerate` over the element list, so a
    # diff is only safe when the element list is provably identical to last turn. Sending a diff
    # while the element list itself changed would leave the model acting on a stale index, which
    # is a wrong-element click, not a token saving.
    previous_full_render: str | None = None
    previous_digest: str | None = None

    # D4 stall tracking, one shared stall_limit across all three stall-style conditions.
    no_progress_streak = 0
    dead_end_streak = 0
    last_action_key: tuple[str, str | None] | None = None
    repeat_streak = 0

    # Escalation is enabled by the presence of `broker`, nothing else (task C0): a run constructed
    # with broker=None (every test predating this phase, and any caller that opts out) behaves
    # exactly as it did before this phase existed. `last_intervention_id`/`last_resolution` track
    # the LAST intervention this run raised, however it resolved, so `_end()` can attach it to
    # every terminal event and RunOutcome from here on -- including a run that was rescued and
    # then went on to finish normally, which still needs to say a human was involved.
    last_intervention_id: str | None = None
    last_resolution: str | None = None
    # A rescued MAX_STEPS escalation extends the budget by the run's OWN original allowance,
    # rather than re-triggering the identical condition on the very next round with zero headroom.
    original_max_steps = max_steps

    def _end(
        status: RunStatus,
        checkpoint: dict[str, Any] | None = None,
        **event_fields: Any,
    ) -> RunOutcome:
        # The only place that builds this run's terminal RunOutcome and writes its one run_end
        # event -- through EvidenceLogger.run_end, which itself guards against a second call, so
        # every stopping condition (including goal_verified, below) routes through here rather
        # than reimplementing this shape inline. `checkpoint` rides into the returned RunOutcome
        # only, never into the event: the separate `goal_verified` event already carries the
        # checkpoint under `checkpoint_eval`. `intervention_id`/`escalation_resolution` ride into
        # both the event and the outcome automatically -- no call site passes them, so a call
        # site cannot forget to (`resolution` is already RunEvent's own field name for a locator
        # Resolution dump, hence `escalation_resolution` here rather than colliding with it).
        logger.run_end(
            status.value,
            rounds=rounds,
            steps_executed=steps_executed,
            intervention_id=last_intervention_id,
            escalation_resolution=last_resolution,
            **event_fields,
        )
        return RunOutcome(
            status=status,
            run_id=logger.run_id,
            steps_executed=steps_executed,
            rounds=rounds,
            rejected_turns=rejected_turns,
            outputs=outputs,
            checkpoint=checkpoint,
            usage=usage_totals,
            intervention_id=last_intervention_id,
            resolution=last_resolution,
        )

    def _escalate_stall(
        reason_code: ReasonCode,
        what_it_tried: str,
        what_it_observed: str,
        context: dict[str, str],
    ) -> bool:
        """Try to rescue THE STOPPING CONDITION THAT JUST FIRED through a human escalation.

        True (the loop should keep going) once a human resolved it as `approved` or
        `took_control`. Draining the surface's captured human actions and logging
        `handoff_resumed` both now happen inside `broker.escalate()` itself (round H) -- the ONE
        shared entry point, so this caller (and the other two below) no longer needs its own copy
        of either. `approved` has no specific refused action to re-dispatch at any of the FOUR
        call sites this serves (no_progress, loop_detected, dead_end, max_steps) -- unlike
        RISKY_ACTION_REQUIRES_APPROVAL and POLICY_REFUSED, both handled inline where the action
        that was actually refused is still in scope -- so it is treated the same as
        `took_control`: the model's next turn just sees a fresh observation.

        False -- the run must end under its ORIGINAL status -- when no broker is configured, the
        operator rejected it, or the intervention expired unanswered.

        `context` (F2) is the caller's own streak/limit detail: which counter fired, and against
        what limit, so an operator can see the run was genuinely stuck rather than merely slow.
        """
        nonlocal last_intervention_id, last_resolution
        if broker is None:
            return False
        request_id, resolution = _resolve_escalation(
            broker, logger, surface, goal, reason_code, what_it_tried, what_it_observed,
            context, intervention_ttl_s,
        )
        last_intervention_id = request_id
        last_resolution = resolution.action_taken if resolution is not None else "expired"
        return resolution is not None and resolution.action_taken != "rejected"

    messages: list[dict[str, Any]] = []
    gate.dispatch(
        surface,
        Navigate(url=target),
        context={"tool": "navigate", "rationale": f"open the target to begin the goal: {goal}"},
    )
    steps_executed += 1
    # No pre-navigation observation exists to protect (the browser starts blank), so the
    # bootstrap step observes once, immediately after the navigate completes, and uses that
    # fresh observation for its own screenshot -- there is no "before" for the very first action,
    # only an "after" (the same "observe, then screenshot" order as every round below).
    _screenshot_safely(logger, surface, rounds, "after", surface.observe())

    while True:
        if rounds >= max_steps:
            if _escalate_stall(
                ReasonCode.MAX_STEPS,
                what_it_tried=f"used its full step budget ({max_steps} steps)",
                what_it_observed="the goal's success checkpoint was never verified",
                context={"steps_used": str(rounds), "max_steps": str(max_steps)},
            ):
                max_steps += original_max_steps
                continue
            return _end(RunStatus.MAX_STEPS)
        if time.monotonic() - start > timeout_s:
            return _end(RunStatus.TIMEOUT)

        rounds += 1
        observation = surface.observe()
        _screenshot_safely(logger, surface, rounds, "before", observation)

        new_dialogs = getattr(surface, "dialog_events", [])[seen_dialogs:]
        for dialog in new_dialogs:
            logger.event("native_dialog", **dialog)
        seen_dialogs += len(new_dialogs)

        current_digest = observation.digest()
        full_render = observation.render()
        # Turn 1 and every full_render_every-th turn are an unconditional refresh; otherwise a
        # diff is sent only when the digest proves nothing about the element list moved.
        is_refresh_turn = rounds == 1 or rounds % full_render_every == 0
        digest_unchanged = previous_digest is not None and current_digest == previous_digest
        if is_refresh_turn or not digest_unchanged:
            prompt_text = full_render
        else:
            prompt_text = _build_diff(previous_full_render or "", full_render)
        previous_full_render = full_render
        previous_digest = current_digest

        if rounds == 1:
            prompt_text = f"Goal: {goal}\n\n{prompt_text}"
        messages.append({"role": "user", "text": prompt_text})

        turn_start = time.monotonic()
        response = llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=ALL_TOOLS)
        duration_ms = (time.monotonic() - turn_start) * 1000
        # D8: every turn's token usage and latency, independent of what the turn decided to do.
        logger.event(
            "decide",
            phase="decide",
            step_id=rounds,
            observation_digest=current_digest,
            tokens=response.usage,
            duration_ms=duration_ms,
        )
        for key, value in response.usage.items():
            usage_totals[key] = usage_totals.get(key, 0) + value
        # A crashed run still has every turn it completed, written incrementally, one line per
        # turn, redacted the same way run.jsonl is (transcript.jsonl is discovery-only: replay
        # has no model turns to record).
        logger.transcript_turn(
            {
                "round": rounds,
                "prompt": prompt_text,
                "tool_calls": [{"name": c.name, "args": c.args} for c in response.tool_calls],
                "text": response.text,
            }
        )

        if not response.tool_calls:
            rejected_turns += 1
            logger.event("rejected_turn", reason="no tool call returned", text=response.text)
            # Do not append a model turn here: an empty tool_calls list becomes
            # types.Content(role="model", parts=[]) in llm/gemini.py's history conversion, and the
            # Gemini API rejects zero-part content -- which would break the *next* call, not this
            # one. Free-tier quota is 20 requests/day, so a run dying on the following turn is
            # expensive. The rejected turn is already logged above; nothing needs to go in
            # `messages` for a turn that produced no content.
            continue

        call = response.tool_calls[0]
        name = call.name
        args = call.args
        messages.append({"role": "model", "tool_calls": [{"name": name, "args": args}]})

        # D1: every one of the eight tools requires a rationale; validated once, up front, before
        # any tool-specific branching -- finish and escalate included, not just the action tools.
        if _rationale_is_malformed(args):
            rejected_turns += 1
            logger.event("rejected_turn", reason="tool call is missing a rationale", tool=name)
            messages.append(
                {"role": "tool", "name": name, "response": {"error": "a rationale is required"}}
            )
            continue
        rationale = args["rationale"]

        if name == "finish":
            checkpoint = args.get("checkpoint")
            ok = isinstance(checkpoint, dict) and verify_checkpoint(surface, checkpoint)
            if ok:
                logger.event(
                    "goal_verified",
                    phase="verify",
                    checkpoint_eval=checkpoint,
                    rationale=rationale,
                )
                return _end(RunStatus.GOAL_VERIFIED, checkpoint=checkpoint)
            logger.event(
                "rejected_completion",
                phase="verify",
                note="checkpoint did not verify",
                checkpoint_eval=checkpoint if isinstance(checkpoint, dict) else None,
            )
            messages.append({"role": "tool", "name": name, "response": {"verified": False}})
            # fall through and keep going; the model does not get to declare success
            continue

        if name == "escalate":
            # D2/D5: the model declares itself stuck. Ends the run cleanly with the `escalation`
            # status and records why; Phase 10 turns this into a live handoff.
            return _end(
                RunStatus.ESCALATION,
                phase="escalate",
                reason_code=args.get("reason_code"),
                rationale=rationale,
            )

        try:
            action = _build_action(name, args, observation)
        except (KeyError, ValueError, TypeError) as exc:
            rejected_turns += 1
            dead_end_streak += 1
            logger.event("rejected_turn", reason=str(exc), tool=name, args=args)
            messages.append({"role": "tool", "name": name, "response": {"error": str(exc)}})
            if dead_end_streak >= stall_limit:
                if _escalate_stall(
                    ReasonCode.LOCATOR_UNRESOLVED,
                    what_it_tried=(
                        f"tried to resolve a target for {name!r} {dead_end_streak} times in a row"
                    ),
                    what_it_observed=str(exc),
                    context={
                        "dead_end_streak": str(dead_end_streak),
                        "stall_limit": str(stall_limit),
                    },
                ):
                    dead_end_streak = 0
                    continue
                return _end(RunStatus.DEAD_END, dead_end_streak=dead_end_streak)
            continue

        # D2 (Phase 8): the digest of the observation this decision was made from, so a reviewer
        # can see WHY no_progress/loop_detected fired and reconstruct the page progression from
        # the evidence log alone -- previously computed in memory (current_digest, above) and
        # compared locally, but never itself written down.
        context: dict[str, Any] = {
            "tool": name,
            "rationale": rationale,
            "observation_digest": current_digest,
        }
        element: UIElement | None = None
        if name in ("click", "type", "select", "read", "extract"):
            element = observation.elements[args["index"]]
            context["target"] = describe(element, observation).model_dump()
        if name == "extract":
            context["output_name"] = args.get("output_name")

        try:
            result_text = gate.dispatch(surface, action, context=context, element=element)
        except PolicyDenied as exc:
            # A refused-but-retryable turn; NavigationBlocked is handled separately, below.
            rejected_turns += 1
            logger.event(
                "rejected_turn", reason=exc.decision.reason, tool=name, rule=exc.decision.rule
            )
            messages.append(
                {
                    "role": "tool",
                    "name": name,
                    "response": {"error": f"action refused by policy: {exc.decision.reason}"},
                }
            )
            continue
        except EscalationRequired as exc:
            # D5: a RISKY_IRREVERSIBLE action the gate itself refused in discovery mode is a
            # stopping condition, the same as an explicit `escalate` call, just triggered by
            # policy rather than by the model's choice. With a broker present, this is exactly
            # the path a one-shot human approval exists for (task C): try to rescue it before
            # ending the run under it.
            if broker is None:
                return _end(
                    RunStatus.ESCALATION,
                    phase="escalate",
                    reason=exc.decision.reason,
                    rule=exc.decision.rule,
                )
            request_id, resolution = _resolve_escalation(
                broker,
                logger,
                surface,
                goal,
                # G1: derived from the decision's own rule (safety/policy.py's
                # reason_code_for_decision), the same call replay/engine.py makes for the
                # identical "risk_replay" rule -- discovery's own rule here is always
                # "risk_discovery" (PolicyGate only raises EscalationRequired in mode="discovery"),
                # so this is a no-op in practice today, but it is the ONE place that decides this
                # mapping rather than a second hardcoded copy of it.
                reason_code_for_decision(exc.decision),
                what_it_tried=(
                    f"attempted a RISKY_IRREVERSIBLE action ({name}): {exc.decision.reason}"
                ),
                what_it_observed="the policy gate refused it pending human approval",
                context=decision_context(exc.decision),
                ttl_seconds=intervention_ttl_s,
            )
            last_intervention_id = request_id
            last_resolution = resolution.action_taken if resolution is not None else "expired"
            if resolution is None or resolution.action_taken == "rejected":
                return _end(
                    RunStatus.ESCALATION,
                    phase="escalate",
                    reason=exc.decision.reason,
                    rule=exc.decision.rule,
                )
            if resolution.action_taken == "took_control":
                # Draining and logging `handoff_resumed` both already happened inside
                # `broker.escalate()` itself (round H) -- nothing left to do here but continue.
                continue
            # "approved": PolicyGate.dispatch consumes the one-shot approval (keyed off the
            # CURRENT control token's own intervention_id, which escalate() already restored to
            # AUTOMATION -- escalation/control.py's ControlToken docstring) and lets the SAME
            # action through this time. Deliberately falls through to the ordinary post-dispatch
            # bookkeeping below rather than a second copy of it -- there is exactly one dispatch
            # success path in this loop. `escalate()` already logged `handoff_resumed`.
            result_text = gate.dispatch(surface, action, context=context, element=element)
        except NavigationBlocked as exc:
            # A navigation that left the allowlist means the session state is no longer
            # trustworthy (decision 59): with no broker this still propagates uncaught and ends
            # the run, unchanged from before this phase. With one, it is worth one human look
            # before giving up on the whole run -- but neither "approved" nor "took_control" ever
            # re-dispatches the SAME action here (unlike the risky-action case above): the
            # navigation already executed, off-allowlist, so the honest next step either way is a
            # fresh observation next round, not blindly repeating what just went wrong.
            if broker is None:
                raise
            request_id, resolution = _resolve_escalation(
                broker,
                logger,
                surface,
                goal,
                ReasonCode.POLICY_REFUSED,
                what_it_tried=f"dispatched {name!r}, which navigated off the allowlist",
                what_it_observed=f"navigation left the allowlist: {exc.urls}",
                context={
                    "reason": f"navigation left the allowlist: {exc.urls}",
                    "action_kind": name,
                },
                ttl_seconds=intervention_ttl_s,
            )
            last_intervention_id = request_id
            last_resolution = resolution.action_taken if resolution is not None else "expired"
            if resolution is None or resolution.action_taken == "rejected":
                return _end(
                    RunStatus.ESCALATION,
                    phase="escalate",
                    reason=f"navigation left the allowlist: {exc.urls}",
                )
            # Draining and logging `handoff_resumed` (for either "took_control" or "approved")
            # both already happened inside `broker.escalate()` itself (round H).
            continue
        steps_executed += 1
        dead_end_streak = 0

        if name == "extract" and args.get("output_name"):
            outputs[args["output_name"]] = result_text or ""

        messages.append({"role": "tool", "name": name, "response": {"result": result_text}})

        # D4: the action just changed the page, so the 'after' screenshot needs a FRESH
        # observation, never the pre-action `observation` above -- one extra observe() per acted
        # round, the same cost replay's engine.py pays per step.
        after_observation = surface.observe()
        _screenshot_safely(logger, surface, rounds, "after", after_observation)

        # no_progress: actions ARE dispatching, but the observation's structure is not moving.
        if after_observation.digest() == current_digest:
            no_progress_streak += 1
        else:
            no_progress_streak = 0
        if no_progress_streak >= stall_limit:
            if _escalate_stall(
                ReasonCode.STUCK_NO_PROGRESS,
                what_it_tried=f"dispatched {name!r} {no_progress_streak} times in a row",
                what_it_observed="the observation's structure has not changed since",
                context={
                    "no_progress_streak": str(no_progress_streak),
                    "stall_limit": str(stall_limit),
                },
            ):
                no_progress_streak = 0
                continue
            return _end(RunStatus.NO_PROGRESS, no_progress_streak=no_progress_streak)

        # loop_detected: the same (tool, resolved target) dispatched stall_limit times running.
        action_key = _target_key(name, action, context)
        if action_key == last_action_key:
            repeat_streak += 1
        else:
            repeat_streak = 1
        last_action_key = action_key
        if repeat_streak >= stall_limit:
            if _escalate_stall(
                ReasonCode.LOOP_DETECTED,
                what_it_tried=(
                    f"dispatched {name!r} against the same target {repeat_streak} times in a row"
                ),
                what_it_observed="the model kept repeating the same action",
                context={"repeat_streak": str(repeat_streak), "stall_limit": str(stall_limit)},
            ):
                repeat_streak = 0
                last_action_key = None
                continue
            return _end(RunStatus.LOOP_DETECTED, repeat_streak=repeat_streak, tool=name)
