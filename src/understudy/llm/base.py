"""LLMClient: the provider-agnostic contract the discovery agent loop talks to.

Nothing under src/understudy/replay/ may import this module, even transitively --
tests/test_constraints.py (invariant 1) enforces it, because replay has no model in the
decision loop.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    tool_calls: list[ToolCall] = Field(default_factory=list)
    text: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class LLMClient(Protocol):
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...
