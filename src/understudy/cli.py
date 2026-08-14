"""Understudy CLI: discover | replay | operator | catalog.

Phase 0 stub: every subcommand prints "not implemented" and exits 2, except that `discover`
also resolves and echoes its target (from the policy entry_point when --target is omitted),
since that resolution is part of what this phase proves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from understudy.config import load_policy

app = typer.Typer(add_completion=False)


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
    resolved_target = target or load_policy(policy).get("entry_point")
    typer.echo(f"target: {resolved_target}")
    typer.echo("not implemented")
    raise typer.Exit(2)


@app.command()
def replay(
    artifact: Annotated[Path, typer.Option("--artifact", help="path to a capability artifact")],
    params: Annotated[str, typer.Option("--params", help="JSON-encoded input parameters")],
    policy: Annotated[
        Path, typer.Option("--policy", help="path to the policy YAML")
    ] = Path("policies/legacy_bank.yaml"),
) -> None:
    typer.echo("not implemented")
    raise typer.Exit(2)


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
