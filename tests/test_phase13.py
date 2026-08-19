"""Phase 13 task A: two small fixes to what gets WRITTEN into evidence, before any evidence is
regenerated (task B).

Task A1: a human's password keystrokes were captured in plain text by `WebSurface`'s human-action
capture (surface/web.py's `_HUMAN_ACTION_CAPTURE_SCRIPT` / `drain_human_actions`). Measured on the
Phase 10 handoff record, which held the operator's password as its own typing sequence: "h", "hu",
"hun", and so on to the whole word, plus the change event. Redaction never had a chance to catch
them: R1 (`Redactor.register_secret`) needs a value a caller DECLARED as a capability parameter, and
a value a human typed during a live handoff was never that; R3 (`_is_credential_shaped_literal`)
needs a credential-shaped literal, and an ordinary word typed into a password box carries no
credential token. Masking after the fact would also be useless -- the final value could be redacted,
but every keystroke-by-keystroke PREFIX would still sit in the record and reconstruct it. The fix is
to never capture the value at the source, in two layers: the injected script itself (for a raw
`input type="password"`) and `WebSurface.drain_human_actions`'s own second layer (for a sensitive
field that is not `type="password"` at all, keyed off the SAME `classify_field_sensitivity`
(safety/redact.py) the rest of this codebase already uses -- never a second keyword list).

These two tests are the regression guard, and each was verified to fail with its own layer's line
removed. The handoff record was re-recorded against the fix in Phase 13
(`evidence/escalation/escc59ee3e456.json`): its eight password-field entries now read
`[SUPPRESSED]`, while the username entries beside them still carry the operator's real keystrokes,
which is what distinguishes suppressing one field from wiping the record.

Task A2: a replay that ends on a business outcome or a hard failure returns from inside the step
loop, so its own run.jsonl ended on THAT event -- looking truncated to a reviewer even though
`result.json` was always written. `replay/engine.py` now logs one `replay_end` terminal marker,
carrying the result's own `kind`, through `_finish_replay` -- the one function every one of
`replay_resolved`'s three finalization sites (the two caller-error returns before a browser ever
launches, and the main run's own `finally`) calls, rather than a marker written by hand at each of
the dozen `return` statements a step, the entry navigate, or an escalation can take. The
pre-existing verify-phase event (what the success checkpoint evaluated to -- a different fact than
"the run ended") is renamed `checkpoint_eval` to keep the two distinguishable.

Offline tests need no browser, no network, no API key. LIVE tests drive the real fixture app and
skip loudly if it is not reachable on 127.0.0.1:5055 -- the same convention every other phase's
live tests use.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from understudy.models.artifact import Capability, Checkpoint, InputParam, Provenance, TargetApp
from understudy.models.observation import Observation, UIElement
from understudy.models.result import FailureCategory
from understudy.replay import engine as replay_engine
from understudy.safety.policy import PolicyGate, load_policy
from understudy.surface.base import Navigate, Type
from understudy.surface.web import _HUMAN_ACTION_SUPPRESSED, WebSurface

POLICY_PATH = Path(__file__).parent.parent / "policies" / "legacy_bank.yaml"


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


def _find(observation: Observation, role: str, name: str) -> UIElement:
    for element in observation.elements:
        if element.role == role and element.name == name:
            return element
    raise AssertionError(
        f"no element with role={role!r} name={name!r}; got: "
        f"{[(e.role, e.name) for e in observation.elements]}"
    )


def _run_dir(base_dir: Path) -> Path:
    run_dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith("replay-")]
    assert len(run_dirs) == 1, run_dirs
    return run_dirs[0]


def _events(run_dir: Path) -> list[dict]:
    text = (run_dir / "run.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ================================================================================================
# TASK A1: a human's password is never captured, at the source. The defect and the fix both live
# inside real browser JS execution (a raw `input type="password"` DOM event), so this is
# LIVE-only, the same way surface/web.py's own existing human-action-capture tests are
# (test_phase10.py).
# ================================================================================================


def test_live_human_password_keystrokes_are_never_captured_in_plain_text() -> None:
    _skip_if_fixture_unreachable()

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    typed_username = "distinctive-real-name"
    typed_password = "N3wSecr3t-Distinctive!42"

    gate = PolicyGate(policy, mode="discovery")
    try:
        gate.dispatch(surface, Navigate(url=policy.entry_point))  # -> /login

        obs = surface.observe()
        username = _find(obs, "textbox", "Username")
        gate.dispatch(
            surface, Type(node_id=username.node_id, text=typed_username), element=username
        )

        # The password field, driven DIRECTLY through Playwright's own per-character typing (not
        # WebSurface.act's single-shot .fill(), which the line above uses for the username field)
        # -- this reproduces the actual defect: a real human typing character by character, which
        # is what produced the keystroke-by-keystroke record ("h", "hu", "hun", ...) in the Phase 10
        # handoff, since re-recorded against this fix. This does not call Surface.act at all (it
        # never touches WebSurface's own methods), so it does not run through PolicyGate.dispatch
        # -- exactly like a real human's own click and keystrokes, which Playwright only ever
        # observes, never dispatches (surface/web.py's `drain_human_actions`, "FINDING 2").
        surface._page.locator("input[name='f2']").press_sequentially(typed_password)  # noqa: SLF001

        actions = surface.drain_human_actions()
    finally:
        surface.close()

    # (a) no drained action's value is the full password OR any keystroke-by-keystroke PREFIX a
    # per-character capture would have produced -- the exact shape of the original defect, not
    # just a check against the whole string.
    prefixes = {typed_password[: i + 1] for i in range(len(typed_password))}
    captured_values = {action.value for action in actions if action.value is not None}
    leaked = prefixes & captured_values
    assert not leaked, f"password prefixes leaked into captured human actions: {leaked}"

    # (b) the password field's own input events ARE present, carrying the sentinel -- the ACTION
    # is still recorded (R6: know what the human did), only the value is suppressed. The
    # captured `name` is this fixture's raw HTML `name` attribute ("f2"), not the row-label name
    # ("Password") the AGENT's own a11y-based perception derives -- `bestName()` in the injected
    # script reads `el.getAttribute("name")` directly, the same fact this module's own
    # `drain_human_actions` docstring already measures for "f7".
    password_events = [
        action for action in actions if action.name == "f2" and action.kind == "input"
    ]
    assert password_events, "expected input events on the password field"
    assert all(action.value == _HUMAN_ACTION_SUPPRESSED for action in password_events)

    # (c) a NON-secret field on the same page still captures its real value -- this is
    # suppression of a sensitive field, never a blanket wipe of every captured value.
    username_events = [
        action for action in actions if action.name == "f1" and action.kind == "input"
    ]
    assert any(action.value == typed_username for action in username_events)


def test_live_human_action_capture_suppresses_a_sensitive_named_field_that_is_not_password() -> (
    None
):
    """Layer two of the A1 fix: `drain_human_actions`'s own `classify_field_sensitivity(name)`
    check catches a sensitive field whose raw `name` attribute reads as secret/pii even when its
    `type` is plain text, not "password" -- a case layer one (the injected script's own
    `el.type === "password"` check) cannot see at all. The legacy_bank fixture has no such field
    by name (its own inputs are opaquely named f1..f7), so this test injects one directly into the
    live page's own document, via `page.evaluate` (never `page.set_content`, which starts an
    about:blank-shaped document Chromium denies `sessionStorage` access from -- measured directly
    against this fixture). The injected listener is delegated at the `document` level
    (`_HUMAN_ACTION_CAPTURE_SCRIPT`'s own `addEventListener` calls), so it fires for these
    elements exactly as it would for any the app itself rendered.
    """
    _skip_if_fixture_unreachable()

    policy = load_policy(POLICY_PATH)
    try:
        surface = WebSurface(policy=policy, headless=False)
    except Exception as exc:  # noqa: BLE001 - reported as a skip reason, not a failure
        pytest.skip(f"could not launch a Playwright browser: {exc}")
        return

    typed_ssn = "distinctive-ssn-value"
    typed_notes = "distinctive-notes-value"

    try:
        surface.act(Navigate(url=policy.entry_point))  # -> /login, a real document
        surface._page.evaluate(  # noqa: SLF001
            "() => document.body.insertAdjacentHTML('beforeend', "
            '\'<input name="ssn" type="text" id="probe-ssn">'
            "<input name=\"notes\" type=\"text\" id=\"probe-notes\">')"
        )
        surface._page.locator("#probe-ssn").press_sequentially(typed_ssn)  # noqa: SLF001
        surface._page.locator("#probe-notes").press_sequentially(typed_notes)  # noqa: SLF001

        actions = surface.drain_human_actions()
    finally:
        surface.close()

    # classify_field_sensitivity("ssn") returns "pii" via _PII_PATTERNS -- this exercises layer
    # two specifically, since "ssn" is not type="password" and layer one never touches it.
    ssn_events = [action for action in actions if action.name == "ssn" and action.kind == "input"]
    assert ssn_events, "expected input events on the ssn field"
    assert all(action.value == _HUMAN_ACTION_SUPPRESSED for action in ssn_events)

    # A non-sensitive name on the same injected document still captures its real value.
    notes_events = [
        action for action in actions if action.name == "notes" and action.kind == "input"
    ]
    assert any(action.value == typed_notes for action in notes_events)


# ================================================================================================
# TASK A2: every replay ends its event stream with one `replay_end` terminal marker, on every
# return path, carrying the result's own `kind`.
# ================================================================================================


def test_replay_end_terminal_marker_fires_on_an_early_invalid_params_return(
    tmp_path: Path,
) -> None:
    """Before this phase, a run refused for INVALID_PARAMS (checked before any browser launches)
    wrote `result.json` directly at that return site but logged no run.jsonl event marking the
    run as OVER -- exactly the "looks truncated" defect this task fixes. This is its simplest
    reproduction: no browser, no fixture, nothing but the parameter check itself, which is also
    the one return path this phase's fix touches that the main run's own shared `finally` block
    never covered at all (that early return happens before a `WebSurface` even exists).
    """
    capability = Capability(
        capability_id="phase13-terminal-marker-invalid-params",
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
    artifact_path = tmp_path / "capability.json"
    artifact_path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")

    result = replay_engine.replay(artifact_path, {}, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "hard_failure"
    assert result.category == FailureCategory.INVALID_PARAMS

    events = _events(_run_dir(tmp_path))
    replay_end_events = [e for e in events if e["type"] == "replay_end"]
    assert len(replay_end_events) == 1
    assert replay_end_events[0]["kind"] == "hard_failure"
    assert events[-1] is replay_end_events[0]
    assert not any(e["type"] == "checkpoint_eval" for e in events)


def test_live_replay_end_terminal_marker_is_the_last_event_on_a_successful_run(
    tmp_path: Path,
) -> None:
    """A zero-step capability whose success checkpoint is already satisfied on its own entry
    point: the smallest live run that reaches the verify phase at all. Proves both halves of the
    A2 rename at once -- the renamed `checkpoint_eval` event still carries the verify-phase
    detail, and the NEW `replay_end` terminal marker is the actual last line of run.jsonl,
    carrying `kind="success"`.
    """
    _skip_if_fixture_unreachable()

    capability = Capability(
        capability_id="phase13-terminal-marker-success",
        name="n",
        description="d",
        target=TargetApp(app_id="legacy_bank", entry_point="http://127.0.0.1:5055/login"),
        inputs=[],
        steps=[],
        success=Checkpoint(kind="text_present", target="page", value="Username"),
        provenance=Provenance(
            run_id="r", model="m", timestamp="t", transcript_hash="0123456789abcdef" * 4
        ),
    )
    artifact_path = tmp_path / "capability.json"
    artifact_path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")

    result = replay_engine.replay(artifact_path, {}, POLICY_PATH, evidence_base_dir=tmp_path)
    assert result.kind == "success"

    events = _events(_run_dir(tmp_path))

    checkpoint_events = [e for e in events if e["type"] == "checkpoint_eval"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0]["phase"] == "verify"

    replay_end_events = [e for e in events if e["type"] == "replay_end"]
    assert len(replay_end_events) == 1
    assert replay_end_events[0]["kind"] == "success"
    assert events[-1] is replay_end_events[0]
