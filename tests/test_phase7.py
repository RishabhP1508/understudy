"""Phase 7 tests: the seven-way stopping condition taxonomy, the digest-gated observation diff,
per-turn token logging, and the eight-tool schema (select, escalate). No browser, no network, no
API key, no live provider anywhere in this file -- every LLMClient here is a deterministic stub.
Every EvidenceLogger constructed by a test takes base_dir=tmp_path (tests/conftest.py checks this
is true for the whole suite).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from understudy.agent.loop import RunStatus, run
from understudy.agent.tools import ALL_TOOLS
from understudy.config import Settings
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall, build_llm
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import Policy, PolicyGate, load_policy
from understudy.surface.base import Action, Click, Navigate, Select

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"


def _permissive_policy(**overrides: Any) -> Policy:
    base: dict[str, Any] = dict(
        version=1,
        app_id="test",
        entry_point="http://fake/start",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["textbox", "searchbox", "combobox", "button", "link", "generic"],
    )
    base.update(overrides)
    return Policy(**base)


def _events(logger: EvidenceLogger) -> list[dict[str, Any]]:
    text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------------------------------------
# The eight-tool schema: select and escalate exist and require the right arguments (D2).
# ------------------------------------------------------------------------------------------


def test_all_eight_tools_present_with_correct_required_arguments() -> None:
    names = {tool["name"] for tool in ALL_TOOLS}
    assert names == {
        "navigate",
        "click",
        "type",
        "select",
        "read",
        "extract",
        "finish",
        "escalate",
    }
    for tool in ALL_TOOLS:
        assert "rationale" in tool["parameters"]["required"], (
            f"{tool['name']} does not require a rationale"
        )
    select_tool = next(t for t in ALL_TOOLS if t["name"] == "select")
    assert set(select_tool["parameters"]["required"]) == {"index", "value", "rationale"}
    escalate_tool = next(t for t in ALL_TOOLS if t["name"] == "escalate")
    assert set(escalate_tool["parameters"]["required"]) == {"reason_code", "rationale"}


def test_policy_has_stall_limit_and_full_render_every_with_documented_defaults() -> None:
    policy = load_policy(POLICY_PATH)
    assert policy.stall_limit == 3
    assert policy.full_render_every == 5


# ------------------------------------------------------------------------------------------
# Seven tests, one per RunStatus value, each triggered deterministically.
# ------------------------------------------------------------------------------------------


class _GoalVerifiedSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._clicked = False

    @property
    def url(self) -> str:
        return "http://fake/goal"

    def observe(self) -> Observation:
        status = "Status: DONE_TOKEN" if self._clicked else "Status: pending"
        return Observation(
            url=self.url,
            title="Fake",
            elements=[
                UIElement(node_id="0", role="button", name="Go"),
                UIElement(node_id="1", role="generic", name=status),
            ],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Click):
            self._clicked = True
        return None


class _GoalVerifiedLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(name="click", args={"index": 0, "rationale": "click Go"})
            return LLMResponse(tool_calls=[call], text=None, usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_stop_goal_verified(tmp_path: Path) -> None:
    surface = _GoalVerifiedSurface()
    logger = EvidenceLogger("phase7-goal-verified", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach DONE_TOKEN",
        target="http://fake/goal",
        surface=surface,
        llm=_GoalVerifiedLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED


# ------------------------------------------------------------------------------------------
# Regression: a live discovery run measured TWO "run_end" events per run.jsonl (agent/loop.py's
# own terminal event, plus a second one cli.py logged after run() returned). agent/loop.py is the
# only place that knows WHICH stopping condition fired and why, so it owns the run's one terminal
# event; cli.py no longer logs a second. Covered from both code paths that can write one: the
# `finish`/goal_verified branch, and the shared `_end()` helper every other status goes through.
# ------------------------------------------------------------------------------------------


def test_completed_run_writes_exactly_one_run_end_event_goal_verified(tmp_path: Path) -> None:
    surface = _GoalVerifiedSurface()
    logger = EvidenceLogger("phase7-single-run-end-goal", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach DONE_TOKEN",
        target="http://fake/goal",
        surface=surface,
        llm=_GoalVerifiedLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED
    run_end_events = [e for e in _events(logger) if e.get("type") == "run_end"]
    assert len(run_end_events) == 1
    assert run_end_events[0]["steps_executed"] == outcome.steps_executed


class _NeverVerifiesSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/never"

    def observe(self) -> Observation:
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="button", name="Go")]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _NeverVerifiesLLM:
    """Always calls finish with a checkpoint value that never appears on the page."""

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        checkpoint = {"kind": "text_present", "target": "page", "value": "NEVER_ON_PAGE"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "I am done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_stop_max_steps(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase7-max-steps", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach a state that never happens",
        target="http://fake/never",
        surface=_NeverVerifiesSurface(),
        llm=_NeverVerifiesLLM(),
        gate=gate,
        logger=logger,
        max_steps=3,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.MAX_STEPS
    assert outcome.rounds == 3


def test_completed_run_writes_exactly_one_run_end_event_max_steps(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase7-single-run-end-maxsteps", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach a state that never happens",
        target="http://fake/never",
        surface=_NeverVerifiesSurface(),
        llm=_NeverVerifiesLLM(),
        gate=gate,
        logger=logger,
        max_steps=3,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.MAX_STEPS
    run_end_events = [e for e in _events(logger) if e.get("type") == "run_end"]
    assert len(run_end_events) == 1
    assert run_end_events[0]["steps_executed"] == outcome.steps_executed


class _NeverCalledLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        raise AssertionError("the model must not be consulted once the loop has already timed out")


def test_stop_timeout(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase7-timeout", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="irrelevant, the clock has already run out",
        target="http://fake/timeout",
        surface=_NeverVerifiesSurface(),
        llm=_NeverCalledLLM(),
        gate=gate,
        logger=logger,
        max_steps=100,
        timeout_s=-0.001,  # guaranteed already elapsed before the first check
    )
    assert outcome.status == RunStatus.TIMEOUT
    assert outcome.rounds == 0


class _NoProgressSurface:
    """The element list never changes shape; only a `value` digest() deliberately ignores
    moves. A click always dispatches successfully but never moves the structure."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._counter = 0

    @property
    def url(self) -> str:
        return "http://fake/stall"

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake",
            elements=[
                UIElement(node_id="0", role="button", name="Refresh"),
                UIElement(node_id="1", role="generic", name="Counter", value=str(self._counter)),
            ],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Click):
            self._counter += 1
        return None


