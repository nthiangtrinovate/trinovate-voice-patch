"""Shared FastAPI dependencies: admin-session auth and worker bearer auth.

Logging rule enforced throughout this module: we log *outcomes*
("login failed: bad password", "worker auth failed") never the
credential itself, and we never log the raw Authorization header.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from starlette.responses import RedirectResponse

from . import config, security

log = logging.getLogger("portal.auth")


def get_current_admin(request: Request):
    """Return the admin_users row for the current session, or None."""
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    session = security.get_session(token) if token else None
    if session is None:
        return None
    return security.get_admin(session["username"])


def require_admin_page(request: Request):
    """For HTML page routes. Returns the admin row, or a RedirectResponse
    the caller should return directly if auth/forced-change isn't
    satisfied. Usage:

        admin = require_admin_page(request)
        if isinstance(admin, RedirectResponse):
            return admin
    """
    admin = get_current_admin(request)
    if admin is None:
        return RedirectResponse("/login", status_code=303)
    if admin["must_change_password"] and request.url.path not in (
        "/change-password",
        "/logout",
    ):
        return RedirectResponse("/change-password", status_code=303)
    return admin


def require_admin_api(request: Request):
    """For JSON API routes. Raises HTTPException on failure."""
    admin = get_current_admin(request)
    if admin is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if admin["must_change_password"] and request.url.path != "/api/change-password":
        raise HTTPException(
            status_code=403,
            detail="password change required before using the portal",
        )
    return admin


def require_worker(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        log.warning("worker auth failed: missing/malformed Authorization header")
        raise HTTPException(status_code=401, detail="worker bearer token required")
    token = auth_header[7:].strip()
    if not security.verify_worker_token(token):
        log.warning("worker auth failed: token did not match")
        raise HTTPException(status_code=401, detail="invalid worker token")
    # Worker identity for claim/lease bookkeeping; a static id is fine for
    # the single shared-token dev scheme. Multi-worker support can extend
    # verify_worker_token to also return a worker_id.
    return request.headers.get("x-worker-id", "worker")
