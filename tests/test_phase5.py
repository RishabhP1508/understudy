"""Phase 5 tests: PolicyGate as the one choke point, risk classification, and the rewritten
Redactor. Everything here runs with no browser, no network, and no API key, EXCEPT the tests
explicitly marked LIVE below, which skip loudly naming exactly what precondition is missing.
"""

from __future__ import annotations

import base64
import io
import json
import re
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from understudy.agent.loop import run
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.artifact import Capability
from understudy.models.observation import Observation, UIElement
from understudy.record.recorder import build_capability
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    Policy,
    PolicyDenied,
    PolicyGate,
    load_policy,
)
from understudy.safety.redact import Redactor, redact_screenshot
from understudy.safety.risk import RiskClass, classify
from understudy.surface.base import Action, Click, Navigate, ReadText, Type
from understudy.surface.locator import RelationalHint, TargetDescriptor

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"
ARTIFACT_PATH = (
    Path(__file__).parent.parent
    / "artifacts"
    / "look-up-member-12345-and-read-their-current-savings-balance.v1.json"
)
_SENTINELS = ("SECRET_SENTINEL_VALUE", "123-45-6789")


def _permissive_policy(**overrides: Any) -> Policy:
    """A minimal Policy scoped to the fake http://fake/... surfaces used below (T3/T4/T8): these
    tests exercise the loop and the redaction pipeline against a full discovery run, not the
    real fixture's own allowlist (see T1/T2/T9/T10 for tests against the real policy)."""
    base: dict[str, Any] = dict(
        version=1,
        app_id="test",
        entry_point="http://fake/start",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["textbox", "searchbox", "combobox", "button", "link"],
        risky_labels=["transfer", "delete", "approve", "submit payment", "close account", "wire"],
        mutating_routes=[],
        sensitive_fields={"secret": ["password"], "pii": ["ssn"]},
    )
    base.update(overrides)
    return Policy(**base)


def _assert_no_sentinel_leak(text: str) -> None:
    """Mirrors tests/test_constraints.py's invariant 3 encoding checks (utf-8, url-encoded,
    base64), against a real run.jsonl / artifact produced by actually running the system."""
    for secret in _SENTINELS:
        assert secret not in text, f"raw sentinel {secret!r} leaked"
        assert urllib.parse.quote(secret) not in text
        assert urllib.parse.quote_plus(secret) not in text
        for pad in (b"", b"x", b"xx"):
            encoded = base64.b64encode(pad + secret.encode()).rstrip(b"=")
            start = {0: 0, 1: 2, 2: 3}[len(pad)]
            substring = encoded[start:-2]
            if substring:
                assert substring.decode("ascii") not in text, (
                    f"base64-encoded sentinel {secret!r} (pad={len(pad)}) leaked"
                )


class _FakeSurface:
    """A minimal Surface fake for direct PolicyGate.dispatch tests: no browser, url is settable
    at construction and updated by act() on a Navigate, mirroring WebSurface's real behaviour
    closely enough for the gate's own checks (which only ever read .url and .act())."""

    def __init__(self, url: str = "http://127.0.0.1:5055/login") -> None:
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


# ----------------------------------------------------------------------------------------
# T1: off-allowlist navigation is refused.
# ----------------------------------------------------------------------------------------


def test_offallowlist_navigation_is_refused() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")
    surface = _FakeSurface()

    with pytest.raises(PolicyDenied) as exc_info:
        gate.dispatch(surface, Navigate(url="https://evil.example/x"))

    decision = exc_info.value.decision
    assert decision.allowed is False
    assert decision.rule == "allowlist"
    assert decision.reason
    assert surface.acted == []  # surface.act was never reached


# ----------------------------------------------------------------------------------------
# Regression (defect 1): R3's exemption for a caption like "Password" must be SHAPE-based,
# not key-based, so it survives at any nesting depth -- a dict field or a list item alike.
# ----------------------------------------------------------------------------------------


def test_r3_shape_rule_lets_a_caption_survive_at_any_nesting_depth() -> None:
    descriptor = TargetDescriptor(
        role="textbox",
        name="Password",
        scope=[("cell", "Password"), ("row", "Login")],
        relational=RelationalHint(label="Password"),
    )

    out = Redactor().dumps(descriptor)

    assert "[REDACTED]" not in out
    # top-level name, a list item inside scope, and relational.label all survive
    assert out.count("Password") == 3


def test_secret_sentinel_nested_inside_a_list_is_still_redacted() -> None:
    payload = {"customer": {"items": ["ok", "SECRET_SENTINEL_VALUE"]}}

    out = Redactor().dumps(payload)

    assert "SECRET_SENTINEL_VALUE" not in out
    assert "[REDACTED]" in out