class _AlwaysClickLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(name="click", args={"index": 0, "rationale": "click refresh again"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_stop_no_progress(tmp_path: Path) -> None:
    surface = _NoProgressSurface()
    logger = EvidenceLogger("phase7-no-progress", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="make the counter move the page structure (it never will)",
        target="http://fake/stall",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
    )
    assert outcome.status == RunStatus.NO_PROGRESS
    clicks = [a for a in surface.acted if isinstance(a, Click)]
    assert len(clicks) == 2  # both clicks DID dispatch; the page just never moved


class _LoopDetectSurface:
    """A decoy element toggles in and out every action, so digest() changes every round (never
    triggering no_progress), while the SAME button is clicked every round (triggering
    loop_detected on the resolved target descriptor alone)."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._counter = 0

    @property
    def url(self) -> str:
        return "http://fake/loop"

    def observe(self) -> Observation:
        elements = [UIElement(node_id="0", role="button", name="Refresh")]
        if self._counter % 2 == 0:
            elements.append(UIElement(node_id="decoy", role="generic", name="Decoy"))
        return Observation(url=self.url, title="Fake", elements=elements)

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Click):
            self._counter += 1
        return None


def test_stop_loop_detected(tmp_path: Path) -> None:
    surface = _LoopDetectSurface()
    logger = EvidenceLogger("phase7-loop-detected", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="click refresh (the page structure keeps changing around it, on purpose)",
        target="http://fake/loop",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
    )
    assert outcome.status == RunStatus.LOOP_DETECTED
    clicks = [a for a in surface.acted if isinstance(a, Click)]
    assert len(clicks) == 2


class _DeadEndSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/deadend"

    def observe(self) -> Observation:
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="button", name="Go")]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _AlwaysBadIndexLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(name="click", args={"index": 99, "rationale": "try index 99"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_stop_dead_end(tmp_path: Path) -> None:
    surface = _DeadEndSurface()
    logger = EvidenceLogger("phase7-dead-end", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="click an element that is never at index 99",
        target="http://fake/deadend",
        surface=surface,
        llm=_AlwaysBadIndexLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
    )
    assert outcome.status == RunStatus.DEAD_END
    # only the bootstrap navigate ever dispatched; every proposed click was unresolvable
    assert surface.acted == [Navigate(url="http://fake/deadend")]


class _EscalateLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(
            name="escalate",
            args={"reason_code": "target_not_found", "rationale": "cannot find the field"},
        )
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_stop_escalation_via_escalate_tool(tmp_path: Path) -> None:
    surface = _DeadEndSurface()
    logger = EvidenceLogger("phase7-escalate-tool", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="something the model decides it cannot do",
        target="http://fake/deadend",
        surface=surface,
        llm=_EscalateLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.ESCALATION
    events = _events(logger)
    escalate_events = [e for e in events if e.get("phase") == "escalate"]
    assert escalate_events
    assert escalate_events[-1].get("reason_code") == "target_not_found"


# ------------------------------------------------------------------------------------------
# Supporting coverage (D5): a RISKY_IRREVERSIBLE action the gate itself refuses in discovery
# mode is now CAUGHT and also ends the run with `escalation`, not a propagated exception.
# ------------------------------------------------------------------------------------------


class _RiskySurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/start"

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake",
            elements=[UIElement(node_id="0", role="button", name="Transfer Funds")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _ClickRiskyLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(name="click", args={"index": 0, "rationale": "click transfer funds"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_escalation_required_from_policy_gate_is_caught_not_propagated(tmp_path: Path) -> None:
    surface = _RiskySurface()
    logger = EvidenceLogger("phase7-escalate-policy", "test", base_dir=tmp_path)
    policy = _permissive_policy(risky_labels=["transfer"])
    gate = PolicyGate(policy, logger, mode="discovery")

    outcome = run(
        goal="transfer funds",
        target="http://fake/start",
        surface=surface,
        llm=_ClickRiskyLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.ESCALATION
    # only the bootstrap navigate ever dispatched; the risky click was refused
    assert surface.acted == [Navigate(url="http://fake/start")]


# ------------------------------------------------------------------------------------------
# Invariant 5, non-trivially: a false checkpoint must not end the run, and must leave a
# rejected_completion event behind for every turn it was rejected.
# ------------------------------------------------------------------------------------------


class _AlwaysFalseCheckpointSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/checkpoint"

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake",
            elements=[UIElement(node_id="0", role="generic", name="Balance: 42")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _AlwaysFalseCheckpointLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        checkpoint = {"kind": "text_present", "target": "page", "value": "NEVER_PRESENT"}
        call = ToolCall(
            name="finish", args={"checkpoint": checkpoint, "rationale": "I believe this is done"}
        )
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_invariant5_false_checkpoint_does_not_terminate_and_logs_rejected_completion(
    tmp_path: Path,
) -> None:
    surface = _AlwaysFalseCheckpointSurface()
    logger = EvidenceLogger("phase7-invariant5", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach a state that never shows up",
        target="http://fake/checkpoint",
        surface=surface,
        llm=_AlwaysFalseCheckpointLLM(),
        gate=gate,
        logger=logger,
        max_steps=3,
        timeout_s=30,
    )

    assert outcome.status != RunStatus.GOAL_VERIFIED
    assert outcome.status == RunStatus.MAX_STEPS
    assert outcome.rounds == 3

    rejected_completions = [e for e in _events(logger) if e.get("type") == "rejected_completion"]
    assert len(rejected_completions) == 3
    assert all(e.get("note") == "checkpoint did not verify" for e in rejected_completions)


# ------------------------------------------------------------------------------------------
# Malformed tool arguments (missing rationale, whitespace-only rationale, out-of-range index)
# are all rejected without ever dispatching.
# ------------------------------------------------------------------------------------------


class _MalformedThenFinishSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/malformed"

    def observe(self) -> Observation:
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="button", name="Go")]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _MalformedThenFinishLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(name="click", args={"index": 0})  # rationale missing entirely
        elif self.calls == 2:
            call = ToolCall(name="click", args={"index": 0, "rationale": "   "})  # whitespace-only
        elif self.calls == 3:
            call = ToolCall(name="click", args={"index": 99, "rationale": "try index 99"})
        else:
            checkpoint = {"kind": "text_present", "target": "page", "value": "Go"}
            call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_malformed_tool_arguments_are_rejected_without_dispatching(tmp_path: Path) -> None:
    surface = _MalformedThenFinishSurface()
    logger = EvidenceLogger("phase7-malformed", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="click the button",
        target="http://fake/malformed",
        surface=surface,
        llm=_MalformedThenFinishLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
    )

    assert outcome.status == RunStatus.GOAL_VERIFIED
    assert outcome.rejected_turns == 3
    # only the bootstrap navigate ever dispatched: missing rationale, whitespace rationale, and
    # a bad index were all rejected before surface.act() could be reached for any of them
    assert surface.acted == [Navigate(url="http://fake/malformed")]


# ------------------------------------------------------------------------------------------
# select actually dispatches a Select action.
# ------------------------------------------------------------------------------------------


class _SelectSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/select"

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake",
            elements=[UIElement(node_id="0", role="combobox", name="Account Type")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _SelectThenFinishLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(
                name="select", args={"index": 0, "value": "SAV", "rationale": "choose savings"}
            )
            return LLMResponse(tool_calls=[call], text=None, usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "Account Type"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_select_tool_dispatches_a_select_action(tmp_path: Path) -> None:
    surface = _SelectSurface()
    logger = EvidenceLogger("phase7-select", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="choose the savings account type",
        target="http://fake/select",
        surface=surface,
        llm=_SelectThenFinishLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED
    select_actions = [a for a in surface.acted if isinstance(a, Select)]
    assert len(select_actions) == 1
    assert select_actions[0].value == "SAV"


# ------------------------------------------------------------------------------------------
# The diff logic: full render when the digest changed, a diff when it did not, and a full
# render on the periodic refresh turn, with the full render still present earlier in history.
# ------------------------------------------------------------------------------------------


class _DiffLogicSurface:
    """State A on the very first observe(); state B forever after. A `read` action advances the
    step counter without changing what is returned within a state, so round 2 and round 3 both
    see (unchanging) state B -- an unchanged digest across that boundary -- while round 1 -> 2
    crosses the A -> B change. The bootstrap navigate must NOT advance this counter, or the A -> B
    transition happens before round 1 ever observes state A at all."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._step = 0

    @property
    def url(self) -> str:
        return "http://fake/diff"

    def observe(self) -> Observation:
        name = "StateA" if self._step == 0 else "StateB"
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="generic", name=name)]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if not isinstance(action, Navigate):
            self._step += 1
        return None


