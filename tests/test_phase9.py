"""Phase 9 tests.

Task A (top of file): the one-and-only run_end event.

Measured defect: evidence/discovery-b2405e162ba4/run.jsonl has TWO "run_end" lines, because
agent/loop.py's goal_verified branch used to reimplement _end()'s shape inline with its own
logger.event("run_end", ...) call. EvidenceLogger.run_end() closes that off structurally: it is
the only method that writes a run_end event, and a second call on the same logger instance is a
silent no-op.

Task B (below): replay/outcomes.py's detectors, replay/recovery.py's triggers and recovery
actions, models/artifact.py's login_prefix_len, record/recorder.py's gated seeding and its B5/B6
fixes, and replay/engine.py's parameter validation, drift detection, and recovery loop -- all
against real, produced data (evidence/discovery-b2405e162ba4, the one genuine discovery run this
project's non-negotiable requirement depends on), never hand-authored.

Every EvidenceLogger here takes base_dir=tmp_path (tests/conftest.py enforces this for the whole
suite). Tests above the "LIVE" marker run with no browser, no network, no API key; tests below it
drive the real fixture app and skip loudly if it is not reachable on 127.0.0.1:5055.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from understudy.agent.loop import RunStatus, run
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    InputParam,
    KnownOutcome,
    Provenance,
    RecoveryRule,
    Step,
    TargetApp,
    login_prefix_len,
)
from understudy.models.observation import Observation, UIElement
from understudy.models.result import FailureCategory
from understudy.record.recorder import (
    _generalize_success_checkpoint,
    _parameterize_target,
    _seed_known_outcomes,
    _seed_recovery_rules,
    build_capability,
)
from understudy.replay import engine as replay_engine
from understudy.replay import outcomes, recovery
from understudy.replay.engine import _interpolate_descriptor
from understudy.replay.outcomes import UnknownDetector
from understudy.replay.recovery import TriggerContext
from understudy.safety.policy import Policy, PolicyGate, load_policy
from understudy.surface.base import Action, Click
from understudy.surface.locator import TargetDescriptor, resolve


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


def _run_end_events(logger: EvidenceLogger) -> list[dict[str, Any]]:
    text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [e for e in events if e.get("type") == "run_end"]


# ------------------------------------------------------------------------------------------
# EvidenceLogger.run_end itself: a second call on the same instance is a silent no-op.
# ------------------------------------------------------------------------------------------


def test_run_end_second_call_on_same_logger_is_a_silent_noop(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase9-guard", "test", base_dir=tmp_path)
    logger.run_end("goal_verified", rounds=1, steps_executed=1)
    logger.run_end("max_steps", rounds=99, steps_executed=99)  # must be swallowed, not raise
    events = _run_end_events(logger)
    assert len(events) == 1
    assert events[0]["status"] == "goal_verified"
    assert events[0]["rounds"] == 1


# ------------------------------------------------------------------------------------------
# (a) A run that ends goal_verified writes exactly one run_end event.
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


def test_goal_verified_run_writes_exactly_one_run_end_event(tmp_path: Path) -> None:
    surface = _GoalVerifiedSurface()
    logger = EvidenceLogger("phase9-goal-verified", "test", base_dir=tmp_path)
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
    events = _run_end_events(logger)
    assert len(events) == 1
    assert events[0]["status"] == "goal_verified"
    assert events[0]["steps_executed"] == outcome.steps_executed
    # checkpoint rides into the returned RunOutcome, not into the run_end event itself (the
    # separate goal_verified event already carries it under checkpoint_eval).
    assert outcome.checkpoint is not None
    assert "checkpoint" not in events[0]


# ------------------------------------------------------------------------------------------
# (b) A run that ends on a stopping condition (max_steps) writes exactly one run_end event.
# ------------------------------------------------------------------------------------------


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


def test_max_steps_run_writes_exactly_one_run_end_event(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase9-max-steps", "test", base_dir=tmp_path)
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
    events = _run_end_events(logger)
    assert len(events) == 1
    assert events[0]["status"] == "max_steps"
    assert events[0]["steps_executed"] == outcome.steps_executed


# ------------------------------------------------------------------------------------------
# cli.py's own except-branch call site also goes through the same guarded method.
# ------------------------------------------------------------------------------------------


def test_cli_error_path_uses_the_guarded_run_end_method(tmp_path: Path) -> None:
    from understudy.cli import _discover_and_capture

    class _ExplodingLLM:
        def complete(
            self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMResponse:
            raise RuntimeError("boom")

    logger = EvidenceLogger("phase9-cli-error", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")
    surface = _NeverVerifiesSurface()

    try:
        _discover_and_capture(
            goal="irrelevant",
            target="http://fake/never",
            surface=surface,
            llm=_ExplodingLLM(),
            gate=gate,
            logger=logger,
            max_steps=5,
            timeout_s=30,
            stall_limit=3,
            full_render_every=5,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the exploding LLM's error to propagate")

    events = _run_end_events(logger)
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error"] == "RuntimeError"


# ============================================================================================
# Task B: replay/outcomes.py, replay/recovery.py, models/artifact.py's login_prefix_len,
# record/recorder.py's B4-B7 fixes, and replay/engine.py's parameter validation, drift, and
# recovery loop.
# ============================================================================================

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"
EVIDENCE_DIR = Path(__file__).parent.parent / "evidence" / "discovery-b2405e162ba4"
GOAL = "look up member 12345 and read their current savings balance"
TARGET = "http://127.0.0.1:5055/login"
CAPABILITY_ID = "look-up-member-12345-and-read-their-current-savings-balance"


def _rebuilt_capability() -> Capability:
    """Built fresh from the real evidence log every time this suite runs (docs/adr/0011: never
    depend on a frozen file under artifacts/) -- genuine, produced data, never hand-authored.
    `llm=None` exercises D5's deterministic degrade path; no network call anywhere here.
    """
    policy = load_policy(POLICY_PATH)
    return build_capability(
        run_dir=EVIDENCE_DIR,
        goal=GOAL,
        target=TARGET,
        run_id="b2405e162ba4",
        model="gemini-3.6-flash",
        capability_id=CAPABILITY_ID,
        policy=policy,
        llm=None,
    )


def _fake_observation(*needles: str) -> Observation:
    return Observation(
        url="http://fake/x",
        title="Fake",
        elements=[UIElement(node_id="0", role="generic", name=" ".join(needles))],
    )


def _minimal_capability(**overrides: Any) -> Capability:
    base: dict[str, Any] = dict(
        capability_id="min",
        name="n",
        description="d",
        target=TargetApp(app_id="a", entry_point="http://fake/a"),
        steps=[],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )
    base.update(overrides)
    return Capability(**base)


# --------------------------------------------------------------------------------------------
# B1: outcomes.py detectors
# --------------------------------------------------------------------------------------------


def test_member_lookup_no_match_detector_fires_on_real_fixture_messages() -> None:
    for message in (
        "No member matches that search.",
        "No such member: 99999.",
        "No matching record was found.",
    ):
        assert outcomes.DETECTORS["member_lookup_no_match"](_fake_observation(message)) == message

    unrelated = _fake_observation("Welcome back")
    assert outcomes.DETECTORS["member_lookup_no_match"](unrelated) is None


def test_permission_denied_detector_fires_on_real_fixture_messages() -> None:
    for message in (
        "You do not have permission to view member 55555.",
        "Permission denied for this operation.",
    ):
        assert outcomes.DETECTORS["permission_denied"](_fake_observation(message)) == message

    unrelated = _fake_observation("Access granted")
    assert outcomes.DETECTORS["permission_denied"](unrelated) is None


def test_validation_rejected_detector_returns_the_field_message() -> None:
    message = "Member ID could not be validated. Please re-enter."
    assert outcomes.DETECTORS["validation_rejected"](_fake_observation(message)) == message

    unrelated = _fake_observation("Saved successfully")
    assert outcomes.DETECTORS["validation_rejected"](unrelated) is None


# --------------------------------------------------------------------------------------------
# B2: recovery.py triggers
# --------------------------------------------------------------------------------------------


def _ctx(**overrides: Any) -> TriggerContext:
    base: dict[str, Any] = dict(
        observation=Observation(url="http://fake/x", title="Fake", elements=[]),
        step_index=0,
        login_prefix_len=0,
        last_navigation="none",
        new_dialogs=[],
    )
    base.update(overrides)
    return TriggerContext(**base)


def test_native_dialog_appeared_trigger() -> None:
    dialogs = [{"dialog_type": "confirm", "message": "Are you sure?", "handled": "dismiss"}]
    fired = recovery.TRIGGERS["native_dialog_appeared"](_ctx(new_dialogs=dialogs))
    assert fired is not None and "Are you sure?" in fired
    assert recovery.TRIGGERS["native_dialog_appeared"](_ctx(new_dialogs=[])) is None


def test_native_dialog_unhandled_trigger() -> None:
    unhandled = [{"dialog_type": "confirm", "message": "?", "handled": "none"}]
    handled = [{"dialog_type": "confirm", "message": "?", "handled": "dismiss"}]
    assert recovery.TRIGGERS["native_dialog_unhandled"](_ctx(new_dialogs=unhandled)) is not None
    assert recovery.TRIGGERS["native_dialog_unhandled"](_ctx(new_dialogs=handled)) is None


def test_html_interstitial_present_trigger() -> None:
    present = _fake_observation("A confirmation is required before continuing.")
    absent = _fake_observation("Welcome")
    assert recovery.TRIGGERS["html_interstitial_present"](_ctx(observation=present)) is not None
    assert recovery.TRIGGERS["html_interstitial_present"](_ctx(observation=absent)) is None


def test_transient_error_page_trigger() -> None:
    present = _fake_observation("Service temporarily unavailable. Please retry.")
    absent = _fake_observation("All good")
    assert recovery.TRIGGERS["transient_error_page"](_ctx(observation=present)) is not None
    assert recovery.TRIGGERS["transient_error_page"](_ctx(observation=absent)) is None


def test_app_error_page_trigger() -> None:
    present = _fake_observation("An unexpected error occurred.")
    absent = _fake_observation("Fine")
    assert recovery.TRIGGERS["app_error_page"](_ctx(observation=present)) is not None
    assert recovery.TRIGGERS["app_error_page"](_ctx(observation=absent)) is None


def test_navigation_still_in_flight_trigger() -> None:
    in_flight = recovery.TRIGGERS["navigation_still_in_flight"](_ctx(last_navigation="in_flight"))
    settled = recovery.TRIGGERS["navigation_still_in_flight"](_ctx(last_navigation="settled"))
    none_ = recovery.TRIGGERS["navigation_still_in_flight"](_ctx(last_navigation="none"))
    assert in_flight is not None
    assert settled is None
    assert none_ is None


def test_session_lost_mid_flow_trigger() -> None:
    login_page = Observation(
        url="http://fake/login",
        title="Fake",
        elements=[UIElement(node_id="0", role="button", name="Login")],
    )
    other_page = Observation(url="http://fake/x", title="Fake", elements=[])
    trigger = recovery.TRIGGERS["session_lost_mid_flow"]

    # Boundary (login_prefix_len=3, so the login click itself is step index 2): index 0 and 1
    # must NOT fire (still earlier than the login click's own action having run), index 2 MUST
    # fire (the trigger is evaluated AFTER that step's action ran, and landing back on the login
    # form at that point is precisely a session loss, not merely a candidate for one).
    assert trigger(_ctx(observation=login_page, step_index=0, login_prefix_len=3)) is None
    assert trigger(_ctx(observation=login_page, step_index=1, login_prefix_len=3)) is None
    assert trigger(_ctx(observation=login_page, step_index=2, login_prefix_len=3)) is not None
    # Does not fire: no known login boundary for this flow at all (login_prefix_len == 0).
    assert trigger(_ctx(observation=login_page, step_index=0, login_prefix_len=0)) is None
    # Does not fire: past the prefix, but no login form showing.
    assert trigger(_ctx(observation=other_page, step_index=2, login_prefix_len=3)) is None


def test_reauth_unavailable_still_reports_session_expired_via_unrecovered() -> None:
    """The other half of the session-loss story, offline: a capability that HAS a login prefix
    (login_prefix_len > 0) but declares NO reauth rule at all. `recovery.match()` then finds
    nothing to apply (no rule in `capability.recovery_rules` names the `session_lost_mid_flow`
    trigger), and replay/engine.py's own fallthrough, `recovery.unrecovered(ctx)`, is what a
    caller actually depends on for the final category -- it is unconditional (it does not consult
    `capability.recovery_rules` at all), so it still names this SESSION_EXPIRED even with no rule
    to have tried. No live row reaches this: every capability record/recorder.py builds seeds a
    reauth rule whenever it has a login prefix (test_reauth_rule_only_seeded_when_a_login_prefix_
    exists, above); this is the branch a hand-trimmed or pre-seed artifact would hit.
    """
    capability = _minimal_capability()  # recovery_rules=[] by default: no reauth rule declared
    login_page = Observation(
        url="http://fake/login",
        title="Fake",
        elements=[UIElement(node_id="0", role="button", name="Login")],
    )
    ctx = _ctx(observation=login_page, step_index=2, login_prefix_len=3)

    assert recovery.match(capability, ctx) is None

    result = recovery.unrecovered(ctx)
    assert result is not None
    category, _reason = result
    assert category == FailureCategory.SESSION_EXPIRED


# --------------------------------------------------------------------------------------------
# outcomes.validate / recovery.validate: UnknownDetector, naming the unknown one, at load time
# --------------------------------------------------------------------------------------------


def test_outcomes_validate_raises_unknown_detector_naming_it() -> None:
    capability = _minimal_capability(
        known_outcomes=[KnownOutcome(code="c", detector="no_such_detector", terminal=True)]
    )
    with pytest.raises(UnknownDetector, match="no_such_detector"):
        outcomes.validate(capability)


def test_recovery_validate_raises_unknown_detector_naming_it() -> None:
    capability = _minimal_capability(
        recovery_rules=[
            RecoveryRule(id="r", trigger="no_such_trigger", action="wait", max_attempts=1)
        ]
    )
    with pytest.raises(UnknownDetector, match="no_such_trigger"):
        recovery.validate(capability)


# --------------------------------------------------------------------------------------------
# B3: login_prefix_len
# --------------------------------------------------------------------------------------------


def _step(index: int, action: str, postcondition: Checkpoint | None = None) -> Step:
    return Step(
        id=str(index),
        index=index,
        action=action,
        target=None,
        value=None,
        precondition=None,
        postcondition=postcondition,
        rationale="r",
    )


def test_login_prefix_len_counts_up_to_and_including_the_departure_step() -> None:
    entry_check = Checkpoint(kind="element_present", target="textbox", value="Password")
    login_check = Checkpoint(kind="element_present", target="button", value="Login")
    left_entry = Checkpoint(kind="url_matches", target="any_frame", value="http://fake/app")
    search_check = Checkpoint(kind="element_present", target="button", value="Search")
    steps = [
        _step(0, "type", entry_check),
        _step(1, "type", login_check),
        _step(2, "click", left_entry),
        _step(3, "type", search_check),
    ]
    assert login_prefix_len(steps, "http://fake/login") == 3


def test_login_prefix_len_is_zero_when_no_step_leaves_the_entry_route() -> None:
    stayed = Checkpoint(kind="url_matches", target="any_frame", value="http://fake/login")
    steps = [
        _step(0, "type", Checkpoint(kind="element_present", target="textbox", value="Password")),
        _step(1, "click", stayed),
    ]
    assert login_prefix_len(steps, "http://fake/login") == 0
    assert login_prefix_len([], "http://fake/login") == 0


# --------------------------------------------------------------------------------------------
# B4: recorder seed gating
# --------------------------------------------------------------------------------------------

_UNCONDITIONAL_RECOVERY_SEED_IDS = {
    "dismiss_native_dialog",
    "dismiss_html_interstitial",
    "retry_transient_failure",
    "wait_for_slow_load",
}


def _step_with_target(index: int, action: str, name: str, role: str = "textbox") -> Step:
    return Step(
        id=str(index),
        index=index,
        action=action,
        target=TargetDescriptor(role=role, name=name),
        value=None,
        precondition=None,
        postcondition=None,
        rationale="r",
    )


def test_read_only_lookup_flow_earns_three_outcomes_not_insufficient_funds() -> None:
    steps = [
        _step_with_target(0, "type", "Member ID"),
        _step_with_target(1, "click", "Search", role="button"),
    ]
    recorded_urls = ["http://fake/member/12345/balance"]

    codes = {o.code for o in _seed_known_outcomes(steps, recorded_urls)}

    assert codes == {"member_not_found", "permission_denied", "validation_rejected"}
    # insufficient_funds is DROPPED entirely (B1 defines no `balance_check` detector) -- no
    # predicate in this recorder can ever produce it, regardless of what the flow does.
    assert "insufficient_funds" not in codes


def test_risky_or_deposit_flow_still_never_earns_insufficient_funds() -> None:
    """Even a flow with a deposit-shaped field must never earn `insufficient_funds`: B4 drops the
    seed entirely (its detector, `balance_check`, does not exist in B1's DETECTORS registry --
    outcomes.validate() would fail loudly the moment it were ever emitted), so there is no
    predicate left that could produce this code at all."""
    steps = [_step_with_target(0, "type", "Deposit Amount")]
    codes = {o.code for o in _seed_known_outcomes(steps, [])}
    assert "insufficient_funds" not in codes


def test_reauth_rule_only_seeded_when_a_login_prefix_exists() -> None:
    left_entry = Checkpoint(kind="url_matches", target="any_frame", value="http://fake/app")
    with_prefix = [_step(0, "type"), _step(1, "click", left_entry)]
    rule_ids_with = {r.id for r in _seed_recovery_rules(with_prefix, "http://fake/login")}
    assert "reauth_on_session_expiry" in rule_ids_with
    assert _UNCONDITIONAL_RECOVERY_SEED_IDS <= rule_ids_with

    without_prefix = [_step(0, "type")]
    rule_ids_without = {r.id for r in _seed_recovery_rules(without_prefix, "http://fake/login")}
    assert "reauth_on_session_expiry" not in rule_ids_without
    assert _UNCONDITIONAL_RECOVERY_SEED_IDS <= rule_ids_without


# --------------------------------------------------------------------------------------------
# B5: descriptor name / frame_path parameterization
# --------------------------------------------------------------------------------------------


def test_recorder_generalizes_an_embedded_literal_name_to_a_regex() -> None:
    target = TargetDescriptor(role="link", name="12345 - Testuser Alpha", name_match="exact")
    generalized = _parameterize_target(target, {"12345": "member_id"})
    assert generalized.name == ":member_id.*"
    assert generalized.name_match == "regex"
    assert "member_id" in generalized.notes


def test_recorder_generalizes_an_exact_literal_name_without_regex() -> None:
    target = TargetDescriptor(role="textbox", name="12345", name_match="exact")
    generalized = _parameterize_target(target, {"12345": "member_id"})
    assert generalized.name == ":member_id"
    assert generalized.name_match == "exact"


def test_recorder_generalizes_a_frame_path_segment() -> None:
    target = TargetDescriptor(
        role="generic",
        name="Savings Balance",
        frame_path=["contentframe", "/member/12345/balance"],
    )
    generalized = _parameterize_target(target, {"12345": "member_id"})
    assert generalized.frame_path == ["contentframe", "/member/:member_id/balance"]


# --------------------------------------------------------------------------------------------
# B6: success checkpoint generalization
# --------------------------------------------------------------------------------------------


def test_recorder_generalizes_a_checkpoint_that_is_really_the_runs_own_output() -> None:
    checkpoint = Checkpoint(kind="text_present", target="page", value="$1,204.55")
    act_events: list[dict[str, Any]] = [
        {
            "context": {
                "tool": "extract",
                "target": {"role": "generic", "name": "Savings Balance"},
            },
            "act_result": "$1,204.55",
        }
    ]
    generalized = _generalize_success_checkpoint(checkpoint, act_events)
    assert generalized.kind == "element_present"
    assert generalized.target == "generic"
    assert generalized.value == "Savings Balance"


def test_recorder_leaves_a_checkpoint_alone_when_no_extract_produced_it() -> None:
    checkpoint = Checkpoint(kind="text_present", target="page", value="Transfer Complete")
    act_events: list[dict[str, Any]] = [
        {
            "context": {
                "tool": "extract",
                "target": {"role": "generic", "name": "Savings Balance"},
            },
            "act_result": "$1,204.55",
        }
    ]
    generalized = _generalize_success_checkpoint(checkpoint, act_events)
    assert generalized == checkpoint


# --------------------------------------------------------------------------------------------
# engine interpolation: a regex-generalized descriptor resolves to the RIGHT element, never the
# recording's own literal one.
# --------------------------------------------------------------------------------------------


def test_engine_interpolates_a_regex_descriptor_to_the_right_member_only() -> None:
    capability = _rebuilt_capability()
    step5 = next(
        s
        for s in capability.steps
        if s.action == "click" and s.target is not None and s.target.name_match == "regex"
    )
    assert step5.target is not None  # narrows for mypy-adjacent readers

    bravo_page = Observation(
        url="http://fake/x",
        title="Fake",
        elements=[
            UIElement(
                node_id="0", role="link", name="22222 - Sample Bravo", frame_path=["contentframe"]
            )
        ],
    )
    alpha_page = Observation(
        url="http://fake/x",
        title="Fake",
        elements=[
            UIElement(
                node_id="0",
                role="link",
                name="12345 - Testuser Alpha",
                frame_path=["contentframe"],
            )
        ],
    )

    interpolated = _interpolate_descriptor(step5.target, capability, {"member_id": 22222})
    assert resolve(interpolated, bravo_page).element is not None
    assert resolve(interpolated, alpha_page).element is None


# --------------------------------------------------------------------------------------------
# parameter type validation: a bad type is INVALID_PARAMS, and no browser is launched.
# --------------------------------------------------------------------------------------------


def test_bad_param_type_is_invalid_params_before_any_browser_launches(tmp_path: Path) -> None:
    capability = Capability(
        capability_id="type-check",
        name="n",
        description="d",
        target=TargetApp(app_id="a", entry_point="http://127.0.0.1:1/unreachable"),
        inputs=[InputParam(name="member_id", type="integer", required=True)],
        steps=[],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )
    artifact_path = tmp_path / "type-check.json"
    artifact_path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")

    result = replay_engine.replay(
        artifact_path, {"member_id": "not-a-number"}, POLICY_PATH, evidence_base_dir=tmp_path
    )

    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.INVALID_PARAMS
    # If a browser had launched, an unreachable entry point would report TARGET_UNREACHABLE
    # instead -- getting INVALID_PARAMS proves the type check ran first, before any browser did.


# ==============================================================================================
# LIVE tests below this line: drive the real fixture app. Skip loudly if it is not reachable.
# ==============================================================================================


def _fixture_reachable(host: str = "127.0.0.1", port: int = 5055) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _skip_if_fixture_unreachable() -> None:
    if not _fixture_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )


def _live_capability_path(tmp_path: Path, entry_point: str, name: str = "capability.json") -> Path:
    """A copy of the REAL capability (rebuilt from the real evidence directory, never the frozen
    artifacts/*.v2.json file -- see this module's own docstring for why: v2 was recorded before
    the B4 known_outcomes gating existed, and its own `insufficient_funds` seed now names a
    detector, `balance_check`, that no longer exists at all -- `outcomes.validate()` correctly
    refuses it at load time, for EVERY row, not just the ones the phase brief anticipated. That
    is real, measured behaviour, reported in full below and in this session's own report; it is
    also exactly why every live test here is built from the real evidence directory instead).
    Only `target.entry_point` is rewritten, to reach the fixture's one injection door
    (`?inject=<mode>` on /login). Copying a capability for a test is fine; editing artifacts/ is
    not.
    """
    capability = _rebuilt_capability()
    updated = capability.model_copy(
        update={"target": capability.target.model_copy(update={"entry_point": entry_point})}
    )
    path = tmp_path / name
    path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return path


def _events(base_dir: Path) -> list[dict[str, Any]]:
    run_dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1, run_dirs
    text = (run_dirs[0] / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


_PARAMS = {"password": "testpass", "member_id": 12345}


def test_live_row1_happy_path(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    artifact = _live_capability_path(tmp_path, "http://127.0.0.1:5055/login")
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "success"
    assert result.outputs["savings_balance"] == "$1,204.55"


def test_live_row2_different_member_same_artifact(tmp_path: Path) -> None:
    """v2 predates B5/B6 and cannot do this at all (its step 5 descriptor is an exact match on
    member 12345's own rendered link text, and its success checkpoint is member 12345's own
    balance) -- this is exactly the defect B5/B6 fix, exercised against the rebuilt capability."""
    _skip_if_fixture_unreachable()
    artifact = _live_capability_path(tmp_path, "http://127.0.0.1:5055/login")
    params = {"password": "testpass", "member_id": 22222}
    result = replay_engine.replay(artifact, params, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "success"
    assert result.outputs["savings_balance"] == "$532.10"
    assert result.outputs["savings_balance"] != "$1,204.55"


def test_live_row3_member_not_found(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    artifact = _live_capability_path(tmp_path, "http://127.0.0.1:5055/login")
    params = {"password": "testpass", "member_id": 99999}
    result = replay_engine.replay(artifact, params, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "business_outcome"
    assert result.code == "member_not_found"
    # `message` is the capability's own declared meaning (KnownOutcome.message_template);
    # `observed` is the application's own literal wording. See models/result.py's
    # BusinessOutcome docstring for why the two must never collapse into one field.
    assert result.observed == "No member matches that search."
    assert result.message == "No member found for the given id."


def test_live_row4_permission_denied(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    artifact = _live_capability_path(tmp_path, "http://127.0.0.1:5055/login")
    params = {"password": "testpass", "member_id": 55555}
    result = replay_engine.replay(artifact, params, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "business_outcome"
    assert result.code == "permission_denied"
    assert result.observed == "You do not have permission to view member 55555."
    assert result.message == "You do not have permission to view this record."


def test_live_row5_validation_rejected(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=validation"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "business_outcome"
    assert result.code == "validation_rejected"
    assert result.observed == "Member ID could not be validated. Please re-enter."
    assert result.message == "The submitted value could not be validated."


def test_live_row6_html_interstitial_recovered(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=unexpected_dialog"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "success"
    recovered = [
        e
        for e in _events(tmp_path)
        if e.get("type") == "recovered" and e.get("rule_id") == "dismiss_html_interstitial"
    ]
    assert recovered


def test_live_row7_native_dialog_recovered(tmp_path: Path) -> None:
    """MEASURED: the fixture's `native_dialog` mode is SESSION-PERSISTENT (unlike
    `unexpected_dialog`, one-shot), so it injects a FRESH `window.confirm()` on every page load a
    frameset reload produces. Round 2 fixed the real defect: `_make_dialog_policy`'s budget was
    scoped to the whole RUN, so `max_attempts=3` meant three dialogs total across all seven steps
    -- six dialogs across the flow genuinely exhausted it and the run ended UNHANDLED_DIALOG.
    `max_attempts` on a RecoveryRule is a PER-STEP budget everywhere else in this engine
    (`attempts_by_rule` in `_run_step`); `_DialogPolicy.reset()`, called at the start of every
    step (replay/engine.py), makes the dialog budget agree -- no single step's own dialogs ever
    exceed 3, so the run now reaches Success end to end, and every dismissal is reported with the
    real dialog text.
    """
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=native_dialog"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)

    recovered = [
        e
        for e in _events(tmp_path)
        if e.get("type") == "recovered" and e.get("rule_id") == "dismiss_native_dialog"
    ]
    assert recovered, "expected at least one native-dialog dismissal to be reported"
    assert all("confirm" in e["result"] for e in recovered)
    assert all("Are you sure?" in e["result"] for e in recovered)

    assert result.kind == "success"


def test_dialog_policy_budget_is_per_step_and_still_real() -> None:
    """Offline: the per-step reset must not make the budget unlimited. A step with FOUR dialogs
    and max_attempts=3 must still leave the fourth undismissed -- the run-level cap
    (`_MAX_RECOVERY_ATTEMPTS_PER_RUN`) is not the only thing standing between this policy and a
    blanket auto-dismiss; the per-step budget itself has to be a real ceiling, not merely reset to
    a number that never runs out within one step."""
    rule = RecoveryRule(
        id="dismiss_native_dialog",
        trigger="native_dialog_appeared",
        action="dismiss_dialog",
        max_attempts=3,
    )
    capability = _minimal_capability(recovery_rules=[rule])
    policy = replay_engine._make_dialog_policy(capability)

    decisions = [policy({"dialog_type": "confirm", "message": str(i)}) for i in range(4)]
    assert decisions == ["dismiss", "dismiss", "dismiss", "none"]

    # A fresh step gets a fresh budget -- reset() is what makes max_attempts mean "per step".
    policy.reset()
    assert policy({"dialog_type": "confirm", "message": "next step"}) == "dismiss"


def test_live_row8_slow_load_wait_recovery(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=slow_load"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "success"
    waits = [
        e
        for e in _events(tmp_path)
        if e.get("type") == "recovered" and e.get("rule_id") == "wait_for_slow_load"
    ]
    assert waits
    assert all(e["backoff_ms"] is None for e in waits)  # a real condition wait, never a backoff


def test_live_row9_transient_failure_backoff_strictly_increases(tmp_path: Path) -> None:
    """MEASURED, after the Round 2 fixture fix: fixtures/legacy_bank/app.py's transient_failure
    counter is now keyed on the SESSION, not on request.path. The old per-path counter gave this
    app's three independent frameset paths (/app, /nav, /members) their own separate 0->3 quotas
    -- that models three concurrent outages, not the one brief, whole-session outage a transient
    failure actually is, and it was why a lingering 503 in an unrelated frame could still fire
    `retry` on a later, non-navigating step and wipe that step's own just-typed value via
    `retry`'s page-wide reload.

    With one counter for the whole session: the login click's own navigation to /app fails twice
    (count 1, 2) and succeeds the third time (count 3); by the time /nav and /members load as
    children of that same successful /app response, the counter is already past 3, so they load
    clean with no retry needed. The run now reaches Success end to end, with exactly two `retry`
    recovery events, both against the login click's own step, with strictly increasing
    backoff_ms.
    """
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=transient_failure"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)

    retries = [
        e
        for e in _events(tmp_path)
        if e.get("type") == "recovered" and e.get("rule_id") == "retry_transient_failure"
    ]
    assert len(retries) == 2
    assert len({e["step_id"] for e in retries}) == 1  # both against the same (login) step
    backoffs = [e["backoff_ms"] for e in retries]
    assert backoffs == [recovery.backoff_ms_for_attempt(1), recovery.backoff_ms_for_attempt(2)]
    assert backoffs[1] > backoffs[0]

    assert result.kind == "success"


def test_live_row10_session_expired_reports_the_real_outcome(tmp_path: Path) -> None:
    """MEASURED, after the Round 2 off-by-one fix to `session_lost_mid_flow`: the fixture's
    `session_expired` injection clears the session (and itself) on the very next non-exempt
    request after arming, which is the redirect this capability's own login click (step 2, the
    LAST of its 3-step login prefix) produces -- that redirect lands back on /login, mid-step,
    which IS a session loss, not merely a candidate for one (the trigger is evaluated AFTER the
    step's action has already run, so step `login_prefix_len - 1` is exactly the step whose own
    action should have left the login page).

    Reauth fires once: `_apply_reauth` re-navigates to the capability's own recorded entry point,
    which is `/login?inject=session_expired` -- so the very re-navigation reauth performs to log
    back in RE-ARMS the identical injection that caused the loss. Reauth's own login attempt
    lands back on /login too, and (its budget being 1) the run correctly stops there rather than
    looping: the SECOND time this step's own action re-dispatches against an emptied login form,
    the reauth rule's per-step budget is spent, so `recovery.unrecovered()` gets the final word.
    Both halves are asserted: the reauth event genuinely fired, and the terminal category is
    SESSION_EXPIRED, not a generic POSTCONDITION_FAILED.
    """
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=session_expired"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)

    reauth_events = [
        e
        for e in _events(tmp_path)
        if e.get("type") == "recovered" and e.get("rule_id") == "reauth_on_session_expiry"
    ]
    assert reauth_events, "expected reauth to have been attempted at least once"
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.SESSION_EXPIRED


def test_live_row11_app_error_hard_failure_with_evidence(tmp_path: Path) -> None:
    _skip_if_fixture_unreachable()
    entry = "http://127.0.0.1:5055/login?inject=app_error"
    artifact = _live_capability_path(tmp_path, entry)
    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.APP_ERROR
    assert result.evidence_refs


def test_live_row19_mutated_name_still_resolves_and_reports_drift(tmp_path: Path) -> None:
    """Before this phase, mutating step 0's target name to "NoSuchControlAnywhere" neither failed
    the replay nor produced any warning (measured, docs/reports/phase-6.md). MEASURED here: the
    mutated descriptor resolves via RELATIONAL (rank 4), not literally "its ordinal" -- the real
    descriptor also carries a relational hint (a row labelled "Username"), which the name mutation
    does not touch, and that rung is tried before ROLE_ORDINAL.

    `recorded_rank` is None on this descriptor (this evidence log predates that field), so
    Round 2's `_drift_reason` fires its SECOND clause, not the rank-comparison one: the mutated
    descriptor still carries a non-empty name ("NoSuchControlAnywhere"), and the strategy that
    actually won (RELATIONAL) is not one of the three name-matching rungs -- drift is detected
    with no baseline invented for the missing `recorded_rank` at all, and the event logs that
    field as null rather than a guessed number.
    """
    _skip_if_fixture_unreachable()
    capability = _rebuilt_capability()
    assert capability.steps[0].target is not None
    assert capability.steps[0].target.recorded_rank is None  # this log predates the field
    mutated_target = capability.steps[0].target.model_copy(
        update={"name": "NoSuchControlAnywhere"}
    )
    new_steps = [
        capability.steps[0].model_copy(update={"target": mutated_target}),
        *capability.steps[1:],
    ]
    capability = capability.model_copy(
        update={
            "steps": new_steps,
            "target": capability.target.model_copy(
                update={"entry_point": "http://127.0.0.1:5055/login"}
            ),
        }
    )
    artifact = tmp_path / "mutated.json"
    artifact.write_text(capability.model_dump_json(indent=2), encoding="utf-8")

    result = replay_engine.replay(artifact, _PARAMS, POLICY_PATH, evidence_base_dir=tmp_path)

    assert result.kind == "success"
    drift = [
        e for e in _events(tmp_path) if e.get("type") == "locator_drift" and e.get("step_id") == 0
    ]
    assert drift
    assert drift[0]["recorded_name"] == "NoSuchControlAnywhere"
    assert drift[0]["recorded_rank"] is None  # never a substituted/guessed number
    assert drift[0]["clause"] == "name_no_longer_matched"
    assert drift[0]["actual_rank"] == 4  # RELATIONAL
    assert drift[0]["strategy"] == "relational"
