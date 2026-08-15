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
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from understudy.agent.prompts import SYSTEM_PROMPT
from understudy.agent.tools import ALL_TOOLS
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMClient
from understudy.models.artifact import Checkpoint, checkpoint_satisfied
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import EscalationRequired, PolicyDenied, PolicyGate
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

    def _end(status: RunStatus, **event_fields: Any) -> RunOutcome:
        # The one and only "run_end" event for this run: a completed run must write exactly one
        # terminal event, never two. Callers (cli.py) must not log a second one after run()
        # returns, because only this function knows WHICH stopping condition fired and why.
        logger.event(
            "run_end",
            status=status.value,
            rounds=rounds,
            steps_executed=steps_executed,
            **event_fields,
        )
        return RunOutcome(
            status=status,
            run_id=logger.run_id,
            steps_executed=steps_executed,
            rounds=rounds,
            rejected_turns=rejected_turns,
            outputs=outputs,
            usage=usage_totals,
        )

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
                logger.event(
                    "run_end",
                    status=RunStatus.GOAL_VERIFIED.value,
                    rounds=rounds,
                    steps_executed=steps_executed,
                )
                return RunOutcome(
                    status=RunStatus.GOAL_VERIFIED,
                    run_id=logger.run_id,
                    steps_executed=steps_executed,
                    rounds=rounds,
                    rejected_turns=rejected_turns,
                    outputs=outputs,
                    checkpoint=checkpoint,
                    usage=usage_totals,
                )
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
                return _end(RunStatus.DEAD_END, dead_end_streak=dead_end_streak)
            continue

        context: dict[str, Any] = {"tool": name, "rationale": rationale}
        element: UIElement | None = None
        if name in ("click", "type", "select", "read", "extract"):
            element = observation.elements[args["index"]]
            context["target"] = describe(element, observation).model_dump()
        if name == "extract":
            context["output_name"] = args.get("output_name")

        try:
            result_text = gate.dispatch(surface, action, context=context, element=element)
        except PolicyDenied as exc:
            # A refused-but-retryable turn; NavigationBlocked is NOT caught (D5): a navigation
            # that escaped the allowlist means the session state is no longer trustworthy, so it
            # propagates and ends the run rather than being retried.
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
            # D5: a RISKY_IRREVERSIBLE action the gate itself refused in discovery mode is now a
            # stopping condition, not a propagated exception -- a human is needed, the same as an
            # explicit `escalate` call, just triggered by policy rather than by the model's choice.
            return _end(
                RunStatus.ESCALATION,
                phase="escalate",
                reason=exc.decision.reason,
                rule=exc.decision.rule,
            )
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
            return _end(RunStatus.NO_PROGRESS, no_progress_streak=no_progress_streak)

        # loop_detected: the same (tool, resolved target) dispatched stall_limit times running.
        action_key = _target_key(name, action, context)
        if action_key == last_action_key:
            repeat_streak += 1
        else:
            repeat_streak = 1
        last_action_key = action_key
        if repeat_streak >= stall_limit:
            return _end(RunStatus.LOOP_DETECTED, repeat_streak=repeat_streak, tool=name)
