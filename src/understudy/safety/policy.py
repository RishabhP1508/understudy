"""PolicyGate: the one choke point every action passes through, in discovery and in replay.

`PolicyGate.dispatch` is genuinely the only call site for `Surface.act` anywhere in the
codebase -- tests/test_constraints.py (invariant 2) enforces that by walking the AST of every
file under src/ -- and every call, allowed or refused, is logged with its decision before it runs
(or before it is refused).

Checks run in a fixed order and the first to refuse wins: the control token (Phase 10,
escalation/control.py -- only AUTOMATION may dispatch), then pending navigation violations, then
the origin+route allowlist, then the action type, then the target's role, then forbidden text
patterns, then risk. `classify()` (safety/risk.py) is computed up front so its reason travels with
every logged decision, allow or deny, not only the ones it itself refuses.

For a non-Navigate action, the allowlist and risk checks both read `Surface.urls()` (every URL
currently loaded, across every frame), not `surface.url` alone. A live discovery run found this
gate checking only a frameset's shell URL, which never navigates at all
(docs/adr/0005-child-frame-navigation-wait.md) -- so a form living in a content frame that had
actually navigated onto a `mutating_routes` pattern was invisible to this gate, and the fixture's
own subaccount "Submit" was dispatched as SAFE_REVERSIBLE. See docs/adr/0007's update.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from understudy.escalation.control import ControlHeld, SessionBroker
from understudy.evidence.logger import EvidenceLogger
from understudy.models.intervention import ReasonCode
from understudy.models.observation import UIElement
from understudy.safety.redact import slugify_param_name
from understudy.safety.risk import RiskClass, classify
from understudy.surface.base import Action, Navigate, Surface, Type


class Policy(BaseModel):
    version: int
    app_id: str
    entry_point: str
    allowed_origins: list[str]
    allowed_routes: list[str]
    allowed_actions: list[str]
    allowed_roles: list[str]
    risky_labels: list[str] = Field(default_factory=list)
    mutating_routes: list[str] = Field(default_factory=list)
    sensitive_fields: dict[str, list[str]] = Field(default_factory=dict)
    max_steps: int = 25
    max_wall_clock_seconds: float = 180
    max_action_retries: int = 2
    # Shared by agent/loop.py's three stall-style stopping conditions (no_progress,
    # loop_detected, dead_end): the same "how many times before we call it" question, not three
    # independently-tuned knobs. See docs/adr/0010.
    stall_limit: int = 3
    # How often a discovery turn gets an unconditional full render as a refresh, even when the
    # observation digest says a diff would otherwise be safe to send.
    full_render_every: int = 5
    forbidden_text_patterns: list[str] = Field(default_factory=list)

    def allows_url(self, url: str) -> bool:
        """Origin exact match, path fnmatch against `allowed_routes` (query string ignored).
        `WebSurface`'s navigation guard calls this exact method too, so there is one matcher, not
        a second copy that could drift from it.
        """
        split = urlsplit(url)
        origin = f"{split.scheme}://{split.netloc}"
        if origin not in self.allowed_origins:
            return False
        return any(fnmatch(split.path, pattern) for pattern in self.allowed_routes)


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Policy.model_validate(data)


def _loaded_urls(surface: Surface) -> list[str]:
    """Every URL this surface currently has loaded: `Surface.urls()` if the surface has one
    (`WebSurface`: the shell plus every child frame), else the single `surface.url` a minimal
    fake or `DesktopSurface` still provides -- the same "default it for fakes" pattern already
    used for `dialog_events`, `screenshot_bytes`, `fill_bounds`, `tracing`, and `dom_snapshot`.
    """
    urls_method = getattr(surface, "urls", None)
    if urls_method is None:
        return [surface.url]
    result: list[str] = urls_method()
    return result


class PolicyDecision(BaseModel):
    allowed: bool
    rule: str
    reason: str
    risk: str
    risk_reason: str
    action_kind: str
    url: str | None
    role: str | None
    # Every URL actually checked for this decision: [action.url] for a Navigate, or every URL
    # Surface.urls() reports currently loaded (every frame, not just the shell) for anything
    # else. `url` above stays a single readable value for logging/display; this is the full set
    # a reviewer can use to see what was actually checked (docs/adr/0007's update).
    checked_urls: list[str] = Field(default_factory=list)


def decision_context(decision: PolicyDecision) -> dict[str, str]:
    """Flat, already-plain-string detail for an escalation raised over a refusing
    `PolicyDecision` (F2, Phase 10 round F): the operator is being asked to authorize or reject
    one SPECIFIC refusal, and the gate's own words -- which rule, why, the risk class and its own
    reason, and the action kind -- are the best statement of what that refusal was. Shared by
    agent/loop.py and replay/engine.py so the two execution paths build this from the same five
    fields rather than each inventing its own subset.
    """
    return {
        "rule": decision.rule,
        "reason": decision.reason,
        "risk": decision.risk,
        "risk_reason": decision.risk_reason,
        "action_kind": decision.action_kind,
    }


# The two rules a refusing PolicyDecision can carry when the refusal is a RISKY_IRREVERSIBLE
# action pending human authorization: "risk_discovery" (dispatch above, mode="discovery") and
# "risk_replay" (dispatch above, mode="replay", capability not approved+allow_risky). Every other
# refusing rule (allowlist/action_type/role/forbidden_text/control_token) means the action is not
# permitted at all, in either mode.
_RISK_RULES = frozenset({"risk_discovery", "risk_replay"})


def reason_code_for_decision(decision: PolicyDecision) -> ReasonCode:
    """G1 (Phase 10 round G): the escalation reason code for a refusing `PolicyDecision`, derived
    from the decision's own `rule` -- NEVER from which exception type (`PolicyDenied` versus
    `EscalationRequired`) carried it here. Those two types are a discovery/replay CONTROL-FLOW
    detail (decision 41: discovery and replay want to handle a refusal differently, which is why
    there are two types at all), not a statement of WHY the action was refused -- replay's own
    risk-refusal rule (`risk_replay`) raises `PolicyDenied`, the exact same exception type replay
    raises for an allowlist/role/etc. refusal, so branching on exception type there produced the
    same reason code for two different conditions. Both execution paths call this one function so
    they cannot independently drift on what the same rule means.

    `risk_discovery`/`risk_replay` mean a human could authorize this ONE action --
    `RISKY_ACTION_REQUIRES_APPROVAL`, which is also the only reason code the operator console
    (escalation/operator_app.py) offers a per-action approve/reject decision for. Every other rule
    means the action is not permitted at all, and no per-action approval should ever be offered
    for it -- `POLICY_REFUSED`.
    """
    if decision.rule in _RISK_RULES:
        return ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL
    return ReasonCode.POLICY_REFUSED


class PolicyDenied(Exception):
    """Refused by the allowlist, action-type, role, forbidden-text, or (in replay, without an
    approved+allow_risky capability) risk check."""

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class EscalationRequired(Exception):
    """A RISKY_IRREVERSIBLE action was refused in discovery. Discovery never auto-approves an
    irreversible action; a human has to (Phase 10)."""

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class NavigationBlocked(Exception):
    """A browser-initiated navigation (a redirect, a meta-refresh, a clicked external link) left
    the allowlist. Aborts the run: this is not a refusal of one proposed action, it is evidence
    the session itself is no longer where the artifact or the agent believes it is."""

    def __init__(self, urls: list[str], message: str | None = None) -> None:
        self.urls = urls
        super().__init__(message or f"navigation left the allowlist: {urls}")


class PolicyGate:
    def __init__(
        self,
        policy: Policy,
        logger: EvidenceLogger | None = None,
        mode: Literal["discovery", "replay"] = "discovery",
        allow_risky: bool = False,
        capability_status: str | None = None,
        broker: SessionBroker | None = None,
    ) -> None:
        self._policy = policy
        self._logger = logger
        self._mode = mode
        self._allow_risky = allow_risky
        self._capability_status = capability_status
        self._broker = broker

    def dispatch(
        self,
        surface: Surface,
        action: Action,
        context: dict[str, Any] | None = None,
        element: UIElement | None = None,
    ) -> str | None:
        context = context or {}
        policy = self._policy

        # 0. control token (escalation/control.py's SessionBroker): only AUTOMATION may
        # dispatch. Checked before every other rule, including the pending-navigation check
        # right below, because a human holding the token (or a handoff in either transient
        # direction, PENDING_HANDOFF/PENDING_RESUME) is not a property of THIS action at all --
        # it is true regardless of what was proposed, so it must refuse before anything about
        # the action is even inspected.
        if self._broker is not None:
            try:
                self._broker.require_automation()
            except ControlHeld as exc:
                current_url = action.url if isinstance(action, Navigate) else surface.url
                role = element.role if element is not None else None
                decision = PolicyDecision(
                    allowed=False,
                    rule="control_token",
                    reason=(
                        f"control is held by {exc.holder!r} in state {exc.state.value!r}, not "
                        "AUTOMATION: the run is paused for a human handoff"
                    ),
                    risk="n/a",
                    risk_reason=(
                        "not classified: the control token refused the action before risk "
                        "classification ran"
                    ),
                    action_kind=action.kind,
                    url=current_url,
                    role=role,
                    checked_urls=[],
                )
                self._log(action, context, decision, element)
                raise PolicyDenied(decision) from exc

        # 1. Pending navigation violations only (surface.navigation_violations), not a
        # synchronous surface.url check -- a freshly launched WebSurface's page starts at
        # about:blank, which is not itself a violation, only the CURRENT page never having left
        # the allowlist (checked again, this time including surface.url, in the finally below).
        self._raise_if_navigation_violated(surface, include_current_url=False)

        current_url = action.url if isinstance(action, Navigate) else surface.url
        role = element.role if element is not None else None

        if isinstance(action, Navigate):
            # The destination has not loaded yet; there is exactly one URL to check, the target.
            checked_urls = [action.url]
        else:
            # Every URL actually loaded right now, across every frame -- not just surface.url,
            # which in a frameset app is frequently the shell and never the page an action's
            # element actually lives on (docs/adr/0005, docs/adr/0007's update). "about:blank" /
            # "" is the absence of a navigation (Phase 5's rule), never a route to check.
            checked_urls = [u for u in _loaded_urls(surface) if u not in ("about:blank", "")]

        risk, risk_reason = classify(
            action,
            element,
            policy,
            url=action.url if isinstance(action, Navigate) else checked_urls,
        )

        # 2. origin + route allowlist: EVERY currently loaded URL must be allowed.
        disallowed = [u for u in checked_urls if not policy.allows_url(u)]
        if disallowed:
            decision = PolicyDecision(
                allowed=False,
                rule="allowlist",
                reason=(
                    f"{disallowed!r} not within an allowed origin+route (checked "
                    f"{checked_urls!r}; allowed_origins={policy.allowed_origins}, "
                    f"allowed_routes={policy.allowed_routes})"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
                checked_urls=checked_urls,
            )
            self._log(action, context, decision, element)
            raise PolicyDenied(decision)

        # 3. action type
        if action.kind not in policy.allowed_actions:
            decision = PolicyDecision(
                allowed=False,
                rule="action_type",
                reason=(
                    f"action kind {action.kind!r} is not in allowed_actions="
                    f"{policy.allowed_actions}"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
                checked_urls=checked_urls,
            )
            self._log(action, context, decision, element)
            raise PolicyDenied(decision)

        # 4. target role -- skipped for read_text (reading is not an action on the application)
        # and when there is no element to check (e.g. Navigate, Key).
        if action.kind == "read_text":
            role_note = (
                "role check skipped for read_text: reading is not an action on the application"
            )
        elif element is None:
            role_note = f"role check skipped: no element resolved for action kind {action.kind!r}"
        elif element.role not in policy.allowed_roles:
            decision = PolicyDecision(
                allowed=False,
                rule="role",
                reason=(
                    f"element role {element.role!r} is not in allowed_roles={policy.allowed_roles}"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
                checked_urls=checked_urls,
            )
            self._log(action, context, decision, element)
            raise PolicyDenied(decision)
        else:
            role_note = f"element role {element.role!r} is in allowed_roles"

        # 5. forbidden text (Type actions only)
        if isinstance(action, Type):
            for pattern in policy.forbidden_text_patterns:
                if re.search(pattern, action.text):
                    decision = PolicyDecision(
                        allowed=False,
                        rule="forbidden_text",
                        reason=f"typed text matches forbidden_text_patterns entry {pattern!r}",
                        risk=risk.value,
                        risk_reason=risk_reason,
                        action_kind=action.kind,
                        url=current_url,
                        role=role,
                        checked_urls=checked_urls,
                    )
                    self._log(action, context, decision, element)
                    raise PolicyDenied(decision)

        # 6. risk. A one-shot intervention approval (SessionBroker.grant_approval /
        # consume_approval) is checked FIRST, before either mode-specific refusal below: a human
        # resolving an escalation with "approved" authorizes exactly the ONE risky dispatch that
        # raised it, in EITHER discovery or replay -- the same mechanism, not two. The approval
        # is looked up by the CURRENT control token's own intervention_id (set when the
        # transition that resolved it ran, escalation/control.py), never by a caller-supplied
        # flag: the whole value of this choke point is that a caller cannot argue its way past
        # it, only external state a human actually created can.
        approved_by_intervention: str | None = None
        if risk == RiskClass.RISKY_IRREVERSIBLE and self._broker is not None:
            pending_intervention_id = self._broker.state().intervention_id
            if pending_intervention_id is not None and self._broker.consume_approval(
                pending_intervention_id
            ):
                approved_by_intervention = pending_intervention_id

        if risk == RiskClass.RISKY_IRREVERSIBLE and approved_by_intervention is None:
            if self._mode == "discovery":
                decision = PolicyDecision(
                    allowed=False,
                    rule="risk_discovery",
                    reason=f"RISKY_IRREVERSIBLE refused in discovery mode: {risk_reason}",
                    risk=risk.value,
                    risk_reason=risk_reason,
                    action_kind=action.kind,
                    url=current_url,
                    role=role,
                    checked_urls=checked_urls,
                )
                self._log(action, context, decision, element)
                raise EscalationRequired(decision)
            approved_for_replay = self._capability_status == "approved" and self._allow_risky
            if not approved_for_replay:
                decision = PolicyDecision(
                    allowed=False,
                    rule="risk_replay",
                    reason=(
                        "RISKY_IRREVERSIBLE refused in replay: requires capability status "
                        f"'approved' and allow_risky=True (got capability_status="
                        f"{self._capability_status!r}, allow_risky={self._allow_risky}): "
                        f"{risk_reason}"
                    ),
                    risk=risk.value,
                    risk_reason=risk_reason,
                    action_kind=action.kind,
                    url=current_url,
                    role=role,
                    checked_urls=checked_urls,
                )
                self._log(action, context, decision, element)
                raise PolicyDenied(decision)

        # 7. allowed
        if approved_by_intervention is not None:
            decision = PolicyDecision(
                allowed=True,
                rule="risk_approved_by_intervention",
                reason=(
                    "RISKY_IRREVERSIBLE action allowed: one-shot approval granted by "
                    f"intervention {approved_by_intervention!r} ({role_note})"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
                checked_urls=checked_urls,
            )
        else:
            decision = PolicyDecision(
                allowed=True,
                rule="allowed",
                reason=(
                    "passed the allowlist, action-type, role, forbidden-text, and risk checks "
                    f"({role_note})"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
                checked_urls=checked_urls,
            )

        # Logged AFTER executing (not before, as the refusal branches above have to be), so the
        # one "act" event for a dispatched action also carries its own result -- R5's "what the
        # agent did" is stronger evidence once we know it actually happened. Logged in `except`
        # or `else`, never after the whole block, so the event is written even when the
        # `finally`'s own navigation check goes on to raise (the succeeded-but-then-violated
        # case): otherwise that path would leave this dispatch with no act event at all.
        try:
            result = surface.act(action)
        except Exception:
            self._log(action, context, decision, element, act_result=None)
            raise
        else:
            self._log(action, context, decision, element, act_result=result)
        finally:
            self._raise_if_navigation_violated(surface, include_current_url=True)
        return result

    def _raise_if_navigation_violated(self, surface: Surface, *, include_current_url: bool) -> None:
        # Recorded violations (from the guard listeners) raise regardless of surface.act's own
        # outcome, including when it is raising -- that is the real /external -> example.com
        # case, and dropping this check to fix the false positive below would silently drop that
        # too. Only the SYNCHRONOUS current-url check is conditional.
        violations = list(getattr(surface, "navigation_violations", []))
        if include_current_url:
            current_url = surface.url
            # "about:blank" (a fresh page) and "" are the ABSENCE of a navigation, never one off
            # the allowlist. Without this, a failed goto() to a target nobody is listening on
            # (net::ERR_CONNECTION_REFUSED) leaves the page at "about:blank", which fails
            # allows_url and replaces the real connection error with a fabricated
            # NavigationBlocked -- reporting a safety violation that never happened (measured
            # live against a dead port).
            already_flagged = current_url in violations
            is_absent = current_url in ("about:blank", "")
            if not is_absent and not self._policy.allows_url(current_url) and not already_flagged:
                violations.append(current_url)
        if violations:
            raise NavigationBlocked(violations)

    def _log(
        self,
        action: Action,
        context: dict[str, Any],
        decision: PolicyDecision,
        element: UIElement | None,
        act_result: str | None = None,
    ) -> None:
        if self._logger is None:
            return
        action_dict = action.model_dump()
        if isinstance(action, Type) and element is not None:
            if element.sensitivity == "secret":
                self._logger.redactor.register_secret(action.text)
                slug = slugify_param_name(element.name or "secret")
                action_dict["text"] = f"${{param:{slug}}}"
            elif element.sensitivity == "pii":
                # A parameter reference, not a bare "[REDACTED]" mask: record/recorder.py needs
                # something to bind a declared, replayable InputParam(sensitivity="pii") to,
                # exactly as it already does for a secret. A bare mask has no name in it for the
                # recorder to recover, so a pii-marked field used to become a hardcoded step value
                # replay could never reproduce -- the same defect class ParamRef already fixed for
                # secrets. The `${pii:...}` prefix (not `${param:...}`) is what lets the recorder
                # tell the two sensitivities apart from the placeholder text alone.
                self._logger.redactor.register_secret(action.text)
                slug = slugify_param_name(element.name or "pii")
                action_dict["text"] = f"${{pii:{slug}}}"
        # `type="act"`: evidence/logger.py's RunEvent requires a real rationale on every event of
        # this type (R5). `rationale`, `step_id`, and (D2, Phase 8) `observation_digest` are
        # promoted to their own named fields; `context` (tool name, resolved target descriptor,
        # output name) rides along as-is for record/recorder.py, which still needs all three to
        # rebuild a Step.
        self._logger.event(
            "act",
            phase="act",
            step_id=context.get("step_id"),
            observation_digest=context.get("observation_digest"),
            proposed_action=action_dict,
            rationale=context.get("rationale"),
            policy_decision=decision.model_dump(),
            dispatched=decision.allowed,
            act_result=act_result,
            context=context,
        )
