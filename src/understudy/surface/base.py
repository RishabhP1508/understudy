"""The Surface protocol: observe() and act(). Nothing else touches a live UI.

Recorded flows name normalized roles and accessible names, never CSS or a provider API, so the
artifact is surface-agnostic by construction (ARCHITECTURE.md decision 2). `act` may only ever
be called from `PolicyGate.dispatch` -- tests/test_constraints.py enforces that by walking the
AST of every file under src/.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from understudy.models.observation import Observation


class Navigate(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url: str


class Click(BaseModel):
    kind: Literal["click"] = "click"
    node_id: str


class Type(BaseModel):
    kind: Literal["type"] = "type"
    node_id: str
    text: str


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

    def observe(self) -> Observation: ...

    def act(self, action: Action) -> str | None: ...
