"""Tool schemas the model calls, as plain provider-agnostic dicts.

llm/gemini.py converts these to FunctionDeclaration objects. Every tool requires `rationale`:
the "why" is a stated requirement (CLAUDE.md R5), not optional colour.
"""

from __future__ import annotations

from typing import Any

NAVIGATE_TOOL: dict[str, Any] = {
    "name": "navigate",
    "description": "Go to an absolute URL. Use this only to reach a URL you were explicitly given.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The absolute URL to navigate to."},
            "rationale": {"type": "string", "description": "Why this navigation is needed now."},
        },
        "required": ["url", "rationale"],
    },
}

CLICK_TOOL: dict[str, Any] = {
    "name": "click",
    "description": "Click the element shown at the given [index] in the observation.",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "The [index] of the element to click, exactly as rendered.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this is the right element to click right now.",
            },
        },
        "required": ["index", "rationale"],
    },
}

TYPE_TOOL: dict[str, Any] = {
    "name": "type",
    "description": "Type text into the textbox at the given [index].",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "The [index] of the textbox."},
            "text": {"type": "string", "description": "The text to type into it."},
            "rationale": {"type": "string", "description": "Why this field and this value."},
        },
        "required": ["index", "text", "rationale"],
    },
}

READ_TOOL: dict[str, Any] = {
    "name": "read",
    "description": (
        "Read the visible text at the given [index] to inspect page state before deciding the "
        "next step. Use extract instead when this value IS part of the answer the goal asked for."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "The [index] of the element to read."},
            "rationale": {"type": "string", "description": "Why you need to see this value now."},
        },
        "required": ["index", "rationale"],
    },
}

EXTRACT_TOOL: dict[str, Any] = {
    "name": "extract",
    "description": (
        "Read the text at the given [index] and record it as a named output value, because it "
        "IS part of the answer the goal asked for (e.g. a balance or a status)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "The [index] of the element holding the value.",
            },
            "output_name": {
                "type": "string",
                "description": "A short, stable name for this output, e.g. 'balance'.",
            },
            "rationale": {"type": "string", "description": "Why this value answers the goal."},
        },
        "required": ["index", "output_name", "rationale"],
    },
}

FINISH_TOOL: dict[str, Any] = {
    "name": "finish",
    "description": (
        "Declare the goal achieved. The runner independently re-observes the page and verifies "
        "the checkpoint before accepting this; it does not take your word for it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["text_present"]},
                    "target": {
                        "type": "string",
                        "description": "Where to look, or 'page'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "The exact text that must be present.",
                    },
                },
                "required": ["kind", "target", "value"],
            },
            "rationale": {"type": "string"},
        },
        "required": ["checkpoint", "rationale"],
    },
}

ALL_TOOLS: list[dict[str, Any]] = [
    NAVIGATE_TOOL,
    CLICK_TOOL,
    TYPE_TOOL,
    READ_TOOL,
    EXTRACT_TOOL,
    FINISH_TOOL,
]