# ----------------------------------------------------------------------------------------
# Regression (defect 2): about:blank (or an empty URL) is the ABSENCE of a navigation, not a
# violation, so a failed goto() must not have its real error replaced by a fabricated
# NavigationBlocked; a genuinely recorded violation must still raise.
# ----------------------------------------------------------------------------------------


def test_navigation_check_ignores_about_blank_but_still_raises_on_recorded_violation() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")

    fresh = _FakeSurface(url="about:blank")
    gate._raise_if_navigation_violated(fresh, include_current_url=True)  # must not raise

    violated = _FakeSurface(url="about:blank")
    violated.navigation_violations.append("https://evil.example/")
    with pytest.raises(NavigationBlocked):
        gate._raise_if_navigation_violated(violated, include_current_url=True)


# ----------------------------------------------------------------------------------------
# T2: classify() -- label heuristic and the route-scoping second layer.
# ----------------------------------------------------------------------------------------


def test_classify_transfer_funds_is_risky_search_is_safe() -> None:
    policy = load_policy(POLICY_PATH)

    transfer = UIElement(node_id="0", role="button", name="Transfer Funds")
    risk, reason = classify(Click(node_id="0"), transfer, policy)
    assert risk == RiskClass.RISKY_IRREVERSIBLE
    assert reason

    search = UIElement(node_id="1", role="button", name="Search")
    risk2, reason2 = classify(Click(node_id="1"), search, policy)
    assert risk2 == RiskClass.SAFE_REVERSIBLE
    assert reason2


def test_classify_submit_on_mutating_route_is_risky_via_route_not_label() -> None:
    """The fixture's real case: the subaccount submit control is literally named "Submit",
    which matches no risky_labels entry -- only the mutating_routes second layer catches it."""
    policy = load_policy(POLICY_PATH)
    submit = UIElement(node_id="0", role="button", name="Submit")

    risky, risky_reason = classify(
        Click(node_id="0"),
        submit,
        policy,
        url="http://127.0.0.1:5055/member/12345/subaccount/new",
    )
    assert risky == RiskClass.RISKY_IRREVERSIBLE
    assert risky_reason

    safe, safe_reason = classify(
        Click(node_id="0"), submit, policy, url="http://127.0.0.1:5055/member/12345"
    )
    assert safe == RiskClass.SAFE_REVERSIBLE
    assert safe_reason


# ----------------------------------------------------------------------------------------
# Regression: a live discovery run found PolicyGate.dispatch reading only `surface.url` (a
# frameset's SHELL, which never navigates -- docs/adr/0005) instead of `Surface.urls()` (every
# loaded frame), so the mutating-route layer above never fired against the real fixture at all.
# This is the test whose absence let that through: it goes through `PolicyGate.dispatch` and a
# real (fake) Surface, not `classify()` called directly with a hand-built URL string.
# ----------------------------------------------------------------------------------------


class _FramesetSurface:
    """Mirrors the real fixture's shape: `.url` is the frameset shell, which never itself
    navigates, but `.urls()` additionally reports a content-frame URL that has actually
    navigated onto a mutating route. `PolicyGate.dispatch` must consult `.urls()`, not `.url`
    alone, or this scenario is invisible to it -- which is exactly the live defect."""

    def __init__(self) -> None:
        self.acted: list[Action] = []

    @property
    def url(self) -> str:
        return "http://127.0.0.1:5055/app"  # the shell: never navigates in a frameset app

    def urls(self) -> list[str]:
        return [
            "http://127.0.0.1:5055/app",
            "http://127.0.0.1:5055/nav",
            "http://127.0.0.1:5055/member/12345/subaccount/new",
        ]

    def observe(self) -> Observation:
        return Observation(url=self.url, title="Fake", elements=[])

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        return None


def test_mutating_route_detected_via_content_frame_urls_not_shell_url() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")
    surface = _FramesetSurface()
    submit = UIElement(node_id="0", role="button", name="Submit")

    with pytest.raises(EscalationRequired) as exc_info:
        gate.dispatch(surface, Click(node_id="0"), element=submit)

    decision = exc_info.value.decision
    assert decision.risk == RiskClass.RISKY_IRREVERSIBLE.value
    assert "subaccount/new" in decision.risk_reason
    assert "http://127.0.0.1:5055/member/12345/subaccount/new" in decision.checked_urls
    assert surface.acted == []  # refused before surface.act() ever ran


