"""build_capability: a separate pass over a written run.jsonl, producing a Capability.

This reads the evidence log rather than any live object, so recording never depends on the
discovery process still being in memory. Only ALLOWED `policy_decision` events become Steps: a
refused one never reached surface.act, so it never happened as far as the recorded flow is
concerned (Phase 5 renamed the event PolicyGate.dispatch logs from "dispatch" to
"policy_decision" and gave it an `allowed` field precisely so a refusal and an action are the same
event type, distinguished by that field, rather than needing two names). The harness's own
bootstrap navigate to the target (always the first ALLOWED policy_decision event) is represented
by target.entry_point instead, so it is not duplicated as a Step. Pruning and value
parameterization (turning "12345" into a named input) are Phase 8 concerns; this phase records the
literal values the run actually used.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from understudy import __version__
from understudy.models.artifact import Capability, Checkpoint, OutputSpec, Provenance, Step, Target
from understudy.surface.locator import TargetDescriptor


def build_capability(
    run_dir: Path,
    goal: str,
    target: str,
    run_id: str,
    model: str,
    capability_id: str,
    name: str,
) -> Capability:
    raw = (run_dir / "run.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]

    steps: list[Step] = []
    outputs: list[OutputSpec] = []
    success: Checkpoint | None = None
    first_dispatch_seen = False

    for event in events:
        event_type = event.get("type")
        if event_type == "policy_decision":
            decision = event.get("decision") or {}
            if decision.get("allowed") is not True:
                # A refused action was never dispatched to the surface; it cannot become a Step.
                continue
            if not first_dispatch_seen:
                # The harness's own bootstrap navigate to the target; target.entry_point
                # already records this, so it is not also recorded as a Step.
                first_dispatch_seen = True
                continue

            action = event.get("action") or {}
            context = event.get("context") or {}
            tool = context.get("tool")
            is_extract = tool == "extract"

            action_kind = "extract" if is_extract else action.get("kind", "")
            if is_extract:
                value: str | None = context.get("output_name")
            elif action.get("kind") == "navigate":
                value = action.get("url")
            else:
                value = action.get("text")

            target_desc: TargetDescriptor | None = None
            if context.get("target") is not None:
                target_desc = TargetDescriptor(**context["target"])

            steps.append(
                Step(
                    index=len(steps),
                    action=action_kind,
                    target=target_desc,
                    value=value,
                    rationale=context.get("rationale") or "",
                    postcondition=None,
                )
            )
            if is_extract and context.get("output_name"):
                outputs.append(
                    OutputSpec(
                        name=context["output_name"],
                        type="string",
                        description=context.get("rationale") or "",
                    )
                )
        elif event_type == "goal_verified":
            success = Checkpoint(**event["checkpoint"])

    if success is None:
        raise ValueError("run.jsonl has no goal_verified event; cannot record a capability")

    transcript_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return Capability(
        capability_id=capability_id,
        name=name,
        description=goal,
        target=Target(entry_point=target),
        inputs=[],
        outputs=outputs,
        steps=steps,
        success=success,
        provenance=Provenance(
            created_at=datetime.now(UTC).isoformat(),
            model=model,
            run_id=run_id,
            transcript_hash=transcript_hash,
            understudy_version=__version__,
        ),
    )
