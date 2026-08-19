"""Phase 8 tests: the full Capability schema, record/canonicalize.py, and the rewritten
record/recorder.py. Everything here runs with no browser, no network, and no API key: the real
capability under test is built by calling `build_capability` directly against the REAL,
already-on-disk evidence log at `evidence/discovery/` (the one genuine discovery run
this project's non-negotiable requirement depends on) with `llm=None`, so this exercises D5's
deterministic degrade path -- never a live model call. Per docs/adr/0011, no test here depends on
the frozen CONTENT of a file under `artifacts/`; the capability under test is built fresh, in
memory, from the evidence log every time this suite runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from understudy.cli import _discover_and_capture
from understudy.evidence.logger import EvidenceLogger
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    InputParam,
    ParamRef,
    Provenance,
    StabilitySignal,
    TargetApp,
    checkpoint_satisfied,
)
from understudy.models.observation import Observation, UIElement
from understudy.record.recorder import _prune_dead_ends, build_capability
from understudy.replay import engine as replay_engine
from understudy.replay.engine import _action_for_step, _resolve_checkpoint
from understudy.safety.policy import Policy, PolicyGate
from understudy.safety.redact import Redactor
from understudy.surface.base import Action, Navigate, Type

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence" / "discovery"
POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"
GOAL = "look up member 12345 and read their current savings balance"
TARGET = "http://127.0.0.1:5055/login"
_BANNED_SCHEMA_KEYS = {"messages", "transcript", "completion", "choices", "content"}


def _fake_policy(**overrides: Any) -> Policy:
    base: dict[str, Any] = dict(
        version=1,
        app_id="test-app",
        entry_point="http://fake/a",
        allowed_origins=["http://fake"],
        allowed_routes=["/*"],
        allowed_actions=["navigate", "click", "type", "select", "read_text"],
        allowed_roles=["textbox", "searchbox", "combobox", "button", "link", "generic"],
        sensitive_fields={"secret": ["password"], "pii": ["ssn"]},
    )
    base.update(overrides)
    return Policy(**base)


@pytest.fixture(scope="module")
def real_capability() -> Capability:
    """Built fresh from the real evidence log every time this suite runs (docs/adr/0011: never
    depend on a frozen file under artifacts/). `llm=None` exercises D5's deterministic
    name/description fallback -- no network call anywhere in this fixture.
    """
    return build_capability(
        run_dir=EVIDENCE_DIR,
        goal=GOAL,
        target=TARGET,
        run_id="b2405e162ba4",
        model="gemini-3.6-flash",
        capability_id="look-up-member-12345-and-read-their-current-savings-balance",
        policy=_fake_policy(app_id="legacy_bank"),
        llm=None,
    )


def _collect_dict_keys(data: object) -> set[str]:
    keys: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            keys.update(node.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return keys


# ------------------------------------------------------------------------------------------
# Invariant 4, non-trivially, against the REAL recorded capability.
# ------------------------------------------------------------------------------------------


def test_real_capability_has_no_transcript_shaped_keys_and_a_real_transcript_hash(
    real_capability: Capability,
) -> None:
    schema = type(real_capability).model_json_schema()
    schema_keys = {key.lower() for key in _collect_dict_keys(schema)}
    assert not _BANNED_SCHEMA_KEYS & schema_keys

    dumped = json.loads(Redactor().dumps(real_capability))
    data_keys = {key.lower() for key in _collect_dict_keys(dumped)}
    assert not _BANNED_SCHEMA_KEYS & data_keys

    transcript_hash = real_capability.provenance.transcript_hash
    assert re.fullmatch(r"[0-9a-f]{32,128}", transcript_hash)


# ------------------------------------------------------------------------------------------
# At least one ParamRef and one canonicalized route.
# ------------------------------------------------------------------------------------------


def test_at_least_one_param_ref_and_one_canonicalized_route(real_capability: Capability) -> None:
    assert real_capability.inputs, "expected at least one InputParam"
    param_names = {param.name for param in real_capability.inputs}

    param_refs = [
        step.value
        for step in real_capability.steps
        if isinstance(step.value, ParamRef) and step.value.name in param_names
    ]
    assert param_refs, "expected at least one step whose value is a ParamRef"

    checkpoints = [step.postcondition for step in real_capability.steps if step.postcondition]
    checkpoints.append(real_capability.success)
    canonicalized = [
        cp
        for cp in checkpoints
        if cp.kind == "url_matches" and any(f":{name}" in cp.value for name in param_names)
    ]
    assert canonicalized, "expected at least one canonicalized route in a url_matches checkpoint"


# ------------------------------------------------------------------------------------------
# Pruning: a synthetic three-action dead end is removed, before/after counts asserted.
# ------------------------------------------------------------------------------------------


def _act_event(url: str, index: int, name: str) -> dict[str, Any]:
    return {
        "type": "act",
        "rationale": f"click {name}",
        "policy_decision": {"allowed": True, "checked_urls": [url], "risk": "SAFE_REVERSIBLE"},
        "proposed_action": {"kind": "click", "node_id": str(index)},
        "context": {
            "tool": "click",
            "rationale": f"click {name}",
            "target": {"role": "button", "name": name},
        },
    }


def test_prune_dead_ends_removes_a_synthetic_three_action_detour() -> None:
    """[A, B, C, A, D]: `a` and `a_again` share a state with nothing adjacent between them and
    `a`, so the whole `a, b, c` prefix is the detour (it is `a`'s own action that starts the
    excursion away from A) -- only the RETURNING occurrence (`a_again`) and `d` survive."""
    a = _act_event("http://fake/a", 0, "A")
    b = _act_event("http://fake/b", 1, "B")
    c = _act_event("http://fake/c", 2, "C")
    a_again = _act_event("http://fake/a", 3, "A")  # same state as `a`: b and c led nowhere
    d = _act_event("http://fake/d", 4, "D")
    events = [a, b, c, a_again, d]

    pruned = _prune_dead_ends(events)

    assert len(events) == 5
    assert len(pruned) == 2
    assert pruned == [a_again, d]


def test_prune_dead_ends_keeps_the_action_that_progresses_out_of_a_revisited_state() -> None:
    """[A, A, B, A, C]: `a1` and `a2` are ADJACENT at the same state -- ordinary sequential
    progress (e.g. a login form's username then password), never a detour -- so `a1` stays even
    though the state repeats immediately. `b` is a real, non-adjacent detour away from A and back
    (via `a3`), so `b` alone is dropped. `a3` is the action that actually escapes the loop toward
    `c` and must survive: a version of this function that drops it instead leaves a gap replay
    cannot bridge.
    """
    a1 = _act_event("http://fake/a", 0, "A1")
    a2 = _act_event("http://fake/a", 1, "A2")
    b = _act_event("http://fake/b", 2, "B")
    a3 = _act_event("http://fake/a", 3, "A3")
    c = _act_event("http://fake/c", 4, "C")
    events = [a1, a2, b, a3, c]

    pruned = _prune_dead_ends(events)

    assert pruned == [a1, a3, c]


def _write_synthetic_run(tmp_path: Path, steps: list[tuple[str, str]]) -> Path:
    """A run.jsonl built through the REAL EvidenceLogger.event() (so every line validates
    against RunEvent exactly like a real run's does), not hand-written JSON."""
    logger = EvidenceLogger("synthetic", "test", base_dir=tmp_path)
    logger.event(
        "run_start", goal="reach fake/d", target="http://fake/a", run_id="synthetic", model="fake"
    )
    logger.event(
        "act",
        phase="act",
        rationale="open the target to begin the goal: reach fake/d",
        policy_decision={
            "allowed": True,
            "checked_urls": ["http://fake/a"],
            "risk": "SAFE_REVERSIBLE",
        },
        proposed_action={"kind": "navigate", "url": "http://fake/a"},
        context={"tool": "navigate", "rationale": "open the target"},
    )
    for index, (url, name) in enumerate(steps):
        event = _act_event(url, index, name)
        logger.event("act", phase="act", **{k: v for k, v in event.items() if k != "type"})
    logger.event(
        "goal_verified",
        phase="verify",
        rationale="done",
        checkpoint_eval={"kind": "text_present", "target": "page", "value": "DONE"},
    )
    return logger.dir


def test_build_capability_drops_the_pruned_dead_end_steps(tmp_path: Path) -> None:
    steps = [
        ("http://fake/a", "A"),
        ("http://fake/b", "B"),
        ("http://fake/c", "C"),
        ("http://fake/a", "A"),
        ("http://fake/d", "D"),
    ]
    run_dir = _write_synthetic_run(tmp_path, steps)
    before_dispatched_count = len(steps)

    capability = build_capability(
        run_dir=run_dir,
        goal="reach fake/d",
        target="http://fake/a",
        run_id="synthetic",
        model="fake-model",
        capability_id="synthetic-goal",
        policy=_fake_policy(),
    )

    assert before_dispatched_count == 5
    assert len(capability.steps) == 2


# ------------------------------------------------------------------------------------------
# Every step has a non-null postcondition.
# ------------------------------------------------------------------------------------------


def test_every_step_has_a_non_null_postcondition(real_capability: Capability) -> None:
    assert real_capability.steps
    assert all(step.postcondition is not None for step in real_capability.steps)


# ------------------------------------------------------------------------------------------
# Both directions of the parameter contract.
# ------------------------------------------------------------------------------------------


def test_paramref_and_inputparam_contract_holds_both_directions(
    real_capability: Capability,
) -> None:
    declared = {param.name for param in real_capability.inputs}
    referenced = {
        step.value.name for step in real_capability.steps if isinstance(step.value, ParamRef)
    }
    assert declared, "expected at least one declared InputParam"
    undeclared = referenced - declared
    assert not undeclared, f"a ParamRef references an undeclared param: {undeclared}"
    unreferenced = declared - referenced
    assert not unreferenced, f"a declared InputParam is never referenced: {unreferenced}"


# ------------------------------------------------------------------------------------------
# No serialized confidence float has more than 4 decimal places (D6).
# ------------------------------------------------------------------------------------------


def test_no_serialized_confidence_has_more_than_four_decimal_places(
    real_capability: Capability,
) -> None:
    serialized = Redactor().dumps(real_capability)
    confidences = re.findall(r'"confidence":\s*(-?\d+\.\d+)', serialized)
    assert confidences, "expected at least one confidence value in the serialized artifact"
    for value in confidences:
        decimals = value.split(".")[1]
        assert len(decimals) <= 4, f"confidence {value!r} has more than 4 decimal places"


# ------------------------------------------------------------------------------------------
# Every step rationale is non-empty and matches the corresponding run.jsonl event VERBATIM.
# ------------------------------------------------------------------------------------------


def test_every_step_rationale_matches_run_jsonl_verbatim(real_capability: Capability) -> None:
    raw = (EVIDENCE_DIR / "run.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    dispatched_rationales = [
        event["rationale"]
        for event in events
        if event.get("type") == "act"
        and (event.get("policy_decision") or {}).get("allowed") is True
    ][1:]  # drop the bootstrap navigate, exactly like the recorder does

    assert len(dispatched_rationales) == len(real_capability.steps)
    for step, expected in zip(real_capability.steps, dispatched_rationales, strict=True):
        assert step.rationale != ""
        assert step.rationale == expected


# ------------------------------------------------------------------------------------------
# Capability.json_schema() is valid JSON Schema, checked structurally.
# ------------------------------------------------------------------------------------------


def test_json_schema_is_structurally_valid(real_capability: Capability) -> None:
    schema = real_capability.json_schema()
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict) and schema["properties"]
    valid_types = {"string", "integer", "number", "boolean", "array", "object"}
    for name, prop in schema["properties"].items():
        assert isinstance(name, str) and name
        assert prop["type"] in valid_types
        assert isinstance(prop["description"], str)
    if "required" in schema:
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])


