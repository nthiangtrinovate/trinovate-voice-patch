from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, security
from ..deps import get_current_admin, require_admin_api, require_admin_page
from ..templating import templates

log = logging.getLogger("portal.routes.auth")
router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_current_admin(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    admin = security.get_admin(username)
    # Constant-shape failure path: don't reveal whether the username exists.
    if admin is None or not security.verify_password(password, admin["password_hash"]):
        log.info("login failed for username=%s", username)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."}, status_code=401
        )
    token, expires = security.create_session(admin["username"])
    log.info("login succeeded for username=%s", username)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        config.SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        expires=int((expires - security.utcnow()).total_seconds()),
    )
    return resp


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if token:
        security.delete_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.SESSION_COOKIE_NAME)
    return resp


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        # Forced-change page must still render even if must_change_password
        # redirect target IS this page; require_admin_page already allows
        # this path through, so a redirect here only means "not logged in".
        return admin
    return templates.TemplateResponse(
        request,
        "change_password.html",
        {"error": None, "forced": bool(admin["must_change_password"])},
    )


@router.post("/change-password")
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        return admin

    def fail(msg: str):
        return templates.TemplateResponse(
            request,
            "change_password.html",
            {"error": msg, "forced": bool(admin["must_change_password"])},
            status_code=400,
        )

    if not security.verify_password(current_password, admin["password_hash"]):
        log.info("change-password rejected: current password mismatch")
        return fail("Current password is incorrect.")
    # Check against the default password before the length check: with the
    # out-of-the-box "admin" default (5 chars) the length check below would
    # always catch it first, making this branch dead code in that specific
    # case — but PORTAL_DEFAULT_ADMIN_PASSWORD can be configured to anything,
    # including something >=8 chars, so this must run independently of
    # length to actually enforce "never reuse the default" in general.
    if new_password == config.DEFAULT_ADMIN_PASSWORD:
        return fail("New password cannot be the default password.")
    if len(new_password) < 8:
        return fail("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return fail("New password and confirmation do not match.")

    security.set_password(admin["username"], new_password)
    log.info("password changed for username=%s", admin["username"])
    return RedirectResponse("/", status_code=303)


@router.post("/api/change-password")
def api_change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    admin=Depends(require_admin_api),
):
    if not security.verify_password(current_password, admin["password_hash"]):
        return {"ok": False, "error": "current password is incorrect"}
    # Same ordering rationale as change_password_submit above: check the
    # default-password rule independently of (and before) length, so it
    # isn't dead code when PORTAL_DEFAULT_ADMIN_PASSWORD is configured to
    # something >=8 characters.
    if new_password == config.DEFAULT_ADMIN_PASSWORD:
        return {"ok": False, "error": "new password cannot be the default password"}
    if len(new_password) < 8:
        return {"ok": False, "error": "new password must be at least 8 characters"}
    security.set_password(admin["username"], new_password)
    return {"ok": True}
