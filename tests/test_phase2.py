"""Phase 2 tests. No browser, no API key: everything here runs against fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from understudy.agent.loop import run
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import Policy, PolicyGate
from understudy.surface.base import Action


def _permissive_policy() -> Policy:
    """A test Policy permissive enough that the bootstrap navigate to http://fake/start (and any
    action against the http://fake/ fakes below) passes the allowlist, action-type, and role
    checks -- these tests exercise the loop's own control flow, not the gate's refusal paths (see
    tests/test_phase5.py for those)."""
    return Policy(
        version=1,
        app_id="test",
        entry_point="http://fake/start",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=[
            "textbox",
            "searchbox",
            "combobox",
            "button",
            "link",
            "checkbox",
            "radio",
            "option",
        ],
    )


class _FakeSurface:
    """A Surface whose page never shows the checkpoint text, so finish must be rejected."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/"

    def observe(self) -> Observation:
        return Observation(
            url="http://fake/",
            title="Fake",
            elements=[UIElement(node_id="0", role="button", name="Login")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _FakeLLM:
    """Always calls finish with a checkpoint value that is never actually on the page."""

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        checkpoint = {"kind": "text_present", "target": "page", "value": "NEVER_ON_PAGE"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "I am done"})
        return LLMResponse(tool_calls=[call], text=None, usage={"total_tokens": 10})


class _VerifiableSurface:
    """A Surface whose page always shows the checkpoint text, so a well-formed finish call
    verifies immediately."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/"

    def observe(self) -> Observation:
        return Observation(
            url="http://fake/",
            title="Fake",
            elements=[UIElement(node_id="0", role="generic", name="Balance: DONE_TOKEN")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _NoToolCallThenFinishLLM:
    """First call returns no tool call at all (the free-text turn the Gemini API sometimes
    returns under forced function calling); second call finishes with a checkpoint that is
    actually on the page. Records the raw `messages` list it was given on each call so the test
    can assert on exactly what got sent."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            return LLMResponse(tool_calls=[], text="thinking out loud", usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_no_tool_call_turn_is_not_appended_as_a_zero_part_model_message(tmp_path: Path) -> None:
    """Round 3 fix: a no-tool-call round used to append {"role": "model", "tool_calls": []},
    which llm/gemini.py turns into types.Content(role="model", parts=[]) -- content the Gemini API
    rejects, breaking the *next* call. The rejected turn must be logged (below) but must not leave
    a message behind for the following call to send back."""
    surface = _VerifiableSurface()
    llm = _NoToolCallThenFinishLLM()
    logger = EvidenceLogger("test", "phase2-no-tool-call", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger)

    outcome = run(
        goal="reach the DONE_TOKEN state",
        target="http://fake/start",
        surface=surface,
        llm=llm,
        gate=gate,
        logger=logger,
        max_steps=3,
        timeout_s=30,
    )

    assert outcome.status == "goal_verified"
    assert outcome.rejected_turns == 1
    assert len(llm.calls) == 2
    second_call_messages = llm.calls[1]
    assert not any(
        message["role"] == "model" and message.get("tool_calls") == []
        for message in second_call_messages
    )


def test_false_checkpoint_does_not_terminate_the_run(tmp_path: Path) -> None:
    surface = _FakeSurface()
    llm = _FakeLLM()
    logger = EvidenceLogger("test", "phase2-false-checkpoint", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger)

    outcome = run(
        goal="reach a state that never happens",
        target="http://fake/start",
        surface=surface,
        llm=llm,
        gate=gate,
        logger=logger,
        max_steps=3,
        timeout_s=30,
    )

    assert outcome.status != "goal_verified"
    assert outcome.status == "max_steps"
    assert outcome.rejected_turns == 0  # finish was well-formed, just unverified
    assert outcome.rounds == 3


def test_observation_render_is_indexed_and_has_no_html() -> None:
    observation = Observation(
        url="http://fake/login",
        title="Login",
        elements=[
            UIElement(node_id="0", role="table"),
            UIElement(node_id="1", role="row", depth=1),
            UIElement(node_id="2", role="cell", name="Username", depth=2),
            UIElement(node_id="3", role="textbox", depth=2),
        ],
    )
    rendered = observation.render()

    assert "[0]" in rendered
    assert "[3]" in rendered
    assert '[2]     cell "Username"' in rendered
    assert "<" not in rendered
    assert ">" not in rendered


# Phase 2's locator was a stub (exact role+name match, ordinal to disambiguate, raise on
# ambiguity or on no match). Phase 4 replaced it with the full ranked strategy list, which never
# raises and never needs an ordinal-only test double; see tests/test_phase4.py.
