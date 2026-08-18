"""understudy.catalog.demo: a REAL MCP client, over stdio, against a real subprocess running
`understudy catalog`. This is the proof that a calling agent can discover and invoke Understudy's
capabilities over the protocol -- not an in-process function call standing in for one.

Run: .venv/Scripts/python.exe -m understudy.catalog.demo

Straight-line script: no argparse, no config file, no abstraction layer. It performs seven acts
(0-6), in order, against this project's own two shipped capabilities (balance lookup, subaccount
opening), against a FRESH COPY of artifacts/ under evidence/catalog-invocation/ so re-running it
never mutates the repository's real artifacts and never depends on state a previous run left
behind.

The catalog refuses ANY draft capability outright (server.py, B1) -- there is no "read-only
drafts are fine" carve out. Act 0 approves the COPIED balance capability so acts 2-3 (both
read-only) can be invoked at all, and IS narrated in transcript.jsonl (round 2, F2): a reviewer
reading only the transcript must be able to see the approval that makes act 2's Success
reachable, not infer it from something that happened off to the side. `_approve` (round 2, F3)
treats "already approved" as a clean outcome, never a crash: once the real balance artifact ships
approved (a one-command state change, not part of this script -- see docs/adr/0017), a second
demo run's act 0 hits that branch instead of the draft -> approved transition, and either way is
recorded the same way. Act 5 is the OTHER approval this demo performs, on the subaccount
capability, which STAYS DRAFT in the real artifacts/ on purpose (its last step is irreversible and
has had no human sign-off) -- the deliberate demonstration of a human review standing in for the
server's own refusal to ever self-approve.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from understudy.catalog.server import _load_published
from understudy.safety.policy import load_policy
from understudy.safety.redact import Redactor

POLICY_PATH = Path("policies/legacy_bank.yaml")
SOURCE_ARTIFACTS_DIR = Path("artifacts")
BASE_DIR = Path("evidence/catalog-invocation")
ARTIFACTS_COPY_DIR = BASE_DIR / "artifacts"
INTERVENTION_DIR = BASE_DIR / "interventions"
TRANSCRIPT_PATH = BASE_DIR / "transcript.jsonl"

BALANCE_CAPABILITY_NAME = "get_member_savings_balance"
SUBACCOUNT_CAPABILITY_NAME = "open_member_subaccount"

# Short on purpose: act 6 blocks for real, for up to this long, waiting on a human nobody plays
# in this demo. The CLI's own --intervention-ttl default (180s, low hundreds, cli.py) is what a
# real deployment should use; this is a real value, just a smaller one, so the demo's own
# unresolved escalation stays genuine without costing minutes of idle waiting.
INTERVENTION_TTL_S = 30.0

# The fixture accepts any non-empty username/password (fixtures/legacy_bank/app.py) -- this is
# not a real credential, but it is still registered as a secret below before anything is written,
# exactly as a real one would have to be.
DEMO_PASSWORD = "demo-password-not-a-real-secret"


def _check_fixture_reachable(entry_point: str) -> None:
    try:
        urllib.request.urlopen(entry_point, timeout=5)  # noqa: S310 - fixed local fixture URL
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"the legacy_bank fixture is not reachable at {entry_point!r} ({exc}); start it "
            "(see README.md) before running this demo."
        ) from None


def _reset_run_output() -> None:
    """Wipe everything a PREVIOUS run of this script left under `BASE_DIR` before this run writes
    anything of its own: the fresh artifact copies (as before), but also every `replay-*` evidence
    directory, the intervention store, and the old transcript. Without this, `BASE_DIR` drifts into
    describing several runs at once -- an orphaned `replay-*` from a prior round sitting next to a
    `transcript.jsonl` that only narrates the latest one is stale evidence, not a demo artifact.
    `BASE_DIR` itself must always describe exactly one run: its own.
    """
    if ARTIFACTS_COPY_DIR.exists():
        shutil.rmtree(ARTIFACTS_COPY_DIR)
    ARTIFACTS_COPY_DIR.mkdir(parents=True)
    for path in SOURCE_ARTIFACTS_DIR.glob("*.json"):
        shutil.copy2(path, ARTIFACTS_COPY_DIR / path.name)

    for replay_dir in BASE_DIR.glob("replay-*"):
        shutil.rmtree(replay_dir)
    if INTERVENTION_DIR.exists():
        shutil.rmtree(INTERVENTION_DIR)
    TRANSCRIPT_PATH.unlink(missing_ok=True)


def _published_path(tool_name: str) -> Path:
    """The path of the highest-version artifact copy published under `tool_name`, per the same
    "highest version wins" rule `catalog/server.py:_load_published` already answers -- reusing it
    here keeps there being exactly one place that rule lives.
    """
    entry = _load_published(ARTIFACTS_COPY_DIR).get(tool_name)
    if entry is None:
        raise SystemExit(f"no published artifact copy found for tool name {tool_name!r}")
    return entry[0]


def _approve(artifact_path: Path) -> str:
    """Run `understudy approve` on `artifact_path`'s copy. `approve` exits 1 (by design -- that
    exit code is right for a human at a terminal, and stays that way) on an artifact that is
    ALREADY approved, echoing "... is already approved; nothing to do." -- a real outcome, not a
    demo failure, and one this script must survive now that the balance capability is about to
    ship approved for real (F5): a second run's copy of it starts out already approved, and act 0
    still has to record something sensible rather than crash on a non-zero exit (F3, round 2). Any
    OTHER non-zero exit is a genuine failure and propagates via `check_returncode()`.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "understudy.cli", "approve", "--artifact", str(artifact_path)],
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0 and "already approved" not in output:
        completed.check_returncode()
    return output


