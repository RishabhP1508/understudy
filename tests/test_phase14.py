"""Phase 14: `understudy drift` must find a run.jsonl at any depth under --evidence-dir, not
just one level deep.

Phase 13's curation nested some runs a second level (evidence/cross-tenant/tenant-a/,
evidence/cross-tenant/tenant-b/, evidence/catalog-invocation/*), and `analyze_evidence_dir`'s
former `evidence_dir.iterdir()` only ever looked one level deep -- silently omitting every one of
those, including the only two runs in the repository that carry locator drift, while still
printing a clean report (a run.jsonl-shaped file simply never existed within its search depth).
See evidence/drift.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from understudy.cli import app
from understudy.evidence.drift import analyze_evidence_dir

_ARTIFACT_PATH = (
    Path(__file__).parent.parent
    / "artifacts"
    / "look-up-member-12345-and-read-their-current-savings-balance.v3.json"
)


def _write_run_jsonl(run_dir: Path, *, step_id: int = 0) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "act",
        "dispatched": True,
        "step_id": step_id,
        "proposed_action": {"kind": "click"},
        "context": {
            "resolution_strategy": "role_name_exact",
            "recorded_rank": 1,
            "actual_rank": 1,
        },
    }
    (run_dir / "run.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


def test_nested_run_directory_is_found(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    _write_run_jsonl(evidence_dir / "top-level-run")
    _write_run_jsonl(evidence_dir / "cross-tenant" / "tenant-b")

    reports = analyze_evidence_dir(evidence_dir)
    labels = {report.run_dir for report in reports}

    assert "top-level-run" in labels
    # A nested run is labelled by its path relative to evidence_dir, not a bare leaf name, so
    # it is distinguishable from a hypothetical top-level "tenant-b".
    assert "cross-tenant/tenant-b" in labels
    assert len(reports) == 2


def test_malformed_params_exits_2_with_no_traceback() -> None:
    """A malformed `--params` used to raise `json.JSONDecodeError`, escape uncaught, and print a
    full traceback with exit code 1 -- while `replay`'s own docstring documents exit 2 for a
    caller error. Fixed at the one call site that parses caller-supplied JSON (cli.py's
    `replay`); catalog/server.py never parses a JSON string itself, it receives already-parsed
    MCP arguments, so there is no second site to fix.
    """
    result = CliRunner().invoke(
        app, ["replay", "--artifact", str(_ARTIFACT_PATH), "--params", "{bad json"]
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "--params must be a JSON object" in result.output
