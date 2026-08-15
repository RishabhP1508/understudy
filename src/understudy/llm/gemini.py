"""GeminiClient: function-calling implementation of LLMClient.

Model defaults to "gemini-3.1-flash-lite", overridable with the GEMINI_MODEL environment
variable. Verified live: it completes the same goal in the same 8 rounds with 0 rejected turns
as "gemini-flash-latest" (which resolves to "gemini-3.7-flash"), and quotas are tracked per
model, so pinning to a specific model name also gives it its own separate budget. A pinned name
is also reproducible across runs, unlike a moving alias.

Forced function calling (tool_config mode="ANY") is used because four different models
otherwise returned four different checkpoint `kind` spellings for the same task; forcing a tool
call plus a JSON-schema enum on FINISH_TOOL is what makes verify_checkpoint dispatchable at all.

The free tier's real quota shape, verified live against
GenerateRequestsPerDayPerProjectPerModel-FreeTier, is per-DAY per-model (quotaValue: 20 for
"gemini-3.7-flash"), not the commonly-assumed per-minute limit. One discovery run costs 8-9
calls, so two runs a day exhausts it. GEMINI_MODEL overrides DEFAULT_MODEL for a reviewer who
needs a fresh budget without a code change.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, cast

from google import genai
from google.genai import errors, types

from understudy.llm.base import LLMResponse, ToolCall

DEFAULT_MODEL = "gemini-3.1-flash-lite"
# 429 covers both a transient per-minute rate limit and a per-DAY quota exhaustion; the two are
# indistinguishable from `exc.code` alone. Retrying the latter just spends the backoff delay
# before still raising on the last attempt -- a per-day quota is not cleared by waiting seconds,
# only by the day rolling over or GEMINI_MODEL pointing at a model with its own budget.
_RETRYABLE_CODES = {429, 503}
_MAX_ATTEMPTS = 5


class GeminiClient:
    def __init__(self, api_key: str, model: str | None = None) -> None:
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self._client = genai.Client(api_key=api_key)
        self.total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        # A plain `list[types.Content]` (what this comprehension infers to on its own) does not
        # typecheck against `generate_content`'s declared `contents` parameter type (verified via
        # inspect.signature: a large Union whose list arms are `list[Content | ContentDict | ...]`)
        # because list is invariant -- mypy does not widen a list comprehension's inferred element
        # type to match a Union-typed target the way it sometimes can for a list literal. The
        # runtime value is unchanged: `cast` has no effect at runtime, it only tells the type
        # checker this list satisfies the SDK's own declared type for this parameter.
        contents = cast(
            "types.ContentListUnionDict", [self._to_content(message) for message in messages]
        )
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                            parameters_json_schema=tool["parameters"],
                        )
                        for tool in tools
                    ]
                )
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
        )

        response = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except errors.APIError as exc:
                retryable = getattr(exc, "code", None) in _RETRYABLE_CODES
                if not retryable or attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep((2**attempt) + random.uniform(0, 1))
        if response is None:  # pragma: no cover - unreachable, loop always breaks or raises
            raise RuntimeError("Gemini call did not complete")

        usage = self._accumulate_usage(response.usage_metadata)
        tool_calls = [
            ToolCall(name=call.name, args=dict(call.args or {}))
            for call in (response.function_calls or [])
            if call.name is not None
        ]
        # response.text raises google-genai's own warning ("there are non-text parts in the
        # response... returning concatenated text result from text parts") on every call that
        # has function calls, since forced function calling means there rarely is text. Only
        # read it when there are no tool calls to read text out of instead.
        text = response.text if not tool_calls else None
        return LLMResponse(tool_calls=tool_calls, text=text, usage=usage)

    def _accumulate_usage(self, usage_metadata: Any) -> dict[str, int]:
        if usage_metadata is None:
            return {}
        call_usage = {
            "prompt_tokens": usage_metadata.prompt_token_count or 0,
            "completion_tokens": usage_metadata.candidates_token_count or 0,
            "total_tokens": usage_metadata.total_token_count or 0,
        }
        for key, value in call_usage.items():
            self.total_usage[key] += value
        return call_usage

    @staticmethod
    def _to_content(message: dict[str, Any]) -> types.Content:
        role = message["role"]
        if role == "user":
            return types.Content(role="user", parts=[types.Part.from_text(text=message["text"])])
        if role == "model":
            parts = [
                types.Part.from_function_call(name=call["name"], args=call["args"])
                for call in message["tool_calls"]
            ]
            return types.Content(role="model", parts=parts)
        if role == "tool":
            # Verified live: this API rejects role="tool" ("Role 'tool' is not supported.
            # Please use ... USER ... MODEL ...", despite google-genai's own docstrings
            # suggesting "tool" is a valid Content role). A function response turn has to be
            # sent as role="user" instead.
            return types.Content(
                role="user",
                parts=[
                    types.Part.from_function_response(
                        name=message["name"], response=message["response"]
                    )
                ],
            )
        raise ValueError(f"unknown message role: {role!r}")
