"""InterventionStore: the file-backed rendezvous between the run process and the operator
console process (a separate local process, per CLAUDE.md -- no queue, no broker daemon, no
database). One JSON file per intervention id, under `evidence/interventions/` by default, holding
the request, the current `ControlToken` (once one is set), the full chain of `ControlTransition`s
that produced it (F3: both the runner's own moves and the operator console's), and the resolution
(once an operator or an expiry decides one).

Every write re-serializes the WHOLE record through `Redactor().dumps` against a LIVE
`InterventionRecord` model, never a pre-dumped plain dict: that is the only way the D4
STRUCTURAL/VALUE_CARRYING field marking (docs/adr/0012, safety/redact.py's own module docstring)
survives the round trip. Reading a record back in and writing it again (`set_token`, `resolve`)
re-redacts content that is already redacted from the first write -- a no-op in practice, not a
second, weaker pass, since R0-R2 are idempotent and R3 only ever replaces a whole matching string
with the same literal `[REDACTED]` it would already be.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# A directly-bound reference, not `import time` + `time.sleep(...)`: a test that monkeypatches
# `understudy.escalation.control.time.sleep` (to make SessionBroker.await_resolution's poll loop
# fast) mutates the SHARED `time` module object itself -- `time` has exactly one module instance
# across the whole process, so that patch would silently disable THIS retry's own real backoff
# too if it read `time.sleep` live. Binding the function once, at import time, keeps this retry
# genuinely real regardless of what a test does to `control.py`'s poll loop.
from time import monotonic as _real_monotonic
from time import sleep as _real_sleep

from pydantic import BaseModel, Field

from understudy.escalation.control import ControlToken, ControlTransition
from understudy.models.intervention import HumanAction, InterventionRequest, InterventionResolution
from understudy.safety.redact import Redactor

# Bounded, not unbounded: `_locked`'s own docstring already names a lock left behind by a crashed
# holder as this mechanism's one honest limitation, and an unbounded spin on that turns "one
# intervention id is stuck" into an indefinite hang with no output at all -- strictly harder to
# diagnose than a clear, named error. A few seconds is generous: every real hold under this lock
# is a single small file read-modify-write (`_read` + `_write`), never anything that blocks on an
# external actor (that wait -- SessionBroker.await_resolution -- happens entirely outside a
# `_locked` block).
_LOCK_ACQUIRE_TIMEOUT_S = 5.0


class InterventionRecord(BaseModel):
    """The full contents of one intervention's JSON file.

    `approval_consumed` is the one piece of state a one-shot risky-action approval genuinely
    needs of its own (escalation/control.py's `SessionBroker.consume_approval` docstring, B0):
    the grant itself is just `resolution.action_taken == "approved"`, already recorded here, so
    there is no separate "granted" flag to keep in sync with it -- only whether that grant has
    already been spent, which nothing else on this record expresses.
    """

    request: InterventionRequest
    token: ControlToken | None = None
    resolution: InterventionResolution | None = None
    approval_consumed: bool = False
    # The full custody chain (F3): every ControlTransition either process has ever made for this
    # intervention, oldest first -- appended by `set_token` below, inside the same lock that
    # writes the token itself, never a second write path.
    transitions: list[ControlTransition] = Field(default_factory=list)


class InterventionStore:
    def __init__(self, base_dir: str | Path = "evidence/interventions") -> None:
        self._dir = Path(base_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # A fresh Redactor, not the run's own EvidenceLogger.redactor: this store can be read and
        # written by a second process (the operator console) that never shared a redactor
        # instance with the run in the first place. R2 (named PII patterns) and R3 (a
        # credential-shaped literal, which is how the project's own sentinel is caught) are both
        # stateless pattern matches, so this instance still catches them; only R1 (a value
        # explicitly registered via `register_secret` elsewhere) needs a shared registry, and
        # nothing this project puts into an InterventionRequest depends on that -- an already-
        # redacted `context`/`what_it_observed` string is exactly what the caller is expected to
        # hand this store to begin with.
        self._redactor = Redactor()

    def _path(self, intervention_id: str) -> Path:
        return self._dir / f"{intervention_id}.json"

    def _read_text(self, path: Path) -> str:
        """`path.read_text()`, retried a few times with a brief real backoff on the SAME
        Windows-only transient `PermissionError` `_write`'s own retry loop guards against: a
        reader's `open()` can be refused for the instant another writer's `os.replace` is landing
        on this same path -- measured directly in this project's own test suite, from the READER
        side this time, not just the writer side `_write` already covers.
        """
        for attempt in range(10):
            try:
                return path.read_text(encoding="utf-8")
            except PermissionError:
                if attempt == 9:
                    raise
                _real_sleep(0.01)
        raise AssertionError("unreachable")  # the loop above always returns or raises

    def _read(self, intervention_id: str) -> InterventionRecord | None:
        path = self._path(intervention_id)
        if not path.exists():
            return None
        return InterventionRecord.model_validate(json.loads(self._read_text(path)))

    def _write(self, intervention_id: str, record: InterventionRecord) -> None:
        # Written to a UNIQUELY-NAMED temp file in the same directory, then renamed into place,
        # never written directly to the final path and never to a temp name shared across
        # writers: this file is the rendezvous between two independent processes/threads (the
        # run and the operator console, or -- measured directly in this project's own test suite
        # -- a run thread transitioning the control token at the same moment a test's background
        # "operator" thread resolves the same intervention), and both a plain write_text() and a
        # temp name shared across concurrent writers are unsafe here. A plain write_text() is not
        # atomic -- a concurrent reader can observe a torn (partially-written) file mid-write,
        # which measurably produced a JSONDecodeError. A SHARED temp name measurably produced a
        # PermissionError instead, from two writers opening the same temp file for writing at
        # once. os.replace (Path.replace) is atomic on both POSIX and Windows once each writer
        # has its own temp file, so a reader only ever sees one complete record or another, never
        # a partial one, and "last rename wins" is the only race left -- the ordinary, accepted
        # shape of any unsynchronized multi-writer file, not a corruption risk.
        path = self._path(intervention_id)
        tmp_path = path.with_suffix(f".json.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(self._redactor.dumps(record, indent=2), encoding="utf-8")
        # Windows-only quirk, also measured directly in this project's own test suite: a
        # concurrent reader can hold the destination file open (via its own short-lived
        # read_text()) at the exact instant of the rename, and Win32's MoveFileEx then refuses
        # with a transient PermissionError -- POSIX rename has no such restriction. A few retries
        # with a brief real backoff clears it (the other side's read is a handful of milliseconds
        # at most); this is not a fixed sleep guessing at application timing, it is a bounded
        # retry against a known, transient OS-level file-locking condition.
        for attempt in range(10):
            try:
                tmp_path.replace(path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                _real_sleep(0.01)

    @contextmanager
    def _locked(self, intervention_id: str) -> Iterator[None]:
        """A read-modify-write mutex, one lockfile per intervention id -- `set_token`/`resolve`/
        `consume_approval` all read the WHOLE record, change ONE field, and write the whole
        record back, and without this, two concurrent callers (measured directly in this
        project's own test suite: a run thread transitioning the control token at the same
        moment a test's background "operator" thread resolved the same intervention) can each
        read the record before the other's write lands, then each write their own stale copy
        back -- a classic lost update, e.g. the run's own token write silently vanishing under
        the operator's resolution write. `os.O_CREAT | O_EXCL` is an atomic "create if it does
        not already exist" on both POSIX and Windows, so it needs no platform-specific
        `fcntl`/`msvcrt` call and no third-party dependency -- the lockfile itself IS the mutex.
        A lock left behind by a process that crashed mid-hold is this mechanism's one honest
        limitation (ponytail: a stale lockfile blocks only that ONE intervention id forever;
        the fix is a lease/expiry on the lock file, not built because nothing in this project
        holds one across a crash boundary today) -- bounded by `_LOCK_ACQUIRE_TIMEOUT_S` below
        so that limitation surfaces as a named `TimeoutError`, never a silent indefinite hang.

        Retries on `PermissionError` here, not just the `FileExistsError` the O_EXCL contract
        documents, because Windows genuinely raises the former for this same "someone else is
        touching this path right now" condition: a concurrency stress test in this project (many
        threads calling `set_token`/`consume_approval` on one intervention id with no delay
        between calls) reproduced `os.open(..., O_CREAT | O_EXCL)` itself raising `PermissionError`
        reliably, in under a second, when this create races another thread's create of the SAME
        lock path or the previous holder's `unlink` of it -- an NTFS create/delete metadata race,
        not a genuine ACL denial (this directory is created by `mkdir()` in `__init__`, and every
        `_write`/`_read_text` against files inside it already succeeds). Treating it as anything
        other than contention would let that race crash `set_token`/`resolve`/`consume_approval`
        outright instead of retrying, which is the bug this comment exists to prevent recurring.
        A genuine permission problem (not this race) retries the same way but never clears, so it
        still surfaces -- correctly -- as the bounded timeout below, not a silent hang.
        """
        lock_path = self._dir / f"{intervention_id}.lock"
        deadline = _real_monotonic() + _LOCK_ACQUIRE_TIMEOUT_S
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except (FileExistsError, PermissionError) as exc:
                if _real_monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {lock_path} within "
                        f"{_LOCK_ACQUIRE_TIMEOUT_S}s (last error: {exc!r}); held by another "
                        "writer, or left behind by one that crashed mid-hold"
                    ) from exc
                _real_sleep(0.005)
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def create(self, request: InterventionRequest) -> None:
        self._write(request.id, InterventionRecord(request=request))

    def get(self, intervention_id: str) -> InterventionRecord | None:
        return self._read(intervention_id)

    def set_token(
        self,
        intervention_id: str,
        token: ControlToken,
        transition: ControlTransition | None = None,
    ) -> None:
        """Write the new token and, when `transition` is given, append it to the record's own
        custody chain -- both inside this ONE lock acquisition, so a reader never observes a
        token update with a chain that has not caught up yet. `transition` defaults to None so a
        direct low-level caller that only wants to set up a token for a test (no real transition
        happened) leaves the chain untouched, exactly as before this parameter existed;
        `SessionBroker.transition` (escalation/control.py) is the one caller that always supplies
        one, for every real state change either process makes.
        """
        with self._locked(intervention_id):
            record = self._read(intervention_id)
            if record is None:
                raise KeyError(
                    f"no intervention record for id {intervention_id!r}; call create() first"
                )
            record.token = token
            if transition is not None:
                record.transitions.append(transition)
            self._write(intervention_id, record)

    def resolve(self, intervention_id: str, resolution: InterventionResolution) -> None:
        with self._locked(intervention_id):
            record = self._read(intervention_id)
            if record is None:
                raise KeyError(
                    f"no intervention record for id {intervention_id!r}; call create() first"
                )
            record.resolution = resolution
            self._write(intervention_id, record)

    def attach_human_actions(self, intervention_id: str, actions: list[HumanAction]) -> None:
        """Round H (H3): the narrow read-modify-write this needs, done HERE, inside the SAME lock
        `resolve()` itself uses -- never a read-modify-write performed by the caller
        (`escalation/control.py`'s `SessionBroker.escalate()`), which would race a concurrent
        writer with no lock held across its own read and write. Reads the record FRESH under the
        lock (rather than trusting whatever `InterventionResolution` the caller already has in
        memory, which may be a moment stale), and updates only the one field a human's drained
        actions belong on -- `evidence/interventions/<intervention_id>.json` is where a reviewer
        looks for what a human did (R6), and this is the one path that gets them there. A no-op
        (never an error) when there is no resolution yet to attach onto: `escalate()` only calls
        this once a resolution has actually arrived (its own expiry branch builds the
        `human_actions` list straight into a fresh `InterventionResolution` instead, since there
        is nothing stored yet to attach to).
        """
        with self._locked(intervention_id):
            record = self._read(intervention_id)
            if record is None or record.resolution is None:
                return
            record.resolution = record.resolution.model_copy(
                update={"human_actions": actions}
            )
            self._write(intervention_id, record)

    def consume_approval(self, intervention_id: str) -> bool:
        """True the FIRST time this intervention's approval is checked and it was actually
        granted (a stored resolution with `action_taken == "approved"`), False every call after
        that (including if it was never granted, or the record does not exist at all).

        B0: this used to be an in-memory `set` on `SessionBroker`, which only works when the
        grantor (the operator console) and the consumer (`PolicyGate.dispatch`, inside the run)
        are literally the same Python object -- true in a single-process test, never true in
        practice, since the operator console is a SEPARATE process (CLAUDE.md). Reading and
        writing `approval_consumed` through this store, the one thing both processes actually
        share, is what makes one-shot survive that boundary, and a restart of either process too.
        """
        with self._locked(intervention_id):
            record = self._read(intervention_id)
            if record is None or record.resolution is None:
                return False
            if record.resolution.action_taken != "approved" or record.approval_consumed:
                return False
            record.approval_consumed = True
            self._write(intervention_id, record)
            return True

    def list_open(self) -> list[InterventionRequest]:
        """Every intervention with no resolution yet -- the operator console's own "what needs
        me" queue (Phase 10 task B)."""
        open_requests: list[InterventionRequest] = []
        for path in self._dir.glob("*.json"):
            record = InterventionRecord.model_validate(json.loads(self._read_text(path)))
            if record.resolution is None:
                open_requests.append(record.request)
        return open_requests
