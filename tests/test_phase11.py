"""Phase 11 tests.

Task A: reinstating `insufficient_funds` (docs/adr/0016). The fixture's own subaccount-opening
route can now say no to a deposit that exceeds the member's balance
(fixtures/legacy_bank/app.py's `subaccount_new`), and replay/outcomes.py's `balance_check` detector
recognizes that page.

Every test here is offline: the fixture route is driven through Flask's own test client, never a
live server, and the detector is driven against a hand-built Observation the same way
tests/test_phase9.py's own detector tests are (`_fake_observation`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from fixtures.legacy_bank.app import app as fixture_app
from understudy.catalog import server as catalog_server
from understudy.escalation.control import SessionBroker
from understudy.escalation.store import InterventionStore
from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    InputParam,
    Provenance,
    Step,
    TargetApp,
)
from understudy.models.observation import Observation, UIElement
from understudy.models.result import BusinessOutcome, FailureCategory, HardFailure
from understudy.replay import engine as replay_engine
from understudy.replay import outcomes
from understudy.replay.engine import _param_type_error
from understudy.safety.policy import Policy, PolicyGate
from understudy.surface.locator import TargetDescriptor

# ------------------------------------------------------------------------------------------
# The fixture branch: a POST that exceeds member 12345's balance ($1,204.55) is a business
# rejection, not a validation error, and the happy path (a deposit within balance) is unbroken.
# ------------------------------------------------------------------------------------------


def _log_in_and_open_subaccount_form(client: FlaskClient, deposit: str) -> TestResponse:
    client.post("/login", data={"f1": "operator", "f2": "testpass"})
    return client.post(
        "/member/12345/subaccount/new",
        data={"f1": "SAV", "f2": "Vacation Fund", "f7": deposit},
    )


def test_deposit_exceeding_balance_is_rejected_as_insufficient_funds() -> None:
    with fixture_app.test_client() as client:
        response = _log_in_and_open_subaccount_form(client, "5000")
    assert response.status_code == 200
    assert b"Insufficient funds" in response.data


def test_deposit_within_balance_still_redirects_to_confirm() -> None:
    with fixture_app.test_client() as client:
        response = _log_in_and_open_subaccount_form(client, "250")
    assert response.status_code == 302
    assert "/subaccount/confirm" in response.headers["Location"]


# ------------------------------------------------------------------------------------------
# The detector: matches the fixture's own wording verbatim, and only that wording.
# ------------------------------------------------------------------------------------------


def _fake_observation(*needles: str) -> Observation:
    return Observation(
        url="http://fake/x",
        title="Fake",
        elements=[UIElement(node_id="0", role="generic", name=" ".join(needles))],
    )


def test_balance_check_detector_fires_on_the_fixtures_own_message() -> None:
    message = "Insufficient funds: the initial deposit exceeds the available balance of $1,204.55."
    assert outcomes.DETECTORS["balance_check"](_fake_observation(message)) == message

    unrelated = _fake_observation("Subaccount Opened")
    assert outcomes.DETECTORS["balance_check"](unrelated) is None


# ------------------------------------------------------------------------------------------
# Task B: capabilities as MCP tools (catalog/server.py). Every test here is offline -- no live
# MCP subprocess, no browser -- exercising `_load_published`/`handle_list_tools`/
# `handle_call_tool` directly, the same functions `catalog/server.py`'s `build_server` wires into
# the real `mcp.server.lowlevel.Server`. `evidence/catalog-invocation/transcript.jsonl` (produced
# by `python -m understudy.catalog.demo`, not by this suite) is the real end-to-end proof that a
# genuine MCP client over stdio can reach these tools.
# ------------------------------------------------------------------------------------------


def _write_capability(path: Path, **overrides: Any) -> Capability:
    base: dict[str, Any] = dict(
        capability_id="cap",
        version=1,
        name="cap",
        description="a test capability",
        target=TargetApp(app_id="a", entry_point="http://fake/a"),
        inputs=[],
        outputs=[],
        steps=[],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
        status="draft",
    )
    base.update(overrides)
    capability = Capability(**base)
    path.write_text(capability.model_dump_json(), encoding="utf-8")
    return capability


def test_publication_keeps_only_the_highest_version_per_capability_id(tmp_path: Path) -> None:
    for version in (1, 2, 3):
        _write_capability(
            tmp_path / f"cap-a.v{version}.json",
            capability_id="cap-a",
            version=version,
            name="cap_a",
        )
    _write_capability(tmp_path / "cap-b.v1.json", capability_id="cap-b", version=1, name="cap_b")
    # A stray, unrelated file under the same directory must not be mistaken for a third artifact.
    (tmp_path / "not-an-artifact.txt").write_text("ignore me", encoding="utf-8")

    published = catalog_server._load_published(tmp_path)

    assert set(published) == {"cap_a", "cap_b"}
    _, cap_a = published["cap_a"]
    _, cap_b = published["cap_b"]
    assert cap_a.version == 3
    assert cap_b.version == 1


def test_tool_name_is_sanitized_for_a_whole_goal_sentence_and_input_schema_matches_verbatim(
    tmp_path: Path,
) -> None:
    goal_sentence = "look up member 12345 and read their current savings balance"
    _write_capability(
        tmp_path / "cap.v1.json",
        capability_id="cap",
        version=1,
        name=goal_sentence,
        inputs=[InputParam(name="member_id", type="integer", required=True, example=12345)],
    )

    published = catalog_server._load_published(tmp_path)
    assert len(published) == 1
    tool_name, (_, capability) = next(iter(published.items()))

    assert catalog_server._VALID_TOOL_NAME.fullmatch(tool_name), tool_name
    assert " " not in tool_name

    list_result = asyncio.run(catalog_server.handle_list_tools(tmp_path))
    tools_by_name = {tool.name: tool for tool in list_result.tools}
    assert tool_name in tools_by_name
    published_tool = tools_by_name[tool_name]
    assert published_tool.description == capability.description
    assert published_tool.input_schema == capability.json_schema()


def test_published_capabilities_examples_match_their_declared_types() -> None:
    """Contract self-consistency, over the REAL artifacts/ directory (never a fixture copy): an
    example a capability declares for one of its own inputs must satisfy that same input's own
    declared type, reusing replay/engine.py's own param type-checking helper rather than a second
    type rule that could quietly disagree with it. The unpublished balance v2 (member_id:
    integer, example "12345") legitimately fails this and must never be edited to pass -- it is
    real historical evidence of exactly the defect Phase 9's v3 fixed, and it is not published
    (v3 is higher), so it never reaches this check at all.
    """
    published = catalog_server._load_published(Path("artifacts"))
    assert published, "expected at least one published capability under artifacts/"
    for tool_name, (_, capability) in published.items():
        for param in capability.inputs:
            if param.example is None:
                continue
            error = _param_type_error(param.name, param.type, param.example)
            assert error is None, f"{tool_name}.{param.name}: {error}"


def test_draft_capability_is_refused_without_calling_the_replay_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capability(tmp_path / "cap.v1.json", capability_id="cap", version=1, name="cap")

    def _must_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the replay engine must never run for a draft capability")

    monkeypatch.setattr(catalog_server, "replay_capability", _must_not_be_called)

    result = asyncio.run(
        catalog_server.handle_call_tool(
            tmp_path,
            Path("nonexistent-policy.yaml"),
            tmp_path / "evidence",
            tmp_path / "interventions",
            5.0,
            catalog_server.Redactor(),
            types.CallToolRequestParams(name="cap", arguments={}),
        )
    )

    assert result.is_error is True
    text = result.content[0].text
    assert isinstance(text, str)
    assert "draft" in text


def test_unknown_tool_is_refused_without_calling_the_replay_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capability(
        tmp_path / "cap.v1.json", capability_id="cap", version=1, name="cap", status="approved"
    )

    def _must_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the replay engine must never run for an unknown tool name")

    monkeypatch.setattr(catalog_server, "replay_capability", _must_not_be_called)

    result = asyncio.run(
        catalog_server.handle_call_tool(
            tmp_path,
            Path("nonexistent-policy.yaml"),
            tmp_path / "evidence",
            tmp_path / "interventions",
            5.0,
            catalog_server.Redactor(),
            types.CallToolRequestParams(name="does-not-exist", arguments={}),
        )
    )

    assert result.is_error is True
    assert "unknown tool" in result.content[0].text


def test_business_outcome_maps_to_isError_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capability(
        tmp_path / "cap.v1.json", capability_id="cap", version=1, name="cap", status="approved"
    )
    outcome = BusinessOutcome(
        code="no_such_member", message="no such member", observed="No such member: 99999."
    )
    monkeypatch.setattr(
        catalog_server, "replay_capability", lambda *args, **kwargs: outcome
    )

    result = asyncio.run(
        catalog_server.handle_call_tool(
            tmp_path,
            Path("nonexistent-policy.yaml"),
            tmp_path / "evidence",
            tmp_path / "interventions",
            5.0,
            catalog_server.Redactor(),
            types.CallToolRequestParams(name="cap", arguments={}),
        )
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["kind"] == "business_outcome"
    assert payload["code"] == "no_such_member"
    assert payload["message"] == "no such member"
    assert payload["observed"] == "No such member: 99999."


def test_hard_failure_maps_to_isError_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capability(
        tmp_path / "cap.v1.json", capability_id="cap", version=1, name="cap", status="approved"
    )
    failure = HardFailure(
        step_id=0,
        category=FailureCategory.ACTION_FAILED,
        expected="step 0 to execute",
        observed="boom",
    )
    monkeypatch.setattr(
        catalog_server, "replay_capability", lambda *args, **kwargs: failure
    )

    result = asyncio.run(
        catalog_server.handle_call_tool(
            tmp_path,
            Path("nonexistent-policy.yaml"),
            tmp_path / "evidence",
            tmp_path / "interventions",
            5.0,
            catalog_server.Redactor(),
            types.CallToolRequestParams(name="cap", arguments={}),
        )
    )

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["kind"] == "hard_failure"
    assert payload["category"] == "action_failed"


# ------------------------------------------------------------------------------------------
# Round 2, F1: an expired escalation must not throw away WHY the human was asked in the first
# place. Offline, the same way test_phase10.py drives `_run_step` directly with a duck-typed fake
# surface: a click on an element whose name matches `risky_labels` is refused by PolicyGate as
# RISKY_IRREVERSIBLE (rule="risk_replay") before `surface.act()` is ever called, and an
# already-expired TTL (<=0) makes `SessionBroker.await_resolution` return None on its very first
# check, with no sleep and no background "operator" thread needed.
# ------------------------------------------------------------------------------------------


class _RiskyClickSurface:
    """A single button whose accessible name matches a `risky_labels` entry. `act()` raising is
    the check that the click is genuinely refused by the policy gate before dispatch, never
    actually performed -- the same "irreversible action never executed" property Round 1 verified
    live against the real fixture, reproduced here with no browser."""

    def __init__(self) -> None:
        self.url = "http://fake/risky"

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake",
            elements=[UIElement(node_id="0", role="button", name="Transfer Funds")],
        )

    def act(self, action: Any) -> str | None:
        raise AssertionError("a RISKY_IRREVERSIBLE click refused by policy must never dispatch")

    def urls(self) -> list[str]:
        return [self.url]


def _risky_click_capability() -> Capability:
    step = Step(
        id="0",
        index=0,
        action="click",
        target=TargetDescriptor(role="button", name="Transfer Funds"),
        rationale="click transfer funds",
    )
    return Capability(
        capability_id="risky-expiry-test",
        name="n",
        description="d",
        target=TargetApp(app_id="a", entry_point="http://fake/risky"),
        steps=[step],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )


def test_expired_escalation_preserves_the_original_policy_refusal_reason(tmp_path: Path) -> None:
    """F1: before this fix, the expiry branch's `model_copy(update={"observed": ...})` REPLACED
    `observed` outright, so the calling agent's own result carried only "intervention ... expired
    with no operator resolution" and never the reason a human was asked for in the first place
    (the `risk_replay` refusal: capability status/allow_risky, which risky_labels entry matched,
    the irreversible action itself). That reason survived in the intervention record and in
    run.jsonl, but a caller only ever sees the RETURNED result. This asserts both are present in
    `observed` at once -- it must fail again if either the original reason or the expiry note is
    ever dropped.
    """
    capability = _risky_click_capability()
    surface = _RiskyClickSurface()
    policy = Policy(
        version=1,
        app_id="test",
        entry_point=capability.target.entry_point,
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["button", "generic"],
        risky_labels=["Transfer Funds"],
    )
    logger = EvidenceLogger("risky-expiry", "test", base_dir=tmp_path)
    store = InterventionStore(base_dir=tmp_path / "interventions")
    broker = SessionBroker(surface, store, run_id=logger.run_id, logger=logger)
    gate = PolicyGate(policy, logger, mode="replay", broker=broker)
    dialog_policy = replay_engine._make_dialog_policy(capability, broker=broker)
    run_state = replay_engine._RunState()

    result = replay_engine._run_step(
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
        -1,  # already-expired TTL: await_resolution returns None on its first check
    )

    assert isinstance(result, HardFailure)
    assert result.category == FailureCategory.ESCALATION_UNRESOLVED
    # The ORIGINAL refusal reason -- why a human was asked at all -- must still be there.
    assert "risk_replay" in result.observed
    assert "RISKY_IRREVERSIBLE" in result.observed
    assert "Transfer Funds" in result.observed
    # AND the expiry note, so a caller also learns nobody answered in time.
    assert "expired with no operator resolution" in result.observed
