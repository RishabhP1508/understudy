"""FastAPI operator console (task B, R6): deliberately plain server-rendered HTML, no template
engine, no CSS framework, no client-side JS beyond ordinary form POSTs (CLAUDE.md: "keep the
operator console deliberately plain... it is a mock by design and polish there is wasted
effort").

Every route reads and writes through the SAME `InterventionStore` / `SessionBroker` machinery the
run process uses (escalation/control.py, escalation/store.py) -- there is no second copy of
control-token or approval logic here. This process holds no live `Surface` at all: a Playwright
`Page` cannot cross a process boundary (`SessionBroker.session()`'s own docstring), so every
`SessionBroker` this module constructs is given `_NoSurface()`, a stand-in that carries only
control-token and approval state through the shared store and is never actually asked to observe
or act.
"""

from __future__ import annotations

import html as html_lib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from understudy.escalation.control import (
    ControlState,
    ControlToken,
    ControlTransition,
    IllegalTransition,
    SessionBroker,
)
from understudy.escalation.store import InterventionStore
from understudy.models.intervention import InterventionRequest, InterventionResolution, ReasonCode
from understudy.models.observation import Observation
from understudy.surface.base import Action

_REASON_SENTENCES: dict[ReasonCode, str] = {
    ReasonCode.STUCK_NO_PROGRESS: "The agent kept acting, but the screen stopped changing.",
    ReasonCode.LOOP_DETECTED: (
        "The agent repeated the same action against the same target too many times."
    ),
    ReasonCode.LOCATOR_UNRESOLVED: (
        "The agent (or a recorded step) could not find a unique match for the element it needed."
    ),
    ReasonCode.UNRECOVERABLE_CONDITION: (
        "The application showed something no automatic recovery rule knows how to handle."
    ),
    ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL: (
        "The next step is irreversible. It needs a human to approve it before it runs."
    ),
    ReasonCode.POLICY_REFUSED: (
        "The safety policy refused the next action (outside the allowlist, action type, role, "
        "or a forbidden text pattern)."
    ),
    ReasonCode.SESSION_EXPIRED: "The application's session appears to have expired mid-flow.",
    ReasonCode.MAX_STEPS: "The run used its full step budget before the goal was ever verified.",
}

_STATE_LABELS: dict[ControlState, str] = {
    ControlState.AUTOMATION: "AUTOMATION",
    ControlState.PENDING_HANDOFF: "PENDING HANDOFF",
    ControlState.HUMAN: "HUMAN",
    ControlState.PENDING_RESUME: "PENDING RESUME",
}

_STYLE = """
body { font-family: sans-serif; max-width: 860px; margin: 2rem auto; color: #111; }
.banner { padding: 0.5rem 1rem; margin-bottom: 1rem; border: 1px solid #999; }
.banner-automation { background: #e6ffe6; }
.banner-human { background: #ffe6e6; }
.banner-pending_handoff, .banner-pending_resume { background: #fff6cc; }
dt { font-weight: bold; margin-top: 0.75rem; }
dd { margin: 0.15rem 0 0 0; white-space: pre-wrap; }
img.screenshot { max-width: 100%; border: 1px solid #ccc; }
form { display: inline; }
button { padding: 0.4rem 0.8rem; margin-right: 0.5rem; }
li { margin-bottom: 0.5rem; }
"""


class _NoSurface:
    """A structural `Surface` stand-in for the operator console process, which never holds the
    real Playwright `Page` -- there is no cross-process way to hand one over
    (escalation/control.py's `SessionBroker.session()` docstring). Every `SessionBroker` this
    module constructs exists only for control-token transitions and one-shot-approval
    bookkeeping, neither of which ever touches the surface -- `observe`/`act` are never called
    from here and raise loudly if they ever are, rather than silently returning nothing.
    """

    url = ""

    def observe(self) -> Observation:
        raise NotImplementedError("the operator console process holds no live Surface")

    def act(self, action: Action) -> str | None:
        raise NotImplementedError("the operator console process holds no live Surface")

    def urls(self) -> list[str]:
        return [""]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_run_dir(evidence_dir: Path, run_id: str) -> Path | None:
    """The directory `EvidenceLogger` wrote this run's evidence under: `<run_kind>-<run_id>`,
    somewhere directly beneath `evidence_dir`. `InterventionRequest` only carries `run_id`, not
    `run_kind` (evidence/logger.py's directory name is built from both), so this globs for a
    unique match on the suffix rather than guessing `run_kind`. Returns None (never a wrong
    guess) if zero or more than one directory matches.
    """
    matches = [path for path in evidence_dir.glob(f"*-{run_id}") if path.is_dir()]
    return matches[0] if len(matches) == 1 else None


def _banner(token: ControlToken) -> str:
    label = _STATE_LABELS[token.state]
    return (
        f'<div class="banner banner-{token.state.value}">'
        f"CONTROL: <strong>{label}</strong> (held by {html_lib.escape(token.holder)})"
        f"</div>"
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{html_lib.escape(title)}</h1>{body}</body></html>"
    )


