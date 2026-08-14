"""Legacy Bank: a deliberately hostile back-office fixture app.

This is a test target for the Understudy discovery/replay loop, not evaluated code.
It is ugly on purpose: frameset shell, nested tables, no test hooks, inline onclick
submits. See fixtures/legacy_bank/README.md for the full rationale.
"""

from __future__ import annotations

import os
import time
import uuid
from functools import wraps
from typing import Any

from flask import Flask, g, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FIXTURE_SECRET", "legacy-bank-fixture-not-secret")

# Seed data: five synthetic members. No realistic PII, no SSNs, no addresses.
MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {"name": "Testuser Alpha", "status": "Active", "balance": "$1,204.55"},
    "22222": {"name": "Sample Bravo", "status": "Active", "balance": "$532.10"},
    "33333": {"name": "Fixture Charlie", "status": "Inactive", "balance": "$88.00"},
    "55555": {"restricted": True},
    "99999": {"missing": True},
}

INJECT_MODES = {
    "validation",
    "not_found",
    "permission_denied",
    "unexpected_dialog",
    "native_dialog",
    "session_expired",
    "slow_load",
    "transient_failure",
    "app_error",
}

# /login and /admin/inject never take part in injection, so the fixture can never lock
# the operator out of clearing a mode or logging back in.
EXEMPT_PATHS = {"/login", "/admin/inject"}


def require_login(view: Any) -> Any:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def _get_member_or_response(mid: str) -> tuple[dict[str, Any] | None, Any]:
    """Look up a member; return (member, None) or (None, error_response)."""
    member = MEMBERS.get(mid)
    if member is None or member.get("missing"):
        return None, (render_template("not_found.html", message=f"No such member: {mid}."), 200)
    if member.get("restricted"):
        return None, (
            render_template(
                "permission_denied.html",
                message=f"You do not have permission to view member {mid}.",
            ),
            403,
        )
    return member, None


# ---------------------------------------------------------------------------------
# Failure injection: one dispatch point. ?inject=<mode> applies to a single request;
# /admin/inject?mode=<mode> persists a mode in the session until cleared.
# ---------------------------------------------------------------------------------


@app.before_request
def _dispatch_injection() -> Any:
    g.inject_mode = None
    if request.path in EXEMPT_PATHS:
        return None

    mode = request.args.get("inject") or session.get("inject_mode")
    if not mode or mode not in INJECT_MODES:
        return None
    g.inject_mode = mode

    if mode == "unexpected_dialog":
        if request.args.get("dismiss") == "1":
            return None
        return render_template("interstitial.html", back_path=request.path), 200

    if mode == "session_expired":
        session.clear()
        return redirect(url_for("login"))

    if mode == "slow_load":
        time.sleep(6)
        return None

    if mode == "transient_failure":
        attempts = session.setdefault("attempts", {})
        count = attempts.get(request.path, 0) + 1
        attempts[request.path] = count
        session.modified = True
        if count < 3:
            return (
                render_template(
                    "error_generic.html", message="Service temporarily unavailable. Please retry."
                ),
                503,
            )
        return None

    if mode == "permission_denied":
        return (
            render_template(
                "permission_denied.html", message="Permission denied for this operation."
            ),
            403,
        )

    if mode == "not_found":
        return render_template("not_found.html", message="No matching record was found."), 200

    if mode == "app_error":
        return render_template("error_generic.html", message="An unexpected error occurred."), 500

    # "validation" is applied by the subaccount route itself (it needs the submitted
    # field values to re-render); "native_dialog" is applied below in after_request
    # (it needs to alter already-rendered HTML). Both just fall through here.
    return None


@app.after_request
def _native_dialog_injection(response: Any) -> Any:
    if request.path in EXEMPT_PATHS:
        return response
    if getattr(g, "inject_mode", None) != "native_dialog":
        return response
    if not (response.content_type or "").startswith("text/html"):
        return response
    html = response.get_data(as_text=True)
    if "<head>" in html:
        html = html.replace(
            "<head>", "<head><script>window.confirm('Are you sure?');</script>", 1
        )
        response.set_data(html)
    return response


