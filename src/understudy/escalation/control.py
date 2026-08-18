"""ControlToken / SessionBroker: who holds the browser session right now, and the ONLY mechanism
that lets `PolicyGate.dispatch` (safety/policy.py) refuse to act while a human is, or is about to
be, in charge (R6).

FOUR states, not two, because the two transient ones each genuinely refuse dispatch on their own,
for a different reason than the two settled ones do -- see `ControlState`'s own docstring.

The rendezvous between the run process and the operator console process (a separate local
process, per CLAUDE.md's "single process, plus one local web process" rule -- no queue, no
broker daemon, no database) is `InterventionStore`, a file under `evidence/interventions/`.
`SessionBroker.state()` always reads through the store rather than trusting an in-memory copy,
so a state change either process makes is visible to the other on its next read.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from understudy.evidence.logger import EvidenceLogger
from understudy.models.intervention import HumanAction, InterventionRequest, InterventionResolution
from understudy.surface.base import Surface

if TYPE_CHECKING:
    # Deferred: escalation/store.py imports ControlToken from this module at runtime (its
    # InterventionRecord embeds one), so this module cannot import store.py at runtime too
    # without a cycle. `from __future__ import annotations` means the annotation below is never
    # evaluated at import time, only by a type checker, which does resolve this import -- the
    # same cycle-breaking pattern safety/risk.py already uses for safety/policy.py's `Policy`.
    from understudy.escalation.store import InterventionStore


class ControlState(StrEnum):
    """Four states. Only AUTOMATION permits `PolicyGate.dispatch` to proceed
    (safety/policy.py); the other three each refuse for a genuinely different reason, which is
    why there are four states and not two ("automation" / "human"):

    - AUTOMATION: the runner holds control. THE ONLY state in which an action may be dispatched.
    - PENDING_HANDOFF: the runner raised an intervention and stopped. Nobody is acting -- the
      human has not accepted control yet.
    - HUMAN: the operator holds control and is driving the real, visible browser window
      directly.
    - PENDING_RESUME: the human has handed control back, but the runner has not yet re-observed
      the surface and decided how to continue. Still nobody acting.
    """

    AUTOMATION = "automation"
    PENDING_HANDOFF = "pending_handoff"
    HUMAN = "human"
    PENDING_RESUME = "pending_resume"


class ControlTransition(BaseModel):
    """One state change in an intervention's own custody chain: who moved the token, from what,
    to what, when, and why. Appended to `InterventionRecord.transitions`
    (escalation/store.py's `InterventionStore.set_token`) inside the SAME read-modify-write lock
    that writes the new token, so the record carries the full chain regardless of which PROCESS
    made the move.

    This is distinct from run.jsonl's own `control_transition` event (`SessionBroker.transition`
    below still logs that too, wherever a logger exists): run.jsonl is the runner's own account
    of what IT did, so the operator console's half of a handoff -- made in a separate process,
    with no run logger of its own -- never appeared in it. This list is the one place both halves
    of a handoff always land, which is what lets "who holds control, and who moved it there" be
    read off the intervention record alone rather than reconstructed from two files.
    """

    from_state: ControlState
    to_state: ControlState
    actor: str
    reason: str
    at: str


class ControlToken(BaseModel):
    """Who holds control, and (via `intervention_id`) which intervention -- if any -- produced
    this state. `intervention_id` also carries a one-shot approval forward: once an operator
    resolves an intervention with "approved" and transitions the token back to AUTOMATION, the
    resulting token's `intervention_id` still names that intervention, which is exactly what lets
    `PolicyGate.dispatch` find the right approval to consume (safety/policy.py, step 6)."""

    state: ControlState
    holder: str
    intervention_id: str | None = None
    updated_at: str


class IllegalTransition(Exception):
    """Raised instead of silently allowing an out-of-order state change (e.g. AUTOMATION
    straight to HUMAN, skipping PENDING_HANDOFF)."""

    def __init__(self, from_state: ControlState, to_state: ControlState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal control transition: {from_state.value} -> {to_state.value}")


class ControlHeld(Exception):
    """Raised by `require_automation()` (the token is not AUTOMATION) and by `session()` (the
    caller is not the current holder). Carries the current state and holder so a caller can
    report exactly why it was refused."""

    def __init__(self, state: ControlState, holder: str) -> None:
        self.state = state
        self.holder = holder
        super().__init__(f"control is held by {holder!r} in state {state.value!r}")


# The explicit allowed-transition map. `to == AUTOMATION` is legal from ANY state without
# appearing here (see `transition` below) -- "the runner always takes the token back to
# terminate" applies uniformly, whether that is PENDING_HANDOFF/PENDING_RESUME resolving
# normally or HUMAN itself timing out with no handback. Every other edge must be listed
# explicitly, so an unlisted one is illegal by construction rather than by omission.
_ALLOWED_TRANSITIONS: dict[ControlState, frozenset[ControlState]] = {
    ControlState.AUTOMATION: frozenset({ControlState.PENDING_HANDOFF}),
    # PENDING_HANDOFF -> AUTOMATION: an operator approves or rejects without a full handoff (no
    # HUMAN state at all) -- the "approve without taking the window" path.
    ControlState.PENDING_HANDOFF: frozenset({ControlState.HUMAN, ControlState.AUTOMATION}),
    ControlState.HUMAN: frozenset({ControlState.PENDING_RESUME}),
    ControlState.PENDING_RESUME: frozenset({ControlState.AUTOMATION}),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SessionBroker:
    """Owns the browser session (a `Surface`) and the control token for one run.

    `intervention_id` is a plain public attribute, not a `transition()` argument: the caller (the
    code that detects a stuck run, or the operator console resolving one) sets it once, right
    after `InterventionStore.create(request)` establishes the record, and every subsequent
    `transition()` call for that escalation cycle reads it from here. Keeping it off
    `transition()`'s own signature is deliberate -- `transition(to, actor, reason)` is the whole
    call shape a caller needs for every ordinary state change, and a run can raise more than one
    intervention across its lifetime, so this attribute is reassigned each time a new one is
    created rather than fixed at construction.
    """

    def __init__(
        self,
        surface: Surface,
        store: InterventionStore,
        run_id: str,
        logger: EvidenceLogger | None = None,
        holder: str = "runner",
    ) -> None:
        self._surface = surface
        self._store = store
        self._run_id = run_id
        self._logger = logger
        self.intervention_id: str | None = None
        # The resting AUTOMATION token, before any intervention has ever been raised this run --
        # nothing to read from the store yet, since nothing else needs to observe this run's
        # control state before an intervention exists to synchronize on.
        self._resting_token = ControlToken(
            state=ControlState.AUTOMATION,
            holder=holder,
            intervention_id=None,
            updated_at=_now_iso(),
        )

    def state(self) -> ControlToken:
        """The CURRENT token. Reads through the store whenever an intervention has ever been
        attached (`self.intervention_id` is set), so a transition the OPERATOR process just made
        (approve, take control, hand back) is visible here on this process's very next call --
        the file is the rendezvous, not this object's own memory."""
        if self.intervention_id is not None:
            record = self._store.get(self.intervention_id)
            if record is not None and record.token is not None:
                return record.token
        return self._resting_token

    def transition(self, to: ControlState, actor: str, reason: str) -> ControlToken:
        """Validate `to` against the current state's allowed-transition set, write the new token
        through the store (or in memory, before any intervention exists), and log a
        `control_transition` event. Raises `IllegalTransition` rather than silently allowing an
        out-of-order move."""
        current = self.state()
        allowed = to == ControlState.AUTOMATION or to in _ALLOWED_TRANSITIONS.get(
            current.state, frozenset()
        )
        if not allowed:
            raise IllegalTransition(current.state, to)

        now = _now_iso()
        new_token = ControlToken(
            state=to,
            holder=actor,
            intervention_id=self.intervention_id,
            updated_at=now,
        )
        if self.intervention_id is not None:
            # The same `now` on both the token's `updated_at` and the transition's own `at`:
            # one moment, not two separate clock reads that could disagree by a few microseconds.
            transition_record = ControlTransition(
                from_state=current.state, to_state=to, actor=actor, reason=reason, at=now
            )
            self._store.set_token(self.intervention_id, new_token, transition=transition_record)
        else:
            self._resting_token = new_token

        if self._logger is not None:
            self._logger.event(
                "control_transition",
                from_state=current.state.value,
                to_state=to.value,
                actor=actor,
                reason=reason,
            )
        return new_token

    def require_automation(self) -> None:
        """Raise `ControlHeld` unless the token is AUTOMATION. `PolicyGate.dispatch` calls this
        as its very first check, before anything about the proposed action is even inspected."""
        token = self.state()
        if token.state != ControlState.AUTOMATION:
            raise ControlHeld(token.state, token.holder)

    def session(self, caller: str) -> Surface:
        """The `Surface`, but only to whoever the current token names as `holder`.

        A sync Playwright `Page` is bound to the thread that created it, so a REAL human takes
        control through the visible Chromium window itself, not through this accessor -- there
        is no cross-process way to hand a live Playwright object to the operator console's own
        process. This exists for a same-thread caller instead: an integration test driving both
        sides in one process, and the runner's own re-observation once it has transitioned back
        to AUTOMATION after a handback.
        """
        token = self.state()
        if token.holder != caller:
            raise ControlHeld(token.state, token.holder)
        return self._surface

    def grant_approval(self, intervention_id: str, resolved_by: str = "operator") -> None:
        """Record that a human approved exactly ONE dispatch of the risky action tied to
        `intervention_id`, without granting blanket permission for anything else this run does
        afterward.

        B0: the grant IS the operator's resolution (`action_taken == "approved"`) -- there is no
        separate "granted" flag to invent and keep in sync with it, because that flag would just
        be a second, weaker copy of the same fact this store already durably records
        (InterventionStore.consume_approval's own docstring). This method exists as a small,
        idempotent convenience for a caller (this broker's own one-shot-approval test, or a
        caller that never routed through the operator console's own `/approve` endpoint) that
        wants to grant an approval without constructing an `InterventionResolution` by hand; it
        is a no-op once a resolution already exists, so calling it after the operator console has
        already resolved the intervention some other way changes nothing.
        """
        record = self._store.get(intervention_id)
        if record is None:
            raise KeyError(
                f"no intervention record for id {intervention_id!r}; call create() first"
            )
        if record.resolution is None:
            self._store.resolve(
                intervention_id,
                InterventionResolution(
                    resolved_by=resolved_by,
                    action_taken="approved",
                    human_actions=[],
                    notes="",
                    resolved_at=_now_iso(),
                ),
            )

    def consume_approval(self, intervention_id: str) -> bool:
        """True the first time this intervention's approval is checked, False every time after
        (including if it was never granted). Consumed whether or not the action that follows
        actually succeeds -- `PolicyGate.dispatch` calls this before `surface.act()` runs, and a
        one-shot approval authorizes one ATTEMPT, not unlimited retries.

        Delegates to `InterventionStore.consume_approval`, which owns the read-check-write of the
        durable `approval_consumed` bit (B0) -- there is no broker-local state left to check here
        at all, which is the point: a SECOND `SessionBroker` instance, in a second process,
        reading through the same store directory, sees exactly the same answer.
        """
        return self._store.consume_approval(intervention_id)

    def _drain_human_actions(self) -> list[HumanAction]:
        """`getattr`, not a required capability: this broker is constructed with the `Surface`
        PROTOCOL, and plenty of callers (most of this project's own test suite) hand it a minimal
        fake with no capture at all -- the SAME optional-capability convention `urls`/
        `screenshot_bytes`/`fill_bounds`/`tracing`/`dom_snapshot` already use (surface/base.py's
        own docstring). This is NOT the silent-degrade shape that used to live in agent/loop.py's
        three call sites: those always held a real, concrete `WebSurface` (both execution paths
        construct one directly) and degraded anyway, hiding that nothing was ever wired up. Here,
        a genuine second class of caller (a fake surface in a test) legitimately has no capture,
        and "nothing happened" is the honest, correct answer for it.
        """
        drain = getattr(self._surface, "drain_human_actions", None)
        return drain() if drain is not None else []

    def _safe_drain_human_actions(self, logger: EvidenceLogger | None) -> list[HumanAction]:
        """The ONE guarded wrapper around `_drain_human_actions()`, used at BOTH drain sites in
        `escalate()` below (the discard before blocking, and the keep after resolution) -- one
        guarded helper rather than a guard duplicated at each call site, for the same reason
        `escalate()` itself is one shared entry point rather than a sequence copied into both
        runners.

        Round J: `WebSurface.drain_human_actions` (surface/web.py) now does real work -- it reads
        a live `sessionStorage` buffer that a genuine human's LAST action, if it navigated, can
        race -- and a real live handoff hit exactly that race and crashed the whole run over a
        read of evidence that was never the thing blocking progress. The human's captured actions
        are EVIDENCE, not the run's own correctness; losing them is not a reason to lose the run,
        and a human who has just done manual work in a live browser must not have that work
        discarded because the log of it could not be read.

        This is deliberately the ONE broad `except Exception` in this module, and it is NOT the
        silent-degrade shape ponytail deleted from replay/recovery.py in Phase 9 (ARCHITECTURE.md
        decision 76) or the one round H already refused to reintroduce here (this class's own
        `consume_approval` docstring) -- both of those returned a normal-looking result for work
        that never happened, with no trace it had failed. The difference here is that this
        REPORTS the failure, in the evidence log (`human_action_drain_failed`, carrying the
        error), rather than reporting success for a drain that did not happen. A future reader
        should not "clean this up" into either extreme: neither swallowing it back to silence,
        nor letting it propagate and take the whole escalation down with it.
        """
        try:
            return self._drain_human_actions()
        except Exception as exc:  # noqa: BLE001 - deliberate; see this method's own docstring
            if logger is not None:
                logger.event("human_action_drain_failed", error=str(exc))
            return []

    def escalate(
        self, request: InterventionRequest, logger: EvidenceLogger | None = None
    ) -> InterventionResolution | None:
        """THE one shared escalation entry point (Phase 10 task C0): create the intervention
        record, transition AUTOMATION -> PENDING_HANDOFF, log `escalation_raised`, then block
        until an operator resolves it or its own `expires_at` passes. Both execution paths
        (agent/loop.py, replay/engine.py) call this rather than each keeping its own copy of the
        sequence -- ARCHITECTURE.md decisions 27 and 37 both record this project already having
        been bitten by exactly that kind of duplication once.

        Round H: this is also the ONE place that drains the surface's captured human actions,
        for the SAME reason it is the one place the sequence above lives -- two call sites that
        must both remember to drain is the exact trap that left `drain_human_actions` called from
        nowhere but a test for six rounds. Discard happens immediately below, before the block:
        whatever the AUTOMATION side itself just captured (surface/web.py's capture is installed
        unconditionally, `WebSurface.__init__`) is not a human's, and every state but AUTOMATION
        refuses dispatch (safety/policy.py's `PolicyGate.dispatch`), so nothing the automation
        itself does can land in the window that follows. Keep happens once a real resolution
        arrives: whatever accumulated in that window is attributed to the human and persisted onto
        the STORED resolution (`InterventionStore.attach_human_actions`), because
        `evidence/interventions/<id>.json` is where a reviewer looks for what a human did (R6),
        not just the in-memory `InterventionResolution` this call returns.

        Round J: both drain sites below go through `_safe_drain_human_actions`, not
        `_drain_human_actions` directly -- see that method's own docstring for why a drain
        failure must not crash the escalation, at either site, including the discard before the
        block below (a crash there would kill the run before a human has even been called).

        On expiry: the token comes back to AUTOMATION (the runner always takes it back to
        terminate -- see `_ALLOWED_TRANSITIONS`'s own comment on why `to == AUTOMATION` is legal
        from every state), an `expired` resolution (carrying whatever was drained, if anything) is
        written so a second read of this same intervention agrees, `escalation_expired` is
        logged, and this returns None.

        On a real resolution (approved, rejected, or a full handoff that ends in PENDING_RESUME
        once an operator hands back -- see `escalation/operator_app.py`'s `return_control`, which
        writes the `took_control` resolution with an empty `human_actions`: that process holds no
        live Surface, so it cannot drain anything real): the token is likewise brought back to
        AUTOMATION before this returns, because only AUTOMATION lets `PolicyGate.dispatch` proceed
        with whatever the caller decides to do next (re-dispatch a just-approved action, or simply
        continue). Skipped when the resolver (the operator's own `/approve`/`/reject` handlers)
        already put it there itself, so the evidence log does not carry a second, redundant
        transition for the same human decision. `handoff_resumed` is logged here too, for BOTH
        `took_control` and `approved` (the two outcomes that let the run continue), so a caller
        never has to remember to log it itself -- and `replay/engine.py`'s own `_resume()` path,
        which never logged it at all before this round, now does, with no call of its own.
        """
        self._store.create(request)
        self.intervention_id = request.id
        self.transition(
            ControlState.PENDING_HANDOFF,
            actor="runner",
            reason=f"escalating: {request.reason_code.value}",
        )
        self._safe_drain_human_actions(logger)  # discarded on purpose -- see the docstring above
        if logger is not None:
            logger.event(
                "escalation_raised",
                intervention_id=request.id,
                reason_code=request.reason_code.value,
                where=request.observation.url,
            )

        resolution = self.await_resolution(request)

        if resolution is None:
            drained = self._safe_drain_human_actions(logger)
            resolution = InterventionResolution(
                resolved_by="system",
                action_taken="expired",
                human_actions=drained,
                notes=(
                    f"no operator resolved this intervention before it expired at "
                    f"{request.expires_at}"
                ),
                resolved_at=_now_iso(),
            )
            if self.state().state != ControlState.AUTOMATION:
                self.transition(ControlState.AUTOMATION, actor="runner", reason="expired")
            self._store.resolve(request.id, resolution)
            if logger is not None:
                logger.event(
                    "escalation_expired",
                    intervention_id=request.id,
                    reason_code=request.reason_code.value,
                )
            return None

        drained = self._safe_drain_human_actions(logger)
        if drained:
            self._store.attach_human_actions(request.id, drained)
            resolution = resolution.model_copy(update={"human_actions": drained})
        if logger is not None and resolution.action_taken in ("took_control", "approved"):
            logger.event(
                "handoff_resumed",
                intervention_id=request.id,
                action_taken=resolution.action_taken,
                human_action_count=len(drained),
            )

        if self.state().state != ControlState.AUTOMATION:
            self.transition(
                ControlState.AUTOMATION,
                actor="runner",
                reason=f"resolved: {resolution.action_taken}",
            )
        return resolution

    def await_resolution(
        self,
        intervention: InterventionRequest,
        poll_interval_s: float = 2.0,
        now: Callable[[], datetime] | None = None,
    ) -> InterventionResolution | None:
        """Block until the store shows a resolution for `intervention`, or its own `expires_at`
        passes -- whichever comes first; returns the resolution, or None on expiry.

        This is the ONE place this phase sleeps, and it deliberately does NOT live under
        `src/understudy/replay/`, which bans fixed sleeps (ARCHITECTURE.md decision 18,
        surface/web.py's `WebSurface.pause` docstring makes the same argument for its own one
        deliberate delay): a sleep there would be a guess about a MACHINE this project does not
        control, which is how deterministic replay becomes flaky. This is different in kind --
        it is a bounded wait on an EXTERNAL ACTOR (a human), and the bound is the intervention's
        own `expires_at`, not a guess. `now` is injectable so a test can supply a fixed clock
        instead of depending on wall-clock time.
        """
        clock = now or (lambda: datetime.now(UTC))
        expires_at = datetime.fromisoformat(intervention.expires_at)
        while True:
            record = self._store.get(intervention.id)
            if record is not None and record.resolution is not None:
                return record.resolution
            if clock() >= expires_at:
                return None
            time.sleep(poll_interval_s)
