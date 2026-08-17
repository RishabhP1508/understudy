"""The Surface protocol: observe() and act(). Nothing else touches a live UI.

Recorded flows name normalized roles and accessible names, never CSS or a provider API, so the
artifact is surface-agnostic by construction (ARCHITECTURE.md decision 2). `act` may only ever
be called from `PolicyGate.dispatch` -- tests/test_constraints.py enforces that by walking the
AST of every file under src/.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from understudy.models.observation import STRUCTURAL_EXTRA, VALUE_CARRYING_EXTRA, Observation


class Navigate(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


class Click(BaseModel):
    kind: Literal["click"] = "click"
    node_id: str


class Type(BaseModel):
    kind: Literal["type"] = "type"
    node_id: str
    # D4 (Phase 8): what was actually typed, subject to R3's whole-string credential-shaped-
    # literal rule -- marked explicitly (VALUE_CARRYING is also the default for an unmarked
    # field, but this documents the intent rather than relying on a reader knowing that).
    text: str = Field(json_schema_extra=VALUE_CARRYING_EXTRA)


class Select(BaseModel):
    kind: Literal["select"] = "select"
    node_id: str
    value: str


class Key(BaseModel):
    kind: Literal["key"] = "key"
    key: str


class ReadText(BaseModel):
    kind: Literal["read_text"] = "read_text"
    node_id: str


Action = Annotated[
    Navigate | Click | Type | Select | Key | ReadText, Field(discriminator="kind")
]


class Surface(Protocol):
    """Perception and action, nothing else. No CSS, no raw HTML, no direct browser handle."""

    @property
    def url(self) -> str: ...

    def observe(self) -> Observation: ...

    def act(self, action: Action) -> str | None: ...

    def urls(self) -> list[str]:
        """Every URL currently loaded: the top-level document plus every child frame. A
        frameset's top-level document frequently never navigates at all
        (docs/adr/0005-child-frame-navigation-wait.md), so `.url` alone can describe a shell that
        the element an action targets has nothing to do with -- `PolicyGate.dispatch` reads this,
        not `.url`, for its allowlist and mutating-route checks on non-navigate actions
        (docs/adr/0007's update). Callers treat this as an OPTIONAL capability (read via
        `getattr(surface, "urls", None)`, the same pattern already used for `dialog_events`,
        `screenshot_bytes`, `fill_bounds`, `tracing`, and `dom_snapshot`), so a minimal test
        double that only implements `url` keeps working unmodified.
        """
        ...
