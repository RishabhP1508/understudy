"""Phase 6 tests: the RunEvent schema, screenshot pairs, failure evidence, the four-kind result
contract, and the three-way failure classification. Everything here runs with no browser, no
network, and no API key, EXCEPT the tests explicitly marked LIVE below, which skip loudly naming
exactly what precondition is missing. Every EvidenceLogger constructed by a test takes
base_dir=tmp_path (tests/conftest.py checks this is true for the whole suite).
"""

from __future__ import annotations

import base64
import io
import json
import socket
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import TypeAdapter

from understudy.agent.loop import run
from understudy.evidence.logger import EvidenceLogger, RunEvent
from understudy.llm.base import LLMResponse, ToolCall
from understudy.models.artifact import Capability
from understudy.models.observation import PERCEPTION_VERSION, Observation, UIElement
from understudy.models.result import (
    BusinessOutcome,
    Escalated,
    FailureCategory,
    HardFailure,
    ReplayResult,
    Success,
)
from understudy.replay import engine as replay_engine
from understudy.replay.engine import _classify_entry_navigate_failure, _classify_locator_failure
from understudy.safety.policy import Policy, PolicyGate
from understudy.surface.base import Action, Click, Type
from understudy.surface.locator import resolve

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"
ARTIFACT_PATH = (
    Path(__file__).parent.parent
    / "artifacts"
    / "look-up-member-12345-and-read-their-current-savings-balance.v1.json"
)
_SENTINELS = ("SECRET_SENTINEL_VALUE", "123-45-6789")


def _assert_no_sentinel_leak(text: str) -> None:
    """Mirrors tests/test_constraints.py's invariant 3 encoding checks (utf-8, url-encoded,
    base64), against a real run.jsonl produced by actually running the system under the Phase 6
    event schema."""
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


def _permissive_policy(**overrides: Any) -> Policy:
    base: dict[str, Any] = dict(
        version=1,
        app_id="test",
        entry_point="http://fake/start",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["textbox", "searchbox", "combobox", "button", "link", "generic"],
        sensitive_fields={"secret": ["password"], "pii": ["ssn"]},
    )
    base.update(overrides)
    return Policy(**base)


def _fixture_app_reachable(host: str = "127.0.0.1", port: int = 5055) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


# ------------------------------------------------------------------------------------------
# T1 / T2 / T3: a real discovery run, the full directory layout, and the rationale rule.
# ------------------------------------------------------------------------------------------


class _ClickThenFinishSurface:
    """One click actually changes the page (pending -> DONE_TOKEN), so the run produces at
    least one real 'act' event before finishing -- T3 needs a non-vacuous act-event count."""

    def __init__(self) -> None:
        self.dialog_events: list[dict[str, Any]] = []
        self.acted: list[Action] = []
        self._clicked = False

    @property
    def url(self) -> str:
        return "http://fake/start"

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

    def screenshot_bytes(self) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color=(10, 20, 30)).save(buf, format="PNG")
        return buf.getvalue()


class _ClickThenFinishLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            call = ToolCall(
                name="click",
                args={"index": 0, "rationale": "click Go to trigger the status update"},
            )
            return LLMResponse(tool_calls=[call], text=None, usage={"total_tokens": 5})
        checkpoint = {"kind": "text_present", "target": "page", "value": "DONE_TOKEN"}
        call = ToolCall(name="finish", args={"checkpoint": checkpoint, "rationale": "done"})
        return LLMResponse(tool_calls=[call], text=None, usage={})


