"""catalog/server.py: published capabilities as MCP tools, over stdio (R2/R7: "an agent-invocable
capability, not a step list").

An artifact under `artifacts/` becomes exactly one MCP tool: the tool's name is the capability's
own `name` (sanitized only if it does not already fit MCP's tool-name shape -- the recorder
degrades `name` to the whole goal sentence when no LLM key was configured, and a name with spaces
is not a legal tool name), its description is the capability's `description`, and its input
schema is `Capability.json_schema()` VERBATIM -- that method is the published contract (Phase 8),
and re-deriving a second schema here would be a second, driftable copy of it.

TWO GATES, both real, both structural, neither negotiable:

  1. Only the HIGHEST `version` per `capability_id` is ever published. Artifacts are append-only
     (docs/adr/0011): a caller must reach the current recording of a capability, not four tools
     for four revisions of two capabilities. Everything here re-reads `artifacts/*.json` on every
     request (`_load_published`, called fresh by every handler, never cached at import or server
     start), so a fresh `discover`/`record` or an `approve` is visible with no restart.

  2. A DRAFT capability is refused outright, before anything launches -- not just a
     RISKY_IRREVERSIBLE step within it (that gate is `safety/policy.py`'s own, and stays in
     force independently). This module additionally NEVER passes `allow_risky=True` and has NO
     code path that writes a capability's `status` -- an agent-facing catalog that could quietly
     perform an irreversible action, or self-approve, is the worst version of this feature. See
     docs/adr/0017.

Nothing here may write to stdout: stdout IS the stdio transport once `serve_stdio` starts. Every
diagnostic in this module either becomes part of a structured `CallToolResult`/`ListToolsResult`
or goes to stderr; nothing calls `print()`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import anyio.to_thread
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from understudy.escalation.store import InterventionStore
from understudy.models.artifact import Capability
from understudy.models.result import ReplayResult
from understudy.replay.engine import replay as replay_capability
from understudy.replay.outcomes import UnknownDetector
from understudy.safety.redact import Redactor

LIST_CAPABILITIES_TOOL = "list_capabilities"

_VALID_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_INVALID_TOOL_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")


def _tool_name(capability: Capability) -> str:
    """`Capability.name` verbatim when it already fits MCP's tool-name shape; otherwise a
    sanitized form of it (punctuation and whitespace collapsed to `_`, truncated to 64 chars).
    Collision detection lives in `_load_published`, not here -- this function only ever answers
    "what would this ONE capability's name become", never "is that name already taken".
    """
    if _VALID_TOOL_NAME.fullmatch(capability.name):
        return capability.name
    slug = _INVALID_TOOL_CHARS.sub("_", capability.name).strip("_")
    return (slug or "capability")[:64]


def _load_published(artifacts_dir: Path) -> dict[str, tuple[Path, Capability]]:
    """Load every `artifacts_dir/*.json` fresh (B1: no caching -- called by every handler on
    every request), keep only the highest `version` per `capability_id`, and map each surviving
    capability to its tool name. Raises loudly, naming both capability ids, if two of them
    sanitize to the same tool name -- silently shadowing one is worse than refusing to start.
    """
    highest: dict[str, tuple[Path, Capability]] = {}
    for path in sorted(artifacts_dir.glob("*.json")):
        capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
        current = highest.get(capability.capability_id)
        if current is None or capability.version > current[1].version:
            highest[capability.capability_id] = (path, capability)

    published: dict[str, tuple[Path, Capability]] = {}
    for capability_id, (path, capability) in highest.items():
        name = _tool_name(capability)
        if name in published:
            owner = published[name][1].capability_id
            raise RuntimeError(
                f"tool name collision: capability_id {owner!r} and {capability_id!r} both "
                f"sanitize to the tool name {name!r}"
            )
        published[name] = (path, capability)
    return published


def _list_capabilities_payload(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Everything an agent needs to decide whether, and how, to call a capability -- including
    `status`: an agent that cannot see draft-ness up front can otherwise only learn it by being
    refused (B1)."""
    entries: list[dict[str, Any]] = []
    for tool_name, (_, capability) in sorted(_load_published(artifacts_dir).items()):
        entries.append(
            {
                "tool_name": tool_name,
                "name": capability.name,
                "description": capability.description,
                "input_schema": capability.json_schema(),
                "outputs": [
                    {"name": o.name, "type": o.type, "description": o.description}
                    for o in capability.outputs
                ],
                "status": capability.status,
                "version": capability.version,
            }
        )
    return entries