# ------------------------------------------------------------------------------------------
# Lossless round-trip, including a populated StabilitySignal.
# ------------------------------------------------------------------------------------------


def test_round_trips_losslessly_with_a_populated_stability_signal(
    real_capability: Capability,
) -> None:
    """Round-tripped TWICE, not once against the pre-serialization object: `TargetDescriptor`'s
    own `field_serializer` (D6) rounds `confidence` only at serialization time, by design, so a
    freshly-computed in-memory value with float noise (0.49999999999999994) legitimately differs
    from what a first serialization produces (0.5) -- that difference is the documented behaviour
    D6 exists for, not a round-trip defect. What "loses nothing" actually means once a normalizing
    serializer exists is IDEMPOTENCE: an object that has already been through one serialize/parse
    cycle must come back byte-for-byte identical from every cycle after that.
    """
    stability = StabilitySignal(
        runs=5,
        successes=4,
        last_n_outcomes=["success", "success", "success", "hard_failure"],
        computed_at="2026-01-01T00:00:00+00:00",
    )
    stabilized = real_capability.model_copy(update={"stability": stability})

    once = Capability.model_validate_json(stabilized.model_dump_json())
    twice = Capability.model_validate_json(once.model_dump_json())

    assert once == twice
    assert once.stability == stability


