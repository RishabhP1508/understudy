"""Tenant B: NorthBay Credit Union, a second tenant of the SAME vendor product as tenant A.

Registered on the shared Flask `app` (see the bottom of app.py) as a Blueprint under
`/tenantb`, so `_dispatch_injection`/`_native_dialog_injection` (app-level `before_request`/
`after_request` hooks) apply to it automatically. Two renames are DELIBERATE, to exercise the
ranked-locator + drift-report path a Phase 12 overlay covers, and both stay in the SAME
ordinal position as tenant A's equivalent field:
  - login field 1: "Username" (tenant A) -> "User ID" (tenant B)
  - linked-account field 2: "Initial Deposit" (tenant A) -> "Opening Deposit" (tenant B)
Everything else that differs (route names, form field `name=` attributes, CSS classes, one
extra level of table nesting, the title pattern) is deliberately different in ways the recorded
artifact never references, so it must produce no replay signal at all.

Business rules and error pages are reused, not re-implemented: `_get_member_or_response` and
`_parse_money` come from app.py, and so do the not_found/permission_denied/error_generic/
interstitial templates -- those are vendor-generic pages in this product, and sharing them is
what keeps the Phase 9 outcome/recovery detectors working unmodified on both tenants.
"""

from __future__ import annotations

import uuid
from typing import Any

from flask import Blueprint, g, redirect, render_template, request, session, url_for

from .app import INJECT_MODES, MEMBERS, _get_member_or_response, _parse_money, require_login

bp = Blueprint("tenantb", __name__, url_prefix="/tenantb")


# ---------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    # Mirrors app.py's login(): arms/clears an injection mode from the URL, before anything
    # else. `/tenantb/login` must be in app.py's EXEMPT_PATHS for this to ever run instead of
    # being intercepted by `_dispatch_injection` first.
    mode = request.args.get("inject")
    if mode in INJECT_MODES:
        session["inject_mode"] = mode
        session.modified = True
    elif mode in ("none", "clear"):
        session.pop("inject_mode", None)
        session.modified = True

    if request.method == "POST":
        user_id = request.form.get("uid", "").strip()
        password = request.form.get("pwd", "").strip()
        if user_id and password:
            armed_mode = session.get("inject_mode")
            session.clear()
            session["user"] = user_id
            if armed_mode is not None:
                session["inject_mode"] = armed_mode
            return redirect(url_for("tenantb.app_frameset"))
        return render_template(
            "tenantb/login.html", error="User ID and password are required."
        ), 200
    return render_template("tenantb/login.html", error=None)


# ---------------------------------------------------------------------------------
# The frameset shell and its two frames
# ---------------------------------------------------------------------------------


@bp.route("/app")
@require_login
def app_frameset() -> Any:
    return render_template("tenantb/app_frameset.html")


@bp.route("/nav")
@require_login
def nav() -> Any:
    return render_template("tenantb/nav.html")


# ---------------------------------------------------------------------------------
# Customer search and detail
# ---------------------------------------------------------------------------------


@bp.route("/customers", methods=["GET"])
@require_login
def customers() -> Any:
    query = request.args.get("q", "").strip()
    result: dict[str, Any] | None = None
    query_error: str | None = None
    if query:
        if g.inject_mode == "validation":
            query_error = "Customer ID could not be validated. Please re-enter."
        else:
            match = MEMBERS.get(query)
            if match and not match.get("missing"):
                result = {"found": True, "id": query, "name": match.get("name", "")}
            else:
                result = {"found": False}
    return render_template(
        "tenantb/customers.html", query=query, result=result, query_error=query_error
    )


@bp.route("/customer/<cid>")
@require_login
def customer_detail(cid: str) -> Any:
    member, err = _get_member_or_response(cid)
    if err:
        return err
    return render_template("tenantb/customer.html", member={"id": cid, **member})


@bp.route("/customer/<cid>/balance")
@require_login
def customer_balance(cid: str) -> Any:
    member, err = _get_member_or_response(cid)
    if err:
        return err
    return render_template("tenantb/balance.html", balance=member["balance"])


# ---------------------------------------------------------------------------------
# Linked-account opening: new -> review -> confirmation (tenant B has a review step tenant A
# does not; the "Continue" click submits, the review screen's own "Confirm" is what actually
# creates the account).
# ---------------------------------------------------------------------------------


@bp.route("/customer/<cid>/linked-account/new", methods=["GET", "POST"])
@require_login
def linked_account_new(cid: str) -> Any:
    member, err = _get_member_or_response(cid)
    if err:
        return err

    if request.method == "POST":
        nickname = request.form.get("nick", "")
        deposit = request.form.get("dep", "")
        if g.inject_mode == "validation":
            return (
                render_template(
                    "tenantb/linked_account_new.html",
                    cid=cid,
                    nickname=nickname,
                    deposit=deposit,
                    deposit_error="Deposit amount could not be validated. Please re-enter.",
                ),
                200,
            )
        # Same business rule and same wording as tenant A's subaccount_new, so the shared
        # balance_check detector (replay/outcomes.py) recognizes it on either tenant.
        balance_amount = _parse_money(member.get("balance"))
        deposit_amount = _parse_money(deposit)
        if (
            balance_amount is not None
            and deposit_amount is not None
            and deposit_amount > balance_amount
        ):
            return (
                render_template(
                    "tenantb/linked_account_new.html",
                    cid=cid,
                    nickname=nickname,
                    deposit=deposit,
                    deposit_error=(
                        f"Insufficient funds: the initial deposit exceeds the available "
                        f"balance of {member.get('balance')}."
                    ),
                ),
                200,
            )
        return redirect(
            url_for(
                "tenantb.linked_account_review", cid=cid, nickname=nickname, deposit=deposit
            )
        )

    return render_template(
        "tenantb/linked_account_new.html", cid=cid, nickname="", deposit="", deposit_error=None
    )


@bp.route("/customer/<cid>/linked-account/review", methods=["GET", "POST"])
@require_login
def linked_account_review(cid: str) -> Any:
    member, err = _get_member_or_response(cid)
    if err:
        return err

    if request.method == "POST":
        nickname = request.form.get("nickname", "")
        deposit = request.form.get("deposit", "")
        ref = "REF-" + uuid.uuid4().hex[:8].upper()
        return render_template(
            "tenantb/linked_account_confirm.html",
            cid=cid,
            ref=ref,
            nickname=nickname,
            deposit=deposit,
        )

    nickname = request.args.get("nickname", "")
    deposit = request.args.get("deposit", "")
    return render_template(
        "tenantb/linked_account_review.html", cid=cid, nickname=nickname, deposit=deposit
    )
