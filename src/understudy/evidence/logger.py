"""EvidenceLogger: run.jsonl plus screenshots.

Every line is produced by Redactor.dumps(), so there is no unredacted write path
(ARCHITECTURE.md decision 10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from understudy.safety.redact import Redactor


class EvidenceLogger:
    def __init__(self, kind: str, run_id: str, base_dir: str | Path = "evidence") -> None:
        self.kind = kind
        self.run_id = run_id
        self.dir = Path(base_dir) / f"{kind}-{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redactor = Redactor()

    def event(self, type: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "type": type, **fields}
        line = self._redactor.dumps(record)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def screenshot(self, surface: Any, step: int) -> Path | None:
        """Raw PNG bytes this phase; Phase 5 masks them before the bytes are written.

        A no-op returning None if `surface` has no `screenshot` attribute, so the fake surfaces
        used in tests/test_phase2.py keep working without a real browser.
        """
        if not hasattr(surface, "screenshot"):
            return None
        path = self.dir / f"step-{step}.png"
        surface.screenshot(path)
        return path
