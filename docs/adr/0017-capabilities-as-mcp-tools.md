# 0017. Capabilities as MCP tools

Status: accepted (Phase 11)

## Context

The brief's through-line ends with "deterministic replay is how the AI agent invokes it in
production." Every phase up to this one built the artifact (Phase 8), made it replay
deterministically (Phase 9), and made replay safe to call blind (safety/policy.py's allowlist and
risk gates). Nothing yet let a SEPARATE calling agent actually discover a capability by name, read
its typed contract, and invoke it -- the CLI's `replay` command requires a human (or a script
standing in for one) to already know the artifact path and shape. R2 calls the artifact
"agent-invocable"; this phase is what makes that literally true.

## Decision: MCP over a bespoke HTTP endpoint

The Model Context Protocol is the thing an LLM-calling agent already speaks to discover and invoke
tools, so publishing capabilities as MCP tools reaches a calling agent with no bespoke client this
project would otherwise have to write and the agent would otherwise have to be taught. A bespoke
REST endpoint would need its own schema-discovery convention (`GET /capabilities`, some ad hoc
per-capability `POST` shape) that MCP already standardizes as `list_tools`/`call_tool`. Stdio,
specifically, over HTTP/SSE: this project's "no scaling infrastructure" stance (ARCHITECTURE.md
decision 4) already rules out a always-on server process, and a calling agent that spawns the
catalog as a subprocess per session is the natural fit for a single-process, no-broker submission.

## Decision: the low-level `Server`, not the decorator-based one

The MCP SDK's higher-level `Server.add_tool`/`@server.list_tools()` surface infers a tool's input
schema from a Python function's own signature. This project's tool schemas are not Python
signatures at all -- they are `Capability.json_schema()`, built at artifact-record time from a
list of typed `InputParam`s that varies per capability and is only known at runtime, once
`artifacts/*.json` is read. `mcp.server.lowlevel.Server` takes `on_list_tools`/`on_call_tool` as
plain async callables with no signature-inference step, which is the only shape that can hand back
a schema built from data rather than from a function's own parameters.

## Decision: publish only the highest version per `capability_id`

Artifacts are append-only (docs/adr/0011): `discover`/`record` never overwrites a prior recording,
it writes `{slug}.v{N+1}.json`. Left unfiltered, that means every re-recording of the same goal
would show up as its own tool, and a calling agent has no principled way to choose among four
tools for four revisions of two capabilities -- it wants the CURRENT recording. `_load_published`
groups by `capability_id` and keeps the highest `version`, re-reading `artifacts/*.json` on every
`list_tools`/`call_tool` call rather than caching at server start, so a fresh recording or a fresh
`approve` is visible to a running server with no restart -- caching this would have reintroduced
exactly the "stale process serves old code" failure mode this project has already been bitten by
once (the fixture and operator console both needed restarting after a change, mid-build).

## Decision: a business outcome is `isError=False`; a hard failure is `isError=True`

The MCP protocol's own `isError` flag is the one bit of structure a calling agent gets for free
without parsing `content`. Mapping it onto this project's OWN result contract (`success` /
`business_outcome` / `escalated` -> `isError=False`; `hard_failure` -> `isError=True`) is the same
distinction ARCHITECTURE.md decision 8 already makes and refuses to blur: "no such member" is a
legitimate answer the caller needs, not a tool malfunction, and an agent that treats a business
outcome as an error will retry a correct answer forever. A refusal the catalog itself raises (an
unknown tool name, a draft capability) is also `isError=True` -- unlike a business outcome, there
is no result to hand back at all, and a calling agent needs to be able to tell "the request was
never valid" from "the request ran and got a real answer" the same way `HardFailure` already does.

## Decision: the catalog can never pass `allow_risky` and never approves

`_run_replay` calls `replay()` with `allow_risky=False` as a literal, with no parameter and no code
path anywhere in `catalog/server.py` that could set it otherwise, and there is no function in this
module that writes `Capability.status`. This is deliberately a STRONGER gate than `safety/
policy.py`'s own risk check (which already refuses a `RISKY_IRREVERSIBLE` step unless the artifact
is `"approved"` AND `allow_risky=True`): the catalog additionally refuses to invoke ANY draft
capability at all, before anything launches, even a read-only one. An agent-facing catalog that
could quietly perform an irreversible action, or mark its own artifact reviewed, is the worst
version of this feature -- the whole point of a human-reviewed `status` field is that the review
happens somewhere the calling agent cannot reach. `understudy approve` is therefore a separate,
human-run CLI command (B3), never an MCP tool, and never callable from inside the server process.

