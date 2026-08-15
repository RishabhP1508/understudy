"""PolicyGate: the one choke point every action passes through, in discovery and in replay.

Phase 2 stub: no allowlist or risk enforcement yet (Phase 5 adds both). What is already real:
this is genuinely the only call site for Surface.act anywhere in the codebase --
tests/test_constraints.py (invariant 2) enforces that by walking the AST of every file under
src/ -- and every dispatch is logged with its rationale before it runs.
"""

from __future__ import annotations

from typing import Any

from understudy.evidence.logger import EvidenceLogger
from understudy.surface.base import Action, Surface


class PolicyGate:
    def __init__(self, logger: EvidenceLogger | None = None) -> None:
        self._logger = logger

    def dispatch(
        self, surface: Surface, action: Action, context: dict[str, Any] | None = None
    ) -> str | None:
        if self._logger is not None:
            self._logger.event("dispatch", action=action.model_dump(), context=context or {})
        return surface.act(action)