# ------------------------------------------------------------------------------------------
# A password-typed field records sensitivity=secret and its literal is absent from the file.
# ------------------------------------------------------------------------------------------


class _MinimalSurface:
    def __init__(self, url: str = "http://fake/a") -> None:
        self._url = url
        self.navigation_violations: list[str] = []

    @property
    def url(self) -> str:
        return self._url

    def observe(self) -> Observation:
        return Observation(url=self._url, title="Fake", elements=[])

    def act(self, action: Action) -> str | None:
        if isinstance(action, Navigate):
            self._url = action.url
        return None


def test_password_field_is_secret_and_its_literal_is_absent_from_the_file(tmp_path: Path) -> None:
    real_secret = "correct-horse-battery-staple-9"
    logger = EvidenceLogger("secret-synth", "test", base_dir=tmp_path)
    policy = _fake_policy()
    gate = PolicyGate(policy, logger, mode="discovery")
    surface = _MinimalSurface()

    gate.dispatch(
        surface, Navigate(url="http://fake/a"), context={"tool": "navigate", "rationale": "open"}
    )
    password_element = UIElement(
        node_id="0", role="textbox", name="Password", sensitivity="secret"
    )
    gate.dispatch(
        surface,
        Type(node_id="0", text=real_secret),
        context={"tool": "type", "rationale": "type the password"},
        element=password_element,
    )
    logger.event(
        "goal_verified",
        phase="verify",
        rationale="done",
        checkpoint_eval={"kind": "text_present", "target": "page", "value": "DONE"},
    )

    capability = build_capability(
        run_dir=logger.dir,
        goal="log in",
        target="http://fake/a",
        run_id="secret-synth",
        model="fake-model",
        capability_id="log-in",
        policy=policy,
    )

    secret_params = [param for param in capability.inputs if param.sensitivity == "secret"]
    assert secret_params and secret_params[0].name == "password"
    assert any(
        isinstance(step.value, ParamRef) and step.value.name == "password"
        for step in capability.steps
    )

    serialized = Redactor().dumps(capability)
    assert real_secret not in serialized
    run_jsonl_text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    assert real_secret not in run_jsonl_text