## Decision: escalation over blocking, and the TTL tradeoff

A `RISKY_IRREVERSIBLE` step inside an approved capability still gets refused at replay time,
because the catalog's own `allow_risky=False` never changes. Rather than have that refusal surface
as an opaque failure, `_run_replay` passes an `InterventionStore` and a TTL through to `replay()`,
so the same escalation machinery Phase 10 built (raise an intervention, block, resolve or expire)
fires here too -- a calling agent's blocked tool call is a real, evidenced escalation a human can
act on, not a silent policy rejection. The TTL is the one place this phase deliberately diverges
from `discover`/`replay`'s own 900-second default: an MCP `call_tool` is a synchronous request a
calling agent is blocked on, and a tool call that can hang for fifteen minutes waiting on a human
is a broken contract from the agent's side, even though it is a perfectly reasonable wait from a
human operator's side. `catalog`'s own CLI default is 180 seconds for that reason; the demo (B4)
uses a shorter TTL still, to keep an unresolved escalation (nobody plays the operator in the demo)
real without costing several minutes of idle waiting. Shortening the TTL never shortens the actual
wait -- it is a real, configurable value, not a shortcut around the mechanism.

## Alternatives considered

A single "invoke_capability(name, args)" tool with the capability name as a string argument was
rejected: it hides the schema-per-capability structure MCP's own `list_tools` already expresses
natively, and it would force every calling agent to re-derive which arguments a given capability
name accepts from a nested schema-within-a-schema instead of getting it directly from `list_tools`.

Caching `_load_published` at server start, invalidated by a file-watcher or a manual reload
endpoint, was rejected as unwarranted complexity for a single-process, per-request filesystem
read over two small JSON files -- exactly the shape ponytail's ladder asks to be justified before
being built, and re-reading two files per call is cheap enough that there is nothing to cache.

## Round 2: ship the balance-lookup capability approved

The balance-lookup artifact (`artifacts/look-up-member-12345-and-read-their-current-savings-
balance.v3.json`) ships with `status: "approved"`, run through `understudy approve` once, by hand,
outside the demo script. Its last step is `read_text` against a route the fixture never mutates --
there is no irreversible action anywhere in the flow for a human sign-off to be guarding against --
so the draft gate has nothing left to protect once a human has looked at it, and a reviewer who
points a real MCP client at `understudy catalog` should find at least one capability they can
actually invoke rather than a catalog that refuses everything on principle. The subaccount-opening
artifact stays `"draft"` in `artifacts/`: its last step is a submit on a mutating route, it has had
no human sign-off, and shipping it approved would be exactly the thing the draft gate exists to
prevent. `catalog/demo.py`'s own act 0 still approves a COPY of the balance capability (never the
real artifact under `artifacts/`) so the demo continues to exercise the draft -> approved path on
its own throwaway copy even though the real one no longer needs it; `_approve` (F3, below) treats
an already-approved artifact as a clean outcome specifically so this does not regress the moment
the real artifact ships approved.

Three more round-2 fixes, none changing this ADR's own decisions, only their execution: (F1)
`replay/engine.py`'s two escalation-expiry branches were overwriting `HardFailure.observed`
outright instead of appending to it, so a calling agent whose escalation timed out lost the
ORIGINAL policy refusal reason -- exactly the risky-action reason this ADR's own "escalation over
blocking" decision exists to surface -- and kept only "nobody answered." Fixed to append, never
replace. (F2) `catalog/demo.py`'s setup approval (making the read-only balance capability
invokable at all) went to stdout only, never into `transcript.jsonl`, so the transcript showed act
2 succeeding against a capability the transcript itself never showed being approved; it is now act
0, recorded the same way act 5's approval already was. (F4) the demo's own transcript copy of a
tool result kept `TextContent.text` as a raw JSON string, so `Redactor`'s account-number rule
(applied, correctly, to protect real secrets) fired on stray runs of digits inside a float like
`duration_ms` and mangled it; the demo now parses that string back into a real object before
handing it to the transcript, so only genuine strings go through text redaction and numbers stay
numbers. The wire format a real calling agent receives was never affected by F4 -- only this
demo's own transcript copy of it.
