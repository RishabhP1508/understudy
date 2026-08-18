"""Understudy CLI: discover | replay | operator | catalog."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from google.genai import errors

from understudy.agent.loop import RunOutcome, run
from understudy.config import load_settings
from understudy.escalation.control import SessionBroker
from understudy.escalation.operator_app import create_app
from understudy.escalation.store import InterventionStore
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.base import LLMClient, build_llm
from understudy.models.artifact import Capability, StabilitySignal
from understudy.models.result import FailureCategory, ReplayResult
from understudy.record.recorder import build_capability
from understudy.replay import engine as replay_engine
from understudy.replay.outcomes import UnknownDetector
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    PolicyDenied,
    PolicyGate,
    load_policy,
)
from understudy.safety.redact import Redactor, mint_safe_id
from understudy.surface.base import Surface
from understudy.surface.web import WebSurface

app = typer.Typer(add_completion=False)

_DISCOVERY_POLICY_STOPS = (EscalationRequired, NavigationBlocked, PolicyDenied)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "capability"


_ARTIFACT_VERSION_RE = re.compile(r"\.v(\d+)\.json$")


def _next_artifact_version(artifacts_dir: Path, slug: str) -> int:
    """1 if no `{slug}.v*.json` exists yet, else one past the highest version already on disk.

    Artifacts are append-only (R2: versioned): a second successful discover run of the same
    goal text must never silently overwrite the first recording. That is exactly how the one
    artifact this project shipped, from a genuine earlier discovery run, was lost -- `discover`
    always wrote `{slug}.v1.json` with no check for a prior file.
    """
    versions = [
        int(match.group(1))
        for path in artifacts_dir.glob(f"{slug}.v*.json")
        if (match := _ARTIFACT_VERSION_RE.search(path.name))
    ]
    return max(versions, default=0) + 1


def _policy_exception_message(exc: EscalationRequired | NavigationBlocked | PolicyDenied) -> str:
    if isinstance(exc, NavigationBlocked):
        return f"navigation left the allowlist: {exc.urls}"
    return f"{exc.decision.rule}: {exc.decision.reason}"


def _write_error_result(logger: EvidenceLogger, exc: BaseException) -> None:
    """D7: whatever kind of death `discover` suffers, it leaves a `result.json` behind, not just
    a stack trace -- `evidence/discovery-a3a4a2fc6000` (two events, no terminal record at all) is
    exactly the failure mode this closes. Goes through the same Redactor every other write does
    (ARCHITECTURE.md decision 10); not built through `EvidenceLogger.write_result`, which requires
    a `BaseModel`, because there is no ReplayResult-shaped model for a discovery-time death.
    """
    payload = {"status": "error", "error": type(exc).__name__, "reason": str(exc)}
    (logger.dir / "result.json").write_text(
        logger.redactor.dumps(payload, indent=2), encoding="utf-8"
    )


def _discover_and_capture(
    goal: str,
    target: str,
    surface: Surface,
    llm: LLMClient,
    gate: PolicyGate,
    logger: EvidenceLogger,
    max_steps: int,
    timeout_s: float,
    stall_limit: int,
    full_render_every: int,
    broker: SessionBroker | None = None,
    intervention_ttl_s: float = 900,
) -> RunOutcome:
    """D7: however `run()` dies -- a Gemini API error, a policy stop, or anything else entirely
    unexpected -- a terminal `run_end` event and a `result.json` exist before the exception
    propagates. `evidence/discovery-a3a4a2fc6000` (two events, no terminal record at all) is
    exactly the failure mode this closes: it died inside `llm.complete()` with no handler for
    that at all. Extracted from `discover()` (the typer command) so this failure-handling
    machinery is directly testable against fakes, with no real browser and no network required.
    A run that completes (including one whose OWN stopping condition already logged its own
    `run_end`, e.g. `max_steps`) never reaches the `except` below at all.
    """
    try:
        return run(
            goal=goal,
            target=target,
            surface=surface,
            llm=llm,
            gate=gate,
            logger=logger,
            max_steps=max_steps,
            timeout_s=timeout_s,
            stall_limit=stall_limit,
            full_render_every=full_render_every,
            broker=broker,
            intervention_ttl_s=intervention_ttl_s,
        )
    except Exception as exc:
        if isinstance(exc, _DISCOVERY_POLICY_STOPS):
            status, reason = "policy_stopped", _policy_exception_message(exc)
        else:
            status, reason = "error", str(exc)
        logger.run_end(status=status, error=type(exc).__name__, reason=reason)
        _write_error_result(logger, exc)
        raise


@app.command()
def discover(
    goal: Annotated[str, typer.Option("--goal", help="natural-language goal for the agent")],
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="app id, URL, or entry point; defaults to the policy entry_point",
        ),
    ] = None,
    policy: Annotated[
        Path, typer.Option("--policy", help="path to the policy YAML")
    ] = Path("policies/legacy_bank.yaml"),
    evidence_dir: Annotated[
        Path, typer.Option("--evidence-dir", help="base directory for run evidence")
    ] = Path("evidence"),
    escalate: Annotated[
        bool,
        typer.Option(
            "--escalate/--no-escalate",
            help=(
                "raise a human intervention on a stopping condition instead of just ending the "
                "run"
            ),
        ),
    ] = True,
    intervention_ttl: Annotated[
        float,
        typer.Option(
            "--intervention-ttl",
            help="seconds an intervention waits for an operator before expiring",
        ),
    ] = 900,
    intervention_dir: Annotated[
        Path,
        typer.Option(
            "--intervention-dir",
            help="base directory for intervention records (point the operator console at this)",
        ),
    ] = Path("evidence/interventions"),
    operator_port: Annotated[
        int,
        typer.Option(
            "--operator-port",
            help="port the operator console listens on, for the printed intervention URL",
        ),
    ] = 8765,
) -> None:
    policy_obj = load_policy(policy)
    resolved_target = target or policy_obj.entry_point
    if not resolved_target:
        typer.echo("no --target given and the policy has no entry_point to default to.")
        raise typer.Exit(1)
    typer.echo(f"target: {resolved_target}")

    settings = load_settings(policy)
    try:
        llm = build_llm(settings)
    except ValueError as exc:
        typer.echo(f"{exc} See .env.example.")
        raise typer.Exit(1) from None
    # build_llm's return type is the provider-agnostic LLMClient protocol; `model` and
    # `total_usage` are real attributes of the one concrete implementation (GeminiClient), read
    # via getattr rather than widened onto the protocol itself, so a fake LLMClient in a test
    # never has to carry attributes it does not use.
    model_name = getattr(llm, "model", "unknown-model")

    run_id = mint_safe_id()
    logger = EvidenceLogger(run_id, "discovery", base_dir=evidence_dir)
    surface = WebSurface(policy=policy_obj, headless=False)
    # Escalation is enabled by the presence of a broker, nothing else. `--no-escalate` (or any
    # caller that never asks for one) leaves broker=None, and the run behaves exactly as it did
    # before this option existed.
    broker: SessionBroker | None = None
    if escalate:
        store = InterventionStore(base_dir=intervention_dir)
        broker = SessionBroker(surface, store, run_id=run_id, logger=logger)
        typer.echo(
            f"escalation enabled: if this run raises an intervention, resolve it at "
            f"http://127.0.0.1:{operator_port}/ (start it with `understudy operator "
            f"--port {operator_port} --store-dir {intervention_dir} --evidence-dir {evidence_dir}`)"
        )
    gate = PolicyGate(policy_obj, logger, mode="discovery", broker=broker)

    max_steps = policy_obj.max_steps
    timeout_s = policy_obj.max_wall_clock_seconds

    logger.event("run_start", goal=goal, target=resolved_target, run_id=run_id, model=model_name)
    logger.start_trace(surface)
    outcome = None
    try:
        outcome = _discover_and_capture(
            goal=goal,
            target=resolved_target,
            surface=surface,
            llm=llm,
            gate=gate,
            logger=logger,
            max_steps=max_steps,
            timeout_s=timeout_s,
            stall_limit=policy_obj.stall_limit,
            full_render_every=policy_obj.full_render_every,
            broker=broker,
            intervention_ttl_s=intervention_ttl,
        )
    except errors.APIError as exc:
        typer.echo(
            f"Gemini API error ({exc.code} {exc.status}) for model {model_name}: {exc.message}. "
            "Set GEMINI_MODEL to another model or wait for the quota to reset."
        )
        raise typer.Exit(1) from None
    except _DISCOVERY_POLICY_STOPS as exc:
        # replay/engine.py logs the equivalent of this into its own run.jsonl (a hard_failure
        # event carrying the same reason) because a policy stop there has to be a returned
        # result, not a raised exception. Discovery's stop is a raised exception by design (R6:
        # a human needs to see and act on it); `_discover_and_capture` already wrote the
        # terminal event and result.json before re-raising this.
        typer.echo(f"discovery stopped by policy: {_policy_exception_message(exc)}")
        raise typer.Exit(1) from None
    finally:
        logger.stop_trace(surface, keep=(outcome is None or outcome.status != "goal_verified"))
        surface.close()
    # run() already wrote the one terminal "run_end" event for this run (agent/loop.py's _end()
    # or its goal_verified branch, whichever fired) -- logging a second one here duplicated it in
    # every run's evidence, with no way for a consumer to tell which was authoritative.
    logger.write_result(outcome)

    typer.echo(f"status: {outcome.status}")
    typer.echo(f"rounds: {outcome.rounds}")
    typer.echo(f"steps executed: {outcome.steps_executed}")
    typer.echo(f"rejected turns: {outcome.rejected_turns}")
    typer.echo(f"outputs: {json.dumps(outcome.outputs)}")
    typer.echo(f"usage (this run): {json.dumps(outcome.usage)}")
    typer.echo(f"usage (client total): {json.dumps(getattr(llm, 'total_usage', {}))}")
    if outcome.intervention_id is not None:
        # run() blocks synchronously while an intervention is pending (SessionBroker.escalate's
        # own poll loop), so by the time control returns here it is already resolved or expired --
        # this reports what happened rather than announcing it live. A caller that wants to see
        # it as it happens today has to watch the operator console itself while the run is up;
        # a genuinely live CLI notification would need a callback into a blocking call this
        # phase's own escalate() signature deliberately keeps to (request, logger) -> resolution.
        typer.echo(f"intervention: {outcome.intervention_id} (resolution: {outcome.resolution})")

    if outcome.status != "goal_verified":
        typer.echo("goal was not verified; no artifact recorded.")
        raise typer.Exit(1)

    slug = _slugify(goal)
    capability = build_capability(
        run_dir=logger.dir,
        goal=goal,
        target=resolved_target,
        run_id=run_id,
        model=model_name,
        capability_id=slug,
        policy=policy_obj,
        llm=llm,
    )
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    version = _next_artifact_version(artifacts_dir, slug)
    capability = capability.model_copy(update={"version": version})
    artifact_path = artifacts_dir / f"{slug}.v{version}.json"
    artifact_path.write_text(Redactor().dumps(capability, indent=2), encoding="utf-8")
    typer.echo(f"artifact written: {artifact_path}")


@app.command()
def replay(
    artifact: Annotated[Path, typer.Option("--artifact", help="path to a capability artifact")],
    params: Annotated[str, typer.Option("--params", help="JSON-encoded input parameters")],
    policy: Annotated[
        Path, typer.Option("--policy", help="path to the policy YAML")
    ] = Path("policies/legacy_bank.yaml"),
    allow_risky: Annotated[
        bool,
        typer.Option(
            "--allow-risky",
            help=(
                "permit a RISKY_IRREVERSIBLE step to execute, if and only if the capability's "
                "own status is 'approved'"
            ),
        ),
    ] = False,
    evidence_dir: Annotated[
        Path, typer.Option("--evidence-dir", help="base directory for run evidence")
    ] = Path("evidence"),
    repeat: Annotated[
        int,
        typer.Option(
            "--repeat",
            help=(
                "run replay this many times; N>1 writes a read-only StabilitySignal into the "
                "artifact (never a gate) and the exit code follows the LAST run's result"
            ),
        ),
    ] = 1,
    escalate: Annotated[
        bool,
        typer.Option(
            "--escalate/--no-escalate",
            help=(
                "raise a human intervention on a recoverable-condition or policy stop instead of "
                "just failing"
            ),
        ),
    ] = True,
    intervention_ttl: Annotated[
        float,
        typer.Option(
            "--intervention-ttl",
            help="seconds an intervention waits for an operator before expiring",
        ),
    ] = 900,
    intervention_dir: Annotated[
        Path,
        typer.Option(
            "--intervention-dir",
            help="base directory for intervention records (point the operator console at this)",
        ),
    ] = Path("evidence/interventions"),
    operator_port: Annotated[
        int,
        typer.Option(
            "--operator-port",
            help="port the operator console listens on, for the printed intervention URL",
        ),
    ] = 8765,
) -> None:
    """Exit codes: 0 for success AND for a business outcome (a legitimate answer, never a
    failure); 1 for a hard failure; 2 for a caller error -- an INVALID_PARAMS hard failure, or an
    UnknownDetector escaping replay() (a broken artifact naming a detector/trigger this build does
    not know, which is a request that was never valid, not a run that failed).
    """
    parsed_params = json.loads(params)
    result: ReplayResult | None = None
    outcomes_seen: list[str] = []
    successes = 0
    intervention_store: InterventionStore | None = None
    if escalate:
        intervention_store = InterventionStore(base_dir=intervention_dir)
        typer.echo(
            f"escalation enabled: if this run raises an intervention, resolve it at "
            f"http://127.0.0.1:{operator_port}/ (start it with `understudy operator "
            f"--port {operator_port} --store-dir {intervention_dir} --evidence-dir {evidence_dir}`)"
        )
    try:
        for _run_index in range(repeat):
            result = replay_engine.replay(
                artifact,
                parsed_params,
                policy,
                allow_risky=allow_risky,
                evidence_base_dir=evidence_dir,
                intervention_store=intervention_store,
                intervention_ttl_s=intervention_ttl,
            )
            outcomes_seen.append(result.kind)
            if result.kind in ("success", "business_outcome"):
                successes += 1
    except UnknownDetector as exc:
        typer.echo(f"invalid artifact: {exc}")
        raise typer.Exit(2) from None
    assert result is not None  # repeat >= 1: the loop above always runs at least once

    if repeat > 1:
        # A read-only OBSERVATION of replay reliability, never a gate (models/artifact.py's
        # StabilitySignal docstring) -- rewritten through the one serialization path, never a
        # bare json.dump.
        capability = Capability.model_validate_json(artifact.read_text(encoding="utf-8"))
        stability = StabilitySignal(
            runs=repeat,
            successes=successes,
            last_n_outcomes=outcomes_seen,
            computed_at=datetime.now(UTC).isoformat(),
        )
        capability = capability.model_copy(update={"stability": stability})
        artifact.write_text(Redactor().dumps(capability, indent=2), encoding="utf-8")
        typer.echo(f"stability: {stability.model_dump_json()}")

    if result.kind == "business_outcome":
        # Printed clearly, on its own lines, above the JSON -- a human reading the terminal must
        # never mistake a legitimate business answer for a failure.
        typer.echo("BUSINESS OUTCOME (not a failure):")
        typer.echo(f"  code: {result.code}")
        typer.echo(f"  message: {result.message}")
        typer.echo(f"  observed: {result.observed}")

    if result.kind == "escalated":
        typer.echo("ESCALATED (a human was involved):")
        typer.echo(f"  intervention: {result.intervention_id}")
        typer.echo(f"  resolution: {result.resolution}")
        typer.echo(f"  resumed: {result.resumed}")

    typer.echo(Redactor().dumps(result, indent=2))

    if result.kind == "hard_failure":
        if result.category == FailureCategory.INVALID_PARAMS:
            raise typer.Exit(2)
        raise typer.Exit(1)


@app.command()
def record(
    run_dir: Annotated[
        Path,
        typer.Option(
            "--run-dir",
            help="an already-recorded evidence run directory, e.g. evidence/discovery-XXXX",
        ),
    ],
    policy: Annotated[
        Path, typer.Option("--policy", help="path to the policy YAML")
    ] = Path("policies/legacy_bank.yaml"),
) -> None:
    """Rebuild a Capability from an ALREADY-RECORDED evidence run directory.

    record/recorder.py's `build_capability` is a separate pass over a written run.jsonl by
    design -- it never depends on the discovery process still being in memory. So when the
    recorder itself gets better (a smarter postcondition rule, a parameterization fix), the
    honest way to get a better artifact out of a GENUINE discovery run is to re-run this pass
    over that run's real evidence, never to hand-edit the artifact file directly (evidence and
    artifacts are produced, never authored), and never to burn a fresh live model run that would
    also change the very flow being compared against the last one.
    """
    events_path = run_dir / "run.jsonl"
    if not events_path.exists():
        typer.echo(f"no run.jsonl under {run_dir}")
        raise typer.Exit(2)
    raw = events_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    run_start = next((e for e in events if e.get("type") == "run_start"), None)
    if run_start is None:
        typer.echo(f"{events_path} has no run_start event; cannot recover goal/target/run_id")
        raise typer.Exit(2)
    goal = run_start.get("goal")
    target = run_start.get("target")
    run_id = run_start.get("run_id")
    model_name = run_start.get("model") or "unknown-model"
    if not goal or not target or not run_id:
        typer.echo(f"{events_path}'s run_start event is missing goal/target/run_id")
        raise typer.Exit(2)

    policy_obj = load_policy(policy)
    settings = load_settings(policy)
    llm: LLMClient | None
    try:
        llm = build_llm(settings)
    except ValueError:
        # The recorder degrades gracefully with no LLMClient (D5), exactly like `discover` does
        # when GEMINI_API_KEY is unset -- re-recording a real run must not require a live key.
        llm = None

    slug = _slugify(goal)
    capability = build_capability(
        run_dir=run_dir,
        goal=goal,
        target=target,
        run_id=run_id,
        model=model_name,
        capability_id=slug,
        policy=policy_obj,
        llm=llm,
    )
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    version = _next_artifact_version(artifacts_dir, slug)
    capability = capability.model_copy(update={"version": version})
    artifact_path = artifacts_dir / f"{slug}.v{version}.json"
    artifact_path.write_text(Redactor().dumps(capability, indent=2), encoding="utf-8")
    typer.echo(f"artifact written: {artifact_path}")


@app.command()
def operator(
    port: Annotated[
        int, typer.Option("--port", help="port to serve the operator console on")
    ] = 8765,
    store_dir: Annotated[
        Path, typer.Option("--store-dir", help="base directory for intervention records")
    ] = Path("evidence/interventions"),
    evidence_dir: Annotated[
        Path, typer.Option("--evidence-dir", help="base directory for run evidence")
    ] = Path("evidence"),
) -> None:
    uvicorn.run(create_app(store_dir, evidence_dir), host="127.0.0.1", port=port)


@app.command()
def catalog() -> None:
    typer.echo("not implemented")
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