def _run_replay(
    artifact_path: Path,
    arguments: dict[str, Any],
    policy_path: Path,
    evidence_dir: Path,
    intervention_dir: Path,
    intervention_ttl_s: float,
) -> ReplayResult:
    """The SYNC call into `replay/engine.py` (it drives the sync Playwright API), run off the
    event loop thread by every caller of this function (`anyio.to_thread.run_sync` in
    `handle_call_tool`) -- calling it directly on the loop's own thread raises, since Playwright's
    sync API refuses to run inside a running asyncio event loop.

    `allow_risky=False`, unconditionally, with no parameter or code path to change it: the catalog
    must never be the thing that quietly arms an irreversible replay (docs/adr/0017). A blocked
    RISKY_IRREVERSIBLE step still escalates to a human rather than failing opaquely, via the same
    `InterventionStore` + ttl every other replay path uses.
    """
    store = InterventionStore(base_dir=intervention_dir)
    return replay_capability(
        artifact_path,
        arguments,
        policy_path,
        allow_risky=False,
        evidence_base_dir=evidence_dir,
        intervention_store=store,
        intervention_ttl_s=intervention_ttl_s,
    )


def _text_result(redactor: Redactor, payload: Any, *, is_error: bool) -> types.CallToolResult:
    """The one path any tool result reaches the wire through -- `Redactor.dumps` (ARCHITECTURE.md
    decision 10), never a bare `json.dumps` or an f-string built from unredacted fields."""
    text = redactor.dumps(payload, indent=2)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


async def handle_list_tools(artifacts_dir: Path) -> types.ListToolsResult:
    published = _load_published(artifacts_dir)
    tools = [
        types.Tool(
            name=name,
            description=capability.description,
            input_schema=capability.json_schema(),
        )
        for name, (_, capability) in sorted(published.items())
    ]
    tools.append(
        types.Tool(
            name=LIST_CAPABILITIES_TOOL,
            description=(
                "List every published capability: its tool name, declared name, description, "
                "input schema, declared outputs, status, and version."
            ),
            input_schema={"type": "object", "properties": {}},
        )
    )
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(
    artifacts_dir: Path,
    policy_path: Path,
    evidence_dir: Path,
    intervention_dir: Path,
    intervention_ttl_s: float,
    redactor: Redactor,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    if params.name == LIST_CAPABILITIES_TOOL:
        return _text_result(redactor, _list_capabilities_payload(artifacts_dir), is_error=False)

    published = _load_published(artifacts_dir)
    entry = published.get(params.name)
    if entry is None:
        return _text_result(
            redactor,
            f"unknown tool {params.name!r}; known tools: "
            f"{sorted([*published, LIST_CAPABILITIES_TOOL])}",
            is_error=True,
        )

    artifact_path, capability = entry
    if capability.status == "draft":
        return _text_result(
            redactor,
            f"capability {capability.name!r} is status 'draft' and cannot be invoked; a human "
            "must review and approve it",
            is_error=True,
        )

    arguments = params.arguments or {}
    try:
        result = await anyio.to_thread.run_sync(
            _run_replay,
            artifact_path,
            arguments,
            policy_path,
            evidence_dir,
            intervention_dir,
            intervention_ttl_s,
        )
    except UnknownDetector as exc:
        # A broken artifact (a known_outcomes/recovery_rules entry naming a detector or trigger
        # this build does not have registered) comes back as a named refusal, never a traceback
        # the calling agent has no way to act on.
        return _text_result(
            redactor, f"invalid artifact for capability {capability.name!r}: {exc}", is_error=True
        )

    # success / business_outcome / escalated are all NORMAL RESULTS (isError=False): a business
    # outcome is a legitimate answer the caller needs, and an escalation means a human is or was
    # involved, neither of which is a tool malfunction. Only a hard_failure is an error result.
    return _text_result(redactor, result, is_error=(result.kind == "hard_failure"))


def build_server(
    artifacts_dir: Path,
    policy_path: Path,
    evidence_dir: Path,
    intervention_dir: Path,
    intervention_ttl_s: float,
) -> Server[None]:
    redactor = Redactor()

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return await handle_list_tools(artifacts_dir)

    async def on_call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await handle_call_tool(
            artifacts_dir,
            policy_path,
            evidence_dir,
            intervention_dir,
            intervention_ttl_s,
            redactor,
            params,
        )

    return Server(
        "understudy-catalog",
        version="1.0.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(
    artifacts_dir: Path,
    policy_path: Path,
    evidence_dir: Path,
    intervention_dir: Path,
    intervention_ttl_s: float,
) -> None:
    server = build_server(
        artifacts_dir, policy_path, evidence_dir, intervention_dir, intervention_ttl_s
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
