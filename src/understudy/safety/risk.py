"""Risk classification: reversible versus irreversible, so the irreversible class can be
handled conservatively at the gate (ARCHITECTURE.md decision 11).

Two independent layers catch an irreversible action: a label heuristic (does the element's own
name say what it does) and a route heuristic (is this URL path flagged as mutating in policy).
Neither is trusted alone. The label heuristic misses an unlabeled irreversible control -- this
fixture's own subaccount submit button is literally named "Submit", which matches no risky label
-- so the route rule exists as a second, independent layer behind it. See
docs/adr/0007-block-risky-actions-rather-than-confirm.md for the tradeoff this buys and the
false-positive cost it accepts (every click on a mutating route is treated as irreversible, not
just the one that actually submits).
"""

from __future__ import annotations

import re
from enum import StrEnum
from fnmatch import fnmatch
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from understudy.models.observation import UIElement
from understudy.surface.base import Action, Click, Navigate, ReadText, Select, Type

if TYPE_CHECKING:
    # Deferred to break the cycle: policy.py imports classify() from this module at runtime, so
    # this module cannot import policy.py at runtime too. `from __future__ import annotations`
    # means the `Policy` annotation below is never evaluated at import time, only by a type
    # checker, which does resolve this import.
    from understudy.safety.policy import Policy


class RiskClass(StrEnum):
    SAFE_REVERSIBLE = "SAFE_REVERSIBLE"
    RISKY_IRREVERSIBLE = "RISKY_IRREVERSIBLE"


def _path_is_mutating(url: str | None, policy: Policy) -> bool:
    if not url:
        return False
    path = urlsplit(url).path
    return any(fnmatch(path, pattern) for pattern in policy.mutating_routes)


def _matched_risky_label(name: str, policy: Policy) -> str | None:
    normalized = name.casefold()
    for label in policy.risky_labels:
        # "whole-word-ish": the label must appear as a separate word (or word sequence) inside
        # the name, not as a substring of a longer word -- "transfer" matches "Transfer Funds"
        # but must not match e.g. "Transferable".
        pattern = r"(?<!\w)" + re.escape(label.casefold()) + r"(?!\w)"
        if re.search(pattern, normalized):
            return label
    return None


def classify(
    action: Action, element: UIElement | None, policy: Policy, url: str | None = None
) -> tuple[RiskClass, str]:
    """Classify one proposed action. Every branch returns a non-empty, human-readable reason.

    `url` is a deliberate extension beyond the action and element alone: whether a Navigate or a
    Click lands on a route policy flags as `mutating_routes` is not decidable from the action
    object by itself, since Navigate.url is only sometimes the current page and a Click carries
    no URL at all -- the caller (PolicyGate) passes the surface's current URL for a click, and the
    action's own target URL for a navigate.
    """
    if isinstance(action, ReadText):
        return RiskClass.SAFE_REVERSIBLE, "reading state changes nothing"

    if isinstance(action, Navigate):
        if _path_is_mutating(action.url, policy):
            return (
                RiskClass.RISKY_IRREVERSIBLE,
                f"navigation target {urlsplit(action.url).path!r} matches a mutating_routes "
                "pattern",
            )
        return RiskClass.SAFE_REVERSIBLE, "navigation alone does not commit any state"

    if isinstance(action, Click):
        name = element.name if element is not None else ""
        matched_label = _matched_risky_label(name, policy) if name else None
        if matched_label is not None:
            source = element.name_source if element is not None else "none"
            return (
                RiskClass.RISKY_IRREVERSIBLE,
                f"element name {name!r} (name_source={source!r}) matches risky_labels "
                f"entry {matched_label!r}",
            )
        if _path_is_mutating(url, policy):
            return (
                RiskClass.RISKY_IRREVERSIBLE,
                f"the risky_labels heuristic did not match element name {name!r}, but the "
                f"current route {urlsplit(url).path if url else url!r} matches a "
                "mutating_routes pattern",
            )
        return (
            RiskClass.SAFE_REVERSIBLE,
            "element name matches no risky_labels entry and the current route is not flagged "
            "as mutating",
        )

    if isinstance(action, (Type, Select)):
        return RiskClass.SAFE_REVERSIBLE, "filling an unsubmitted form does not commit state"

    # ponytail: Key is classified SAFE_REVERSIBLE unconditionally. In reality a Key("Enter") can
    # submit a form and commit state exactly like a Click on a submit control, but no code path
    # in agent/loop.py or replay/engine.py currently builds or dispatches a Key action, so there
    # is no real case to get wrong yet. Revisit if a Key action is ever emitted.
    return RiskClass.SAFE_REVERSIBLE, "key actions are not currently emitted by any code path"