# ----------------------------------------------------------------------------------------
# T3: RISKY_IRREVERSIBLE handling differs by mode.
# ----------------------------------------------------------------------------------------


def test_risky_irreversible_discovery_raises_escalation_required() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")
    surface = _FakeSurface(url="http://127.0.0.1:5055/member/12345")
    element = UIElement(node_id="0", role="button", name="Transfer Funds")

    with pytest.raises(EscalationRequired) as exc_info:
        gate.dispatch(surface, Click(node_id="0"), element=element)
    assert exc_info.value.decision.risk == RiskClass.RISKY_IRREVERSIBLE.value


def test_risky_irreversible_replay_needs_approved_status_and_allow_risky() -> None:
    policy = load_policy(POLICY_PATH)
    element = UIElement(node_id="0", role="button", name="Transfer Funds")
    url = "http://127.0.0.1:5055/member/12345"

    neither = PolicyGate(policy, mode="replay", allow_risky=False, capability_status="draft")
    with pytest.raises(PolicyDenied):
        neither.dispatch(_FakeSurface(url=url), Click(node_id="0"), element=element)

    only_status = PolicyGate(
        policy, mode="replay", allow_risky=False, capability_status="approved"
    )
    with pytest.raises(PolicyDenied):
        only_status.dispatch(_FakeSurface(url=url), Click(node_id="0"), element=element)

    only_flag = PolicyGate(policy, mode="replay", allow_risky=True, capability_status="draft")
    with pytest.raises(PolicyDenied):
        only_flag.dispatch(_FakeSurface(url=url), Click(node_id="0"), element=element)

    both = PolicyGate(policy, mode="replay", allow_risky=True, capability_status="approved")
    surface = _FakeSurface(url=url)
    both.dispatch(surface, Click(node_id="0"), element=element)
    assert len(surface.acted) == 1


# ----------------------------------------------------------------------------------------
# T4: REAL generated evidence from a real discovery run, not a synthetic object.
# ----------------------------------------------------------------------------------------


class _SecretFlowSurface:
    """A fake Surface with one sensitivity="secret" textbox, mirroring what
    surface/web.py._resolve_attr_names would produce for a real password field."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._done = False

    @property
    def url(self) -> str:
        return "http://fake/secret-flow"

    def observe(self) -> Observation:
        status = "Status: DONE_TOKEN" if self._done else "Status: pending"
        return Observation(
            url=self.url,
            title="Fake",
            elements=[
                UIElement(
                    node_id="0", role="textbox", name="Password", sensitivity="secret"
                ),
                UIElement(node_id="1", role="generic", name=status),
            ],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Type):
            self._done = True
        return None


class _SecretFlowLLM:
    """Round 1: type the sentinel secret into the password field, with a rationale that itself
    quotes the SSN sentinel (mimicking a model that repeats sensitive text back). Round 2:
    declare the goal met once the page shows it."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(
                name="type",
                args={
                    "index": 0,
                    "text": "SECRET_SENTINEL_VALUE",
                    "rationale": (
                        "Typing the password; the customer's SSN on file is 123-45-6789 for "
                        "reference."
                    ),
                },
            )
            return LLMResponse(tool_calls=[call], text=None, usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def _run_secret_flow(tmp_path: Path, run_id: str) -> tuple[EvidenceLogger, Any]:
    surface = _SecretFlowSurface()
    llm = _SecretFlowLLM()
    logger = EvidenceLogger(run_id, "test", base_dir=tmp_path)
    policy = _permissive_policy()
    gate = PolicyGate(policy, logger, mode="discovery")

    outcome = run(
        goal="update the password",
        target="http://fake/secret-flow",
        surface=surface,
        llm=llm,
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == "goal_verified"
    return logger, outcome


def test_real_run_redacts_secret_and_ssn_from_run_log_and_artifact(tmp_path: Path) -> None:
    logger, _ = _run_secret_flow(tmp_path, "phase5-t4")

    run_jsonl_text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    _assert_no_sentinel_leak(run_jsonl_text)

    capability = build_capability(
        run_dir=logger.dir,
        goal="update the password",
        target="http://fake/secret-flow",
        run_id="phase5-t4",
        model="fake-model",
        capability_id="update-the-password",
        name="update the password",
    )
    artifact_json = Redactor().dumps(capability, indent=2)
    _assert_no_sentinel_leak(artifact_json)


# ----------------------------------------------------------------------------------------
# T5: the over-redaction regression -- a secret value becomes a parameter reference, and a
# rationale that merely MENTIONS the field name survives byte-for-byte.
# ----------------------------------------------------------------------------------------


def test_secret_field_becomes_param_ref_rationale_survives_byte_for_byte() -> None:
    data = {
        "action": "type",
        "element": {
            "role": "textbox",
            "name": "Password",
            "value": "hunter2",
            "sensitivity": "secret",
        },
        "rationale": "Enter the password to log in",
    }
    out = Redactor().dumps(data)

    assert "hunter2" not in out
    assert '"value": "${param:password}"' in out
    assert "Enter the password to log in" in out


# ----------------------------------------------------------------------------------------
# T6: the other direction -- a registered secret value is redacted wherever it is later quoted.
# ----------------------------------------------------------------------------------------


def test_registered_secret_is_redacted_from_a_rationale_that_quotes_it() -> None:
    redactor = Redactor()
    redactor.register_secret("hunter2")

    out = redactor.dumps({"rationale": "The user confirmed the password is hunter2."})

    assert "hunter2" not in out
    assert "[REDACTED]" in out


# ----------------------------------------------------------------------------------------
# T7: pixel comparison -- redact_screenshot masks only the sensitive element's bounds.
# ----------------------------------------------------------------------------------------


def test_redact_screenshot_masks_only_the_flagged_region() -> None:
    image = Image.new("RGB", (20, 20), color=(200, 150, 100))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()

    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(
                node_id="0",
                role="textbox",
                name="SSN",
                value="123-45-6789",
                bounds=[2.0, 2.0, 6.0, 6.0],
            )
        ],
    )

    masked_bytes = redact_screenshot(raw, observation)
    assert masked_bytes is not None
    assert masked_bytes != raw

    original = Image.open(io.BytesIO(raw)).convert("RGB")
    masked = Image.open(io.BytesIO(masked_bytes)).convert("RGB")

    assert masked.getpixel((4, 4)) != original.getpixel((4, 4))  # inside the box: changed
    assert masked.getpixel((15, 15)) == original.getpixel((15, 15))  # outside: unchanged


