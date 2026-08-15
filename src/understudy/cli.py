"""Understudy CLI: discover | replay | operator | catalog."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Annotated

import typer
from google.genai import errors

from understudy.agent.loop import run
from understudy.config import load_settings
from understudy.evidence.logger import EvidenceLogger
from understudy.llm.gemini import GeminiClient
from understudy.record.recorder import build_capability
from understudy.replay import engine as replay_engine
from understudy.safety.policy import (
    EscalationRequired,
    NavigationBlocked,
    PolicyDenied,
    PolicyGate,
    load_policy,
)
from understudy.safety.redact import Redactor
from understudy.surface.web import WebSurface

app = typer.Typer(add_completion=False)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "capability"


def _policy_exception_message(exc: EscalationRequired | NavigationBlocked | PolicyDenied) -> str:
    if isinstance(exc, NavigationBlocked):
        return f"navigation left the allowlist: {exc.urls}"
    return f"{exc.decision.rule}: {exc.decision.reason}"


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
) -> None:
    policy_obj = load_policy(policy)
    resolved_target = target or policy_obj.entry_point
    if not resolved_target:
        typer.echo("no --target given and the policy has no entry_point to default to.")
        raise typer.Exit(1)
    typer.echo(f"target: {resolved_target}")

    settings = load_settings(policy)
    if not settings.gemini_api_key:
        typer.echo("GEMINI_API_KEY is not set; discovery needs a live model. See .env.example.")
        raise typer.Exit(1)

    run_id = uuid.uuid4().hex[:12]
    logger = EvidenceLogger("discovery", run_id)
    llm = GeminiClient(api_key=settings.gemini_api_key)
    gate = PolicyGate(policy_obj, logger, mode="discovery")
    surface = WebSurface(policy=policy_obj, headless=False)

    max_steps = policy_obj.max_steps
    timeout_s = policy_obj.max_wall_clock_seconds

    logger.event("run_start", goal=goal, target=resolved_target, run_id=run_id, model=llm.model)
    try:
        outcome = run(
            goal=goal,
            target=resolved_target,
            surface=surface,
            llm=llm,
            gate=gate,
            logger=logger,
            max_steps=max_steps,
            timeout_s=timeout_s,
        )
    except errors.APIError as exc:
        typer.echo(
            f"Gemini API error ({exc.code} {exc.status}) for model {llm.model}: {exc.message}. "
            "Set GEMINI_MODEL to another model or wait for the quota to reset."
        )
        raise typer.Exit(1) from None
    except (EscalationRequired, NavigationBlocked, PolicyDenied) as exc:
        message = _policy_exception_message(exc)
        # replay/engine.py logs the equivalent of this into its own run.jsonl (a hard_failure
        # event carrying the same reason) because a policy stop there has to be a returned
        # result, not a raised exception. Discovery's stop is a raised exception by design (R6:
        # a human needs to see and act on it), but the evidence trail should not go dark just
        # because the run ends via an exception instead of a return.
        logger.event("run_end", status="policy_stopped", reason=message)
        typer.echo(f"discovery stopped by policy: {message}")
        raise typer.Exit(1) from None
    finally:
        surface.close()
    logger.event("run_end", status=outcome.status, steps_executed=outcome.steps_executed)

    typer.echo(f"status: {outcome.status}")
    typer.echo(f"rounds: {outcome.rounds}")
    typer.echo(f"steps executed: {outcome.steps_executed}")
    typer.echo(f"rejected turns: {outcome.rejected_turns}")
    typer.echo(f"outputs: {json.dumps(outcome.outputs)}")
    typer.echo(f"usage (this run): {json.dumps(outcome.usage)}")
    typer.echo(f"usage (client total): {json.dumps(llm.total_usage)}")

    if outcome.status != "goal_verified":
        typer.echo("goal was not verified; no artifact recorded.")
        raise typer.Exit(1)

    slug = _slugify(goal)
    capability = build_capability(
        run_dir=logger.dir,
        goal=goal,
        target=resolved_target,
        run_id=run_id,
        model=llm.model,
        capability_id=slug,
        name=goal,
    )
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    artifact_path = artifacts_dir / f"{slug}.v1.json"
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
) -> None:
    parsed_params = json.loads(params)
    result = replay_engine.replay(artifact, parsed_params, policy, allow_risky=allow_risky)
    typer.echo(Redactor().dumps(result, indent=2))
    if result.kind == "hard_failure":
        raise typer.Exit(1)


@app.command()
def operator() -> None:
    typer.echo("not implemented")
    raise typer.Exit(2)


@app.command()
def catalog() -> None:
    typer.echo("not implemented")
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
