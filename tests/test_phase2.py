"""Phase 2 tests. No browser, no API key: everything here runs against fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from understudy.agent.loop import run
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import PolicyGate
from understudy.surface.base import Action
from understudy.surface.locator import AmbiguousTarget, TargetDescriptor, TargetNotFound, resolve


class _FakeSurface:
    """A Surface whose page never shows the checkpoint text, so finish must be rejected."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

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


def test_false_checkpoint_does_not_terminate_the_run(tmp_path: Path) -> None:
    surface = _FakeSurface()
    llm = _FakeLLM()
    logger = EvidenceLogger("test", "phase2-false-checkpoint", base_dir=tmp_path)
    gate = PolicyGate(logger)

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


def test_locator_resolve_raises_on_ambiguous_match_without_ordinal() -> None:
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="textbox", name=""),
            UIElement(node_id="1", role="textbox", name=""),
        ],
    )
    with pytest.raises(AmbiguousTarget):
        resolve(observation, TargetDescriptor(role="textbox", name=""))


def test_locator_resolve_with_ordinal_picks_the_right_match() -> None:
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="textbox", name=""),
            UIElement(node_id="1", role="textbox", name=""),
        ],
    )
    node_id = resolve(observation, TargetDescriptor(role="textbox", name="", ordinal=1))
    assert node_id == "1"


def test_locator_resolve_raises_when_nothing_matches() -> None:
    observation = Observation(url="http://fake/", title="Fake", elements=[])
    with pytest.raises(TargetNotFound):
        resolve(observation, TargetDescriptor(role="button", name="Missing"))