def _record(
    transcript_lines: list[str],
    redactor: Redactor,
    act: int,
    label: str,
    request: dict[str, Any],
    response: Any,
) -> None:
    entry = {"act": act, "label": label, "request": request, "response": response}
    line = redactor.dumps(entry)
    transcript_lines.append(line)
    print(line)


def _jsonable(result: Any) -> Any:
    """`result` dumped for the transcript, with any `TextContent.text` that is itself a JSON
    string (every real tool result here IS one -- `_text_result` in catalog/server.py always
    returns `Redactor.dumps()` of a Python object) parsed back into a real object first (F4,
    round 2). Left as a raw JSON string, a redaction pass over the WHOLE transcript entry
    (`_record`, below) sees nothing but digit characters where a number like `duration_ms` used
    to be, and its account-number rule legitimately -- and wrongly, for this purpose -- redacts a
    long-enough run of them. The wire format the agent itself receives is untouched; only this
    script's own transcript copy is reshaped. Falls back to the raw string when it does not parse
    (e.g. the draft-refusal message, which is a bare JSON string either way).
    """
    dumped = result.model_dump(mode="json")
    for item in dumped.get("content", []):
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            try:
                item["text"] = json.loads(item["text"])
            except json.JSONDecodeError:
                pass
    return dumped


