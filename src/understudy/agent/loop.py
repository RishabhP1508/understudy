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
from understudy.models.observation import Observation
from understudy.safety.policy import PolicyGate
from understudy.surface.base import Action, Click, Navigate, ReadText, Surface, Type
from understudy.surface.locator import TargetDescriptor, describe


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


def _describe_target(observation: Observation, index: int) -> TargetDescriptor:
    # docs/adr/0006: describe() captures the full ranked-signal descriptor (scope, relational
    # hint, ordinal only as a tiebreaker), not just role+name+ordinal, so a recorded step gives
    # replay's resolve() more than one way to find the element again.
    return describe(observation.elements[index], observation)


def _index_to_node_id(observation: Observation, index: Any) -> str:
    is_plain_int = isinstance(index, int) and not isinstance(index, bool)
    if not is_plain_int or not 0 <= index < len(observation.elements):
        raise ValueError(f"index {index!r} is out of range for this observation")
    return observation.elements[index].node_id


def _screenshot_safely(logger: EvidenceLogger, surface: Surface, step: int) -> None:
    # R5: at least one richer signal per step. A screenshot failure (browser closed mid-run,
    # a fake surface with no screenshot() in tests, etc.) must never abort a real discovery run.
    try:
        logger.screenshot(surface, step)
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
    _screenshot_safely(logger, surface, rounds)

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
        if name in ("click", "type", "read", "extract"):
            context["target"] = _describe_target(observation, args["index"]).model_dump()
        if name == "extract":
            context["output_name"] = args.get("output_name")

        result_text = gate.dispatch(surface, action, context=context)
        steps_executed += 1
        _screenshot_safely(logger, surface, rounds)

        if name == "extract" and args.get("output_name"):
            outputs[args["output_name"]] = result_text or ""

        messages.append({"role": "tool", "name": name, "response": {"result": result_text}})
