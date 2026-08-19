"""Phase 12: tenant B (Task A) and tenant overlays / app_fingerprint / drift (Task B).

Tenant B is a second tenant of the SAME vendor product as tenant A (legacy_bank), run for a
different credit union under /tenantb, with two DELIBERATE label renames ("Username" -> "User
ID", "Initial Deposit" -> "Opening Deposit") and otherwise-different routes, form field `name=`
attributes, CSS classes, table nesting depth, and title pattern -- none of which the recorded
artifact or a Phase 12 overlay ever references, so none of it should produce any replay signal.

Task A is offline: the fixture is driven through Flask's own test client, never a live server
(matching tests/test_phase11.py's own convention), and the hostility check reads the template
files directly. Task B's `resolve_for_tenant`/vocabulary/fingerprint tests are also offline; one
LIVE test at the bottom drives the real fixture app and skips loudly if it is not reachable
(matching tests/test_phase9.py's own convention).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from flask.testing import FlaskClient

from fixtures.legacy_bank.app import app as fixture_app
from understudy.agent.loop import RunStatus, run
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    ExtraStep,
    OverlayError,
    Provenance,
    Step,
    StepOverride,
    TargetApp,
    TenantOverlay,
    resolve_for_tenant,
)
from understudy.models.observation import Observation, UIElement, app_fingerprint
from understudy.models.result import FailureCategory
from understudy.record.recorder import build_capability
from understudy.replay import engine as replay_engine
from understudy.safety.policy import Policy, PolicyGate, load_policy
from understudy.surface.base import Click
from understudy.surface.locator import RelationalHint, TargetDescriptor

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "legacy_bank" / "templates"


def _login_tenant_a(client: FlaskClient) -> None:
    client.post("/login", data={"f1": "operator", "f2": "testpass"})


def _login_tenant_b(client: FlaskClient) -> None:
    client.post("/tenantb/login", data={"uid": "operator", "pwd": "testpass"})


# ------------------------------------------------------------------------------------------
# 1. Hostility, over BOTH tenants' template directories, one assertion set per file so a
# failure names the file. Tenant B is held to tenant A's own bar, not a lower one.
# ------------------------------------------------------------------------------------------


def _all_template_files() -> list[Path]:
    tenant_a = sorted(p for p in TEMPLATES_DIR.glob("*.html"))
    tenant_b = sorted((TEMPLATES_DIR / "tenantb").glob("*.html"))
    return tenant_a + tenant_b


@pytest.mark.parametrize(
    "path", _all_template_files(), ids=lambda p: str(p.relative_to(TEMPLATES_DIR))
)
def test_template_has_no_modern_test_hooks(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    lowered = html.lower()
    assert "data-testid" not in lowered, f"{path}: found data-testid, a modern automation hook"
    assert "aria-label" not in lowered, f"{path}: found aria-label, a modern automation hook"
    assert "label for=" not in lowered, f"{path}: found <label for=>, a modern accessible label"


def test_tenant_a_shell_is_a_real_frameset() -> None:
    html = (TEMPLATES_DIR / "app_frameset.html").read_text(encoding="utf-8")
    assert "<frameset" in html


def test_tenant_b_shell_is_a_real_frameset() -> None:
    html = (TEMPLATES_DIR / "tenantb" / "app_frameset.html").read_text(encoding="utf-8")
    assert "<frameset" in html


# ------------------------------------------------------------------------------------------
# 2. The full tenant B happy path: login, search, detail, linked-account form, review, confirm.
# ------------------------------------------------------------------------------------------


def test_tenant_b_happy_path_reaches_confirmation() -> None:
    with fixture_app.test_client() as client:
        login_resp = client.post("/tenantb/login", data={"uid": "operator", "pwd": "testpass"})
        assert login_resp.status_code == 302
        assert urlsplit(login_resp.headers["Location"]).path == "/tenantb/app"

        search_resp = client.get("/tenantb/customers?q=12345")
        assert search_resp.status_code == 200
        assert b"12345 - Testuser Alpha" in search_resp.data

        detail_resp = client.get("/tenantb/customer/12345")
        assert detail_resp.status_code == 200
        assert b"Open Linked Account" in detail_resp.data

        form_resp = client.get("/tenantb/customer/12345/linked-account/new")
        assert form_resp.status_code == 200
        assert b"Opening Deposit" in form_resp.data

        submit_resp = client.post(
            "/tenantb/customer/12345/linked-account/new",
            data={"nick": "Vacation Fund", "dep": "250"},
        )
        assert submit_resp.status_code == 302
        review_path = urlsplit(submit_resp.headers["Location"]).path
        assert review_path == "/tenantb/customer/12345/linked-account/review"

        review_resp = client.get(submit_resp.headers["Location"])
        assert review_resp.status_code == 200
        assert b"Confirm" in review_resp.data
        assert b"Vacation Fund" in review_resp.data

        confirm_resp = client.post(
            "/tenantb/customer/12345/linked-account/review",
            data={"nickname": "Vacation Fund", "deposit": "250"},
        )
        assert confirm_resp.status_code == 200
        assert b"Linked Account Opened" in confirm_resp.data


# ------------------------------------------------------------------------------------------
# 3. Insufficient funds: the SAME wording tenant A uses, so the shared balance_check detector
# (replay/outcomes.py) recognizes it on either tenant without a tenant-specific rule.
# ------------------------------------------------------------------------------------------


def test_deposit_exceeding_balance_rejected_with_shared_wording() -> None:
    with fixture_app.test_client() as client:
        _login_tenant_b(client)
        resp = client.post(
            "/tenantb/customer/12345/linked-account/new",
            data={"nick": "Vacation Fund", "dep": "5000"},
        )
    assert resp.status_code == 200
    assert b"Insufficient funds" in resp.data


# ------------------------------------------------------------------------------------------
# 4. Injection modes: app-level hooks that fire for tenant B automatically once /tenantb/login
# is exempt and session_expired redirects to the tenant that was actually in.
# ------------------------------------------------------------------------------------------


def test_not_found_injection_fires_on_tenant_b() -> None:
    with fixture_app.test_client() as client:
        arm_resp = client.get("/tenantb/login?inject=not_found")
        # If /tenantb/login were not exempt, arming would itself be intercepted (redirected or
        # replaced) instead of returning tenant B's own login page.
        assert arm_resp.status_code == 200
        assert b"NorthBay" in arm_resp.data

        hit_resp = client.get("/tenantb/customer/12345")
        assert hit_resp.status_code == 200
        assert b"No matching record was found." in hit_resp.data


def test_session_expired_injection_redirects_to_tenant_b_login() -> None:
    with fixture_app.test_client() as client:
        arm_resp = client.get("/tenantb/login?inject=session_expired")
        assert arm_resp.status_code == 200
        assert b"NorthBay" in arm_resp.data

        hit_resp = client.get("/tenantb/customer/12345")
        assert hit_resp.status_code == 302
        assert urlsplit(hit_resp.headers["Location"]).path == "/tenantb/login"


def test_require_login_redirects_a_sessionless_tenant_b_request_to_tenant_b_login() -> None:
    """`require_login` (app.py) is the SHARED decorator every tenant B view is decorated with
    (imported by tenant_b.py); a tenant B request that never logged in at all -- no
    session_expired injection involved -- must land on tenant B's own login, the same
    tenant-routing fix session_expired already needed."""
    with fixture_app.test_client() as client:
        resp = client.get("/tenantb/customer/12345")
    assert resp.status_code == 302
    assert urlsplit(resp.headers["Location"]).path == "/tenantb/login"


# ------------------------------------------------------------------------------------------
# 5. The two deliberate renames are genuinely renamed: the old tenant A label never appears on
# the tenant B page that carries the corresponding field.
# ------------------------------------------------------------------------------------------


def test_login_field_is_renamed_to_user_id() -> None:
    with fixture_app.test_client() as client:
        resp = client.get("/tenantb/login")
    assert resp.status_code == 200
    assert b"User ID" in resp.data
    assert b"Username" not in resp.data


def test_linked_account_field_is_renamed_to_opening_deposit() -> None:
    with fixture_app.test_client() as client:
        _login_tenant_b(client)
        resp = client.get("/tenantb/customer/12345/linked-account/new")
    assert resp.status_code == 200
    assert b"Opening Deposit" in resp.data
    assert b"Initial Deposit" not in resp.data


# ------------------------------------------------------------------------------------------
# Regression: tenant A's own login and injection behavior must be untouched by the EXEMPT_PATHS
# and session_expired changes made in app.py for tenant B's sake.
# ------------------------------------------------------------------------------------------


def test_tenant_a_session_expired_still_redirects_to_tenant_a_login() -> None:
    with fixture_app.test_client() as client:
        _login_tenant_a(client)  # armed mode below overrides this session's own state
        arm_resp = client.get("/login?inject=session_expired")
        assert arm_resp.status_code == 200

        hit_resp = client.get("/members?f7=12345")
        assert hit_resp.status_code == 302
        assert urlsplit(hit_resp.headers["Location"]).path == "/login"


def test_require_login_still_redirects_a_sessionless_tenant_a_request_to_tenant_a_login() -> None:
    """The blueprint check added to `require_login` for tenant B's sake must leave tenant A's
    own (non-blueprint) requests on the SAME `/login` redirect they always got."""
    with fixture_app.test_client() as client:
        resp = client.get("/members?f7=12345")
    assert resp.status_code == 302
    assert urlsplit(resp.headers["Location"]).path == "/login"


# ============================================================================================
# Task B: tenant overlays (models/artifact.py), app_fingerprint (models/observation.py), and
# the drift report (evidence/drift.py). Everything below is offline except the LIVE test at the
# very end.
# ============================================================================================


def _minimal_capability(
    steps: list[Step],
    success: Checkpoint | None = None,
    capability_id: str = "cap",
    version: int = 1,
) -> Capability:
    return Capability(
        capability_id=capability_id,
        name="test capability",
        description="a test capability",
        target=TargetApp(entry_point="http://x/login"),
        steps=steps,
        success=success or Checkpoint(kind="text_present", target="page", value="Done"),
        provenance=Provenance(
            run_id="r1",
            model="test-model",
            timestamp="2026-01-01T00:00:00+00:00",
            transcript_hash="0" * 64,
        ),
        version=version,
    )


# --------------------------------------------------------------------------------------------
# B1/B2: TenantOverlay validation
# --------------------------------------------------------------------------------------------


def test_resolve_for_tenant_rejects_unknown_step_override_id() -> None:
    step0 = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="Go"),
        rationale="go",
    )
    capability = _minimal_capability([step0])
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        step_overrides={"99": StepOverride(rationale="nope")},
    )
    with pytest.raises(OverlayError, match="99"):
        resolve_for_tenant(capability, overlay)


def test_resolve_for_tenant_rejects_action_type_change() -> None:
    step0 = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="Go"),
        rationale="go",
    )
    capability = _minimal_capability([step0])
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        step_overrides={"0": StepOverride(action="type")},
    )
    with pytest.raises(OverlayError, match="0"):
        resolve_for_tenant(capability, overlay)


def test_resolve_for_tenant_rejects_unknown_after_step_id() -> None:
    step0 = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="Go"),
        rationale="go",
    )
    capability = _minimal_capability([step0])
    extra = Step(
        id="1", index=1, action="click", target=TargetDescriptor(role="button", name="Confirm"),
        rationale="confirm",
    )
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        extra_steps=[ExtraStep(after_step_id="missing", step=extra)],
    )
    with pytest.raises(OverlayError, match="missing"):
        resolve_for_tenant(capability, overlay)


def test_resolve_for_tenant_rejects_version_mismatch() -> None:
    step0 = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="Go"),
        rationale="go",
    )
    capability = _minimal_capability([step0], version=2)
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=1,
    )
    with pytest.raises(OverlayError, match="version"):
        resolve_for_tenant(capability, overlay)


# --------------------------------------------------------------------------------------------
# B1: resolve_for_tenant itself
# --------------------------------------------------------------------------------------------


def test_resolve_for_tenant_applies_vocabulary_without_mutating_base() -> None:
    step = Step(
        id="0",
        index=0,
        action="type",
        target=TargetDescriptor(
            role="textbox",
            name="Member ID",
            relational=RelationalHint(label="Member ID"),
            frame_path=["member_frame"],
        ),
        postcondition=Checkpoint(kind="url_matches", target="any_frame", value="http://x/member/123"),
        rationale="type the member id",
    )
    capability = _minimal_capability(
        [step], success=Checkpoint(kind="text_present", target="page", value="Member ID Saved")
    )
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        vocabulary_map={
            "Member ID": "Customer ID",
            "member_frame": "customer_frame",
            "/member/": "/customer/",
        },
    )

    resolved = resolve_for_tenant(capability, overlay)

    resolved_target = resolved.steps[0].target
    assert resolved_target is not None
    assert resolved_target.name == "Customer ID"
    assert resolved_target.relational is not None
    assert resolved_target.relational.label == "Customer ID"
    assert resolved_target.frame_path == ["customer_frame"]
    assert resolved.steps[0].postcondition is not None
    assert resolved.steps[0].postcondition.value == "http://x/customer/123"
    assert resolved.success.value == "Customer ID Saved"
    assert resolved.target.tenant_id == "tenant_b"

    # the base capability, and everything it owns, is untouched
    assert resolved is not capability
    assert capability.steps[0].target is not None
    assert capability.steps[0].target.name == "Member ID"
    assert capability.steps[0].target.relational is not None
    assert capability.steps[0].target.relational.label == "Member ID"
    assert capability.steps[0].target.frame_path == ["member_frame"]
    assert capability.steps[0].postcondition is not None
    assert capability.steps[0].postcondition.value == "http://x/member/123"
    assert capability.success.value == "Member ID Saved"
    assert capability.target.tenant_id is None


def test_resolve_for_tenant_inserts_extra_step_and_renumbers_index() -> None:
    step0 = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="Go"),
        rationale="go",
    )
    step1 = Step(
        id="1", index=1, action="click", target=TargetDescriptor(role="button", name="Next"),
        rationale="next",
    )
    capability = _minimal_capability([step0, step1])
    extra = Step(
        id="0b", index=99, action="click", target=TargetDescriptor(role="button", name="Confirm"),
        rationale="confirm",
    )
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        extra_steps=[ExtraStep(after_step_id="0", step=extra)],
    )

    resolved = resolve_for_tenant(capability, overlay)

    assert [s.id for s in resolved.steps] == ["0", "0b", "1"]
    assert [s.index for s in resolved.steps] == [0, 1, 2]
    # base untouched
    assert [s.id for s in capability.steps] == ["0", "1"]
    assert [s.index for s in capability.steps] == [0, 1]


def test_vocabulary_substitution_is_single_pass_not_sequential() -> None:
    """A map whose replacement output contains another key must not be re-matched: replacing
    "AA" with "B" while also mapping "B" -> "C" must leave a lone "AA" as "B", never chase the
    output on to "C" -- a sequential str.replace chain over the same dict would."""
    step = Step(
        id="0", index=0, action="click", target=TargetDescriptor(role="button", name="AA"),
        rationale="click",
    )
    capability = _minimal_capability([step])
    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        vocabulary_map={"AA": "B", "B": "C"},
    )

    resolved = resolve_for_tenant(capability, overlay)

    assert resolved.steps[0].target is not None
    assert resolved.steps[0].target.name == "B"


_SHIPPED_OVERLAY_PATH = Path(__file__).resolve().parent.parent / "overlays" / "tenant_b.json"


def test_shipped_overlay_declares_the_checkpoint_but_leaves_the_descriptor_positional() -> None:
    """A rename is absorbable by a LOCATOR (rank_ordinal fallback) and not by a CHECKPOINT
    (`checkpoint_satisfied` matches role+name exactly, once, with no ranked fallback at all). The
    real subaccount flow's step 7 postcondition and step 8 target both name the SAME renamed
    field ("Initial Deposit" -> tenant B's "Opening Deposit"): the shipped overlay leaves that
    rename OUT of `vocabulary_map` (so step 8's descriptor keeps resolving positionally, proving
    the locator strategy absorbs it) but declares it via `step_overrides["7"]` (because step 7's
    postcondition has no fallback to absorb it with). Loaded from the real overlay file on disk,
    not a copy, so a future edit to overlays/tenant_b.json is what this test actually pins.
    """
    overlay = TenantOverlay.model_validate_json(
        _SHIPPED_OVERLAY_PATH.read_text(encoding="utf-8")
    )
    step7 = Step(
        id="7", index=0, action="type",
        target=TargetDescriptor(role="textbox", name="Nickname"),
        postcondition=Checkpoint(kind="element_present", target="textbox", value="Initial Deposit"),
        rationale="type the nickname",
    )
    step8 = Step(
        id="8", index=1, action="type",
        target=TargetDescriptor(role="textbox", name="Initial Deposit"),
        postcondition=Checkpoint(kind="element_present", target="button", value="Submit"),
        rationale="type the initial deposit",
    )
    step9 = Step(
        id="9", index=2, action="click",
        target=TargetDescriptor(role="button", name="Submit"),
        postcondition=Checkpoint(kind="text_present", target="page", value="Subaccount Opened"),
        rationale="submit",
    )
    capability = _minimal_capability(
        [step7, step8, step9],
        capability_id=overlay.base_capability_id,
        version=overlay.base_version,
    )

    resolved = resolve_for_tenant(capability, overlay)

    resolved_by_id = {step.id: step for step in resolved.steps}
    assert resolved_by_id["7"].postcondition is not None
    assert resolved_by_id["7"].postcondition.value == "Opening Deposit"  # checkpoint: declared
    assert resolved_by_id["8"].target is not None
    assert resolved_by_id["8"].target.name == "Initial Deposit"  # descriptor: left positional


# --------------------------------------------------------------------------------------------
# B3: app_fingerprint
# --------------------------------------------------------------------------------------------


def test_app_fingerprint_differs_for_structurally_different_observations() -> None:
    single_frame = Observation(
        url="http://x/a",
        title="Page A",
        urls=["http://x/a"],
        elements=[UIElement(node_id="0", role="button", name="Go")],
    )
    two_frames_more_controls = Observation(
        url="http://x/b",
        title="Page B",
        urls=["http://x/b", "http://x/b/content"],
        elements=[
            UIElement(node_id="0", role="button", name="Go"),
            UIElement(node_id="1", role="textbox", name="Search"),
        ],
    )
    assert app_fingerprint(single_frame) != app_fingerprint(two_frames_more_controls)


def test_app_fingerprint_identical_for_the_same_observation_twice() -> None:
    observation = Observation(
        url="http://x/a",
        title="Page A",
        urls=["http://x/a"],
        elements=[UIElement(node_id="0", role="button", name="Go")],
    )
    assert app_fingerprint(observation) == app_fingerprint(observation)


class _FingerprintSurface:
    """A minimal fake Surface: click "Go" once, then the page shows DONE_TOKEN -- the same shape
    as test_phase7.py's own _GoalVerifiedSurface, kept local since fake harnesses are not shared
    across phase test modules in this project."""

    def __init__(self) -> None:
        self.dialog_events: list[dict] = []
        self._clicked = False

    @property
    def url(self) -> str:
        return "http://fake/entry"

    def observe(self) -> Observation:
        status = "Status: DONE_TOKEN" if self._clicked else "Status: pending"
        return Observation(
            url=self.url,
            title="Fake App",
            urls=[self.url],
            elements=[
                UIElement(node_id="0", role="button", name="Go"),
                UIElement(node_id="1", role="generic", name=status),
            ],
        )

    def act(self, action: object) -> str | None:
        if isinstance(action, Click):
            self._clicked = True
        return None


class _FingerprintLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(name="click", args={"index": 0, "rationale": "click Go"})
            return LLMResponse(tool_calls=[call], text=None, usage={})
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_loop_captures_app_fingerprint_and_recorder_reads_it_offline(tmp_path: Path) -> None:
    """Proves the FULL offline capture path with no live browser and no live model: agent/loop.py
    logs one "app_fingerprint" event from the first observation after navigating to the target,
    and record/recorder.py reads that event into Capability.target.app_fingerprint."""
    surface = _FingerprintSurface()
    logger = EvidenceLogger("phase12-fingerprint", "discovery", base_dir=tmp_path)
    policy = Policy(
        version=1,
        app_id="test",
        entry_point="http://fake/entry",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["textbox", "searchbox", "combobox", "button", "link", "generic"],
    )
    gate = PolicyGate(policy, logger, mode="discovery")

    outcome = run(
        goal="reach DONE_TOKEN",
        target="http://fake/entry",
        surface=surface,
        llm=_FingerprintLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == RunStatus.GOAL_VERIFIED

    events = [
        json.loads(line)
        for line in (logger.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fingerprint_events = [e for e in events if e.get("type") == "app_fingerprint"]
    assert len(fingerprint_events) == 1
    expected_fingerprint = fingerprint_events[0]["fingerprint"]
    assert expected_fingerprint == app_fingerprint(surface.observe())

    capability = build_capability(
        run_dir=logger.dir,
        goal="reach DONE_TOKEN",
        target="http://fake/entry",
        run_id="phase12-fingerprint",
        model="test-model",
        capability_id="reach-done-token",
        policy=policy,
        llm=None,
    )
    assert capability.target.app_fingerprint == expected_fingerprint


# --------------------------------------------------------------------------------------------
# LIVE: one measured run against tenant B, with an in-memory overlay that maps ROUTES but
# deliberately leaves TWO renamed fields out of vocabulary_map, to pin the two different things
# that can happen to a rename the overlay does not know about:
#   - step 0 ("Username" -> tenant B's "User ID"): the recorded descriptor carries no ordinal
#     hint of its own, but role_ordinal still resolves it -- the login form's first textbox is
#     unambiguous -- so it is absorbed POSITIONALLY and reported as drift clause 2
#     ("name_no_longer_matched"; clause 1 cannot fire without a recorded_rank, and this
#     capability predates that field on every step).
#   - step 3 ("Member ID" -> tenant B's "Customer ID"): the customer-search page also has
#     exactly one remaining role="textbox" candidate once every name-matching rung has failed,
#     but the resolver's own rule (surface/locator.py) refuses to use an ordinal fallback to
#     rescue a descriptor that ONCE had a meaningful name -- an ordinal is only trusted for a
#     descriptor that was never given a name to begin with. So this rename is NOT absorbed: the
#     run ends as a hard failure, category locator_unresolved, at step 3.
# Both halves are asserted below. A test that tolerated any outcome (e.g. only inspecting one
# step's own drift event, ignoring how the run as a whole ended) would keep passing even if the
# run started failing for a wholly unrelated reason.
# --------------------------------------------------------------------------------------------

_BALANCE_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "legacy_bank.yaml"
_TENANT_B_POLICY_PATH = (
    Path(__file__).resolve().parent.parent / "policies" / "legacy_bank_tenant_b.yaml"
)
_BALANCE_EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence" / "discovery"
_BALANCE_GOAL = "look up member 12345 and read their current savings balance"
_BALANCE_CAPABILITY_ID = "look-up-member-12345-and-read-their-current-savings-balance"


def _rebuilt_balance_capability() -> Capability:
    """Built fresh from the real evidence log every time this test runs (docs/adr/0011: never
    depend on a frozen file under artifacts/), the same convention tests/test_phase9.py's own
    `_rebuilt_capability` uses."""
    policy = load_policy(_BALANCE_POLICY_PATH)
    return build_capability(
        run_dir=_BALANCE_EVIDENCE_DIR,
        goal=_BALANCE_GOAL,
        target="http://127.0.0.1:5055/login",
        run_id="b2405e162ba4",
        model="gemini-3.6-flash",
        capability_id=_BALANCE_CAPABILITY_ID,
        policy=policy,
        llm=None,
    )


def _fixture_reachable(host: str = "127.0.0.1", port: int = 5055) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _replay_events(base_dir: Path) -> list[dict]:
    run_dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1, run_dirs
    text = (run_dirs[0] / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_live_balance_capability_against_tenant_b_absorbs_one_rename_and_fails_on_the_other(
    tmp_path: Path,
) -> None:
    if not _fixture_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank` before running this test"
        )
    capability = _rebuilt_balance_capability()
    assert capability.steps[0].target is not None
    assert capability.steps[0].target.recorded_rank is None  # this evidence predates the field

    overlay = TenantOverlay(
        tenant_id="tenant_b",
        base_capability_id=capability.capability_id,
        base_version=capability.version,
        entry_point_override="http://127.0.0.1:5055/tenantb/login",
        vocabulary_map={
            "/members": "/tenantb/customers",
            "?f7=": "?q=",
            "/member/": "/tenantb/customer/",
        },
    )
    resolved = resolve_for_tenant(capability, overlay)

    result = replay_engine.replay_resolved(
        resolved,
        {"password": "testpass", "member_id": 12345},
        _TENANT_B_POLICY_PATH,
        evidence_base_dir=tmp_path,
    )

    events = _replay_events(tmp_path)
    drift = [e for e in events if e.get("type") == "locator_drift" and e.get("step_id") == 0]
    assert drift
    assert drift[0]["clause"] == "name_no_longer_matched"
    assert drift[0]["recorded_rank"] is None  # clause 1 cannot fire without a recorded_rank

    # The OTHER rename (step 3, "Member ID" -> "Customer ID") is not absorbed: unlike the login
    # field, this descriptor's sole remaining role="textbox" candidate is deliberately never
    # used to rescue a descriptor that once had a meaningful name (surface/locator.py), so the
    # run ends loudly here rather than being silently carried on a guess.
    assert result.kind == "hard_failure"
    assert result.step_id == 3
    assert result.category == FailureCategory.LOCATOR_UNRESOLVED
