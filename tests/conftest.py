"""Session-scoped guard: the test suite must never leave a trace under evidence/.

Every EvidenceLogger a test constructs must pass base_dir=tmp_path; this fixture makes that a
checked invariant rather than a one-time observation. (The Phase 6 build found two stray
discovery-*/ directories under evidence/ that turned out to be manual CLI verification runs, not
test output -- both were three-line, screenshot-less, artifact-less refused-navigate runs,
verified and removed by hand. This fixture is what keeps that from silently recurring.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

_EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"


@pytest.fixture(scope="session", autouse=True)
def _evidence_dir_gains_nothing_from_the_test_suite() -> object:
    before = set(_EVIDENCE_DIR.iterdir()) if _EVIDENCE_DIR.exists() else set()
    yield
    after = set(_EVIDENCE_DIR.iterdir()) if _EVIDENCE_DIR.exists() else set()
    new_entries = sorted(p.name for p in after - before)
    assert not new_entries, (
        f"the test suite created new entries under evidence/: {new_entries}. Every "
        "EvidenceLogger constructed by a test must pass base_dir=tmp_path."
    )