def _render_index(open_requests: list[InterventionRequest], store: InterventionStore) -> str:
    if not open_requests:
        return _page("Understudy operator console", "<p>No open interventions.</p>")

    items = []
    for request in open_requests:
        broker = SessionBroker(_NoSurface(), store, run_id=request.run_id, holder="operator")
        broker.intervention_id = request.id
        token = broker.state()
        items.append(
            "<li>"
            f"{_banner(token)}"
            f"<a href='/intervention/{html_lib.escape(request.id)}'>"
            f"{html_lib.escape(request.reason_code.value)}</a> -- "
            f"{html_lib.escape(request.goal)} (created {html_lib.escape(request.created_at)})"
            "</li>"
        )
    return _page("Understudy operator console", "<ul>" + "".join(items) + "</ul>")


def _render_actions(request: InterventionRequest, token: ControlToken) -> str:
    if request.reason_code == ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL:
        return (
            "<p>This is a one-time approval for a single irreversible action, not a handoff. "
            "Approve lets the run make exactly that one attempt; reject leaves it refused.</p>"
            f"<form method='post' action='/intervention/{request.id}/approve'>"
            "<button type='submit'>Approve</button></form>"
            f"<form method='post' action='/intervention/{request.id}/reject'>"
            "<button type='submit'>Reject</button></form>"
        )
    if token.state == ControlState.HUMAN:
        return (
            "<p>You hold control of the live browser window right now. When you are done, "
            "hand it back so the run can re-observe the screen and continue.</p>"
            f"<form method='post' action='/intervention/{request.id}/return-control'>"
            "<button type='submit'>Return control</button></form>"
        )
    if token.state == ControlState.PENDING_HANDOFF:
        return (
            "<p>Every other reason needs a human driving the browser directly, not a one-time "
            "approval. Taking control means using the REAL, VISIBLE Chromium window this run "
            "already has open -- same cookies, same login, same half-filled form. Nothing new "
            "opens.</p>"
            f"<form method='post' action='/intervention/{request.id}/take-control'>"
            "<button type='submit'>Take control</button></form>"
        )
    return f"<p>Waiting: control is currently {html_lib.escape(token.state.value)}.</p>"


def _render_transitions(transitions: list[ControlTransition]) -> str:
    """F3: the intervention record's own custody chain, oldest first (the order it was appended
    in, `escalation/store.py`'s `set_token`) -- the clearest possible answer to "who holds
    control, and who put it there", covering both the runner's own moves and the operator
    console's, which never shared a run logger to log the latter into run.jsonl."""
    if not transitions:
        return "<dt>Control history</dt><dd>no transitions recorded yet</dd>"
    items = "".join(
        "<li>"
        f"{html_lib.escape(t.from_state.value)} &rarr; {html_lib.escape(t.to_state.value)} "
        f"by <strong>{html_lib.escape(t.actor)}</strong> at {html_lib.escape(t.at)}: "
        f"{html_lib.escape(t.reason)}"
        "</li>"
        for t in transitions
    )
    return f"<dt>Control history</dt><dd><ol>{items}</ol></dd>"


def _render_detail(
    request: InterventionRequest, token: ControlToken, transitions: list[ControlTransition]
) -> str:
    sentence = _REASON_SENTENCES[request.reason_code]
    rows = [
        ("Reason", f"{html_lib.escape(request.reason_code.value)} -- {html_lib.escape(sentence)}"),
        ("Goal", html_lib.escape(request.goal)),
        (
            "Capability",
            html_lib.escape(request.capability_id)
            if request.capability_id
            else "(none -- this is a discovery run)",
        ),
        ("Step", str(request.step_id) if request.step_id is not None else "(none)"),
        ("What it tried", html_lib.escape(request.what_it_tried)),
        ("What it observed", html_lib.escape(request.what_it_observed)),
    ]
    dl_items = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows)

    if request.screenshot_path:
        screenshot_html = (
            "<dt>Screenshot (masked)</dt><dd>"
            f"<img class='screenshot' src='/intervention/{html_lib.escape(request.id)}"
            "/screenshot'></dd>"
        )
    else:
        screenshot_html = "<dt>Screenshot</dt><dd>none captured</dd>"

    context_html = (
        "<dt>Context (redacted)</dt><dd><pre>"
        f"{html_lib.escape(json.dumps(request.context, indent=2))}</pre></dd>"
    )
    transitions_html = _render_transitions(transitions)

    body = (
        f"{_banner(token)}"
        f"<dl>{dl_items}{screenshot_html}{context_html}{transitions_html}</dl>"
        f"{_render_actions(request, token)}"
        "<p><a href='/'>&laquo; back to open interventions</a></p>"
    )
    return _page(f"Intervention {request.id}", body)