def test_t1_t2_t3_real_discovery_run_produces_the_full_layout_and_valid_act_events(
    tmp_path: Path,
) -> None:
    surface = _ClickThenFinishSurface()
    llm = _ClickThenFinishLLM()
    logger = EvidenceLogger("phase6-t1", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="reach the DONE_TOKEN state",
        target="http://fake/start",
        surface=surface,
        llm=llm,
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    # cli.py's own discover() command calls this after run(); mirrored here so the test covers
    # the whole layout run() plus its caller are together responsible for.
    logger.write_result(outcome)

    assert outcome.status == "goal_verified"

    # T1: the full directory layout run() (plus write_result) is responsible for.
    assert (logger.dir / "run.jsonl").exists()
    assert (logger.dir / "transcript.jsonl").exists()
    assert (logger.dir / "result.json").exists()
    screenshots = sorted((logger.dir / "steps").glob("*.png"))
    assert screenshots, "expected at least one masked screenshot under steps/"

    # T2: every line parses as JSON and validates against RunEvent.
    lines = [
        line
        for line in (logger.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [RunEvent.model_validate(json.loads(line)) for line in lines]
    print(f"\nrun.jsonl line count: {len(lines)}")
    assert len(events) == len(lines) > 0

    # T3: no act event has a null, empty, or whitespace-only rationale, or the literal
    # "[REDACTED]"; the count of act events actually checked must be > 0 so this cannot pass
    # vacuously.
    act_events = [e for e in events if e.type == "act"]
    print(f"act event count: {len(act_events)}")
    assert len(act_events) > 0
    for event in act_events:
        assert event.rationale is not None
        assert event.rationale.strip() != ""
        assert event.rationale != "[REDACTED]"


# ------------------------------------------------------------------------------------------
# T4 (CI-safe half): capture_failure writes dom/ and a11y/ and lists them, using a broken
# artifact built PROGRAMMATICALLY from the real one (never hand-written).
# ------------------------------------------------------------------------------------------


class _FakeSurfaceWithDom:
    """No tracing attribute at all, so logger.capture_failure's trace half is a no-op here --
    the live test below covers trace.zip against a real browser."""

    def dom_snapshot(self) -> str:
        return "<html><body>a fake page, for evidence only, never perception</body></html>"


def _mutated_unresolvable_artifact(tmp_path: Path) -> Capability:
    """Load the real artifact, mutate step 0's target in memory so it can never resolve, write
    it to tmp_path, and read it back -- never hand-write an artifact file."""
    original = Capability.model_validate_json(ARTIFACT_PATH.read_text(encoding="utf-8"))
    mutated_target = original.steps[0].target.model_copy(
        update={"name": "Definitely Not A Real Field On Any Page", "ordinal": None}
    )
    broken = original.model_copy(update={"steps": [
        original.steps[0].model_copy(update={"target": mutated_target}),
        *original.steps[1:],
    ]})
    broken_path = tmp_path / "broken-artifact.json"
    broken_path.write_text(broken.model_dump_json(indent=2), encoding="utf-8")
    return Capability.model_validate_json(broken_path.read_text(encoding="utf-8"))


def test_t4_capture_failure_writes_dom_and_a11y_and_lists_them_in_evidence_refs(
    tmp_path: Path,
) -> None:
    broken = _mutated_unresolvable_artifact(tmp_path)
    login_page = Observation(
        url="http://fake/login",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="textbox", name="Username"),
            UIElement(node_id="1", role="textbox", name="Password"),
            UIElement(node_id="2", role="button", name="Login"),
        ],
    )
    resolution = resolve(broken.steps[0].target, login_page)
    assert resolution.element is None, "the mutated target must not resolve against anything"

    logger = EvidenceLogger("phase6-t4", "test", base_dir=tmp_path)
    refs = logger.capture_failure(_FakeSurfaceWithDom(), 0, login_page)

    assert (logger.dir / "dom" / "000.html").exists()
    assert (logger.dir / "a11y" / "000.json").exists()
    assert any(ref.startswith("dom") for ref in refs)
    assert any(ref.startswith("a11y") for ref in refs)
    assert not any("trace" in ref for ref in refs)  # no tracing attribute on this fake

    # evidence_refs is a result-contract field, always POSIX-separated regardless of host OS: no
    # ref may contain a backslash, and each one must resolve to a real file when joined with "/"
    # -- not merely compare equal to itself, which passes trivially on every platform.
    for ref in refs:
        assert "\\" not in ref, f"evidence_refs must be POSIX-separated, got {ref!r}"
        assert (logger.dir / ref).exists(), f"{ref} does not exist under {logger.dir}"

    result = HardFailure(
        step_id=0,
        category=FailureCategory.LOCATOR_UNRESOLVED,
        expected="a unique element matching the mutated target",
        observed="0 candidates at every rung",
        evidence_refs=refs,
    )
    result_path = logger.write_result(result)
    written = json.loads(result_path.read_text(encoding="utf-8"))
    assert set(written["evidence_refs"]) == set(refs)
    for ref in written["evidence_refs"]:
        assert "\\" not in ref, f"result.json must serialize POSIX-separated refs, got {ref!r}"
        assert (logger.dir / ref).exists(), f"{ref} does not exist under {logger.dir}"


# ------------------------------------------------------------------------------------------
# T4 (live half) + T9: forced failure against the real fixture app keeps trace.zip, and the
# real, unmutated artifact (perception_version defaulting to 1) still replays successfully.
# ------------------------------------------------------------------------------------------


def test_live_forced_failure_keeps_a_trace_and_lists_it_in_evidence_refs(tmp_path: Path) -> None:
    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055` before running "
            "this test"
        )
    broken = _mutated_unresolvable_artifact(tmp_path)
    broken_path = tmp_path / "broken-for-replay.json"
    broken_path.write_text(broken.model_dump_json(indent=2), encoding="utf-8")

    result = replay_engine.replay(broken_path, {}, POLICY_PATH, evidence_base_dir=tmp_path)

    assert result.kind == "hard_failure"
    assert any(ref.endswith("trace.zip") for ref in result.evidence_refs), result.evidence_refs
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1
    for ref in result.evidence_refs:
        assert "\\" not in ref, f"evidence_refs must be POSIX-separated, got {ref!r}"
        assert (run_dirs[0] / ref).exists(), f"{ref} does not exist under {run_dirs[0]}"


def test_t9_live_real_artifact_replays_successfully_despite_perception_version_mismatch(
    tmp_path: Path,
) -> None:
    capability = Capability.model_validate_json(ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert capability.provenance.perception_version == 1  # never backfilled; see D6/ADR 0009
    assert PERCEPTION_VERSION != 1  # a genuine mismatch, not a coincidence

    if not _fixture_app_reachable():
        pytest.skip(
            "the legacy_bank fixture app is not reachable on 127.0.0.1:5055; start it with "
            "`.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055` before running "
            "this test"
        )

    result = replay_engine.replay(ARTIFACT_PATH, {}, POLICY_PATH, evidence_base_dir=tmp_path)

    assert result.kind == "success", (
        f"a perception_version mismatch must only ever CLASSIFY a failure, never gate replay "
        f"up front; got {result!r}"
    )


# ------------------------------------------------------------------------------------------
# T5: redaction through the REAL logger.screenshot path, not redact_screenshot directly.
# ------------------------------------------------------------------------------------------


class _FakeSurfaceWithScreenshot:
    def __init__(self, raw_png: bytes) -> None:
        self._raw_png = raw_png

    def screenshot_bytes(self) -> bytes:
        return self._raw_png

    def fill_bounds(self, elements: list[UIElement]) -> None:
        pass  # bounds are already set on the fixture elements below; nothing to resolve.


def test_t5_logger_screenshot_masks_only_the_flagged_region_through_the_real_path(
    tmp_path: Path,
) -> None:
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

    logger = EvidenceLogger("phase6-t5", "test", base_dir=tmp_path)
    path = logger.screenshot(_FakeSurfaceWithScreenshot(raw), 3, "before", observation)

    assert path is not None
    assert path.name == "003_before.png"

    original = Image.open(io.BytesIO(raw)).convert("RGB")
    masked = Image.open(path).convert("RGB")
    assert masked.getpixel((4, 4)) != original.getpixel((4, 4))  # inside the box: changed
    assert masked.getpixel((15, 15)) == original.getpixel((15, 15))  # outside: unchanged


# ------------------------------------------------------------------------------------------
# T6: all four result kinds round-trip through the discriminated union.
# ------------------------------------------------------------------------------------------


def test_t6_all_four_result_kinds_round_trip_through_the_union() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ReplayResult)
    examples: list[Any] = [
        Success(outputs={"balance": "$1,204.55"}, steps_run=3, duration_ms=120.5),
        BusinessOutcome(code="member_not_found", message="no such member", outputs={}),
        HardFailure(
            step_id=2,
            category=FailureCategory.LOCATOR_UNRESOLVED,
            expected="x",
            observed="y",
            evidence_refs=["a11y/002.json"],
        ),
        Escalated(intervention_id="abc123", resolution=None, resumed=False),
    ]
    for example in examples:
        restored = adapter.validate_json(example.model_dump_json())
        assert restored.kind == example.kind
        assert type(restored) is type(example)


# ------------------------------------------------------------------------------------------
# T7: sentinel absence over a real generated run log from THIS phase's event schema.
# ------------------------------------------------------------------------------------------


class _SecretFlowSurface:
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
                UIElement(node_id="0", role="textbox", name="Password", sensitivity="secret"),
                UIElement(node_id="1", role="generic", name=status),
            ],
        )

    def act(self, action: Action) -> str | None:
        self.acted.append(action)
        if isinstance(action, Type):
            self._done = True
        return None


class _SecretFlowLLM:
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


def test_t7_sentinel_absent_from_a_real_generated_run_log(tmp_path: Path) -> None:
    logger = EvidenceLogger("phase6-t7", "test", base_dir=tmp_path)
    gate = PolicyGate(_permissive_policy(), logger, mode="discovery")

    outcome = run(
        goal="update the password",
        target="http://fake/secret-flow",
        surface=_SecretFlowSurface(),
        llm=_SecretFlowLLM(),
        gate=gate,
        logger=logger,
        max_steps=5,
        timeout_s=30,
    )
    assert outcome.status == "goal_verified"

    run_jsonl_text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    _assert_no_sentinel_leak(run_jsonl_text)
    transcript_text = (logger.dir / "transcript.jsonl").read_text(encoding="utf-8")
    _assert_no_sentinel_leak(transcript_text)


# ------------------------------------------------------------------------------------------
# T8: the three-way failure classification, unit-level.
# ------------------------------------------------------------------------------------------


def test_t8_entry_navigate_failure_classifies_target_unreachable_on_net_err() -> None:
    net_err = Exception("net::ERR_CONNECTION_REFUSED at http://127.0.0.1:1/")
    assert _classify_entry_navigate_failure(net_err) == FailureCategory.TARGET_UNREACHABLE

    other = Exception("Timeout 30000ms exceeded.")
    assert _classify_entry_navigate_failure(other) == FailureCategory.ACTION_FAILED


def test_t8_locator_failure_classifies_stale_perception_vs_locator_unresolved() -> None:
    capability = Capability.model_validate_json(ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert capability.provenance.perception_version == 1
    assert PERCEPTION_VERSION != 1

    # Versions differ (the real artifact's own recorded state): STALE_PERCEPTION.
    assert _classify_locator_failure(capability) == FailureCategory.STALE_PERCEPTION

    # Versions match: LOCATOR_UNRESOLVED.
    matching = capability.model_copy(
        update={
            "provenance": capability.provenance.model_copy(
                update={"perception_version": PERCEPTION_VERSION}
            )
        }
    )
    assert _classify_locator_failure(matching) == FailureCategory.LOCATOR_UNRESOLVED
