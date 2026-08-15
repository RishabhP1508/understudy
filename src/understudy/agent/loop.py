"""observe -> decide -> act: the discovery agent's loop.

The model never declares its own success. verify_checkpoint() is a deterministic re-observe and
check; it is called from the one 'finish' branch below, before the loop can return a verified
outcome, and there is no early return in that branch before the call.
tests/test_constraints.py (invariant 5) enforces that shape by walking loop.py's AST.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from understudy.agent.prompts import SYSTEM_PROMPT
from understudy.agent.tools import ALL_TOOLS
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMClient
from understudy.models.artifact import Checkpoint, checkpoint_satisfied
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import PolicyDenied, PolicyGate
from understudy.surface.base import Action, Click, Navigate, ReadText, Surface, Type
from understudy.surface.locator import describe


class RunOutcome(BaseModel):
    status: str  # "goal_verified" | "max_steps" | "timeout"
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
    logger: EvidenceLogger, surface: Surface, step: int, observation: Observation
) -> None:
    # R5: at least one richer signal per step. A screenshot failure (browser closed mid-run,
    # a fake surface with no screenshot_bytes() in tests, etc.) must never abort a real discovery
    # run. `observation` is passed by the caller and must describe the CURRENT page -- taken
    # right after surface.observe() and before any action, per D7: masking positions a box from
    # an observation's element bounds, and a stale observation would paint that box over pixels
    # from a different moment, which is a leak, not a cosmetic bug.
    try:
        logger.screenshot(surface, step, observation=observation)
    except Exception as exc:
        logger.event("screenshot_failed", step=step, reason=str(exc))


def _build_action(name: str, args: dict[str, Any], observation: Observation) -> Action:
    if name == "navigate":
        return Navigate(url=args["url"])
    if name == "click":
        return Click(node_id=_index_to_node_id(observation, args["index"]))
    if name == "type":
        return Type(node_id=_index_to_node_id(observation, args["index"]), text=args["text"])
    if name in ("read", "extract"):
        return ReadText(node_id=_index_to_node_id(observation, args["index"]))
    raise ValueError(f"unknown tool: {name}")


def run(
    goal: str,
    target: str,
    surface: Surface,
    llm: LLMClient,
    gate: PolicyGate,
    logger: EvidenceLogger,
    max_steps: int,
    timeout_s: float,
) -> RunOutcome:
    start = time.monotonic()
    rounds = 0
    steps_executed = 0
    rejected_turns = 0
    outputs: dict[str, str] = {}
    seen_dialogs = 0
    usage_totals: dict[str, int] = {}

    messages: list[dict[str, Any]] = []
    gate.dispatch(
        surface,
        Navigate(url=target),
        context={"tool": "navigate", "rationale": f"open the target to begin the goal: {goal}"},
    )
    steps_executed += 1
    # No pre-navigation observation exists to protect (the browser starts blank), so the
    # bootstrap step observes once, immediately after the navigate completes, and uses that
    # fresh observation for its own screenshot -- the same "observe, then screenshot" order as
    # every round below, just with the one action that has no prior page to describe.
    _screenshot_safely(logger, surface, rounds, surface.observe())

    while True:
        if rounds >= max_steps:
            logger.event("run_end", status="max_steps", rounds=rounds)
            return RunOutcome(
                status="max_steps",
                run_id=logger.run_id,
                steps_executed=steps_executed,
                rounds=rounds,
                rejected_turns=rejected_turns,
                outputs=outputs,
                usage=usage_totals,
            )
        if time.monotonic() - start > timeout_s:
            logger.event("run_end", status="timeout", rounds=rounds)
            return RunOutcome(
                status="timeout",
                run_id=logger.run_id,
                steps_executed=steps_executed,
                rounds=rounds,
                rejected_turns=rejected_turns,
                outputs=outputs,
                usage=usage_totals,
            )

        rounds += 1
        observation = surface.observe()
        _screenshot_safely(logger, surface, rounds, observation)

        new_dialogs = getattr(surface, "dialog_events", [])[seen_dialogs:]
        for dialog in new_dialogs:
            logger.event("native_dialog", **dialog)
        seen_dialogs += len(new_dialogs)

        prompt_text = observation.render()
        if rounds == 1:
            prompt_text = f"Goal: {goal}\n\n{prompt_text}"
        messages.append({"role": "user", "text": prompt_text})

        response = llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=ALL_TOOLS)
        for key, value in response.usage.items():
            usage_totals[key] = usage_totals.get(key, 0) + value

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

        if name == "finish":
            checkpoint = args.get("checkpoint")
            ok = isinstance(checkpoint, dict) and verify_checkpoint(surface, checkpoint)
            if ok:
                logger.event(
                    "goal_verified", checkpoint=checkpoint, rationale=args.get("rationale")
                )
                return RunOutcome(
                    status="goal_verified",
                    run_id=logger.run_id,
                    steps_executed=steps_executed,
                    rounds=rounds,
                    rejected_turns=rejected_turns,
                    outputs=outputs,
                    checkpoint=checkpoint,
                    usage=usage_totals,
                )
            logger.event(
                "rejected_completion", reason="checkpoint did not verify", checkpoint=checkpoint
            )
            messages.append({"role": "tool", "name": name, "response": {"verified": False}})
            # fall through and keep going; the model does not get to declare success
            continue

        try:
            action = _build_action(name, args, observation)
        except (KeyError, ValueError, TypeError) as exc:
            rejected_turns += 1
            logger.event("rejected_turn", reason=str(exc), tool=name, args=args)
            messages.append({"role": "tool", "name": name, "response": {"error": str(exc)}})
            continue

        context: dict[str, Any] = {"tool": name, "rationale": args.get("rationale")}
        element: UIElement | None = None
        if name in ("click", "type", "read", "extract"):
            element = observation.elements[args["index"]]
            context["target"] = describe(element, observation).model_dump()
        if name == "extract":
            context["output_name"] = args.get("output_name")

        try:
            result_text = gate.dispatch(surface, action, context=context, element=element)
        except PolicyDenied as exc:
            # Whether a policy denial should instead be a STOP condition for the whole run, not
            # just a rejected turn the model gets to try again after, is Phase 7's call and is
            # deliberately not settled here. EscalationRequired and NavigationBlocked are NOT
            # caught: a risky action needs a human (Phase 10), and a navigation that escaped the
            # allowlist means the session state is no longer trustworthy, so both propagate and
            # end the run rather than being retried.
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
        steps_executed += 1

        if name == "extract" and args.get("output_name"):
            outputs[args["output_name"]] = result_text or ""

        messages.append({"role": "tool", "name": name, "response": {"result": result_text}})