def create_app(store_dir: str | Path, evidence_dir: str | Path) -> FastAPI:
    """Factory, not a module-level singleton, so a test can point both directories at `tmp_path`
    (and so a real deployment can point them at wherever the run's own `--store-dir`/
    `--evidence-dir` are, cli.py's `operator` command)."""
    store = InterventionStore(base_dir=store_dir)
    base_evidence_dir = Path(evidence_dir)
    app = FastAPI()

    def _broker_for(request: InterventionRequest) -> SessionBroker:
        broker = SessionBroker(_NoSurface(), store, run_id=request.run_id, holder="operator")
        broker.intervention_id = request.id
        return broker

    def _get_or_404(intervention_id: str) -> InterventionRequest:
        record = store.get(intervention_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no intervention {intervention_id!r}")
        return record.request

    def _resolve(
        intervention_id: str, action_taken: Literal["approved", "rejected"]
    ) -> InterventionRequest:
        request = _get_or_404(intervention_id)
        if request.reason_code != ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{action_taken} is only offered for "
                    f"{ReasonCode.RISKY_ACTION_REQUIRES_APPROVAL.value}, not "
                    f"{request.reason_code.value}"
                ),
            )
        store.resolve(
            intervention_id,
            InterventionResolution(
                resolved_by="operator",
                action_taken=action_taken,
                human_actions=[],
                notes="",
                resolved_at=_now_iso(),
            ),
        )
        return request

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        open_requests = sorted(store.list_open(), key=lambda r: r.created_at, reverse=True)
        return _render_index(open_requests, store)

    @app.get("/intervention/{intervention_id}", response_class=HTMLResponse)
    def detail(intervention_id: str) -> str:
        record = store.get(intervention_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no intervention {intervention_id!r}")
        token = _broker_for(record.request).state()
        return _render_detail(record.request, token, record.transitions)

    @app.get("/intervention/{intervention_id}/screenshot")
    def screenshot(intervention_id: str) -> FileResponse:
        request = _get_or_404(intervention_id)
        if not request.screenshot_path:
            raise HTTPException(status_code=404, detail="no screenshot for this intervention")
        run_dir = _resolve_run_dir(base_evidence_dir, request.run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="run evidence directory not found")
        path = run_dir / request.screenshot_path
        if not path.exists():
            raise HTTPException(status_code=404, detail="screenshot file not found on disk")
        return FileResponse(path)

    @app.post("/intervention/{intervention_id}/take-control")
    def take_control(intervention_id: str) -> RedirectResponse:
        request = _get_or_404(intervention_id)
        broker = _broker_for(request)
        try:
            broker.transition(ControlState.HUMAN, actor="operator", reason="operator took control")
        except IllegalTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/intervention/{intervention_id}", status_code=303)

    @app.post("/intervention/{intervention_id}/return-control")
    def return_control(intervention_id: str) -> RedirectResponse:
        request = _get_or_404(intervention_id)
        broker = _broker_for(request)
        try:
            broker.transition(
                ControlState.PENDING_RESUME, actor="operator", reason="operator returned control"
            )
        except IllegalTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Task C: `SessionBroker.escalate()` blocks on a written resolution, not merely on the
        # control token reaching PENDING_RESUME -- so a full handoff has to leave one behind too,
        # the same as approve/reject already do. `human_actions` stays empty here on purpose:
        # this process holds no live Surface (`_NoSurface`, this module's own docstring), so it
        # cannot drain anything real. The RUNNER's own `SessionBroker.escalate()`
        # (escalation/control.py) is what drains the human's actual DOM actions off the live page
        # once it observes this resolution, and persists them onto this SAME stored record
        # (`InterventionStore.attach_human_actions`) -- round H, after this comment was found to
        # name `agent/loop.py`/`replay/engine.py` instead, neither of which ever drained anything.
        store.resolve(
            intervention_id,
            InterventionResolution(
                resolved_by="operator",
                action_taken="took_control",
                human_actions=[],
                notes="",
                resolved_at=_now_iso(),
            ),
        )
        return RedirectResponse(f"/intervention/{intervention_id}", status_code=303)

    @app.post("/intervention/{intervention_id}/approve")
    def approve(intervention_id: str) -> RedirectResponse:
        request = _resolve(intervention_id, "approved")
        broker = _broker_for(request)
        try:
            broker.transition(ControlState.AUTOMATION, actor="operator", reason="approved")
        except IllegalTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/intervention/{intervention_id}", status_code=303)

    @app.post("/intervention/{intervention_id}/reject")
    def reject(intervention_id: str) -> RedirectResponse:
        request = _resolve(intervention_id, "rejected")
        broker = _broker_for(request)
        try:
            broker.transition(ControlState.AUTOMATION, actor="operator", reason="rejected")
        except IllegalTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/intervention/{intervention_id}", status_code=303)

    return app
