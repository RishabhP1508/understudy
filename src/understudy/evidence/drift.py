"""evidence/drift.py: read run.jsonl across evidence run directories and report per-step locator
rank and drift data (B6, Phase 12) -- plain text, never a gate.

replay/engine.py's `_build_step_context` (B5) puts `resolution_strategy`/`actual_rank`/
`recorded_rank` on every `act` event's own `context` for a step that resolved a target, whether or
not it drifted; a `locator_drift` event exists only for a step that actually did. A run recorded
before B5 carries none of the three context keys at all -- this counts and names that honestly ("no
rank data"), and never imputes a rank for it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Every action kind that resolves a locator at all -- a navigate never does, so it is never
# counted as "missing" rank data; it simply has none to report.
_TARGETED_ACTION_KINDS = frozenset({"click", "type", "select", "read_text"})


@dataclass
class StepRankInfo:
    step_id: int | None
    has_rank_data: bool
    recorded_rank: int | None
    actual_rank: int | None
    strategy: str | None
    clause: str | None


@dataclass
class RunDriftReport:
    run_dir: str
    steps: list[StepRankInfo] = field(default_factory=list)

    @property
    def rank_distribution(self) -> Counter[int]:
        return Counter(step.actual_rank for step in self.steps if step.actual_rank is not None)

    @property
    def no_rank_data_count(self) -> int:
        return sum(1 for step in self.steps if not step.has_rank_data)


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "run.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def analyze_run(run_dir: Path) -> RunDriftReport:
    events = _read_events(run_dir)
    drift_by_step: dict[int, dict[str, Any]] = {
        event["step_id"]: event
        for event in events
        if event.get("type") == "locator_drift" and event.get("step_id") is not None
    }

    steps: list[StepRankInfo] = []
    seen_step_ids: set[int] = set()
    for event in events:
        if event.get("type") != "act" or event.get("dispatched") is not True:
            continue
        action = event.get("proposed_action") or {}
        if action.get("kind") not in _TARGETED_ACTION_KINDS:
            continue
        step_id = event.get("step_id")
        if step_id is None or step_id in seen_step_ids:
            continue
        seen_step_ids.add(step_id)
        context = event.get("context") or {}
        has_rank_data = "resolution_strategy" in context
        drift_event = drift_by_step.get(step_id)
        steps.append(
            StepRankInfo(
                step_id=step_id,
                has_rank_data=has_rank_data,
                recorded_rank=context.get("recorded_rank") if has_rank_data else None,
                actual_rank=context.get("actual_rank") if has_rank_data else None,
                strategy=context.get("resolution_strategy") if has_rank_data else None,
                clause=drift_event.get("clause") if drift_event is not None else None,
            )
        )
    steps.sort(key=lambda step: step.step_id if step.step_id is not None else -1)
    return RunDriftReport(run_dir=run_dir.name, steps=steps)


def analyze_evidence_dir(evidence_dir: Path) -> list[RunDriftReport]:
    """Find every run.jsonl at ANY depth under evidence_dir, not just one level deep -- Phase 13's
    curation nested some runs a second level (evidence/cross-tenant/tenant-a/, .../tenant-b/,
    evidence/catalog-invocation/*), and a one-level `iterdir()` silently missed all of them,
    including the only two runs in the repository that carry locator drift.

    A run's label is its path RELATIVE TO evidence_dir (POSIX-separated), not the bare leaf
    directory name, so "cross-tenant/tenant-b" is distinguishable from a hypothetical top-level
    "tenant-b" rather than colliding with it. evidence_dir itself, if it directly holds a
    run.jsonl, is labelled ".".
    """
    if not evidence_dir.exists():
        return []
    run_files = sorted(evidence_dir.rglob("run.jsonl"))
    reports = []
    for run_file in run_files:
        run_dir = run_file.parent
        report = analyze_run(run_dir)
        label = run_dir.relative_to(evidence_dir).as_posix()
        report.run_dir = label if label else "."
        reports.append(report)
    return reports


def render_report(reports: list[RunDriftReport]) -> str:
    if not reports:
        return "no run.jsonl files found under this evidence directory.\n"
    lines: list[str] = []
    for report in reports:
        lines.append(f"run: {report.run_dir}")
        if not report.steps:
            lines.append("  no step resolved a target in this run")
            lines.append("")
            continue
        for step in report.steps:
            if not step.has_rank_data:
                lines.append(f"  step {step.step_id}: no rank data (recorded before B5)")
                continue
            lines.append(
                f"  step {step.step_id}: recorded_rank={step.recorded_rank} "
                f"actual_rank={step.actual_rank} strategy={step.strategy} "
                f"drift={step.clause or 'none'}"
            )
        distribution = report.rank_distribution
        if distribution:
            dist_text = ", ".join(
                f"rank {rank}: {count}" for rank, count in sorted(distribution.items())
            )
            lines.append(f"  rank distribution: {dist_text}")
        no_data = report.no_rank_data_count
        if no_data:
            lines.append(f"  {no_data} step(s) with no rank data (recorded before B5)")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