# ---------------------------------------------------------------------------------
# Admin: set or clear the persisted injection mode.
# ---------------------------------------------------------------------------------


@app.route("/admin/inject", methods=["GET", "POST"])
def admin_inject() -> Any:
    mode = request.values.get("mode", "none")
    if mode in ("none", "clear"):
        session.pop("inject_mode", None)
    elif mode in INJECT_MODES:
        session["inject_mode"] = mode
        if mode == "transient_failure":
            session["attempts"] = {}
    else:
        return {"error": f"unknown inject mode: {mode}"}, 400
    session.modified = True
    return {"inject_mode": session.get("inject_mode", "none")}, 200


# ---------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if request.method == "POST":
        username = request.form.get("f1", "").strip()
        password = request.form.get("f2", "").strip()
        if username and password:
            session.clear()
            session["user"] = username
            return redirect(url_for("app_frameset"))
        return render_template("login.html", error="Username and password are required."), 200
    return render_template("login.html", error=None)


# ---------------------------------------------------------------------------------
# The frameset shell and its two frames
# ---------------------------------------------------------------------------------


@app.route("/app")
@require_login
def app_frameset() -> Any:
    return render_template("app_frameset.html")


@app.route("/nav")
@require_login
def nav() -> Any:
    return render_template("nav.html")


# ---------------------------------------------------------------------------------
# Member search and detail
# ---------------------------------------------------------------------------------


@app.route("/members", methods=["GET"])
@require_login
def members() -> Any:
    query = request.args.get("f7", "").strip()
    result: dict[str, Any] | None = None
    if query:
        match = MEMBERS.get(query)
        if match and not match.get("missing"):
            result = {"found": True, "id": query, "name": match.get("name", "")}
        else:
            result = {"found": False}
    return render_template("members.html", query=query, result=result)


@app.route("/member/<mid>")
@require_login
def member_detail(mid: str) -> Any:
    member, err = _get_member_or_response(mid)
    if err:
        return err
    return render_template("member.html", member={"id": mid, **member})


@app.route("/member/<mid>/balance")
@require_login
def member_balance(mid: str) -> Any:
    member, err = _get_member_or_response(mid)
    if err:
        return err
    return render_template("balance.html", balance=member["balance"])


# ---------------------------------------------------------------------------------
# Subaccount opening
# ---------------------------------------------------------------------------------


@app.route("/member/<mid>/subaccount/new", methods=["GET", "POST"])
@require_login
def subaccount_new(mid: str) -> Any:
    member, err = _get_member_or_response(mid)
    if err:
        return err

    if request.method == "POST":
        acct_type = request.form.get("f1", "SAV")
        nickname = request.form.get("f2", "")
        deposit = request.form.get("f7", "")
        if g.inject_mode == "validation":
            return (
                render_template(
                    "subaccount_new.html",
                    mid=mid,
                    acct_type=acct_type,
                    nickname=nickname,
                    deposit=deposit,
                    deposit_error="Deposit amount could not be validated. Please re-enter.",
                ),
                200,
            )
        ref = "REF-" + uuid.uuid4().hex[:8].upper()
        return redirect(url_for("subaccount_confirm", mid=mid, ref=ref))

    return render_template(
        "subaccount_new.html", mid=mid, acct_type="SAV", nickname="", deposit="", deposit_error=None
    )


@app.route("/member/<mid>/subaccount/confirm")
@require_login
def subaccount_confirm(mid: str) -> Any:
    member, err = _get_member_or_response(mid)
    if err:
        return err
    ref = request.args.get("ref", "UNKNOWN")
    return render_template("confirm.html", mid=mid, ref=ref)


# ---------------------------------------------------------------------------------
# A real external hop, for the navigation allowlist guard to refuse in Phase 5.
# ---------------------------------------------------------------------------------


@app.route("/external")
def external() -> Any:
    return redirect("https://example.com/")
