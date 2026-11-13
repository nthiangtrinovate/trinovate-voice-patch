"""Auth-flow integration tests, per PLAN.md "Portal authentication":

- default admin/admin credentials
- forced password-change facility
- password stored hashed (never plaintext)
- all uploads/jobs/results/downloads require authentication
- worker-auth is a genuinely separate credential/scheme from admin login
- no secrets in logs (checked by what we can observe over HTTP: responses
  and headers never echo back the password; the log-content guarantee
  itself was verified by reading portal/app/security.py, deps.py, and
  routers/*.py directly during cross-review — every log statement there
  logs usernames/job-ids/outcomes only, never a password/token/raw
  Authorization header)

Contract confirmed against portal-queue's commit dad3a8d
(worktree agent-a5d6afa8b356b9508), merged to master as 75afb54, on
2026-11-10. Endpoints below are no longer ASSUMED.
"""
import pytest

from conftest import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USER, login_form

pytestmark = pytest.mark.usefixtures("live_server")

SESSION_GATED_API_ENDPOINTS = [
    ("GET", "/api/jobs/does-not-exist"),
    ("POST", "/api/jobs/does-not-exist/fruit-replace"),
    ("POST", "/api/jobs/does-not-exist/submit-edit"),
    ("GET", "/jobs/does-not-exist/media/original"),
    ("GET", "/jobs/does-not-exist/media/result"),
    ("GET", "/jobs/does-not-exist/download"),
]

SESSION_GATED_PAGE_ENDPOINTS = [
    ("GET", "/"),
    ("GET", "/jobs/does-not-exist"),
]


def test_default_admin_login_succeeds_but_forces_change(http, live_server):
    """Logging in with the shipped admin/admin must succeed at the login
    step itself (it's a valid credential, not a locked account) but every
    subsequent page/API access must be blocked until the password is
    changed — confirmed behavior: require_admin_api raises 403 with
    detail "password change required before using the portal" for any
    path other than /api/change-password, and require_admin_page redirects
    page routes to /change-password instead."""
    resp = login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    assert resp.status_code in (302, 303), f"default-credential login did not succeed: {resp.text[:200]}"
    assert http.cookies.get("portal_session"), "login did not set the session cookie"

    # Any real API route should now 403 with the forced-change detail.
    api_resp = http.get(f"{live_server}/api/jobs/does-not-exist")
    assert api_resp.status_code == 403
    assert "password change" in api_resp.json().get("detail", "").lower()

    # Any real page route should redirect to /change-password.
    page_resp = http.get(f"{live_server}/", allow_redirects=False)
    assert page_resp.status_code == 303
    assert page_resp.headers["location"] == "/change-password"


def test_login_response_never_echoes_password(http, live_server):
    resp = login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    assert DEFAULT_ADMIN_PASSWORD not in resp.text
    assert "set-cookie" in {k.lower() for k in resp.headers}
    # httponly cookie: the Set-Cookie header itself is fine to contain the
    # session token (that's the point), but must never contain the password.
    assert DEFAULT_ADMIN_PASSWORD not in resp.headers.get("set-cookie", "")


def test_wrong_password_is_rejected_with_generic_error(http, live_server):
    """Constant-shape failure: the login page must not reveal whether the
    username exists."""
    resp = login_form(http, live_server, DEFAULT_ADMIN_USER, "definitely-not-the-password")
    assert resp.status_code == 401
    assert "invalid username or password" in resp.text.lower()

    resp2 = login_form(http, live_server, "no-such-user", "whatever")
    assert resp2.status_code == 401
    assert "invalid username or password" in resp2.text.lower()


def test_password_change_rejects_wrong_current_password(http, live_server):
    login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    resp = http.post(
        f"{live_server}/api/change-password",
        data={"current_password": "definitely-wrong", "new_password": "a-new-strong-password-1!"},
    )
    assert resp.status_code == 200  # API reports failure in-band, per api_change_password
    body = resp.json()
    assert body["ok"] is False
    assert "incorrect" in body["error"].lower()


