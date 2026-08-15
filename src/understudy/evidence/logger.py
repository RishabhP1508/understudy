"""EvidenceLogger: run.jsonl, screenshots, DOM/a11y snapshots, a trace, and the final result.

Every line of run.jsonl is a `RunEvent`, built through this one model and written through
`Redactor.dumps` -- so "every line validates against the event schema" and "there is no
unredacted write path" (ARCHITECTURE.md decision 10) are both true by construction, not by
inspection. Screenshots, the DOM snapshot, and the transcript go through the same Redactor.

`rationale` is the point of R5 ("a structured log of what the agent did AND WHY... every action
event carries a rationale"): `RunEvent` enforces it is present and non-empty on every
`type == "act"` event, so an action without a stated reason cannot be logged at all -- in
discovery OR replay. In discovery it carries the model's own stated reason, read straight off the
tool call, never invented after the fact; agent/loop.py separately rejects a live model turn that
tries to pass the redaction sentinel `"[REDACTED]"` itself as its rationale, before it ever
reaches here. In replay there is no model, so it carries the step's recorded rationale from the
artifact -- which is what lets a replay log still explain itself with no model present, EVEN when
that recorded rationale is itself `"[REDACTED]"` because Phase 2's redaction rule once replaced it
whole (the real artifact in `artifacts/` has exactly this on one step; see the model validator
below for why this schema does not also reject that literal).

Directory layout, under `<base_dir>/<run_kind>-<run_id>/`:

    run.jsonl         one RunEvent per line, append-only, monotonically increasing `seq`
    result.json       the final structured result
    steps/NNN_before.png, NNN_after.png    masked screenshots
    dom/NNN.html      redacted DOM snapshot, failure only
    a11y/NNN.json     redacted Observation snapshot, failure only
    trace.zip         Playwright trace, started at run open, kept ONLY on failure
    transcript.jsonl  redacted model messages, discovery runs only
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, model_validator

from understudy.models.observation import Observation
from understudy.safety.redact import Redactor, is_sensitive_element, redact_screenshot


class RunEvent(BaseModel):
    """One line of run.jsonl. `ts`, `seq`, and `type` are the only fields every event needs;
    everything else is optional and defaults to `None` so a lifecycle marker (`run_start`,
    `run_end`, ...) and a dispatched action can share one schema without either padding the other
    out with meaningless values.

    `phase` is fixed to the five phases of the agent loop (observe, decide, act, verify,
    escalate) and stays `None` for events that are not one of those five -- a lifecycle marker,
    a rejected turn, a screenshot note -- rather than forcing a sixth, meaningless phase value
    into existence for them.

    Extra keys beyond the fixed set are allowed (`model_config`, below): a `run_start` event's
    `goal`/`target`/`model`, or an `act` event's `context` (tool name, resolved target
    descriptor, output name -- everything about *what* was proposed that is not one of the named
    fields below), ride along as-is. The named fields are the ones every consumer of this schema
    can rely on across every event type; the rest is event-specific detail.
    """

    model_config = {"extra": "allow"}

    ts: str
    seq: int
    type: str
    phase: Literal["observe", "decide", "act", "verify", "escalate"] | None = None
    step_id: int | None = None
    observation_digest: str | None = None
    proposed_action: dict[str, Any] | None = None
    rationale: str | None = None
    policy_decision: dict[str, Any] | None = None
    dispatched: bool | None = None
    act_result: str | None = None
    checkpoint_eval: dict[str, Any] | None = None
    outcome_match: str | None = None
    resolution: dict[str, Any] | None = None
    duration_ms: float | None = None
    tokens: dict[str, Any] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _act_events_carry_a_real_rationale(self) -> RunEvent:
        # R5: "every action event carries a rationale". Enforced here, not just by convention, so
        # an action without a stated reason cannot be written to the log at all.
        #
        # This deliberately does NOT also reject the literal "[REDACTED]": measured against the
        # real artifact in artifacts/ (recorded from evidence/discovery-3348784c8a88/, the one
        # genuine discovery run this project's non-negotiable requirement depends on), step 1's
        # own rationale IS that literal string -- the model's real reasoning for typing the
        # password happened to be credential-shaped enough that Phase 2's redaction rule replaced
        # it whole, and that value is now permanently part of a real artifact this project must
        # never edit. Rejecting it here would mean this evidence can never replay again. A model
        # producing that literal live, in discovery, is instead caught one layer up
        # (agent/loop.py), where "was this rationale ever real" can still be judged against a
        # live model turn rather than a historical artifact.
        if self.type == "act":
            rationale = (self.rationale or "").strip()
            if not rationale:
                raise ValueError(
                    "an 'act' event must carry a non-empty rationale (R5): the evidence log has "
                    "to explain what the agent did AND WHY"
                )
        return self


class EvidenceLogger:
    def __init__(self, run_id: str, run_kind: str, base_dir: str | Path = "evidence") -> None:
        self.run_id = run_id
        self.run_kind = run_kind
        self.dir = Path(base_dir) / f"{run_kind}-{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor()
        self._seq = 0
        self._trace_started = False

    # -------------------------------------------------------------------------------- run.jsonl

    def event(self, type: str, **fields: Any) -> None:
        """Build one RunEvent (assigning the next `seq`), then write it through the Redactor.
        Raises if `type == "act"` and the rationale rule is violated -- see RunEvent above."""
        self._seq += 1
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "seq": self._seq,
            "type": type,
            **fields,
        }
        run_event = RunEvent.model_validate(payload)
        line = self.redactor.dumps(run_event)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ------------------------------------------------------------------------------- screenshots

    def screenshot(
        self,
        surface: Any,
        step_id: int,
        when: Literal["before", "after"],
        observation: Observation | None = None,
    ) -> Path | None:
        """Mask, then write, or refuse and log why. A no-op returning None if `surface` has no
        `screenshot_bytes` attribute, so the fake surfaces used in tests keep working without a
        real browser.

        `before` must describe the observation the decision to act was made from; `after` must
        describe a FRESH observation taken once the action has run (Phase 5's D7, extended): the
        action just changed the page, so masking `after` from the `before` observation would
        position a box over pixels that no longer show what it thinks they show -- a leak, not a
        cosmetic bug. Callers pay one extra observe() per step for this; see the callers for
        where that cost is taken.
        """
        if not hasattr(surface, "screenshot_bytes"):
            return None
        if observation is None:
            self.event(
                "screenshot_skipped", step_id=step_id, note="no observation given to mask against"
            )
            return None

        raw = surface.screenshot_bytes()
        sensitive = [element for element in observation.elements if is_sensitive_element(element)]
        if sensitive:
            fill_bounds = getattr(surface, "fill_bounds", None)
            if fill_bounds is None:
                self.event(
                    "screenshot_skipped",
                    step_id=step_id,
                    note="surface cannot resolve element bounds to mask",
                )
                return None
            fill_bounds(sensitive)

        masked = redact_screenshot(raw, observation)
        if masked is None:
            self.event(
                "screenshot_skipped",
                step_id=step_id,
                note="a sensitive element had no resolvable bounds; refusing to write a "
                "partially-masked screenshot",
            )
            return None

        steps_dir = self.dir / "steps"
        steps_dir.mkdir(parents=True, exist_ok=True)
        path = steps_dir / f"{step_id:03d}_{when}.png"
        path.write_bytes(masked)
        return path

    # ------------------------------------------------------------------------ failure evidence

    def capture_failure(
        self, surface: Any, step_id: int, observation: Observation
    ) -> list[str]:
        """DOM snapshot, accessibility snapshot, and the kept trace -- R5's "at least one richer
        signal on failure". Returns paths relative to this run's own directory, for
        `HardFailure.evidence_refs`. `surface.dom_snapshot` and tracing are optional (hasattr
        guards), so a fake surface in a test can exercise the dom/a11y half with no browser.
        """
        refs: list[str] = []

        dom_snapshot = getattr(surface, "dom_snapshot", None)
        if dom_snapshot is not None:
            try:
                html = dom_snapshot()
            except Exception as exc:
                self.event("dom_snapshot_failed", step_id=step_id, note=str(exc))
            else:
                dom_dir = self.dir / "dom"
                dom_dir.mkdir(parents=True, exist_ok=True)
                path = dom_dir / f"{step_id:03d}.html"
                # redact_text, not dumps(): this is a raw HTML string, not a BaseModel/dict. R1
                # (registered secrets) and R2 (PII patterns) still apply within it; R3 (whole-
                # string credential-shaped literal) is a no-op on a whole page of markup, which
                # always contains whitespace.
                path.write_text(self.redactor.redact_text(html), encoding="utf-8")
                # evidence_refs is a result-contract field a calling agent consumes, not a local
                # filesystem path -- always "/"-separated regardless of host OS, so the same run
                # produces identical result.json content on Windows and Linux, and a consumer can
                # join it against a POSIX base path without translating separators first.
                refs.append(path.relative_to(self.dir).as_posix())

        a11y_dir = self.dir / "a11y"
        a11y_dir.mkdir(parents=True, exist_ok=True)
        a11y_path = a11y_dir / f"{step_id:03d}.json"
        a11y_path.write_text(self.redactor.dumps(observation, indent=2), encoding="utf-8")
        refs.append(a11y_path.relative_to(self.dir).as_posix())

        trace_path = self.stop_trace(surface, keep=True)
        if trace_path is not None:
            refs.append(trace_path.relative_to(self.dir).as_posix())

        return refs

    # ------------------------------------------------------------------------------------ trace

    def start_trace(self, surface: Any) -> None:
        """Started at run open, unconditionally, so a run that turns out to fail already has one
        running -- there is no way to start tracing retroactively once something has gone wrong.
        A no-op if `surface` has no `tracing` (a fake surface in a test, or a policy-less probe).
        """
        tracing = getattr(surface, "tracing", None)
        if tracing is None:
            return
        try:
            tracing.start(screenshots=True, snapshots=True)
            self._trace_started = True
        except Exception as exc:
            self.event("trace_start_failed", note=str(exc))

    def stop_trace(self, surface: Any, *, keep: bool) -> Path | None:
        """Idempotent: a second call (e.g. capture_failure already stopped and kept it, and the
        caller's own cleanup calls this again) is a harmless no-op. Discarded on success
        (`tracing.stop()` with no path); written to `trace.zip` and kept on failure."""
        if not self._trace_started:
            return None
        tracing = getattr(surface, "tracing", None)
        if tracing is None:
            self._trace_started = False
            return None
        self._trace_started = False
        try:
            if keep:
                path = self.dir / "trace.zip"
                tracing.stop(path=str(path))
                return path
            tracing.stop()
            return None
        except Exception as exc:
            self.event("trace_stop_failed", note=str(exc))
            return None

    # ------------------------------------------------------------------------------- transcript

    def transcript_turn(self, message: dict[str, Any]) -> None:
        """Append one redacted line to transcript.jsonl. Discovery only -- replay has no model
        turns to record. Written incrementally, one call per turn, so a run that crashes mid-way
        still has every turn it completed on disk."""
        line = self.redactor.dumps(message)
        with (self.dir / "transcript.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ---------------------------------------------------------------------------------- result

    def write_result(self, result: BaseModel) -> Path:
        path = self.dir / "result.json"
        path.write_text(self.redactor.dumps(result, indent=2), encoding="utf-8")
        return path