async def main() -> None:
    policy = load_policy(POLICY_PATH)
    _check_fixture_reachable(policy.entry_point)

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    _reset_run_output()

    redactor = Redactor()
    redactor.register_secret(DEMO_PASSWORD)

    transcript: list[str] = []

    # Act 0, setup: approve the balance-lookup capability's copy so acts 2-3 (both read-only)
    # can be invoked at all -- the catalog refuses ANY draft capability outright. Narrated in
    # transcript.jsonl, same shape as act 5's approval below (round 2, F2): a reviewer reading
    # only the transcript must be able to see the approval that makes act 2's Success reachable.
    balance_path = _published_path(BALANCE_CAPABILITY_NAME)
    approve_stdout_balance = _approve(balance_path)
    _record(
        transcript,
        redactor,
        0,
        (
            "setup: approve the balance-lookup capability's copy via `understudy approve` -- "
            "acts 2-3, both read-only, need it invokable at all"
        ),
        {"command": ["understudy", "approve", "--artifact", str(balance_path)]},
        {"stdout": approve_stdout_balance},
    )

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "understudy.cli",
            "catalog",
            "--transport",
            "stdio",
            "--artifacts-dir",
            str(ARTIFACTS_COPY_DIR),
            "--policy",
            str(POLICY_PATH),
            "--evidence-dir",
            str(BASE_DIR),
            "--intervention-dir",
            str(INTERVENTION_DIR),
            "--intervention-ttl",
            str(INTERVENTION_TTL_S),
        ],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Act 1: discover every published capability and its full input schema.
            list_result = await session.call_tool("list_capabilities", {})
            _record(
                transcript,
                redactor,
                1,
                "list_capabilities: every published capability with its full input schema",
                {"tool": "list_capabilities", "arguments": {}},
                _jsonable(list_result),
            )

            # Act 2: invoke the balance lookup with a real member id -> expect Success.
            args_valid_member = {"member_id": 12345, "password": DEMO_PASSWORD}
            result_valid_member = await session.call_tool(
                BALANCE_CAPABILITY_NAME, args_valid_member
            )
            _record(
                transcript,
                redactor,
                2,
                "invoke balance lookup with a real member id -> expect Success with outputs",
                {"tool": BALANCE_CAPABILITY_NAME, "arguments": args_valid_member},
                _jsonable(result_valid_member),
            )

            # Act 3: invoke it with a member that does not exist -> expect a business outcome.
            args_missing_member = {"member_id": 99999, "password": DEMO_PASSWORD}
            result_missing_member = await session.call_tool(
                BALANCE_CAPABILITY_NAME, args_missing_member
            )
            _record(
                transcript,
                redactor,
                3,
                (
                    "invoke balance lookup with a nonexistent member -> expect a business "
                    "outcome, not an error"
                ),
                {"tool": BALANCE_CAPABILITY_NAME, "arguments": args_missing_member},
                _jsonable(result_missing_member),
            )

            # Act 4: invoke the subaccount capability while it is still draft -> expect a refusal.
            args_subaccount = {
                "member_id": 12345,
                "password": DEMO_PASSWORD,
                "initial_deposit": 250,
            }
            result_draft_refused = await session.call_tool(
                SUBACCOUNT_CAPABILITY_NAME, args_subaccount
            )
            _record(
                transcript,
                redactor,
                4,
                (
                    "invoke the subaccount capability while it is still draft -> expect a "
                    "refusal naming the status, with nothing launched"
                ),
                {"tool": SUBACCOUNT_CAPABILITY_NAME, "arguments": args_subaccount},
                _jsonable(result_draft_refused),
            )

            # Act 5: approve the subaccount COPY -- standing in for a human review the server
            # itself can never perform (it has no code path that writes status at all).
            subaccount_path = _published_path(SUBACCOUNT_CAPABILITY_NAME)
            approve_stdout = _approve(subaccount_path)
            _record(
                transcript,
                redactor,
                5,
                (
                    "approve the subaccount capability's copy via `understudy approve` -- "
                    "STANDING IN FOR A HUMAN REVIEW the catalog server itself cannot perform"
                ),
                {"command": ["understudy", "approve", "--artifact", str(subaccount_path)]},
                {"stdout": approve_stdout},
            )

            # Act 6: invoke the subaccount capability again. Its final step is a submit on a
            # mutating route (RISKY_IRREVERSIBLE), and the catalog NEVER passes allow_risky=True,
            # so this escalates to a human -- nobody plays that role here, so it blocks for up to
            # INTERVENTION_TTL_S and comes back unresolved. Recorded verbatim either way.
            result_after_approval = await session.call_tool(
                SUBACCOUNT_CAPABILITY_NAME, args_subaccount
            )
            _record(
                transcript,
                redactor,
                6,
                (
                    "invoke the subaccount capability again, now approved -- its final step is "
                    "RISKY_IRREVERSIBLE and the catalog never passes allow_risky=True, so expect "
                    "an escalation"
                ),
                {"tool": SUBACCOUNT_CAPABILITY_NAME, "arguments": args_subaccount},
                _jsonable(result_after_approval),
            )

    TRANSCRIPT_PATH.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    print(f"\ntranscript written: {TRANSCRIPT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