class _DiffLogicLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.messages_by_call: list[list[dict[str, Any]]] = []

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        self.messages_by_call.append([dict(m) for m in messages])
        if self.calls < 4:
            call = ToolCall(name="read", args={"index": 0, "rationale": f"read turn {self.calls}"})
            return LLMResponse(tool_calls=[call], text=None, usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "StateB"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def _last_user_text(call_messages: list[dict[str, Any]]) -> str:
    for message in reversed(call_messages):
        if message.get("role") == "user":
            text = message["text"]
            assert isinstance(text, str)
            return text
    raise AssertionError("no user message found in this call's history")


def test_diff_logic_full_render_diff_and_periodic_refresh(tmp_path: Path) -> None:
    surface = _DiffLogicSurface()
    llm = _DiffLogicLLM()
    logger = EvidenceLogger("phase7-diff-logic", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="observe the state settle",
        target="http://fake/diff",
        surface=surface,
        llm=llm,
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=10,  # not what this test is exercising; kept well out of the way
        full_render_every=4,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED
    assert llm.calls == 4

    texts = [_last_user_text(call_messages) for call_messages in llm.messages_by_call]

    # Round 1: turn 1 is always a full render (with the goal prefix).
    assert "URL:" in texts[0]
    # Round 2: digest changed (state A -> B), so a full render is sent, not a diff.
    assert texts[1].startswith("URL:")
    # Round 3: digest unchanged (state B -> B), so a diff is sent instead.
    assert texts[2].startswith("The element list is UNCHANGED")
    # Round 4: periodic refresh (full_render_every=4) forces a full render despite an unchanged
    # digest.
    assert texts[3].startswith("URL:")

    # The diff turn (round 3) must still let the model address indices: the full render from
    # round 2 is present earlier in the very history round 3 was sent.
    round_3_history = llm.messages_by_call[2]
    earlier_user_texts = [
        m["text"] for m in round_3_history if m.get("role") == "user" and m["text"] != texts[2]
    ]
    assert any(t.startswith("URL:") for t in earlier_user_texts)


# ------------------------------------------------------------------------------------------
# Token usage per turn (D8): every "decide" event carries tokens and duration_ms.
# ------------------------------------------------------------------------------------------


class _UsageLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        usage = {
            "prompt_tokens": 10 + self.calls,
            "completion_tokens": 2,
            "total_tokens": 12 + self.calls,
        }
        if self.calls == 1:
            call = ToolCall(name="click", args={"index": 0, "rationale": "click Go"})
            return LLMResponse(tool_calls=[call], text=None, usage=usage)
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage=usage)


def test_token_usage_and_duration_logged_per_turn(tmp_path: Path) -> None:
    surface = _GoalVerifiedSurface()
    logger = EvidenceLogger("phase7-usage", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach DONE_TOKEN",
        target="http://fake/goal",
        surface=surface,
        llm=_UsageLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED

    decide_events = [e for e in _events(logger) if e.get("type") == "decide"]
    assert len(decide_events) == 2  # one per round
    for event in decide_events:
        assert event.get("phase") == "decide"
        assert event.get("tokens")
        assert isinstance(event["tokens"].get("total_tokens"), int)
        assert isinstance(event.get("duration_ms"), (int, float))
        assert event["duration_ms"] >= 0


# ------------------------------------------------------------------------------------------
# build_llm: the provider registry. Neither test constructs a real client -- both fail before
# the registry ever calls GeminiClient(...), so no live provider is ever touched.
# ------------------------------------------------------------------------------------------


def test_build_llm_rejects_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")
    settings = Settings(gemini_api_key="unused", policy_path=POLICY_PATH)
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        build_llm(settings)


def test_build_llm_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = Settings(gemini_api_key=None, policy_path=POLICY_PATH)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_llm(settings)


# ------------------------------------------------------------------------------------------
# Regression: a second successful discover run of the same goal text used to overwrite
# {slug}.v1.json outright -- that is exactly how this project's one genuine Phase 2 artifact
# was lost. Artifacts are append-only: writing twice must produce v1 then v2, and v1 must be
# left byte-for-byte untouched.
# ------------------------------------------------------------------------------------------


def test_discover_artifact_versioning_appends_and_never_overwrites(tmp_path: Path) -> None:
    from understudy.cli import _next_artifact_version

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    slug = "some-goal"

    assert _next_artifact_version(artifacts_dir, slug) == 1
    v1_path = artifacts_dir / f"{slug}.v1.json"
    v1_path.write_text('{"version": 1}', encoding="utf-8")

    assert _next_artifact_version(artifacts_dir, slug) == 2
    v2_path = artifacts_dir / f"{slug}.v2.json"
    v2_path.write_text('{"version": 2}', encoding="utf-8")

    assert _next_artifact_version(artifacts_dir, slug) == 3
    # v1 must still be exactly what it was -- writing v2 (or computing v3) must never touch it.
    assert v1_path.read_text(encoding="utf-8") == '{"version": 1}'
    assert v2_path.read_text(encoding="utf-8") == '{"version": 2}'