# ------------------------------------------------------------------------------------------
# target.entry_point equals the resolved --target from the discovery run.
# ------------------------------------------------------------------------------------------


def test_target_entry_point_equals_resolved_target(real_capability: Capability) -> None:
    assert real_capability.target.entry_point == TARGET


# ------------------------------------------------------------------------------------------
# D7: a run that dies inside llm.complete() still writes a terminal event and a result.json.
# ------------------------------------------------------------------------------------------


class _RaisingLLM:
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        raise RuntimeError("simulated llm.complete() failure")


def test_death_inside_llm_complete_still_writes_terminal_event_and_result(tmp_path: Path) -> None:
    logger = EvidenceLogger("dies-in-complete", "test", base_dir=tmp_path)
    policy = _fake_policy()
    gate = PolicyGate(policy, logger, mode="discovery")
    surface = _MinimalSurface()

    with pytest.raises(RuntimeError):
        _discover_and_capture(
            goal="whatever the goal is",
            target="http://fake/a",
            surface=surface,
            llm=_RaisingLLM(),
            gate=gate,
            logger=logger,
            max_steps=5,
            timeout_s=30,
            stall_limit=3,
            full_render_every=5,
        )

    events = [
        json.loads(line)
        for line in (logger.dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["type"] == "run_end"
    assert events[-1]["status"] == "error"
    assert events[-1]["error"] == "RuntimeError"

    result = json.loads((logger.dir / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["error"] == "RuntimeError"


# ------------------------------------------------------------------------------------------
# Coordinator-flagged defects: replay must interpolate caller-supplied params, both into a
# step's own ParamRef value and into a canonicalized checkpoint's ":name" placeholder, and must
# refuse -- before any browser launches -- a request missing a required parameter.
# ------------------------------------------------------------------------------------------


def test_action_for_step_resolves_a_paramref_to_the_callers_value(
    real_capability: Capability,
) -> None:
    param_ref_step = next(s for s in real_capability.steps if isinstance(s.value, ParamRef))
    assert isinstance(param_ref_step.value, ParamRef)  # narrows for mypy below

    action = _action_for_step(
        param_ref_step, node_id="0", params={param_ref_step.value.name: "resolved-value"}
    )

    assert isinstance(action, Type)
    assert action.text == "resolved-value"
    assert "${param" not in action.text


def test_checkpoint_placeholder_is_interpolated_before_evaluation(
    real_capability: Capability,
) -> None:
    """A canonicalized route (record/canonicalize.py) is never itself something a live page can
    match; it must be resolved against the caller's own params first, through `_resolve_checkpoint`
    -- the same helper `_action_for_step` uses for a step's own `ParamRef` value, so the two can
    never disagree about what `:member_id` means."""
    canonicalized = Checkpoint(
        kind="url_matches",
        target="any_frame",
        value="http://127.0.0.1:5055/member/:member_id/balance",
    )

    resolved = _resolve_checkpoint(canonicalized, real_capability, {"member_id": "22222"})

    assert resolved.value == "http://127.0.0.1:5055/member/22222/balance"

    observation = Observation(
        url="http://127.0.0.1:5055/member/22222/balance",
        title="Fake",
        elements=[],
        urls=["http://127.0.0.1:5055/member/22222/balance"],
    )
    assert checkpoint_satisfied(observation, resolved)
    # The un-interpolated, canonical form must never accidentally match a real page: the
    # placeholder is a literal string no real URL contains.
    assert not checkpoint_satisfied(observation, canonicalized)


def test_missing_required_param_is_a_hard_failure_before_any_browser_launches(
    tmp_path: Path, real_capability: Capability
) -> None:
    """No WebSurface is ever constructed on this path: this test needs no fixture app and no real
    browser at all, which is itself the proof that the check runs before step 0 -- indeed before
    the entry-point navigate."""
    artifact_path = tmp_path / "capability.json"
    artifact_path.write_text(real_capability.model_dump_json(), encoding="utf-8")

    result = replay_engine.replay(
        artifact_path,
        {"member_id": "12345"},  # "password" (required, secret) is missing
        POLICY_PATH,
        evidence_base_dir=tmp_path,
    )

    assert result.kind == "hard_failure"
    assert result.category == "invalid_params"
    assert "password" in result.observed
    assert result.step_id is None

    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1
    written = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))
    assert written["category"] == "invalid_params"


# ------------------------------------------------------------------------------------------
# Code-review round: a pii-marked Type becomes a declared pii InputParam (FIX 2), a param the
# recorder itself labels sensitive never carries the observed literal as its example (FIX 3), and
# checkpoint placeholder interpolation is a single regex pass, not a prefix-unsafe replace chain
# (FIX 4).
# ------------------------------------------------------------------------------------------


def test_pii_field_becomes_a_declared_pii_param_and_its_literal_is_absent(tmp_path: Path) -> None:
    # Deliberately not SSN/credential-shaped: this must survive R1/R2/R3 on its own, so the test
    # proves the NEW pii-placeholder mechanism is what keeps it out, not an incidental pattern
    # match by an unrelated redaction rule.
    real_pii_value = "jane-q-doe"
    logger = EvidenceLogger("pii-synth", "test", base_dir=tmp_path)
    policy = _fake_policy()
    gate = PolicyGate(policy, logger, mode="discovery")
    surface = _MinimalSurface()

    gate.dispatch(
        surface, Navigate(url="http://fake/a"), context={"tool": "navigate", "rationale": "open"}
    )
    ssn_element = UIElement(node_id="0", role="textbox", name="SSN", sensitivity="pii")
    gate.dispatch(
        surface,
        Type(node_id="0", text=real_pii_value),
        context={
            "tool": "type",
            "rationale": "type the ssn",
            "target": {"role": "textbox", "name": "SSN"},
        },
        element=ssn_element,
    )
    logger.event(
        "goal_verified",
        phase="verify",
        rationale="done",
        checkpoint_eval={"kind": "text_present", "target": "page", "value": "DONE"},
    )

    capability = build_capability(
        run_dir=logger.dir,
        goal="verify identity",
        target="http://fake/a",
        run_id="pii-synth",
        model="fake-model",
        capability_id="verify-identity",
        policy=policy,
    )

    pii_params = [param for param in capability.inputs if param.sensitivity == "pii"]
    assert pii_params and pii_params[0].name == "ssn"
    assert pii_params[0].example is None
    assert any(
        isinstance(step.value, ParamRef) and step.value.name == "ssn" for step in capability.steps
    )

    serialized = Redactor().dumps(capability)
    assert real_pii_value not in serialized
    run_jsonl_text = (logger.dir / "run.jsonl").read_text(encoding="utf-8")
    assert real_pii_value not in run_jsonl_text


def test_pii_classified_goal_literal_param_has_no_example(tmp_path: Path) -> None:
    """A parameter the recorder itself classifies as pii via `classify_field_sensitivity` (not
    via a pre-redacted placeholder -- this is the OTHER parameterization path, goal-literal
    matching) must never carry the observed literal in `example` either."""
    logger = EvidenceLogger("pii-goal-literal", "test", base_dir=tmp_path)
    policy = _fake_policy()
    gate = PolicyGate(policy, logger, mode="discovery")
    surface = _MinimalSurface()

    gate.dispatch(
        surface, Navigate(url="http://fake/a"), context={"tool": "navigate", "rationale": "open"}
    )
    # sensitivity="none" at perception (not policy-flagged live) -- the recorder's OWN
    # classify_field_sensitivity, applied to the derived param name "ssn", is what must catch this.
    # "1234" (not 9+ digits) deliberately avoids R2's own account-number pattern, so the value
    # reaches the recorder as a literal instead of being masked before it ever gets there.
    ssn_element = UIElement(node_id="0", role="textbox", name="SSN")
    gate.dispatch(
        surface,
        Type(node_id="0", text="1234"),
        context={
            "tool": "type",
            "rationale": "type the ssn 1234",
            "target": {"role": "textbox", "name": "SSN"},
        },
        element=ssn_element,
    )
    logger.event(
        "goal_verified",
        phase="verify",
        rationale="done",
        checkpoint_eval={"kind": "text_present", "target": "page", "value": "DONE"},
    )

    capability = build_capability(
        run_dir=logger.dir,
        goal="verify ssn 1234 on file",
        target="http://fake/a",
        run_id="pii-goal-literal",
        model="fake-model",
        capability_id="verify-ssn",
        policy=policy,
    )

    ssn_params = [param for param in capability.inputs if param.name == "ssn"]
    assert ssn_params
    assert ssn_params[0].sensitivity == "pii"
    assert ssn_params[0].example is None


def test_resolve_checkpoint_handles_colliding_name_prefixes_in_one_pass() -> None:
    """":id" is a prefix of ":id_long"; an ordered sequence of str.replace calls would corrupt
    ":id_long" if "id" happened to be substituted first. One regex pass, matching the full
    identifier greedily, must resolve both correctly regardless of declaration order."""
    capability = Capability(
        capability_id="collide",
        name="n",
        description="d",
        target=TargetApp(app_id="a", entry_point="http://fake/a"),
        inputs=[
            InputParam(name="id", type="string", required=True),
            InputParam(name="id_long", type="string", required=True),
        ],
        steps=[],
        success=Checkpoint(kind="text_present", target="page", value="x"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )
    checkpoint = Checkpoint(
        kind="url_matches", target="any_frame", value="http://fake/:id/:id_long/x"
    )

    resolved = _resolve_checkpoint(checkpoint, capability, {"id": "1", "id_long": "999"})

    assert resolved.value == "http://fake/1/999/x"
