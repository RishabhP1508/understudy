"""LLMClient: the provider-agnostic contract the discovery agent loop talks to.

Nothing under src/understudy/replay/ may import this module, even transitively --
tests/test_constraints.py (invariant 1) enforces it, because replay has no model in the
decision loop.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from understudy.config import Settings


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


def build_llm(settings: Settings) -> LLMClient:
    """Construct the configured LLMClient. Provider is `LLM_PROVIDER` (default "gemini");
    `GEMINI_MODEL` still overrides the model, read by GeminiClient itself. One registry entry
    today, because one real implementation behind this protocol is the seam the brief asks for --
    a second, never-run provider client would be gold-plating with no test that ever calls it.
    """
    from understudy.llm.gemini import GeminiClient  # deferred: avoids a base<->gemini import cycle

    registry: dict[str, Any] = {"gemini": GeminiClient}
    provider = os.environ.get("LLM_PROVIDER", "gemini")
    if provider not in registry:
        raise ValueError(f"unknown LLM_PROVIDER={provider!r}; available: {sorted(registry)}")
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is not set; discovery needs a live model.")
    return registry[provider](api_key=settings.gemini_api_key)