def test_redact_screenshot_refuses_to_write_partially_masked_image() -> None:
    """Fail safe: a sensitive element with no resolved bounds must not produce a half-masked
    (i.e. unmasked) image; the caller is told to skip the write, not ship a leak."""
    image = Image.new("RGB", (10, 10), color=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    raw = buf.getvalue()

    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[UIElement(node_id="0", role="textbox", name="SSN", value="123-45-6789")],
    )

    assert redact_screenshot(raw, observation) is None


# ----------------------------------------------------------------------------------------
# T8: every policy decision, allow or deny, is in run.jsonl.
# ----------------------------------------------------------------------------------------


def test_every_policy_decision_is_logged_including_allows(tmp_path: Path) -> None:
    logger, _ = _run_secret_flow(tmp_path, "phase5-t8")

    events = [
        json.loads(line)
        for line in (logger.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Phase 6 renamed the event type PolicyGate.dispatch logs from "policy_decision" to "act" to
    # match evidence/logger.py's fixed RunEvent schema, and moved the decision payload from a
    # generic "decision" key to the event's own "policy_decision" field.
    policy_events = [event for event in events if event.get("type") == "act"]

    assert policy_events  # the bootstrap navigate plus the type action, at least
    assert all("policy_decision" in event for event in policy_events)
    assert any(event["policy_decision"]["allowed"] is True for event in policy_events)


# ----------------------------------------------------------------------------------------
# T9: the real artifact round-trips through the new Redactor with provenance intact.
# ----------------------------------------------------------------------------------------


def test_real_artifact_round_trips_created_at_and_transcript_hash_intact() -> None:
    original = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    capability = Capability.model_validate(original)

    redacted_json = Redactor().dumps(capability, indent=2)
    round_tripped = json.loads(redacted_json)

    assert round_tripped["provenance"]["created_at"] == original["provenance"]["created_at"]
    assert (
        round_tripped["provenance"]["transcript_hash"] == original["provenance"]["transcript_hash"]
    )
    assert re.fullmatch(r"[0-9a-f]{32,128}", round_tripped["provenance"]["transcript_hash"])


# ----------------------------------------------------------------------------------------
# T10: LIVE BROWSER + LIVE FIXTURE APP. Skips loudly, naming exactly what precondition failed.
# ----------------------------------------------------------------------------------------


def _fixture_app_reachable(host: str = "127.0.0.1", port: int = 5055) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def test_live_external_redirect_is_navigation_blocked() -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055` before running "
            "this test"
        )

    from playwright.sync_api import Error as PlaywrightError

    from understudy.surface.web import WebSurface

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    try:
        gate = PolicyGate(policy, mode="discovery")
        try:
            gate.dispatch(surface, Navigate(url="http://127.0.0.1:5055/external"))
        except NavigationBlocked as exc:
            message = str(exc)
            assert "example.com" in message or any(
                "example.com" in url for url in exc.urls
            )
            return
        except PlaywrightError as exc:
            pytest.skip(
                "the redirect to https://example.com could not complete (no internet "
                f"access?): {exc}"
            )
        pytest.fail("expected NavigationBlocked for the off-allowlist redirect to example.com")
    finally:
        surface.close()


def _find(observation: Observation, role: str, name_contains: str) -> UIElement:
    for element in observation.elements:
        if element.role == role and name_contains in element.name:
            return element
    raise AssertionError(
        f"no element with role={role!r} whose name contains {name_contains!r}; got: "
        f"{[(e.role, e.name) for e in observation.elements]}"
    )


def test_live_mutating_route_is_detected_via_content_frame_not_frameset_shell() -> None:
    """Reproduces the live defect directly (docs/adr/0007's update): drives WebSurface through
    the gate with no LLM, actually reaching the subaccount form inside the content frame, where
    `surface.url` (the frameset shell) sits on `/app` for the whole test but the content frame
    has genuinely navigated onto the `mutating_routes` pattern `/member/*/subaccount/new`."""
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
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login

        obs = surface.observe()
        username = _find(obs, "textbox", "Username")
        gate.dispatch(surface, Type(node_id=username.node_id, text="tester"), element=username)
        obs = surface.observe()
        password = _find(obs, "textbox", "Password")
        gate.dispatch(surface, Type(node_id=password.node_id, text="secret"), element=password)
        obs = surface.observe()
        login_button = _find(obs, "button", "Login")
        gate.dispatch(surface, Click(node_id=login_button.node_id), element=login_button)

        # Now at /app, the frameset shell -- it never reloads again for the rest of this test
        # (docs/adr/0005); everything below happens inside the content frame.
        obs = surface.observe()
        search_field = _find(obs, "textbox", "Member ID")
        gate.dispatch(
            surface, Type(node_id=search_field.node_id, text="12345"), element=search_field
        )
        obs = surface.observe()
        search_button = _find(obs, "button", "Search")
        gate.dispatch(surface, Click(node_id=search_button.node_id), element=search_button)

        obs = surface.observe()
        member_link = _find(obs, "link", "12345")
        gate.dispatch(surface, Click(node_id=member_link.node_id), element=member_link)

        obs = surface.observe()
        open_subaccount = _find(obs, "link", "Open Subaccount")
        gate.dispatch(surface, Click(node_id=open_subaccount.node_id), element=open_subaccount)

        # The content frame has now genuinely navigated onto the mutating route; the frameset
        # shell (surface.url) has not moved from /app this entire time -- the exact shape of the
        # live defect.
        assert surface.url == "http://127.0.0.1:5055/app"
        assert any(u.endswith("/member/12345/subaccount/new") for u in surface.urls())

        obs = surface.observe()
        submit_button = _find(obs, "button", "Submit")
        with pytest.raises(EscalationRequired) as exc_info:
            gate.dispatch(surface, Click(node_id=submit_button.node_id), element=submit_button)
        assert exc_info.value.decision.risk == RiskClass.RISKY_IRREVERSIBLE.value
        assert "subaccount/new" in exc_info.value.decision.risk_reason
    finally:
        surface.close()


# ----------------------------------------------------------------------------------------
# Supporting coverage: the role check is skipped for read_text, and forbidden text is refused.
# ----------------------------------------------------------------------------------------


def test_role_check_is_skipped_for_read_text() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")
    surface = _FakeSurface(url="http://127.0.0.1:5055/member/12345")
    # "cell" is not in allowed_roles, but read_text is exempt from the role check.
    element = UIElement(node_id="0", role="cell", name="Savings Balance", value="$1,204.55")
    gate.dispatch(surface, ReadText(node_id="0"), element=element)
    assert len(surface.acted) == 1


def test_forbidden_text_pattern_refuses_a_typed_ssn() -> None:
    policy = load_policy(POLICY_PATH)
    gate = PolicyGate(policy, mode="discovery")
    surface = _FakeSurface(url="http://127.0.0.1:5055/members")
    element = UIElement(node_id="0", role="textbox", name="Member ID")

    with pytest.raises(PolicyDenied) as exc_info:
        gate.dispatch(surface, Type(node_id="0", text="123-45-6789"), element=element)
    assert exc_info.value.decision.rule == "forbidden_text"
