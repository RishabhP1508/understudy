"""EvidenceLogger: run.jsonl plus screenshots.

Every line is produced by Redactor.dumps(), so there is no unredacted write path
(ARCHITECTURE.md decision 10). Screenshots go through the same discipline: raw PNG bytes are
masked by safety.redact.redact_screenshot before they are ever written, and a screenshot that
cannot be safely masked is skipped, not written half-redacted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from understudy.models.observation import Observation
from understudy.safety.redact import Redactor, is_sensitive_element, redact_screenshot


class EvidenceLogger:
    def __init__(self, kind: str, run_id: str, base_dir: str | Path = "evidence") -> None:
        self.kind = kind
        self.run_id = run_id
        self.dir = Path(base_dir) / f"{kind}-{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor()

    def event(self, type: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "type": type, **fields}
        line = self.redactor.dumps(record)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def screenshot(
        self, surface: Any, step: int, observation: Observation | None = None
    ) -> Path | None:
        """Mask, then write, or refuse and log why. A no-op returning None if `surface` has no
        `screenshot_bytes` attribute, so the fake surfaces used in tests keep working without a
        real browser.

        `observation` must describe the SAME instant the screenshot pixels show (D7 in Phase 5):
        masking positions a box using bounds resolved from an observation, and a stale
        observation describes pixels that no longer match what is on screen, which paints the
        mask in the wrong place -- a leak, not merely a cosmetic bug. Callers take the screenshot
        immediately after taking the observation they pass here, before acting on it.
        """
        if not hasattr(surface, "screenshot_bytes"):
            return None
        if observation is None:
            self.event(
                "screenshot_skipped", step=step, reason="no observation given to mask against"
            )
            return None

        raw = surface.screenshot_bytes()
        sensitive = [element for element in observation.elements if is_sensitive_element(element)]
        if sensitive:
            fill_bounds = getattr(surface, "fill_bounds", None)
            if fill_bounds is None:
                self.event(
                    "screenshot_skipped",
                    step=step,
                    reason="surface cannot resolve element bounds to mask",
                )
                return None
            fill_bounds(sensitive)

        masked = redact_screenshot(raw, observation)
        if masked is None:
            self.event(
                "screenshot_skipped",
                step=step,
                reason="a sensitive element had no resolvable bounds; refusing to write a "
                "partially-masked screenshot",
            )
            return None

        path = self.dir / f"step-{step}.png"
        path.write_bytes(masked)
        return path
