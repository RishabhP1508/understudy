"""Phase 10: the control core -- ControlToken/SessionBroker (escalation/control.py),
InterventionStore (escalation/store.py), InterventionRequest/InterventionResolution/ReasonCode/
HumanAction (models/intervention.py), PolicyGate's control-token check plus the one-shot approval
(safety/policy.py), and the FastAPI operator console (escalation/operator_app.py).

Most of this file is offline: no browser, no network, no API key, no fixture app. Every store
here takes base_dir=tmp_path (tests/conftest.py enforces this for the whole suite). The one
exception is the LIVE section at the bottom (task B's human-action capture,
`surface/web.py`'s `install_human_action_capture`/`drain_human_actions`), which drives the real
fixture app through a real headed browser and skips loudly if either is unavailable -- the same
convention test_phase5.py's own live tests use, because an offline stub of Playwright's own JS
execution would be stubbing the exact mechanism under test.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from understudy.agent.loop import RunStatus, run
from understudy.escalation import store as store_module
from understudy.escalation.control import (
    ControlHeld,
    ControlState,
    ControlToken,
    IllegalTransition,
    SessionBroker,
)
from understudy.escalation.operator_app import create_app
from understudy.escalation.store import InterventionStore
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.artifact import Capability, Checkpoint, Provenance, Step, TargetApp
from understudy.models.intervention import (
    HumanAction,
    InterventionRequest,
    InterventionResolution,
    ReasonCode,
)
from understudy.models.observation import Observation, UIElement
from understudy.models.result import FailureCategory
from understudy.record.recorder import build_capability
from understudy.replay import engine as replay_engine
from understudy.safety.policy import (
    EscalationRequired,
    Policy,
    PolicyDenied,
    PolicyGate,
    load_policy,
)
from understudy.surface.base import Action, Click, Navigate, Type
from understudy.surface.locator import TargetDescriptor

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"


class _FakeSurface:
    """Mirrors test_phase5.py's own minimal Surface fake: no browser, .url settable at
    construction and updated by act() on a Navigate."""

    def __init__(self, url: str = "http://127.0.0.1:5055/members") -> None:
        self._url = url
        self.acted: list[Action] = []
        self.navigation_violations: list[str] = []

    @property
    def url(self) -> str:
        return self._url

    def observe(self) -> Observation:
        return Observation(url=self._url, title="Fake", elements=[])

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Navigate):
            self._url = action.url
        return None


def _make_request(id: str = "int-1", **overrides: object) -> InterventionRequest:
    base: dict[str, object] = dict(
        id=id,
        run_id="run-1",
        capability_id=None,
        goal="look up member 12345 and read their savings balance",
        step_id=3,
        reason_code=ReasonCode.STUCK_NO_PROGRESS,
        what_it_tried="clicked the search button",
        what_it_observed="the page did not change after three attempts",
        observation=Observation(url="http://127.0.0.1:5055/members", title="Members", elements=[]),
        screenshot_path=None,
        context={},
        created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-01T00:05:00+00:00",
    )
    base.update(overrides)
    return InterventionRequest.model_validate(base)


def _member_textbox() -> UIElement:
    return UIElement(node_id="0", role="textbox", name="Member ID")


# ----------------------------------------------------------------------------------------------
# DoD 1: dispatch refuses while the token is HUMAN; allows while AUTOMATION. PENDING_HANDOFF and
# PENDING_RESUME also refuse -- both transient states, both asserted, because that is the reason
# there are four states and not two.
# ----------------------------------------------------------------------------------------------


def test_dispatch_allows_while_automation(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface()
    broker = SessionBroker(surface, InterventionStore(base_dir=tmp_path), run_id="run-1")
    gate = PolicyGate(policy, mode="discovery", broker=broker)

    gate.dispatch(surface, Type(node_id="0", text="12345"), element=_member_textbox())
    assert len(surface.acted) == 1


def test_dispatch_refuses_while_human(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface()
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-1")
    gate = PolicyGate(policy, mode="discovery", broker=broker)

    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    broker.transition(ControlState.HUMAN, actor="operator", reason="took control")

    with pytest.raises(PolicyDenied) as exc_info:
        gate.dispatch(surface, Type(node_id="0", text="12345"), element=_member_textbox())
    assert exc_info.value.decision.rule == "control_token"
    assert surface.acted == []  # Surface.act was never reached


@pytest.mark.parametrize(
    "state",
    [ControlState.PENDING_HANDOFF, ControlState.PENDING_RESUME],
)
def test_dispatch_refuses_during_transient_states(tmp_path: Path, state: ControlState) -> None:
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface()
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-1")
    gate = PolicyGate(policy, mode="discovery", broker=broker)

    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    if state == ControlState.PENDING_RESUME:
        broker.transition(ControlState.HUMAN, actor="operator", reason="took control")
        broker.transition(ControlState.PENDING_RESUME, actor="operator", reason="handed back")

    assert broker.state().state == state
    with pytest.raises(PolicyDenied) as exc_info:
        gate.dispatch(surface, Type(node_id="0", text="12345"), element=_member_textbox())
    assert exc_info.value.decision.rule == "control_token"
    assert surface.acted == []


# ----------------------------------------------------------------------------------------------
# DoD 2: every allowed transition succeeds; at least three illegal ones raise IllegalTransition.
# ----------------------------------------------------------------------------------------------


def test_full_handoff_and_direct_approve_cycles_succeed(tmp_path: Path) -> None:
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(_FakeSurface(), store, run_id="run-1")
    assert broker.state().state == ControlState.AUTOMATION

    # AUTOMATION -> PENDING_HANDOFF -> HUMAN -> PENDING_RESUME -> AUTOMATION
    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    assert broker.state().state == ControlState.PENDING_HANDOFF
    broker.transition(ControlState.HUMAN, actor="operator", reason="took control")
    assert broker.state().state == ControlState.HUMAN
    broker.transition(ControlState.PENDING_RESUME, actor="operator", reason="handed back")
    assert broker.state().state == ControlState.PENDING_RESUME
    broker.transition(ControlState.AUTOMATION, actor="runner", reason="resumed")
    assert broker.state().state == ControlState.AUTOMATION

    # PENDING_HANDOFF -> AUTOMATION directly: approve/reject resolves without a full handoff.
    broker2 = SessionBroker(_FakeSurface(), store, run_id="run-2")
    broker2.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    broker2.transition(ControlState.AUTOMATION, actor="runner", reason="approved")
    assert broker2.state().state == ControlState.AUTOMATION


@pytest.mark.parametrize(
    ("from_state_sequence", "to"),
    [
        # AUTOMATION -> HUMAN directly, skipping PENDING_HANDOFF.
        ([], ControlState.HUMAN),
        # HUMAN -> PENDING_HANDOFF.
        ([ControlState.PENDING_HANDOFF, ControlState.HUMAN], ControlState.PENDING_HANDOFF),
        # PENDING_HANDOFF -> PENDING_RESUME, skipping HUMAN.
        ([ControlState.PENDING_HANDOFF], ControlState.PENDING_RESUME),
        # AUTOMATION -> PENDING_RESUME directly.
        ([], ControlState.PENDING_RESUME),
    ],
)
def test_illegal_transitions_raise(
    tmp_path: Path, from_state_sequence: list[ControlState], to: ControlState
) -> None:
    store = InterventionStore(base_dir=tmp_path)
    run_id = f"run-{to.value}-{len(from_state_sequence)}"
    broker = SessionBroker(_FakeSurface(), store, run_id=run_id)
    for index, state in enumerate(from_state_sequence):
        broker.transition(state, actor=f"actor-{index}", reason="setup")
    before = broker.state()

    with pytest.raises(IllegalTransition):
        broker.transition(to, actor="someone", reason="illegal")

    # A rejected transition must not have mutated the token.
    assert broker.state() == before


# ----------------------------------------------------------------------------------------------
# DoD 3: every transition logs a control_transition event carrying from, to, actor, and a
# timestamp (RunEvent's own universal `ts` field).
# ----------------------------------------------------------------------------------------------


def test_transition_logs_control_transition_event(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase10-t3", "test", base_dir=tmp_path / "evidence")
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(_FakeSurface(), store, run_id="run-1", logger=logger)

    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck_no_progress")

    lines = (logger.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    transitions = [e for e in events if e["type"] == "control_transition"]
    assert len(transitions) == 1
    event = transitions[0]
    assert event["from_state"] == ControlState.AUTOMATION.value
    assert event["to_state"] == ControlState.PENDING_HANDOFF.value
    assert event["actor"] == "runner"
    assert event["ts"]  # the universal timestamp every RunEvent carries


def test_transition_chain_on_the_record_carries_both_runner_and_operator_moves(
    tmp_path: Path,
) -> None:
    """F3 (Phase 10 round F): before this fix, only the runner's OWN transitions ever appeared
    anywhere durable (run.jsonl's `control_transition` event, logged only in the process that
    happens to hold a logger) -- the operator console's half of a handoff, made in a SEPARATE
    process with no run logger of its own, appeared nowhere as a transition, only implicitly in
    the token's final `holder`. This proves the intervention record's own `transitions` list
    carries the FULL custody chain, oldest first, regardless of which process made each move.
    """
    store_dir = tmp_path / "interventions"
    store = InterventionStore(base_dir=store_dir)
    request = _make_request(id="int-chain-1")
    store.create(request)

    # The runner escalates (its own move: AUTOMATION -> PENDING_HANDOFF).
    runner_broker = SessionBroker(_FakeSurface(), store, run_id=request.run_id)
    runner_broker.intervention_id = request.id
    runner_broker.transition(
        ControlState.PENDING_HANDOFF, actor="runner", reason="escalating: stuck_no_progress"
    )

    # The operator console does the rest of a FULL handoff, in its own process/broker instance
    # (mirroring escalation/operator_app.py's own `_broker_for`, which never shares the runner's
    # broker object or its logger).
    operator_broker = SessionBroker(_FakeSurface(), store, run_id=request.run_id, holder="operator")
    operator_broker.intervention_id = request.id
    operator_broker.transition(ControlState.HUMAN, actor="operator", reason="operator took control")
    operator_broker.transition(
        ControlState.PENDING_RESUME, actor="operator", reason="operator returned control"
    )

    # The runner takes control back once it observes the handoff resolved.
    runner_broker.transition(
        ControlState.AUTOMATION, actor="runner", reason="resolved: took_control"
    )

    record = store.get(request.id)
    assert record is not None
    chain = [(t.from_state, t.to_state, t.actor) for t in record.transitions]
    assert chain == [
        (ControlState.AUTOMATION, ControlState.PENDING_HANDOFF, "runner"),
        (ControlState.PENDING_HANDOFF, ControlState.HUMAN, "operator"),
        (ControlState.HUMAN, ControlState.PENDING_RESUME, "operator"),
        (ControlState.PENDING_RESUME, ControlState.AUTOMATION, "runner"),
    ]


# ----------------------------------------------------------------------------------------------
# DoD 4: the one-shot approval. A granted approval lets exactly ONE RISKY_IRREVERSIBLE dispatch
# through; the SECOND identical dispatch is refused again.
# ----------------------------------------------------------------------------------------------


def test_one_shot_approval_allows_exactly_one_dispatch(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface(url="http://127.0.0.1:5055/member/12345")
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-1")
    gate = PolicyGate(policy, mode="discovery", broker=broker)
    element = UIElement(node_id="0", role="button", name="Transfer Funds")

    request = _make_request(
        id="int-risky",
        reason_code=ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL,
        what_it_tried="clicked 'Transfer Funds'",
        what_it_observed="PolicyGate refused: RISKY_IRREVERSIBLE requires approval",
    )
    store.create(request)
    broker.intervention_id = request.id
    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="risky action refused")

    # The operator approves without a full handoff.
    broker.grant_approval(request.id)
    broker.transition(ControlState.AUTOMATION, actor="operator", reason="approved")

    # First dispatch: the one-shot approval lets it through.
    gate.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1

    # Second, identical dispatch: the approval was already consumed -- refused again.
    with pytest.raises(EscalationRequired):
        gate.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1  # unchanged


# ----------------------------------------------------------------------------------------------
# DoD 5: session() refuses a caller that is not the holder.
# ----------------------------------------------------------------------------------------------


def test_session_refuses_non_holder(tmp_path: Path) -> None:
    surface = _FakeSurface()
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-1", holder="runner")

    assert broker.session("runner") is surface
    with pytest.raises(ControlHeld):
        broker.session("operator")


# ----------------------------------------------------------------------------------------------
# DoD 6: await_resolution returns the resolution once one is written, and returns None once
# expires_at has passed -- no real sleeping in either case.
# ----------------------------------------------------------------------------------------------


def test_await_resolution_returns_resolution_already_written(tmp_path: Path) -> None:
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(_FakeSurface(), store, run_id="run-1")
    request = _make_request(id="int-await-1", expires_at="2099-01-01T00:00:00+00:00")
    store.create(request)
    resolution = InterventionResolution(
        resolved_by="operator",
        action_taken="approved",
        human_actions=[],
        notes="looks fine",
        resolved_at="2026-01-01T00:01:00+00:00",
    )
    store.resolve(request.id, resolution)

    result = broker.await_resolution(request, poll_interval_s=0.01)
    assert result is not None
    assert result.action_taken == "approved"
    assert result.resolved_by == "operator"


def test_await_resolution_returns_none_once_expired(tmp_path: Path) -> None:
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(_FakeSurface(), store, run_id="run-1")
    # Already expired: the very first check must return None with no sleep at all.
    request = _make_request(id="int-await-2", expires_at="2020-01-01T00:00:00+00:00")
    store.create(request)

    result = broker.await_resolution(request, poll_interval_s=0.01)
    assert result is None


# ----------------------------------------------------------------------------------------------
# DoD 7: the store round-trips a request/token/resolution, and a resolution containing the
# sentinel SECRET_SENTINEL_VALUE is redacted on disk.
# ----------------------------------------------------------------------------------------------


def test_store_round_trips_and_redacts_resolution(tmp_path: Path) -> None:
    store = InterventionStore(base_dir=tmp_path)
    request = _make_request(id="int-secret")
    store.create(request)

    token = ControlToken(
        state=ControlState.PENDING_HANDOFF,
        holder="runner",
        intervention_id=request.id,
        updated_at="2026-01-01T00:00:30+00:00",
    )
    store.set_token(request.id, token)

    resolution = InterventionResolution(
        resolved_by="operator",
        action_taken="approved",
        human_actions=[
            HumanAction(
                kind="input",
                role="textbox",
                name="SSN",
                value="123-45-6789",
                url=None,
                at="2026-01-01T00:01:30+00:00",
            )
        ],
        notes="SECRET_SENTINEL_VALUE",
        resolved_at="2026-01-01T00:02:00+00:00",
    )
    store.resolve(request.id, resolution)

    record = store.get(request.id)
    assert record is not None
    assert record.request.id == request.id
    assert record.request.run_id == request.run_id
    assert record.token is not None
    assert record.token.state == ControlState.PENDING_HANDOFF
    assert record.resolution is not None
    assert record.resolution.resolved_by == "operator"
    assert record.resolution.action_taken == "approved"

    raw_text = (tmp_path / f"{request.id}.json").read_text(encoding="utf-8")
    assert "SECRET_SENTINEL_VALUE" not in raw_text
    assert "123-45-6789" not in raw_text

    assert store.get("no-such-id") is None


def test_list_open_excludes_resolved_interventions(tmp_path: Path) -> None:
    store = InterventionStore(base_dir=tmp_path)
    open_request = _make_request(id="int-open")
    store.create(open_request)

    resolved_request = _make_request(id="int-resolved")
    store.create(resolved_request)
    store.resolve(
        resolved_request.id,
        InterventionResolution(
            resolved_by="operator",
            action_taken="rejected",
            human_actions=[],
            notes="",
            resolved_at="2026-01-01T00:02:00+00:00",
        ),
    )

    open_ids = {r.id for r in store.list_open()}
    assert open_ids == {open_request.id}


# ----------------------------------------------------------------------------------------------
# Round E: `_locked()`'s own acquisition loop, under real thread contention on ONE intervention
# id, and its bounded deadline when a lock never clears.
# ----------------------------------------------------------------------------------------------


def test_locked_survives_real_thread_contention_on_one_intervention_id(tmp_path: Path) -> None:
    """The actual defect this round found: hammering `set_token`/`consume_approval`/`resolve`
    for ONE intervention id from several threads, with no delay between calls, drives
    `os.open(lock_path, O_CREAT | O_EXCL)` to raise `PermissionError` on Windows -- an NTFS
    create/delete metadata race between one thread's create and another's create-or-unlink of
    the SAME lock path, not a real ACL denial -- rather than the `FileExistsError` the O_EXCL
    contract documents. Confirmed directly against the pre-fix code: this exact thread/iteration
    count reproduced it on the first attempt, reliably, in well under a second. `_locked` must
    retry through it instead of propagating it and crashing the caller.
    """
    store = InterventionStore(base_dir=tmp_path)
    rid = "stress-id"
    store.create(_make_request(id=rid))

    errors: list[Exception] = []

    def worker(n: int) -> None:
        for i in range(30):
            try:
                if i % 3 == 0:
                    store.set_token(
                        rid,
                        ControlToken(
                            state=ControlState.PENDING_HANDOFF,
                            holder="runner",
                            updated_at="2026-01-01T00:00:00+00:00",
                        ),
                    )
                elif i % 3 == 1:
                    store.consume_approval(rid)
                else:
                    store.resolve(
                        rid,
                        InterventionResolution(
                            resolved_by="operator",
                            action_taken="approved",
                            human_actions=[],
                            notes="",
                            resolved_at="2026-01-01T00:00:00+00:00",
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 -- captured for the assertion below, not swallowed
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not [t for t in threads if t.is_alive()], "a worker thread did not finish"
    assert errors == [], f"lock acquisition raised under contention: {errors!r}"


def test_locked_raises_a_named_timeout_instead_of_hanging_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file left behind by a holder that crashed mid-hold is `_locked`'s own documented
    limitation. This proves that now surfaces as a clear `TimeoutError` naming the lock path,
    not the indefinite hang the pre-round `while True` spun forever on. The module's real
    deadline (`_LOCK_ACQUIRE_TIMEOUT_S`, a few seconds -- plenty for any genuine hold, which is a
    single small file read-modify-write) is monkeypatched down so this test does not itself wait
    that long."""
    monkeypatch.setattr(store_module, "_LOCK_ACQUIRE_TIMEOUT_S", 0.05)
    store = InterventionStore(base_dir=tmp_path)
    store.create(_make_request(id="int-stuck"))
    (tmp_path / "int-stuck.lock").write_bytes(b"")  # a lock a crashed holder never released

    with pytest.raises(TimeoutError, match=r"int-stuck\.lock"):
        store.set_token(
            "int-stuck",
            ControlToken(
                state=ControlState.PENDING_HANDOFF,
                holder="runner",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        )


# ----------------------------------------------------------------------------------------------
# B0: the one-shot approval must survive a PROCESS boundary. `test_one_shot_approval_allows_
# exactly_one_dispatch` above grants and consumes on the SAME broker object, which cannot express
# the real situation this feature exists for -- the operator console is a separate local process
# (CLAUDE.md), so its `grant_approval` call and the run's `consume_approval` call are two
# different Python objects, communicating only through the store on disk. This test uses TWO
# SEPARATE SessionBroker instances over the SAME store directory, one standing in for each
# process, to prove the grant actually crosses that boundary.
# ----------------------------------------------------------------------------------------------


def test_one_shot_approval_survives_a_process_boundary(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface(url="http://127.0.0.1:5055/member/12345")
    store = InterventionStore(base_dir=tmp_path)

    # Stands in for the RUN process.
    run_broker = SessionBroker(surface, store, run_id="run-1")
    gate = PolicyGate(policy, mode="discovery", broker=run_broker)
    element = UIElement(node_id="0", role="button", name="Transfer Funds")

    request = _make_request(
        id="int-risky-2p",
        reason_code=ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL,
        what_it_tried="clicked 'Transfer Funds'",
        what_it_observed="PolicyGate refused: RISKY_IRREVERSIBLE requires approval",
    )
    store.create(request)
    run_broker.intervention_id = request.id
    run_broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="risky refused")

    # Stands in for the OPERATOR CONSOLE process: a second, unrelated SessionBroker instance
    # that never shares memory with run_broker, only the store directory on disk.
    operator_broker = SessionBroker(_FakeSurface(), store, run_id="run-1", holder="operator")
    operator_broker.intervention_id = request.id
    operator_broker.grant_approval(request.id)
    operator_broker.transition(ControlState.AUTOMATION, actor="operator", reason="approved")

    # The run's OWN broker never had grant_approval called on it -- it only ever reads the store
    # -- and yet the gate now lets exactly one dispatch through, because the grant crossed the
    # boundary through the file, not through either broker's memory.
    gate.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1

    # Consumed: the SAME run broker's second dispatch is refused again.
    with pytest.raises(EscalationRequired):
        gate.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1

    # One-shot across a RESTART too, not just across the two live brokers above: a THIRD broker
    # (e.g. the run process restarting after a crash) still finds the approval already spent.
    third_broker = SessionBroker(surface, store, run_id="run-1")
    assert third_broker.consume_approval(request.id) is False


# ----------------------------------------------------------------------------------------------
# B3/B4: the FastAPI operator console (escalation/operator_app.py). Offline: FastAPI's own
# TestClient, tmp_path for both the intervention store and the evidence directory.
# ----------------------------------------------------------------------------------------------


def _operator_client(tmp_path: Path) -> tuple[TestClient, InterventionStore, Path, Path]:
    store_dir = tmp_path / "interventions"
    evidence_dir = tmp_path / "evidence"
    client = TestClient(create_app(store_dir=store_dir, evidence_dir=evidence_dir))
    return client, InterventionStore(base_dir=store_dir), store_dir, evidence_dir


def _set_pending_handoff(store: InterventionStore, request_id: str) -> None:
    store.set_token(
        request_id,
        ControlToken(
            state=ControlState.PENDING_HANDOFF,
            holder="runner",
            intervention_id=request_id,
            updated_at="2026-01-01T00:00:10+00:00",
        ),
    )


def test_operator_index_lists_open_intervention_with_a_control_banner(tmp_path: Path) -> None:
    client, store, _, _ = _operator_client(tmp_path)
    request = _make_request(id="int-index-1")
    store.create(request)
    _set_pending_handoff(store, request.id)

    response = client.get("/")
    assert response.status_code == 200
    assert request.goal in response.text
    # DoD 12: the banner is on the index page too, not only the detail page.
    assert "PENDING HANDOFF" in response.text


def test_operator_detail_renders_reason_tried_observed_and_a_screenshot_reference(
    tmp_path: Path,
) -> None:
    client, store, _, evidence_dir = _operator_client(tmp_path)
    steps_dir = evidence_dir / "discovery-run-detail" / "steps"
    steps_dir.mkdir(parents=True)
    (steps_dir / "003_before.png").write_bytes(b"masked-png-bytes-stand-in")

    request = _make_request(
        id="int-detail-1",
        run_id="run-detail",
        step_id=3,
        what_it_tried="clicked the search button",
        what_it_observed="the page did not change after three attempts",
        screenshot_path="steps/003_before.png",
    )
    store.create(request)
    _set_pending_handoff(store, request.id)

    response = client.get(f"/intervention/{request.id}")
    assert response.status_code == 200
    assert ReasonCode.STUCK_NO_PROGRESS.value in response.text
    assert "clicked the search button" in response.text
    assert "the page did not change after three attempts" in response.text
    assert f"/intervention/{request.id}/screenshot" in response.text


def test_take_control_then_return_control_moves_states_and_the_banner_changes(
    tmp_path: Path,
) -> None:
    client, store, _, _ = _operator_client(tmp_path)
    request = _make_request(id="int-handoff-1")
    store.create(request)
    _set_pending_handoff(store, request.id)

    take = client.post(f"/intervention/{request.id}/take-control", follow_redirects=False)
    assert take.status_code == 303

    after_take = client.get(f"/intervention/{request.id}")
    assert 'class="banner banner-human"' in after_take.text
    assert "CONTROL: <strong>HUMAN</strong>" in after_take.text
    assert 'class="banner banner-pending_handoff"' not in after_take.text

    ret = client.post(f"/intervention/{request.id}/return-control", follow_redirects=False)
    assert ret.status_code == 303

    after_return = client.get(f"/intervention/{request.id}")
    assert 'class="banner banner-pending_resume"' in after_return.text
    assert "CONTROL: <strong>PENDING RESUME</strong>" in after_return.text
    assert 'class="banner banner-human"' not in after_return.text


def test_approve_writes_resolution_and_leaves_an_approval_a_different_broker_can_consume_once(
    tmp_path: Path,
) -> None:
    client, store, store_dir, _ = _operator_client(tmp_path)
    request = _make_request(
        id="int-approve-1",
        reason_code=ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL,
        what_it_tried="clicked 'Transfer Funds'",
        what_it_observed="PolicyGate refused: RISKY_IRREVERSIBLE requires approval",
    )
    store.create(request)
    _set_pending_handoff(store, request.id)

    response = client.post(f"/intervention/{request.id}/approve", follow_redirects=False)
    assert response.status_code == 303

    record = store.get(request.id)
    assert record is not None
    assert record.resolution is not None
    assert record.resolution.action_taken == "approved"
    assert record.token is not None
    assert record.token.state == ControlState.AUTOMATION

    # A DIFFERENT broker (standing in for the run process) consumes it exactly once.
    run_broker = SessionBroker(
        _FakeSurface(), InterventionStore(base_dir=store_dir), run_id=request.run_id
    )
    assert run_broker.consume_approval(request.id) is True
    assert run_broker.consume_approval(request.id) is False


def test_reject_writes_resolution_and_grants_no_approval(tmp_path: Path) -> None:
    client, store, store_dir, _ = _operator_client(tmp_path)
    request = _make_request(
        id="int-reject-1", reason_code=ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL
    )
    store.create(request)
    _set_pending_handoff(store, request.id)

    response = client.post(f"/intervention/{request.id}/reject", follow_redirects=False)
    assert response.status_code == 303

    record = store.get(request.id)
    assert record is not None
    assert record.resolution is not None
    assert record.resolution.action_taken == "rejected"

    run_broker = SessionBroker(
        _FakeSurface(), InterventionStore(base_dir=store_dir), run_id=request.run_id
    )
    assert run_broker.consume_approval(request.id) is False


def test_approve_and_reject_are_not_offered_for_a_non_risky_reason_code(tmp_path: Path) -> None:
    client, store, _, _ = _operator_client(tmp_path)
    request = _make_request(id="int-nonrisky-1", reason_code=ReasonCode.STUCK_NO_PROGRESS)
    store.create(request)
    _set_pending_handoff(store, request.id)

    response = client.get(f"/intervention/{request.id}")
    assert response.status_code == 200
    assert "/approve" not in response.text
    assert "/reject" not in response.text
    assert "/take-control" in response.text

    # Defense in depth: the route itself refuses even called directly, not only omitted from the
    # rendered page.
    approve_response = client.post(f"/intervention/{request.id}/approve")
    assert approve_response.status_code == 400
    reject_response = client.post(f"/intervention/{request.id}/reject")
    assert reject_response.status_code == 400


def test_illegal_operator_move_is_refused_not_silently_accepted(tmp_path: Path) -> None:
    client, store, _, _ = _operator_client(tmp_path)
    request = _make_request(id="int-illegal-1")
    store.create(request)
    _set_pending_handoff(store, request.id)  # PENDING_HANDOFF, not HUMAN

    response = client.post(f"/intervention/{request.id}/return-control")
    assert response.status_code == 409

    record = store.get(request.id)
    assert record is not None
    assert record.token is not None
    assert record.token.state == ControlState.PENDING_HANDOFF  # unchanged


def test_operator_serves_the_masked_screenshot_file_never_an_unmasked_original(
    tmp_path: Path,
) -> None:
    """The evidence logger masks a screenshot BEFORE it ever reaches disk
    (evidence/logger.py's `screenshot()` calls `redact_screenshot` before `Path.write_bytes`,
    safety/redact.py's own module docstring: "there is no unredacted write path"). There is
    therefore no unmasked original anywhere on disk for this run at all, at any point -- a
    stronger property than "the operator masks it on the way out", which this test does NOT
    claim: it proves the operator serves exactly the bytes the evidence logger already wrote.

    F1 (Phase 10 round F): drives a REAL escalation (a genuine NO_PROGRESS discovery stall)
    rather than hand-constructing an `InterventionRequest` with a manually-supplied
    `screenshot_path` -- before this fix, both escalation call sites hardcoded
    `screenshot_path=None`, so nothing in production ever put a path on a request for this
    endpoint to serve, and the old version of this test proved only that the operator CAN serve
    whatever path it is handed, never that one is genuinely produced.
    """
    store_dir = tmp_path / "interventions"
    evidence_dir = tmp_path / "evidence"
    surface = _StallSurface()
    logger = EvidenceLogger("screenshot-esc", "test", base_dir=evidence_dir)
    store = InterventionStore(base_dir=store_dir)
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="make the counter move (it never will)",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.NO_PROGRESS
    assert outcome.intervention_id is not None

    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.screenshot_path is not None
    on_disk = (logger.dir / record.request.screenshot_path).read_bytes()
    assert on_disk  # a genuine masked PNG, not a placeholder

    client = TestClient(create_app(store_dir=store_dir, evidence_dir=evidence_dir))
    response = client.get(f"/intervention/{record.request.id}/screenshot")
    assert response.status_code == 200
    assert response.content == on_disk


def test_operator_detail_reports_no_screenshot_honestly_when_none_was_captured(
    tmp_path: Path,
) -> None:
    """F1: a surface with no `screenshot_bytes` at all (most fakes in this suite, and any real
    surface that refuses to mask) must leave `screenshot_path` None -- and the operator page must
    say so plainly rather than rendering a broken `<img>` against a path that does not exist."""
    store_dir = tmp_path / "interventions"
    evidence_dir = tmp_path / "evidence"
    surface = _RiskySurface()  # no screenshot_bytes attribute at all
    logger = EvidenceLogger("no-screenshot-esc", "test", base_dir=evidence_dir)
    store = InterventionStore(base_dir=store_dir)
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(risky_labels=["transfer"])
    gate = PolicyGate(policy, logger, mode="discovery", broker=broker)

    outcome = run(
        goal="transfer funds",
        target="http://fake/start",
        surface=surface,
        llm=_ClickRiskyLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.screenshot_path is None

    client = TestClient(create_app(store_dir=store_dir, evidence_dir=evidence_dir))
    response = client.get(f"/intervention/{record.request.id}")
    assert response.status_code == 200
    assert "none captured" in response.text
    assert f"/intervention/{record.request.id}/screenshot" not in response.text


# ----------------------------------------------------------------------------------------------
# B2: HumanAction round-trips through the store with SECRET_SENTINEL_VALUE in its `value` field
# and comes back redacted -- see test_store_round_trips_and_redacts_resolution above for the
# 123-45-6789 case; this covers the credential-shaped sentinel specifically.
# ----------------------------------------------------------------------------------------------


def test_human_action_value_carrying_sentinel_is_redacted_through_the_store(
    tmp_path: Path,
) -> None:
    store = InterventionStore(base_dir=tmp_path)
    request = _make_request(id="int-human-action-secret")
    store.create(request)

    resolution = InterventionResolution(
        resolved_by="operator",
        action_taken="took_control",
        human_actions=[
            HumanAction(
                kind="input",
                role="textbox",
                name="Password",
                value="SECRET_SENTINEL_VALUE",
                url=None,
                at="2026-01-01T00:01:00+00:00",
            )
        ],
        notes="",
        resolved_at="2026-01-01T00:02:00+00:00",
    )
    store.resolve(request.id, resolution)

    raw_text = (tmp_path / f"{request.id}.json").read_text(encoding="utf-8")
    assert "SECRET_SENTINEL_VALUE" not in raw_text

    record = store.get(request.id)
    assert record is not None
    assert record.resolution is not None
    assert record.resolution.human_actions[0].value != "SECRET_SENTINEL_VALUE"


# ----------------------------------------------------------------------------------------------
# B2: human-action capture. LIVE BROWSER + LIVE FIXTURE APP -- skips loudly if either is
# unavailable, the same convention test_phase5.py's own live tests use. An offline stub of
# Playwright's own JS execution would be stubbing the exact mechanism under test.
# ----------------------------------------------------------------------------------------------


def _fixture_app_reachable(host: str = "127.0.0.1", port: int = 5055) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _find(observation: Observation, role: str, name_contains: str) -> UIElement:
    for element in observation.elements:
        if element.role == role and name_contains in element.name:
            return element
    raise AssertionError(
        f"no element with role={role!r} whose name contains {name_contains!r}; got: "
        f"{[(e.role, e.name) for e in observation.elements]}"
    )


def test_live_human_action_capture_survives_navigation_click_and_type() -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055` before running "
            "this test"
        )

    from understudy.surface.web import WebSurface

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    gate = PolicyGate(policy, mode="discovery")
    try:
        # No explicit install_human_action_capture() call: WebSurface.__init__ installs it
        # unconditionally now (round H) -- this test exercises that default, not a manual step.
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login

        obs = surface.observe()
        username = _find(obs, "textbox", "Username")
        gate.dispatch(surface, Type(node_id=username.node_id, text="tester"), element=username)
        obs = surface.observe()
        password = _find(obs, "textbox", "Password")
        gate.dispatch(surface, Type(node_id=password.node_id, text="secret"), element=password)
        obs = surface.observe()
        login_button = _find(obs, "button", "Login")
        # -> /app: a REAL top-level navigation, exercising the exact thing
        # install_human_action_capture exists for -- the capture surviving it.
        gate.dispatch(surface, Click(node_id=login_button.node_id), element=login_button)

        actions = surface.drain_human_actions()
        second_drain = surface.drain_human_actions()
    finally:
        surface.close()

    kinds = [action.kind for action in actions]
    assert "navigate" in kinds  # at least the /login page's own init-script run
    input_actions = [action for action in actions if action.kind == "input"]
    assert any(action.value == "tester" and action.role == "textbox" for action in input_actions)
    click_actions = [action for action in actions if action.kind == "click"]
    assert any(action.role == "button" for action in click_actions)
    assert second_drain == []  # drained, not left to accumulate


# ================================================================================================
# TASK C: escalation wired into agent/loop.py and replay/engine.py.
#
# Offline where the condition can be driven deterministically without a browser (every discovery
# stopping condition, and replay/engine.py's `_run_step` called directly with a duck-typed fake
# surface -- `_run_step` is typed to take a concrete `WebSurface`, but nothing in Python enforces
# that at runtime, and the RESUME DECISION under test needs a controllable observe(), not a real
# page). Live, guarded by the same "fixture not reachable -> skip" pattern test_phase9.py uses,
# only for the two conditions that genuinely come from `recovery.unrecovered()` against the real
# fixture's own failure-injection routes (session_expired, app_error) and for the subaccount flow
# that gives the RISKY_IRREVERSIBLE "approved" retry something real to prove.
#
# Every intervention TTL in this section is either already-expired (<=0, so
# `SessionBroker.await_resolution`'s own first check returns None with no sleep at all -- see its
# docstring) or a generous positive bound, paired with a background "operator" thread that writes
# a resolution as soon as it sees the intervention appear. `await_resolution`'s own poll interval
# (2s, not overridable through the fixed `escalate(request, logger)` signature this phase
# specifies) is a REAL sleep in every one of those cases -- deliberately not monkeypatched away:
# `time` is one shared module object, and patching `time.sleep` to make the run's own poll loop
# instant silently disables every OTHER caller's real timing too, including this file's own
# polling helpers and escalation/store.py's write-retry backoff, which measurably broke both when
# tried. A handful of these tests each cost roughly one real poll interval; that is the honest
# price of a fixed 2s poll with no override point, not a test weakness.
# ================================================================================================


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


def _run_events(logger: EvidenceLogger) -> list[dict[str, Any]]:
    text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _replay_events(base_dir: Path) -> list[dict[str, Any]]:
    run_dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1, run_dirs
    text = (run_dirs[0] / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _wait_for_open_intervention(
    store: InterventionStore, timeout: float = 10.0, exclude: set[str] = frozenset()
) -> InterventionRequest:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        open_requests = [r for r in store.list_open() if r.id not in exclude]
        if open_requests:
            return open_requests[0]
        time.sleep(0.01)
    raise AssertionError("no open intervention appeared within the timeout")


def _resolve(store: InterventionStore, intervention_id: str, action_taken: str) -> None:
    store.resolve(
        intervention_id,
        InterventionResolution(
            resolved_by="operator",
            action_taken=action_taken,  # type: ignore[arg-type]
            human_actions=[],
            notes="",
            resolved_at=datetime.now(UTC).isoformat(),
        ),
    )


# ------------------------------------------------------------------------------------------
# Discovery-side fakes, one small surface/LLM pair per stopping condition -- test_phase7.py's
# own patterns, reused here rather than imported across test modules.
# ------------------------------------------------------------------------------------------


class _StallSurface:
    """The element list never changes shape at all: every click dispatches, nothing moves.

    `screenshot_bytes` (a plain 10x10 PNG, no sensitive elements to mask) is here so an
    escalation this surface raises carries a genuine screenshot -- F1 (Phase 10 round F): both
    escalation call sites used to hardcode `screenshot_path=None`, so nothing in production ever
    produced one to test against."""

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
            elements=[UIElement(node_id="0", role="button", name="Refresh")],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None

    def screenshot_bytes(self) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color=(10, 20, 30)).save(buf, format="PNG")
        return buf.getvalue()


class _AlwaysClickLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(name="click", args={"index": 0, "rationale": "click refresh again"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


class _LoopDetectSurface:
    """A decoy element toggles in and out every action so digest() changes every round (never
    triggering no_progress), while the SAME button is clicked every round (triggering
    loop_detected on the resolved target descriptor alone)."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._counter = 0

    @property
    def url(self) -> str:
        return "http://fake/start"

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


class _DeadEndSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/start"

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


class _NeverVerifiesSurface:
    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://fake/start"

    def observe(self) -> Observation:
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="button", name="Go")]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


class _NeverVerifiesLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        checkpoint = {"kind": "text_present", "target": "page", "value": "NEVER_ON_PAGE"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "I am done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


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


class _NavigationBlockedSurface:
    """The first click "succeeds" but leaves the session on an off-allowlist URL -- exactly the
    NavigationBlocked shape (a redirect a real app could produce), without a real browser."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self.navigation_violations: list[str] = []

    @property
    def url(self) -> str:
        return "http://fake/start"

    def observe(self) -> Observation:
        return Observation(
            url=self.url, title="Fake", elements=[UIElement(node_id="0", role="button", name="Go")]
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Click):
            self.navigation_violations.append("http://evil.example/redirected")
        return None


class _ClickGoLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        call = ToolCall(name="click", args={"index": 0, "rationale": "click go"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


# ------------------------------------------------------------------------------------------
# DoD 2: eight reason codes, eight tests, each triggered deterministically.
# ------------------------------------------------------------------------------------------


def test_reason_code_stuck_no_progress_is_raised_by_discovery_no_progress_stall(
    tmp_path: Path,
) -> None:
    surface = _StallSurface()
    logger = EvidenceLogger("no-progress-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="make the counter move (it never will)",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.NO_PROGRESS
    assert outcome.intervention_id is not None
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.STUCK_NO_PROGRESS.value
    assert any(e["type"] == "escalation_expired" for e in events)
    # F2: the stall-code group carries the counter that fired and the limit it fired against.
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.context == {"no_progress_streak": "2", "stall_limit": "2"}


def test_reason_code_loop_detected_is_raised_by_discovery_repeated_action(tmp_path: Path) -> None:
    surface = _LoopDetectSurface()
    logger = EvidenceLogger("loop-detect-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="click refresh (the page structure keeps changing around it, on purpose)",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.LOOP_DETECTED
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.LOOP_DETECTED.value
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.context == {"repeat_streak": "2", "stall_limit": "2"}


def test_reason_code_locator_unresolved_is_raised_by_discovery_dead_end(tmp_path: Path) -> None:
    surface = _DeadEndSurface()
    logger = EvidenceLogger("dead-end-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="click an element that is never at index 99",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysBadIndexLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.DEAD_END
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.LOCATOR_UNRESOLVED.value
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.context == {"dead_end_streak": "2", "stall_limit": "2"}


def test_reason_code_max_steps_is_raised_by_discovery_step_budget(tmp_path: Path) -> None:
    surface = _NeverVerifiesSurface()
    logger = EvidenceLogger("max-steps-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="reach a state that never happens",
        target="http://fake/start",
        surface=surface,
        llm=_NeverVerifiesLLM(),
        gate=gate,
        logger=logger,
        max_steps=2,
        timeout_s=30,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.MAX_STEPS
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.MAX_STEPS.value
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.context == {"steps_used": "2", "max_steps": "2"}


def test_reason_code_risky_action_requires_approval_is_raised_by_discovery_risky_click(
    tmp_path: Path,
) -> None:
    surface = _RiskySurface()
    logger = EvidenceLogger("risky-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(risky_labels=["transfer"])
    gate = PolicyGate(policy, logger, mode="discovery", broker=broker)

    outcome = run(
        goal="transfer funds",
        target="http://fake/start",
        surface=surface,
        llm=_ClickRiskyLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.ESCALATION
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL.value
    # F2: this group carries the refusing PolicyDecision's own rule/reason/risk/risk_reason and
    # the action kind, not the streak/limit shape the stall codes use.
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    context = record.request.context
    assert context["rule"] == "risk_discovery"
    assert context["risk"] == "RISKY_IRREVERSIBLE"
    assert context["action_kind"] == "click"
    assert context["reason"] and context["risk_reason"]


def test_reason_code_policy_refused_is_raised_by_discovery_navigation_blocked(
    tmp_path: Path,
) -> None:
    surface = _NavigationBlockedSurface()
    logger = EvidenceLogger("nav-blocked-esc", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="click go",
        target="http://fake/start",
        surface=surface,
        llm=_ClickGoLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.ESCALATION
    assert outcome.resolution == "expired"
    events = _run_events(logger)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.POLICY_REFUSED.value
    # F2: NavigationBlocked carries no PolicyDecision, so this group's context here is smaller
    # (just the reason and the action kind) rather than the full PolicyDecision shape above.
    assert outcome.intervention_id is not None
    record = store.get(outcome.intervention_id)
    assert record is not None
    assert record.request.context["action_kind"] == "click"
    assert "navigation left the allowlist" in record.request.context["reason"]


BALANCE_EVIDENCE_DIR = Path(__file__).parent.parent / "evidence" / "discovery-b2405e162ba4"
BALANCE_GOAL = "look up member 12345 and read their current savings balance"
SUBACCOUNT_EVIDENCE_DIR = Path(__file__).parent.parent / "evidence" / "discovery-adccddf2b6e5"
SUBACCOUNT_GOAL = "open a new sub-account for member 12345 and reach the confirmation screen"
_LIVE_PARAMS = {"password": "testpass", "member_id": 12345}


def _rebuilt_balance_capability() -> Capability:
    """Rebuilt fresh from the real evidence log every time this suite runs (docs/adr/0011: never
    depend on a frozen artifacts/ file) -- genuine, produced data, the SAME real recording
    test_phase9.py's own live section is built from."""
    policy = load_policy(POLICY_PATH)
    return build_capability(
        run_dir=BALANCE_EVIDENCE_DIR,
        goal=BALANCE_GOAL,
        target="http://127.0.0.1:5055/login",
        run_id="b2405e162ba4",
        model="gemini-3.6-flash",
        capability_id="look-up-member-12345-balance-escalation-test",
        policy=policy,
        llm=None,
    )


def _rebuilt_subaccount_capability() -> Capability:
    """Rebuilt from the real subaccount-goal discovery run -- the one genuine recording whose own
    'Submit' step is a mutating_routes match (docs/adr/0007's update), so replaying it TODAY,
    under the fixed policy gate, correctly refuses it as RISKY_IRREVERSIBLE. This is the exact
    scenario ARCHITECTURE.md decision 13's own PolicyGate docstring names: "the fixture's own
    subaccount 'Submit' was dispatched as SAFE_REVERSIBLE" under the old, buggy gate -- replaying
    the SAME real recording under the current one is what makes it a genuine RISKY_IRREVERSIBLE
    policy refusal to escalate, not a fabricated one.
    """
    policy = load_policy(POLICY_PATH)
    return build_capability(
        run_dir=SUBACCOUNT_EVIDENCE_DIR,
        goal=SUBACCOUNT_GOAL,
        target="http://127.0.0.1:5055/login",
        run_id="adccddf2b6e5",
        model="gemini-3.6-flash",
        capability_id="open-subaccount-escalation-test",
        policy=policy,
        llm=None,
    )


def _live_artifact_path(
    tmp_path: Path, capability: Capability, entry_point: str, name: str = "capability.json"
) -> Path:
    updated = capability.model_copy(
        update={"target": capability.target.model_copy(update={"entry_point": entry_point})}
    )
    path = tmp_path / name
    path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_reason_code_risky_action_requires_approval_is_raised_by_replay_subaccount_submit(
    tmp_path: Path,
) -> None:
    """G1: a risk refusal in REPLAY (`rule="risk_replay"`, raised as `PolicyDenied`, never
    `EscalationRequired`) still maps to `RISKY_ACTION_REQUIRES_APPROVAL` -- the same code
    discovery's own risky-click test gets for the identical condition under `rule="risk_discovery"`
    -- because `reason_code_for_decision` (safety/policy.py) reads the DECISION's own rule, never
    which exception type carried it. Before this fix this case (wrongly) got `POLICY_REFUSED`,
    which the operator console never offers a per-action approve/reject flow for, making the
    one-shot approval machinery unreachable from replay entirely for exactly the refusal it exists
    to rescue. Renamed from `test_reason_code_policy_refused_is_raised_by_replay_subaccount_submit`,
    which asserted the wrong code by this same reasoning.
    """
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    artifact = _live_artifact_path(
        tmp_path, _rebuilt_subaccount_capability(), "http://127.0.0.1:5055/login"
    )
    store = InterventionStore(base_dir=tmp_path / "interventions")
    result = replay_engine.replay(
        artifact,
        _LIVE_PARAMS,
        POLICY_PATH,
        evidence_base_dir=tmp_path,
        intervention_store=store,
        intervention_ttl_s=-1,
    )
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.ESCALATION_UNRESOLVED
    events = _replay_events(tmp_path)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL.value
    assert any(e["type"] == "escalation_expired" for e in events)
    # F2: unlike discovery's own risky-click test, this refusal comes from replay's own dispatch
    # (rule="risk_replay", PolicyGate.dispatch step 6) -- the refusing PolicyDecision's own words.
    record = store.get(raised[-1]["intervention_id"])
    assert record is not None
    context = record.request.context
    assert context["rule"] == "risk_replay"
    assert context["risk"] == "RISKY_IRREVERSIBLE"
    assert context["action_kind"] == "click"


def test_reason_code_policy_refused_is_raised_by_replay_disallowed_role(tmp_path: Path) -> None:
    """G1: the OTHER half of the same mapping -- a NON-risk refusal on the replay path
    (`rule="role"`, nothing to do with `classify()`/risk at all) must still map to
    `POLICY_REFUSED`, so this code keeps real coverage on the replay path now that the
    subaccount-submit test above asserts `RISKY_ACTION_REQUIRES_APPROVAL` instead. Offline: the
    target resolves (proving the refusal comes from the role check, not the locator), and
    `PolicyGate` refuses it for a reason with no risk rule in it whatsoever.
    """
    capability = _resume_test_capability()
    surface = _RetryableStepSurface()
    surface.resolvable = True  # the "Confirm" button resolves; the role check refuses the click
    logger = EvidenceLogger("role-refused", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(entry_point="http://fake/step", allowed_roles=["generic"])
    gate = PolicyGate(policy, logger, mode="replay", broker=broker)
    dialog_policy = replay_engine._make_dialog_policy(capability, broker=broker)
    run_state = replay_engine._RunState()

    thread, result_holder = _run_step_in_background(
        capability, surface, gate, logger, run_state, dialog_policy, broker, 15
    )
    request = _wait_for_open_intervention(store)
    assert request.reason_code == ReasonCode.POLICY_REFUSED
    assert request.context["rule"] == "role"
    _resolve(store, request.id, "rejected")
    thread.join(timeout=15)
    assert not thread.is_alive()

    result = result_holder[0]
    assert result.kind == "escalated"
    assert result.resolution == "rejected"
    assert surface.acted == []  # the refused click never actually dispatched


def test_one_shot_approval_allows_exactly_one_dispatch_in_replay_mode(tmp_path: Path) -> None:
    """The replay-mode mirror of `test_one_shot_approval_allows_exactly_one_dispatch` above: a
    granted approval lets exactly one RISKY_IRREVERSIBLE dispatch through under mode="replay" too,
    even though a non-approved/non-allow_risky capability there raises `PolicyDenied`
    (`rule="risk_replay"`), never `EscalationRequired` -- the one-shot consumption itself
    (`SessionBroker.consume_approval`) is identical either way. The SECOND identical dispatch is
    refused again: a one-shot operator approval authorizes exactly the one dispatch that raised it
    and must never quietly become an approved capability_status or allow_risky=True for the rest
    of the run (G1)."""
    policy = load_policy(POLICY_PATH)
    surface = _FakeSurface(url="http://127.0.0.1:5055/member/12345")
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-1")
    gate = PolicyGate(
        policy, mode="replay", broker=broker, capability_status="draft", allow_risky=False
    )
    element = UIElement(node_id="0", role="button", name="Transfer Funds")

    request = _make_request(
        id="int-risky-replay",
        reason_code=ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL,
        what_it_tried="clicked 'Transfer Funds'",
        what_it_observed="PolicyGate refused: RISKY_IRREVERSIBLE requires approval",
    )
    store.create(request)
    broker.intervention_id = request.id
    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="risky action refused")

    # The operator approves without a full handoff.
    broker.grant_approval(request.id)
    broker.transition(ControlState.AUTOMATION, actor="operator", reason="approved")

    # First dispatch: the one-shot approval lets it through.
    gate.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1

    # Second, identical dispatch: the approval was already consumed -- refused again, this time
    # with PolicyDenied (mode="replay"'s own risk-refusal exception), not EscalationRequired.
    with pytest.raises(PolicyDenied) as exc_info:
        gate.dispatch(surface, Click(node_id="0"), element=element)
    assert exc_info.value.decision.rule == "risk_replay"
    assert len(surface.acted) == 1  # unchanged


def test_reason_code_session_expired_is_raised_by_replay_session_loss(tmp_path: Path) -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    entry = "http://127.0.0.1:5055/login?inject=session_expired"
    artifact = _live_artifact_path(tmp_path, _rebuilt_balance_capability(), entry)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    result = replay_engine.replay(
        artifact,
        _LIVE_PARAMS,
        POLICY_PATH,
        evidence_base_dir=tmp_path,
        intervention_store=store,
        intervention_ttl_s=-1,
    )
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.ESCALATION_UNRESOLVED
    events = _replay_events(tmp_path)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.SESSION_EXPIRED.value
    # F2: the replay-code group carries the capability id, the step index, and the trigger
    # reason recovery reported -- what fired, not merely that something did.
    record = store.get(raised[-1]["intervention_id"])
    assert record is not None
    context = record.request.context
    assert context["capability_id"] == "look-up-member-12345-balance-escalation-test"
    assert context["step_index"]
    assert context["trigger_reason"]


def test_reason_code_unrecoverable_condition_is_raised_by_replay_app_error(tmp_path: Path) -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    entry = "http://127.0.0.1:5055/login?inject=app_error"
    artifact = _live_artifact_path(tmp_path, _rebuilt_balance_capability(), entry)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    result = replay_engine.replay(
        artifact,
        _LIVE_PARAMS,
        POLICY_PATH,
        evidence_base_dir=tmp_path,
        intervention_store=store,
        intervention_ttl_s=-1,
    )
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.ESCALATION_UNRESOLVED
    events = _replay_events(tmp_path)
    raised = [e for e in events if e["type"] == "escalation_raised"]
    assert raised and raised[-1]["reason_code"] == ReasonCode.UNRECOVERABLE_CONDITION.value
    record = store.get(raised[-1]["intervention_id"])
    assert record is not None
    context = record.request.context
    assert context["capability_id"] == "look-up-member-12345-balance-escalation-test"
    assert context["step_index"]
    assert context["trigger_reason"]


# ------------------------------------------------------------------------------------------
# DoD 6: exactly one run_end event for an escalated discovery run that was RESOLVED, and exactly
# one for one that EXPIRED.
# ------------------------------------------------------------------------------------------


def test_run_end_event_count_is_exactly_one_for_a_resolved_escalated_discovery_run(
    tmp_path: Path,
) -> None:
    surface = _StallSurface()
    logger = EvidenceLogger("no-progress-resolved", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    def _reject_soon() -> None:
        request = _wait_for_open_intervention(store)
        _resolve(store, request.id, "rejected")

    resolver = threading.Thread(target=_reject_soon)
    resolver.start()
    outcome = run(
        goal="make the counter move (it never will)",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=15,
    )
    resolver.join(timeout=15)
    assert not resolver.is_alive()

    assert outcome.status == RunStatus.NO_PROGRESS
    assert outcome.resolution == "rejected"
    run_end_events = [e for e in _run_events(logger) if e["type"] == "run_end"]
    assert len(run_end_events) == 1


def test_run_end_event_count_is_exactly_one_for_an_expired_escalated_discovery_run(
    tmp_path: Path,
) -> None:
    surface = _StallSurface()
    logger = EvidenceLogger("no-progress-expired", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery", broker=broker)

    outcome = run(
        goal="make the counter move (it never will)",
        target="http://fake/start",
        surface=surface,
        llm=_AlwaysClickLLM(),
        gate=gate,
        logger=logger,
        max_steps=10,
        timeout_s=30,
        stall_limit=2,
        broker=broker,
        intervention_ttl_s=-1,
    )
    assert outcome.status == RunStatus.NO_PROGRESS
    assert outcome.resolution == "expired"
    run_end_events = [e for e in _run_events(logger) if e["type"] == "run_end"]
    assert len(run_end_events) == 1


# ------------------------------------------------------------------------------------------
# DoD 7: the engine returns a real Escalated result on rejection. DoD 10 (a very short TTL ends
# the run as HardFailure(ESCALATION_UNRESOLVED), never a hang) is already covered by the three
# reason-code replay tests above, every one of which uses an already-expired TTL.
# ------------------------------------------------------------------------------------------


def test_engine_returns_a_real_escalated_result_when_the_operator_rejects(
    tmp_path: Path,
) -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    artifact = _live_artifact_path(
        tmp_path, _rebuilt_subaccount_capability(), "http://127.0.0.1:5055/login"
    )
    store = InterventionStore(base_dir=tmp_path / "interventions")

    def _reject_soon() -> None:
        request = _wait_for_open_intervention(store, timeout=30)
        assert request.reason_code == ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL
        _resolve(store, request.id, "rejected")

    resolver = threading.Thread(target=_reject_soon)
    resolver.start()
    result = replay_engine.replay(
        artifact,
        _LIVE_PARAMS,
        POLICY_PATH,
        evidence_base_dir=tmp_path,
        intervention_store=store,
        intervention_ttl_s=60,
    )
    resolver.join(timeout=30)
    assert not resolver.is_alive()

    assert result.kind == "escalated"
    assert result.resolution == "rejected"
    assert result.resumed is False


def test_approved_resolution_redispatches_the_refused_action_and_the_subaccount_flow_completes(
    tmp_path: Path,
) -> None:
    """The scenario the phase prompt itself names as the one to get right: a human approves the
    subaccount capability's own RISKY_IRREVERSIBLE 'Submit' click, PolicyGate.dispatch consumes
    the one-shot approval and lets the SAME action through, and the recorded flow -- which never
    changes at all -- goes on to reach its own recorded success checkpoint."""
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    artifact = _live_artifact_path(
        tmp_path, _rebuilt_subaccount_capability(), "http://127.0.0.1:5055/login"
    )
    store = InterventionStore(base_dir=tmp_path / "interventions")

    def _approve_soon() -> None:
        request = _wait_for_open_intervention(store, timeout=30)
        assert request.reason_code == ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL
        _resolve(store, request.id, "approved")

    resolver = threading.Thread(target=_approve_soon)
    resolver.start()
    result = replay_engine.replay(
        artifact,
        _LIVE_PARAMS,
        POLICY_PATH,
        evidence_base_dir=tmp_path,
        intervention_store=store,
        intervention_ttl_s=60,
    )
    resolver.join(timeout=30)
    assert not resolver.is_alive()

    assert result.kind == "success"
    events = _replay_events(tmp_path)
    assert any(e["type"] == "handoff_resumed" for e in events)


def test_live_handoff_drains_and_persists_real_human_actions(tmp_path: Path) -> None:
    """Round H (H5): the real defect this closes -- `drain_human_actions` had exactly one caller
    in the whole tree and it was a test, so a real handoff's STORED resolution always carried
    `human_actions: []` no matter what a human actually did. This test drives the real
    underlying primitives `SessionBroker.escalate()` itself calls internally (`store.create`,
    `broker.transition`, `broker.session`, `store.resolve`, `broker.await_resolution`,
    `store.attach_human_actions`) in the SAME order, on ONE thread, rather than through one
    literal call to `escalate()` on a second thread: a live Playwright `Page` is bound to the
    thread that created it (`SessionBroker.session()`'s own docstring), and Playwright's sync API
    genuinely raises `greenlet.error: cannot switch to a different thread` the instant a second
    OS thread touches the same page (verified against this project's own installed Playwright:
    `_sync_base.py`'s `_sync()` switches a greenlet bound to the thread that called
    `sync_playwright().start()`). A REAL human needs no Python thread at all -- they click the
    actual OS window directly, independent of whatever the driving thread is doing -- but a test
    simulating one has no such out; it has to play both the runner's and the operator's/human's
    parts itself, in order, on this one thread, exactly what `session()`'s own docstring says
    that accessor exists for.
    """
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    from understudy.surface.web import WebSurface

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    gate = PolicyGate(policy, mode="discovery")
    store = InterventionStore(base_dir=tmp_path / "interventions")
    logger = EvidenceLogger("live-handoff-drain", "test", base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    try:
        # 1. The AGENT's own actions, before any escalation exists. Capture is always on now
        # (WebSurface.__init__, round H's H1), so these get captured too -- H2's discard below
        # must make sure they never reach the stored resolution. The second action (focusing the
        # password field) is not incidental: a browser fires a native 'change' event, carrying
        # whatever value the field holds, when a focused field BLURS -- measured directly against
        # this fixture, typing into Username and escalating immediately, with focus never moved
        # away, left that 'change' event pending until the HUMAN's own first action (focusing a
        # different field) triggered it, timing-wise inside the human's window despite carrying
        # the agent's own stale value. Moving focus here, as part of the agent's OWN turn, fires
        # and discards that 'change' together with everything else the agent did.
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login
        obs = surface.observe()
        agent_field = _find(obs, "textbox", "Username")
        gate.dispatch(
            surface,
            Type(node_id=agent_field.node_id, text="agent-typed-this"),
            element=agent_field,
        )
        obs_focus = surface.observe()
        agent_next_field = _find(obs_focus, "textbox", "Password")
        gate.dispatch(surface, Click(node_id=agent_next_field.node_id), element=agent_next_field)

        # 2. Escalate: the same store.create + transition(PENDING_HANDOFF) + discard sequence
        # SessionBroker.escalate() itself runs, in the same order.
        now = datetime.now(UTC)
        request = InterventionRequest(
            id="live-handoff-h5",
            run_id=logger.run_id,
            capability_id=None,
            goal="prove the real drain/attach wiring",
            step_id=None,
            reason_code=ReasonCode.STUCK_NO_PROGRESS,
            what_it_tried="dispatched the same action repeatedly",
            what_it_observed="the observation's structure did not change",
            observation=surface.observe(),
            screenshot_path=None,
            context={},
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=900)).isoformat(),
        )
        store.create(request)
        broker.intervention_id = request.id
        broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="escalating: test")
        surface.drain_human_actions()  # H2's discard point -- the agent's own typing above must
        # not survive this.

        # 3. Take control through the operator app -- the same store, matching
        # escalation/operator_app.py's take_control() handler exactly.
        broker.transition(ControlState.HUMAN, actor="operator", reason="operator took control")

        # 4. Perform actions on the page AS THE HUMAN, same thread, through session() -- the
        # accessor exists for exactly this (its own docstring).
        human_surface = broker.session("operator")
        obs2 = human_surface.observe()
        password_field = _find(obs2, "textbox", "Password")
        human_surface.act(Type(node_id=password_field.node_id, text="a-human-typed-this"))
        obs3 = human_surface.observe()
        login_button = _find(obs3, "button", "Login")
        human_surface.act(Click(node_id=login_button.node_id))

        # 5. Return control -- matching escalation/operator_app.py's return_control() handler,
        # which writes the took_control resolution with human_actions=[] (that process holds no
        # live Surface). Step 6 below is what the RUNNER does once it observes this resolution.
        broker.transition(
            ControlState.PENDING_RESUME, actor="operator", reason="operator returned control"
        )
        store.resolve(
            request.id,
            InterventionResolution(
                resolved_by="operator",
                action_taken="took_control",
                human_actions=[],
                notes="",
                resolved_at=datetime.now(UTC).isoformat(),
            ),
        )

        # 6. The runner resumes: await_resolution (the real SessionBroker method escalate() calls)
        # finds the resolution immediately (already written above -- no real wait), then drains
        # and KEEPS (H2's second half), persists onto the STORED resolution (H3), logs
        # handoff_resumed, and hands control back to AUTOMATION -- the exact sequence
        # broker.escalate() itself runs once its own await_resolution call returns.
        resolution = broker.await_resolution(request)
        assert resolution is not None
        assert resolution.action_taken == "took_control"
        drained = surface.drain_human_actions()
        assert drained, "expected real captured DOM events from the human's actions above"
        store.attach_human_actions(request.id, drained)
        logger.event(
            "handoff_resumed",
            intervention_id=request.id,
            action_taken="took_control",
            human_action_count=len(drained),
        )
        broker.transition(ControlState.AUTOMATION, actor="runner", reason="resolved: took_control")
    finally:
        surface.close()

    record = store.get(request.id)
    assert record is not None
    assert record.resolution is not None
    stored_actions = record.resolution.human_actions
    assert stored_actions, "the stored resolution's human_actions must be non-empty"
    assert any(
        a.kind == "input" and a.role == "textbox" and a.value == "a-human-typed-this"
        for a in stored_actions
    )
    assert any(a.kind == "click" and a.role == "button" for a in stored_actions)
    # H2: the AGENT's own earlier typing must not have survived the discard.
    assert not any(a.value == "agent-typed-this" for a in stored_actions)

    assert any(e["type"] == "handoff_resumed" for e in _run_events(logger))


def test_live_escalate_drains_and_persists_human_actions_through_the_real_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round I: the test above proves the drain/attach/log PRIMITIVES work in the right order,
    but never calls `SessionBroker.escalate()` itself -- severing `escalate()`'s own resolution-
    path drain (`drained = self._drain_human_actions()` -> `drained = []`) left every phase-10
    test green, including the one above, because none of them actually went through `escalate()`.
    This one does: `escalate()`'s only blocking call is `await_resolution`, monkeypatched here to
    stand in for "time passes, an operator takes control, a human acts, hands back" -- the same
    greenlet constraint the test above documents (a live Playwright Page is bound to the thread
    that created it) means the human's part still runs on this one thread, inside the monkeypatch,
    rather than on a second thread while `escalate()` blocks for real. Every other line of
    `escalate()` -- create, transition to PENDING_HANDOFF, the pre-block discard, drain-and-keep,
    attach_human_actions, the handoff_resumed log, transition back to AUTOMATION -- runs for real.
    """
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    from understudy.surface.web import WebSurface

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    gate = PolicyGate(policy, mode="discovery")
    store = InterventionStore(base_dir=tmp_path / "interventions")
    logger = EvidenceLogger("live-escalate-drain", "test", base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    try:
        # The agent's own actions, before any escalation exists -- must not survive the discard.
        # The Click below moves focus off Username, which is the point (see the test above's own
        # comment on the blur/'change' event): without it the agent's stale value would still be
        # pending when the human's window opens.
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login
        obs = surface.observe()
        agent_field = _find(obs, "textbox", "Username")
        gate.dispatch(
            surface,
            Type(node_id=agent_field.node_id, text="agent-typed-this"),
            element=agent_field,
        )
        obs_focus = surface.observe()
        agent_next_field = _find(obs_focus, "textbox", "Password")
        gate.dispatch(surface, Click(node_id=agent_next_field.node_id), element=agent_next_field)

        now = datetime.now(UTC)
        request = InterventionRequest(
            id="live-escalate-h5b",
            run_id=logger.run_id,
            capability_id=None,
            goal="prove escalate() itself drains and persists",
            step_id=None,
            reason_code=ReasonCode.STUCK_NO_PROGRESS,
            what_it_tried="dispatched the same action repeatedly",
            what_it_observed="the observation's structure did not change",
            observation=surface.observe(),
            screenshot_path=None,
            context={},
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=900)).isoformat(),
        )

        def fake_await_resolution(
            req: InterventionRequest, *args: Any, **kwargs: Any
        ) -> InterventionResolution:
            # The operator's side, on this same thread: takes control, the human acts through
            # session("operator") (the accessor exists for exactly this -- its own docstring),
            # hands control back, and writes the took_control resolution the same way
            # escalation/operator_app.py's return_control() handler does (empty human_actions --
            # that process holds no live Surface to drain from).
            broker.transition(ControlState.HUMAN, actor="operator", reason="took control")
            human_surface = broker.session("operator")
            obs2 = human_surface.observe()
            password_field = _find(obs2, "textbox", "Password")
            human_surface.act(Type(node_id=password_field.node_id, text="a-human-typed-this"))
            obs3 = human_surface.observe()
            login_button = _find(obs3, "button", "Login")
            human_surface.act(Click(node_id=login_button.node_id))
            broker.transition(
                ControlState.PENDING_RESUME, actor="operator", reason="returned control"
            )
            store.resolve(
                req.id,
                InterventionResolution(
                    resolved_by="operator",
                    action_taken="took_control",
                    human_actions=[],
                    notes="",
                    resolved_at=datetime.now(UTC).isoformat(),
                ),
            )
            record = store.get(req.id)
            assert record is not None and record.resolution is not None
            return record.resolution

        monkeypatch.setattr(broker, "await_resolution", fake_await_resolution)

        resolution = broker.escalate(request, logger)
    finally:
        surface.close()

    assert resolution is not None
    assert resolution.action_taken == "took_control"
    assert broker.state().state == ControlState.AUTOMATION

    record = store.get(request.id)
    assert record is not None
    assert record.resolution is not None
    stored_actions = record.resolution.human_actions
    assert stored_actions, "the stored resolution's human_actions must be non-empty"
    assert any(
        a.kind == "input" and a.role == "textbox" and a.value == "a-human-typed-this"
        for a in stored_actions
    )
    assert any(a.kind == "click" and a.role == "button" for a in stored_actions)
    # The discard window: the AGENT's own earlier typing must not have survived it.
    assert not any(a.value == "agent-typed-this" for a in stored_actions)

    assert any(e["type"] == "handoff_resumed" for e in _run_events(logger))


# ------------------------------------------------------------------------------------------
# Round J: `drain_human_actions` used to CRASH a real handoff, not just return an empty list,
# when the human's last action before control came back started a navigation (surface/web.py's
# own docstring, Finding 2). Neither test above reproduces this: every live "human" action in
# both of them is simulated through `gate.dispatch(...)` / `human_surface.act(...)`, and `act()`'s
# own `_click_and_settle` already waits for the navigation it starts, so the race this round
# fixes was never on the path either test exercises.
# ------------------------------------------------------------------------------------------


def test_drain_human_actions_survives_a_real_navigating_click() -> None:
    """The click is dispatched RAW (`locator.dispatch_event("click")`), not through
    `gate.dispatch(Click(...))` -- that is the whole point. A real human's mouse click reaches
    the actual browser window directly, with no Understudy-side wait wrapped around it at all;
    `gate.dispatch` cannot stand in for that because `_click_and_settle` already waits for the
    resulting navigation to settle before returning, which makes the race this test needs
    impossible to hit. Measured directly, before round J's fix: an unwrapped click that starts a
    top-level navigation (exactly this fixture's Login button) crashed
    `drain_human_actions` with Playwright's own `Execution context was destroyed, most likely
    because of a navigation` on every single attempt, in both headed and headless mode -- not a
    flaky race, a deterministic one, because nothing at all was waiting for it.
    """
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    from understudy.surface.web import WebSurface

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    gate = PolicyGate(policy, mode="discovery")
    try:
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login
        obs = surface.observe()
        username = _find(obs, "textbox", "Username")
        gate.dispatch(surface, Type(node_id=username.node_id, text="tester"), element=username)
        obs = surface.observe()
        password = _find(obs, "textbox", "Password")
        gate.dispatch(surface, Type(node_id=password.node_id, text="secret"), element=password)

        # The human's own click: a raw DOM event dispatched straight at the live element, with
        # no Playwright-side actionability or navigation wait around it -- the closest this test
        # can get, without an actual person at the keyboard, to what an OS-level mouse click
        # looks like from this surface's point of view. This is a top-level navigation (POST
        # /login -> redirect -> GET /app), exactly the case the live regression report named.
        # A plain CSS locator, not `aria-ref=` (Playwright's `dispatch_event` does not resolve
        # an `aria-ref=` locator at all -- verified live, it hangs to its own timeout regardless
        # of this fix) -- reaching into `_page` for a raw selector is the point here, not an
        # accident: nothing public on this surface performs an unwrapped click.
        surface._page.locator("input[value='Login']").dispatch_event("click")  # noqa: SLF001

        actions = surface.drain_human_actions()  # must not raise
    finally:
        surface.close()

    kinds = [action.kind for action in actions]
    assert "navigate" in kinds
    click_actions = [action for action in actions if action.kind == "click"]
    assert any(action.role == "button" for action in click_actions), (
        f"expected the Login click in the drained chain; got kinds={kinds}"
    )


def test_escalate_survives_a_drain_failure_and_logs_it(tmp_path: Path) -> None:
    """The other half of round J: a drain that genuinely cannot read must not take the whole
    escalation down with it. Forces the failure by monkeypatching a FAKE surface's own
    `drain_human_actions` to always raise -- offline, so this proves the SURVIVAL path
    deterministically; the real race that can cause such a raise is proven separately, live, by
    the navigating-click test above.

    Both of `escalate()`'s drain sites run against this fake (the discard before the block, and
    the keep after resolution), so this exercises `_safe_drain_human_actions` at both call sites,
    not just one.
    """

    class _DrainAlwaysFails(_FakeSurface):
        def drain_human_actions(self) -> list[HumanAction]:
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, most likely because of a "
                "navigation"
            )

    surface = _DrainAlwaysFails()
    store = InterventionStore(base_dir=tmp_path / "interventions")
    logger = EvidenceLogger("drain-failure-survivable", "test", base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)

    now = datetime.now(UTC)
    request = _make_request(
        id="drain-failure-1",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
    )

    # `escalate()` itself calls `store.create(request)` first thing, which would overwrite a
    # resolution written any earlier -- so, like this file's other live-resolution tests, a
    # background "operator" resolves it only once `escalate()`'s own create() has run.
    def _resolve_soon() -> None:
        _wait_for_open_intervention(store, timeout=30)
        _resolve(store, request.id, "approved")

    resolver = threading.Thread(target=_resolve_soon)
    resolver.start()
    resolution = broker.escalate(request, logger)
    resolver.join(timeout=30)
    assert not resolver.is_alive()

    assert resolution is not None, "a drain failure must not turn a real resolution into None"
    assert resolution.action_taken == "approved"
    assert broker.state().state == ControlState.AUTOMATION, (
        "the run must still resume -- a drain failure is not a reason to strand control"
    )

    events = _run_events(logger)
    drain_failures = [e for e in events if e["type"] == "human_action_drain_failed"]
    assert drain_failures, (
        f"expected a human_action_drain_failed event; got types={[e['type'] for e in events]}"
    )
    assert "Execution context was destroyed" in drain_failures[0]["error"]


# ------------------------------------------------------------------------------------------
# DoD 9 / the second-escalation rule, both offline: replay/engine.py's `_run_step` called
# directly against a small duck-typed fake surface, so the RESUME DECISION itself (postcondition
# already satisfied -> skip; neither checkpoint holds -> escalate again) is deterministic and
# needs no browser at all.
# ------------------------------------------------------------------------------------------


class _FakeStepSurface:
    """`fixed` flips (from this test's own background 'operator' thread) to simulate a human
    changing the page while holding control. `_run_step` never dispatches a real action against
    this fake in either scenario below -- the target it is looking for never resolves, which is
    the whole point (it is what raises the LOCATOR_UNRESOLVED escalation to begin with)."""

    def __init__(self) -> None:
        self.url = "http://fake/step"
        self.fixed = False

    def observe(self) -> Observation:
        if self.fixed:
            return Observation(
                url=self.url,
                title="Fake",
                elements=[UIElement(node_id="0", role="generic", name="Confirmed")],
            )
        return Observation(url=self.url, title="Fake", elements=[])

    def urls(self) -> list[str]:
        return [self.url]


def _resume_test_capability(precondition: Checkpoint | None = None) -> Capability:
    step = Step(
        id="0",
        index=0,
        action="click",
        target=TargetDescriptor(role="button", name="Confirm"),
        postcondition=Checkpoint(kind="element_present", target="generic", value="Confirmed"),
        precondition=precondition,
        rationale="click confirm",
    )
    return Capability(
        capability_id="resume-test",
        name="n",
        description="d",
        target=TargetApp(app_id="a", entry_point="http://fake/step"),
        steps=[step],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )


def _run_step_in_background(
    capability: Capability,
    surface: _FakeStepSurface,
    gate: PolicyGate,
    logger: EvidenceLogger,
    run_state: replay_engine._RunState,
    dialog_policy: replay_engine._DialogPolicy,
    broker: SessionBroker,
    ttl: float,
) -> tuple[threading.Thread, list[Any]]:
    result_holder: list[Any] = []

    def _worker() -> None:
        result_holder.append(
            replay_engine._run_step(
                capability.steps[0],
                capability,
                {},
                surface,
                gate,
                logger,
                run_state,
                lambda: "no-op",
                0,
                dialog_policy,
                broker,
                ttl,
            )
        )

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread, result_holder


def test_step_skipped_after_handoff_when_postcondition_already_satisfied(tmp_path: Path) -> None:
    capability = _resume_test_capability()
    surface = _FakeStepSurface()
    logger = EvidenceLogger("resume-skip", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(
        entry_point="http://fake/step", allowed_roles=["button", "generic"]
    )
    gate = PolicyGate(policy, logger, mode="replay", broker=broker)
    dialog_policy = replay_engine._make_dialog_policy(capability, broker=broker)
    run_state = replay_engine._RunState()

    thread, result_holder = _run_step_in_background(
        capability, surface, gate, logger, run_state, dialog_policy, broker, 15
    )
    request = _wait_for_open_intervention(store)
    assert request.reason_code == ReasonCode.LOCATOR_UNRESOLVED
    surface.fixed = True  # the human already did the step's own work
    _resolve(store, request.id, "took_control")
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert isinstance(result_holder[0], tuple)  # the step is done, not a HardFailure/Escalated
    skipped = [e for e in _run_events(logger) if e["type"] == "step_skipped_after_handoff"]
    assert skipped and skipped[0]["step_id"] == 0


def test_resume_satisfying_neither_postcondition_nor_precondition_escalates_again(
    tmp_path: Path,
) -> None:
    capability = _resume_test_capability(
        precondition=Checkpoint(kind="element_present", target="generic", value="NeverAppears")
    )
    surface = _FakeStepSurface()
    logger = EvidenceLogger("resume-second-escalation", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(
        entry_point="http://fake/step", allowed_roles=["button", "generic"]
    )
    gate = PolicyGate(policy, logger, mode="replay", broker=broker)
    dialog_policy = replay_engine._make_dialog_policy(capability, broker=broker)
    run_state = replay_engine._RunState()

    thread, result_holder = _run_step_in_background(
        capability, surface, gate, logger, run_state, dialog_policy, broker, 15
    )
    first_request = _wait_for_open_intervention(store)
    assert first_request.reason_code == ReasonCode.LOCATOR_UNRESOLVED
    # took_control, but nothing was actually fixed: surface.fixed stays False, and the declared
    # precondition never appears anywhere either -- neither checkpoint can justify skip or retry.
    _resolve(store, first_request.id, "took_control")

    second_request = _wait_for_open_intervention(store, timeout=15, exclude={first_request.id})
    assert second_request.reason_code == ReasonCode.UNRECOVERABLE_CONDITION
    _resolve(store, second_request.id, "rejected")
    thread.join(timeout=15)
    assert not thread.is_alive()

    result = result_holder[0]
    assert result.kind == "escalated"
    assert result.resolution == "rejected"
    assert result.resumed is True  # the FIRST escalation WAS rescued, even though this one wasn't


class _RetryableStepSurface:
    """Distinct from `_FakeStepSurface` above: that fake's target NEVER resolves, by design, so
    it can only ever exercise the skip/escalate-again branches of `_resume()` (neither needs a
    real dispatch to succeed). This one's target becomes resolvable once `resolvable` flips --
    standing in for a human, holding control, clearing whatever was blocking the page -- entirely
    independently of `clicked` (only `act()` sets that), so a retry-from-the-top can genuinely
    re-resolve the locator and re-dispatch the recorded click, rather than failing to resolve a
    second time."""

    def __init__(self) -> None:
        self.url = "http://fake/step"
        self.resolvable = False
        self.clicked = False
        self.acted: list[Action] = []
        self.navigation_violations: list[str] = []

    def observe(self) -> Observation:
        elements = []
        if self.resolvable:
            elements.append(UIElement(node_id="0", role="button", name="Confirm"))
        if self.clicked:
            elements.append(UIElement(node_id="1", role="generic", name="Confirmed"))
        return Observation(url=self.url, title="Fake", elements=elements)

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Click):
            self.clicked = True
        return None

    def urls(self) -> list[str]:
        return [self.url]


def test_resume_retries_step_from_the_top_when_precondition_holds_but_postcondition_does_not(
    tmp_path: Path,
) -> None:
    """The THIRD `_resume()` branch (task C2), previously untested: the human took control but
    did NOT do the step's own work (its postcondition stays unsatisfied), and the step is still
    runnable (no precondition, so it trivially holds) -- the step is RETRIED FROM THE TOP, neither
    skipped (that needs the postcondition already satisfied) nor escalated a second time (that
    needs the precondition to fail too). Proven, not just inferred from the return type: the
    recorded click is genuinely re-dispatched against the surface, and no
    `step_skipped_after_handoff` event is ever written for this step.
    """
    capability = _resume_test_capability()  # no precondition declared -> "holds" trivially
    surface = _RetryableStepSurface()
    logger = EvidenceLogger("resume-retry", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    policy = _permissive_policy(
        entry_point="http://fake/step", allowed_roles=["button", "generic"]
    )
    gate = PolicyGate(policy, logger, mode="replay", broker=broker)
    dialog_policy = replay_engine._make_dialog_policy(capability, broker=broker)
    run_state = replay_engine._RunState()

    thread, result_holder = _run_step_in_background(
        capability, surface, gate, logger, run_state, dialog_policy, broker, 15
    )
    request = _wait_for_open_intervention(store)
    assert request.reason_code == ReasonCode.LOCATOR_UNRESOLVED
    # The human unblocks the page (the button now resolves) but does not click it themselves --
    # the postcondition ("Confirmed" present) is still unsatisfied when control returns.
    surface.resolvable = True
    assert surface.acted == []  # nothing dispatched yet: the escalation fired before any action
    _resolve(store, request.id, "took_control")
    thread.join(timeout=15)
    assert not thread.is_alive()

    result = result_holder[0]
    assert isinstance(result, tuple)  # the step completed via retry, not a HardFailure/Escalated

    # The retry is not a no-op: the recorded action was genuinely dispatched again.
    clicks = [a for a in surface.acted if isinstance(a, Click)]
    assert len(clicks) == 1

    events = _run_events(logger)
    assert any(e["type"] == "escalation_raised" for e in events)  # the one, first, escalation
    assert not [e for e in events if e["type"] == "step_skipped_after_handoff"]


# ------------------------------------------------------------------------------------------
# C3: the dialog policy stands down during a handoff, before its own budget is even consulted.
# ------------------------------------------------------------------------------------------


def test_dialog_policy_stands_down_during_a_handoff(tmp_path: Path) -> None:
    surface = _FakeStepSurface()
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-dialog")
    policy = replay_engine._make_dialog_policy(_resume_test_capability(), broker=broker)
    # No dismiss_dialog rule declared at all -- budget is 0 regardless, so this also proves the
    # AUTOMATION/budget path is checked BEFORE the (already-empty) budget, not instead of it.
    assert broker.state().state == ControlState.AUTOMATION
    assert policy({"dialog_type": "confirm", "message": "?"}) == "none"

    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    broker.transition(ControlState.HUMAN, actor="operator", reason="took control")
    assert policy({"dialog_type": "confirm", "message": "?"}) == "none"


def test_dialog_policy_dismisses_while_automation_with_budget_remaining(tmp_path: Path) -> None:
    from understudy.models.artifact import RecoveryRule

    capability = _resume_test_capability()
    capability = capability.model_copy(
        update={
            "recovery_rules": [
                RecoveryRule(
                    id="d",
                    trigger="native_dialog_appeared",
                    action="dismiss_dialog",
                    max_attempts=1,
                )
            ]
        }
    )
    surface = _FakeStepSurface()
    store = InterventionStore(base_dir=tmp_path)
    broker = SessionBroker(surface, store, run_id="run-dialog-2")
    policy = replay_engine._make_dialog_policy(capability, broker=broker)

    assert broker.state().state == ControlState.AUTOMATION
    assert policy({"dialog_type": "confirm", "message": "?"}) == "dismiss"

    broker.transition(ControlState.PENDING_HANDOFF, actor="runner", reason="stuck")
    broker.transition(ControlState.HUMAN, actor="operator", reason="took control")
    # Budget was NOT spent by standing down: still 0 attempts used, but state is HUMAN, so "none".
    assert policy({"dialog_type": "confirm", "message": "?"}) == "none"
