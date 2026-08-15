"""PolicyGate: the one choke point every action passes through, in discovery and in replay.

`PolicyGate.dispatch` is genuinely the only call site for `Surface.act` anywhere in the
codebase -- tests/test_constraints.py (invariant 2) enforces that by walking the AST of every
file under src/ -- and every call, allowed or refused, is logged with its decision before it runs
(or before it is refused).

Checks run in a fixed order and the first to refuse wins: pending navigation violations, then the
origin+route allowlist, then the action type, then the target's role, then forbidden text
patterns, then risk. `classify()` (safety/risk.py) is computed up front so its reason travels with
every logged decision, allow or deny, not only the ones it itself refuses.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from understudy.evidence.logger import EvidenceLogger
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


class PolicyDecision(BaseModel):
    allowed: bool
    rule: str
    reason: str
    risk: str
    risk_reason: str
    action_kind: str
    url: str | None
    role: str | None


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
    ) -> None:
        self._policy = policy
        self._logger = logger
        self._mode = mode
        self._allow_risky = allow_risky
        self._capability_status = capability_status

    def dispatch(
        self,
        surface: Surface,
        action: Action,
        context: dict[str, Any] | None = None,
        element: UIElement | None = None,
    ) -> str | None:
        context = context or {}
        policy = self._policy

        # 1. Pending navigation violations only (surface.navigation_violations), not a
        # synchronous surface.url check -- a freshly launched WebSurface's page starts at
        # about:blank, which is not itself a violation, only the CURRENT page never having left
        # the allowlist (checked again, this time including surface.url, in the finally below).
        self._raise_if_navigation_violated(surface, include_current_url=False)

        current_url = action.url if isinstance(action, Navigate) else surface.url
        role = element.role if element is not None else None
        risk, risk_reason = classify(action, element, policy, url=current_url)

        # 2. origin + route allowlist
        if not policy.allows_url(current_url):
            decision = PolicyDecision(
                allowed=False,
                rule="allowlist",
                reason=(
                    f"{current_url!r} is not within an allowed origin+route "
                    f"(allowed_origins={policy.allowed_origins}, "
                    f"allowed_routes={policy.allowed_routes})"
                ),
                risk=risk.value,
                risk_reason=risk_reason,
                action_kind=action.kind,
                url=current_url,
                role=role,
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
                    )
                    self._log(action, context, decision, element)
                    raise PolicyDenied(decision)

        # 6. risk
        if risk == RiskClass.RISKY_IRREVERSIBLE:
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
                )
                self._log(action, context, decision, element)
                raise PolicyDenied(decision)

        # 7. allowed
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
                self._logger.redactor.register_secret(action.text)
                action_dict["text"] = "[REDACTED]"
        # `type="act"`: evidence/logger.py's RunEvent requires a real rationale on every event of
        # this type (R5). `rationale` and `step_id` are promoted to their own named fields;
        # `context` (tool name, resolved target descriptor, output name) rides along as-is for
        # record/recorder.py, which still needs all three to rebuild a Step.
        self._logger.event(
            "act",
            phase="act",
            step_id=context.get("step_id"),
            proposed_action=action_dict,
            rationale=context.get("rationale"),
            policy_decision=decision.model_dump(),
            dispatched=decision.allowed,
            act_result=act_result,
            context=context,
        )
