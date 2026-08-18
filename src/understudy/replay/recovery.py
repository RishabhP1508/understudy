"""replay/recovery.py: recoverable conditions -- the middle tier between a legitimate business
outcome (replay/outcomes.py, evaluated first) and a hard failure (evaluated last, replay/engine.py).

A `RecoveryRule.trigger` names a registered, pure Condition (`TRIGGERS`), never free prose, for the
same reason a `KnownOutcome.detector` does (see outcomes.py's module docstring): both the recorder
(which decides whether a rule is even earnable/executable, models/artifact.py's
`login_prefix_len`) and the engine (which fires it at replay time) have to agree on exactly what a
named condition means, and a string a reader has to interpret cannot be resolved or validated.
`validate(capability)` reuses `outcomes.UnknownDetector` rather than defining a second exception for
the identical failure shape (an artifact naming a condition this build does not know).

The five recovery ACTIONS are executed here, never in replay/engine.py directly, but every actual
dispatch still goes through `PolicyGate.dispatch` (invariant 2) -- recovery proposes a click same as
any recorded step, it never touches a Surface directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from understudy.models.artifact import Capability, RecoveryRule
from understudy.models.observation import Observation
from understudy.models.result import FailureCategory
from understudy.replay.outcomes import UnknownDetector, scan_text
from understudy.safety.policy import PolicyGate
from understudy.surface.base import Click, Surface
from understudy.surface.locator import TargetDescriptor, resolve
from understudy.surface.web import WebSurface

# Real exponential backoff for the `retry` action: base * 2**(attempt-1), so attempt 1/2/3/4 is
# 250/500/1000/2000ms. A module constant, not a magic number buried in the formula, so
# record/recorder.py's own docstring and this module's tests can both point at the same value.
_BACKOFF_BASE_MS = 250

# How long `wait` will wait for an in-flight navigation to settle before giving up. The fixture's
# own `slow_load` injection sleeps 6s server-side (fixtures/legacy_bank/app.py); this is set well
# above that with headroom, not tuned to the exact figure.
_WAIT_TIMEOUT_MS = 8000


class TriggerContext(BaseModel):
    """Everything a Trigger needs to decide whether its condition holds, gathered once per
    evaluation by replay/engine.py. Frozen: a trigger is a pure predicate over a snapshot, never a
    place to accumulate mutable state."""

    model_config = {"frozen": True}

    observation: Observation
    step_index: int
    login_prefix_len: int
    last_navigation: str
    new_dialogs: list[dict[str, Any]]


Trigger = Callable[[TriggerContext], str | None]


def _native_dialog_appeared(ctx: TriggerContext) -> str | None:
    if not ctx.new_dialogs:
        return None
    dialog = ctx.new_dialogs[0]
    return f"{dialog.get('dialog_type', 'dialog')}: {dialog.get('message', '')}"


def _native_dialog_unhandled(ctx: TriggerContext) -> str | None:
    for dialog in ctx.new_dialogs:
        if dialog.get("handled") == "none":
            return f"{dialog.get('dialog_type', 'dialog')}: {dialog.get('message', '')} (unhandled)"
    return None


def _html_interstitial_present(ctx: TriggerContext) -> str | None:
    return scan_text(ctx.observation, ("A confirmation is required before continuing",))


def _transient_error_page(ctx: TriggerContext) -> str | None:
    return scan_text(ctx.observation, ("Service temporarily unavailable",))


def _session_lost_mid_flow(ctx: TriggerContext) -> str | None:
    """Fires once the login form is showing again at or after the LAST step of the recorded login
    prefix (index `login_prefix_len - 1`, the login click itself) -- not merely after it. A
    trigger is evaluated AFTER a step's own action has already run, and the login click's own
    action IS what should leave the login page; landing back on it once that click has run is
    precisely a session loss, not merely a candidate for one. The previous `step_index <
    login_prefix_len` guard stayed silent through the whole prefix INCLUDING that final step, so
    the earliest a real session loss at login time could ever be caught was one step later than
    it actually happened -- measured live: the fixture's own `session_expired` injection kills the
    session on exactly that request, and with the old guard this trigger never fired at all, so
    the run died as POSTCONDITION_FAILED with no reauth ever attempted.

    Accepted ambiguity, honestly: a genuinely wrong password also lands back on the login form at
    this same step, and this trigger cannot tell the two apart from the rendered screen alone --
    it will be read as a session loss, reauth will be attempted once, and (since re-typing the
    same wrong password fails identically) the run will end SESSION_EXPIRED rather than a
    password-specific category. That is an honest limit of judging state from the rendered screen,
    not a bug to hide.
    """
    if ctx.login_prefix_len <= 0:
        # No known login boundary for this flow (either it genuinely has none, or -- the real
        # case measured live -- a legacy artifact recorded before postconditions existed has no
        # data to compute one from, models/artifact.py's login_prefix_len returns 0). With no
        # boundary there is nothing to compare `step_index` against: treating EVERY step as
        # "already past login" would misfire on step 0 of the login itself, which legitimately
        # types into a login form while ON the login page for a completely ordinary reason.
        return None
    if ctx.step_index < ctx.login_prefix_len - 1:
        return None
    has_login_button = any(
        e.role == "button" and e.name == "Login" for e in ctx.observation.elements
    )
    if not has_login_button:
        return None
    return "the observation shows the login form (a 'Login' button) mid-flow"


def _navigation_still_in_flight(ctx: TriggerContext) -> str | None:
    if ctx.last_navigation != "in_flight":
        return None
    return "the surface's own last_navigation is still 'in_flight'"


def _app_error_page(ctx: TriggerContext) -> str | None:
    return scan_text(ctx.observation, ("An unexpected error occurred",))


TRIGGERS: dict[str, Trigger] = {
    "native_dialog_appeared": _native_dialog_appeared,
    "native_dialog_unhandled": _native_dialog_unhandled,
    "html_interstitial_present": _html_interstitial_present,
    "transient_error_page": _transient_error_page,
    "session_lost_mid_flow": _session_lost_mid_flow,
    "navigation_still_in_flight": _navigation_still_in_flight,
    "app_error_page": _app_error_page,
}


def resolve_trigger(name: str) -> Trigger:
    try:
        return TRIGGERS[name]
    except KeyError:
        raise UnknownDetector(
            f"unknown recovery trigger {name!r}; known triggers: {sorted(TRIGGERS)}"
        ) from None


def validate(capability: Capability) -> None:
    """Resolve every `recovery_rules[].trigger`, or raise UnknownDetector (outcomes.py's exception,
    reused rather than duplicated -- see module docstring)."""
    for rule in capability.recovery_rules:
        resolve_trigger(rule.trigger)


def backoff_ms_for_attempt(attempt: int) -> int:
    """The exact backoff `_apply_retry` uses, exposed so replay/engine.py can log the SAME number
    it actually waited, rather than recomputing (and risking disagreeing with) the formula."""
    return _BACKOFF_BASE_MS * (2 ** (attempt - 1))


def _apply_dismiss_dialog(ctx: TriggerContext) -> str:
    """A NATIVE browser dialog (window.confirm/alert/prompt) is answered by Playwright's own
    `page.on("dialog")` handler, installed by replay/engine.py before the first navigate -- a
    DIFFERENT MECHANISM from clicking a DOM control, which is exactly why `dismiss_dialog` is a
    separate action from `dismiss` rather than the same action reused. By the time this runs, the
    dialog has ALREADY been answered (or left open, if the run's dialog budget was already spent);
    there is nothing left to click. This executor's whole job is to REPORT which of
    `ctx.new_dialogs` actually got dismissed, so the evidence log still explains what happened
    even though the real decision was made synchronously, at the moment the dialog fired.
    """
    dismissed = [d for d in ctx.new_dialogs if d.get("handled") == "dismiss"]
    if not dismissed:
        return "no native dialog in this batch was actually dismissed (budget already spent)"
    described = "; ".join(f"{d.get('dialog_type')}: {d.get('message')}" for d in dismissed)
    return f"the browser's own handler dismissed {len(dismissed)} native dialog(s): {described}"


def _apply_dismiss(ctx: TriggerContext, *, surface: Surface, gate: PolicyGate, rule_id: str) -> str:
    # ordinal=0 is deliberate: this is a descriptor WE ARE CONSTRUCTING right now, with an
    # explicit positional strategy chosen by this code -- never a recorded, possibly-ambiguous
    # descriptor being silently resolved to its first match (docs/adr/0006's rule is about
    # resolve() never doing that for a RECORDED target; here there is no recording at all, only a
    # live decision about which "Dismiss" link to click). The frameset can render more than one
    # "Dismiss" link at once (the nav frame and the content frame can each show their own copy of
    # the interstitial), which is why this rule's max_attempts is > 1 in record/recorder.py's seed.
    descriptor = TargetDescriptor(role="link", name="Dismiss", ordinal=0)
    resolution = resolve(descriptor, ctx.observation)
    if resolution.element is None:
        raise RuntimeError(
            f"recovery rule {rule_id!r} fired (html_interstitial_present) but no 'Dismiss' link "
            "resolved against the current observation"
        )
    gate.dispatch(
        surface,
        Click(node_id=resolution.element.node_id),
        context={
            "tool": "click",
            "rationale": (
                f"recovery rule {rule_id!r}: click 'Dismiss' to clear the HTML interstitial "
                "blocking the recorded flow"
            ),
        },
        element=resolution.element,
    )
    return f"clicked the 'Dismiss' link on the HTML interstitial (recovery rule {rule_id!r})"


def _apply_retry(surface: WebSurface, attempt: int, rule_id: str) -> str:
    """Typed as `WebSurface`, not the `Surface` protocol: `pause`/`reload` are WebSurface-specific,
    and the engine only ever constructs a WebSurface, so mypy is what enforces this requirement
    now. A surface that genuinely cannot retry should fail loudly at the call site (an
    AttributeError), not have this function report a fabricated "nothing was retried" success into
    the evidence log for work it never did (measured: that string was previously logged as a
    `recovered` event, so a recovery that did nothing at all read, in run.jsonl, as one that
    happened).

    ponytail: `reload()` is a PAGE-WIDE reload, which discards any unsubmitted form state on the
    page (measured, row 9: a lingering transient failure elsewhere on the page reloaded away a
    value a step had just typed, before the fixture's own counter was fixed to be per-session).
    The ceiling this hits is a flow that needs to type across a transient failure mid-form; the
    upgrade path is a FRAME-scoped reload (reload only the frame that actually failed), which
    would preserve sibling frames' state but needs the Surface protocol to expose "reload this
    frame", not just "reload the page" -- not built because no recorded flow in this project
    needs it yet, not because it is hard.
    """
    backoff_ms = backoff_ms_for_attempt(attempt)
    surface.pause(backoff_ms)
    surface.reload()
    return f"retried after a {backoff_ms}ms backoff (attempt {attempt}, recovery rule {rule_id!r})"


def _apply_wait(surface: WebSurface, rule_id: str) -> str:
    settled = surface.wait_for_navigation_to_settle(_WAIT_TIMEOUT_MS)
    return (
        f"waited up to {_WAIT_TIMEOUT_MS}ms for the in-flight navigation to settle "
        f"(recovery rule {rule_id!r}); settled={settled}"
    )


def _apply_reauth(reauth: Callable[[], str], rule_id: str) -> str:
    report = reauth()
    return f"re-authenticated via the recorded login steps (recovery rule {rule_id!r}): {report}"


def apply(
    rule: RecoveryRule,
    ctx: TriggerContext,
    *,
    surface: WebSurface,
    gate: PolicyGate,
    attempt: int,
    reauth: Callable[[], str],
) -> str:
    """Execute one recovery attempt for `rule` and return a short, human-readable result string,
    which replay/engine.py logs as the run's own `recovered` event."""
    if rule.action == "dismiss_dialog":
        return _apply_dismiss_dialog(ctx)
    if rule.action == "dismiss":
        return _apply_dismiss(ctx, surface=surface, gate=gate, rule_id=rule.id)
    if rule.action == "retry":
        return _apply_retry(surface, attempt, rule.id)
    if rule.action == "wait":
        return _apply_wait(surface, rule.id)
    if rule.action == "reauth":
        return _apply_reauth(reauth, rule.id)
    raise ValueError(f"unsupported recovery action: {rule.action!r}")


def match(capability: Capability, ctx: TriggerContext) -> tuple[RecoveryRule, str] | None:
    """The first declared rule (in `capability.recovery_rules` order) whose trigger fires,
    together with the reason it fired. Budget accounting (per-rule-per-step, plus the run-level
    cap) is replay/engine.py's job, not this function's -- `match` only ever answers "does a rule
    apply right now", never "is there budget left to apply it"."""
    for rule in capability.recovery_rules:
        trigger = resolve_trigger(rule.trigger)
        reason = trigger(ctx)
        if reason is not None:
            return rule, reason
    return None


# The ordered mapping replay/engine.py consults once no declared recovery rule applies (or its
# budget is exhausted): does this context match one of a small set of conditions serious enough to
# classify the resulting hard failure precisely?
#
# `transient_error_page` is deliberately NOT in this mapping. A 503 with no retry budget left is
# still an ordinary app-rendered error page; `app_error_page` does NOT catch it either, because its
# own trigger scans for different literal text ("An unexpected error occurred", the fixture's
# app_error injection) than a transient failure renders ("Service temporarily unavailable") -- the
# two are genuinely different real messages for two different injected conditions, not two names
# for the same thing. Nothing needs to be done to keep them apart; the fallthrough (whatever
# category the step's own postcondition/checkpoint check already reports) is the honest answer,
# since there is no more specific name for "a transient failure survived past its retry budget" in
# the ten-category taxonomy (docs/adr/0009) than the ordinary failure the step already produces.
_UNRECOVERED_ORDER: tuple[tuple[str, FailureCategory], ...] = (
    ("native_dialog_unhandled", FailureCategory.UNHANDLED_DIALOG),
    ("session_lost_mid_flow", FailureCategory.SESSION_EXPIRED),
    ("app_error_page", FailureCategory.APP_ERROR),
)


def unrecovered(ctx: TriggerContext) -> tuple[FailureCategory, str] | None:
    """`(category, reason)` for the first of the ordered conditions above that holds, or None."""
    for name, category in _UNRECOVERED_ORDER:
        trigger = resolve_trigger(name)
        reason = trigger(ctx)
        if reason is not None:
            return category, reason
    return None