def test_password_change_rejects_the_default_as_the_new_password(http, live_server):
    """Guards against a no-op 'change' that just re-sets admin/admin.

    Note: with the stock default password "admin" (5 chars), the
    length check (`< 8 chars`) fires before the explicit
    `new_password == DEFAULT_ADMIN_PASSWORD` check ever gets evaluated —
    so this test only exercises the *net effect* (default rejected), not
    that specific code path. Flagged to the coordinator: the dedicated
    "cannot be the default password" check is effectively unreachable
    dead code as long as the default stays shorter than the minimum
    length; it would only matter if PORTAL_DEFAULT_ADMIN_PASSWORD is ever
    set to something >= 8 chars. Not a security gap (the password is
    still correctly rejected either way), just worth tidying."""
    login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    resp = http.post(
        f"{live_server}/api/change-password",
        data={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": DEFAULT_ADMIN_PASSWORD},
    )
    body = resp.json()
    assert body["ok"] is False


def test_password_change_rejects_short_new_password(http, live_server):
    login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    resp = http.post(
        f"{live_server}/api/change-password",
        data={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": "short"},
    )
    body = resp.json()
    assert body["ok"] is False


@pytest.mark.parametrize("method,path", SESSION_GATED_API_ENDPOINTS)
def test_api_endpoints_require_authentication(http, live_server, method, path):
    """Every job/upload/result/download API route must reject a request
    with no session cookie at all — the most direct guard against an
    auth-bypass regression."""
    resp = http.request(method, f"{live_server}{path}")
    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code} without authentication — "
        "PLAN.md requires all uploads/jobs/results/downloads to require auth"
    )


@pytest.mark.parametrize("method,path", SESSION_GATED_PAGE_ENDPOINTS)
def test_page_endpoints_require_authentication(http, live_server, method, path):
    resp = http.request(method, f"{live_server}{path}", allow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_upload_requires_authentication(http, live_server, sample_clip_bytes):
    resp = http.post(
        f"{live_server}/upload",
        files={"file": ("clip.mp4", sample_clip_bytes, "video/mp4")},
        allow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_admin_session_does_not_authenticate_as_worker(http, live_server):
    """The admin browser session must NOT be accepted by worker-only
    endpoints — PLAN.md requires the worker-auth scheme to be genuinely
    separate from admin login, not just a different login form on the
    same session mechanism."""
    login_form(http, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    resp = http.get(f"{live_server}/worker/jobs/next")
    assert resp.status_code == 401, (
        "an admin session authenticated against a worker-only endpoint — "
        "worker-auth must be a separate credential/scheme per PLAN.md"
    )


def test_worker_token_does_not_authenticate_as_admin(http, live_server):
    """Conversely, a worker credential must not grant access to admin-only
    browser endpoints."""
    resp = http.get(
        f"{live_server}/api/jobs/does-not-exist",
        headers={"Authorization": "Bearer definitely-not-a-valid-worker-token"},
    )
    assert resp.status_code == 401


def test_worker_endpoints_reject_missing_or_bad_bearer_token(http, live_server):
    resp = http.get(f"{live_server}/worker/jobs/next")
    assert resp.status_code == 401

    resp2 = http.get(
        f"{live_server}/worker/jobs/next",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp2.status_code == 401


def test_default_credentials_must_be_changed_before_public_exposure():
    """Deployment-gate check, not a live-server HTTP check: there is no
    generic HTTP way to verify 'not exposed publicly while still default'
    from inside this suite. Confirmed during cross-review that this IS
    enforced in code (app/main.py:on_startup refuses to start when
    PORTAL_PUBLIC_DEPLOYMENT is set and must_change_password is still 1)
    rather than being left as a runbook-only step — see
    docs/deployment-notes.md and docs/portal-notes.md's "Deployment gate"
    section for the full requirement list (admin password changed,
    PORTAL_WORKER_TOKEN set explicitly, PORTAL_PUBLIC_DEPLOYMENT=1, HTTPS
    at the edge).
    """
    pytest.skip(
        "deployment-time gate, verified by code review (app/main.py:on_startup) "
        "rather than by HTTP here — see docs/deployment-notes.md"
    )
