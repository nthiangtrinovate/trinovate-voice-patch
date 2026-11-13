"""Shared fixtures for voice-word-editor integration tests.

Design goal: this suite must be safe to run against an empty checkout
(nothing deployed yet) without turning into false-failure noise. Every test
that needs a live server should depend on the `live_server` fixture, which
skips (not fails) when BASE_URL is unset or unreachable.

Contract confirmed against portal-queue's commit dad3a8d (worktree
agent-a5d6afa8b356b9508) on 2026-11-10 — see tests/CONTRACT.md for the
full writeup. Key facts baked into these fixtures:

- Admin login is a *form* POST (`/login`, fields `username`/`password`),
  not JSON, and succeeds (303 redirect) regardless of the forced-change
  flag — the flag gates *subsequent* page/API access, not login itself.
- Upload is multipart POST `/upload` with field name `file` (not `video`),
  and the portal validates duration server-side by parsing the MP4/MOV
  `mvhd` atom directly (no ffmpeg) — `build_minimal_mp4()` below produces
  the smallest input that satisfies that parser.
"""
import os
import struct
import subprocess
import sys
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("TEST_HTTP_TIMEOUT", "5"))
LIFECYCLE_TIMEOUT = float(os.environ.get("TEST_LIFECYCLE_TIMEOUT", "60"))

# Default admin credentials per PLAN.md — used ONLY against disposable/local
# test deployments. Never point this suite's BASE_URL at a server that is
# reachable from the public domain; forced-password-change tests deliberately
# exercise the default-credential path.
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
# Used by the password-change-dependent fixtures below. Not a real secret —
# this is a throwaway credential for an ephemeral/local test deployment.
TEST_ADMIN_NEW_PASSWORD = "integration-test-password-1!"


def _server_reachable():
    if not BASE_URL:
        return False
    try:
        requests.get(BASE_URL, timeout=DEFAULT_TIMEOUT)
        return True
    except requests.RequestException:
        return False


@pytest.fixture(scope="session")
def live_server():
    """Skip the test if no BASE_URL is configured or it isn't reachable.

    Deliberately session-scoped and cheap: one reachability probe, reused by
    every test. Tests that need it should request this fixture by name even
    if they don't use the return value, so skip reasons are consistent.
    """
    if not BASE_URL:
        pytest.skip("BASE_URL not set — no live server configured for integration tests")
    if not _server_reachable():
        pytest.skip(f"server at {BASE_URL} not reachable — skipping live integration test")
    return BASE_URL


@pytest.fixture
def http():
    """A requests.Session with sane defaults; caller is responsible for auth."""
    s = requests.Session()
    s.request = _with_default_timeout(s.request)
    return s


def _with_default_timeout(request_fn):
    def wrapped(method, url, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return request_fn(method, url, **kwargs)
    return wrapped


@pytest.fixture
def unique_username():
    """A throwaway username suffix so repeated runs don't collide."""
    return f"test-{uuid.uuid4().hex[:8]}"


def build_minimal_mp4(duration_sec: float = 35.0, timescale: int = 1000) -> bytes:
    """The smallest byte sequence that satisfies
    portal/app/media.py:probe_duration_seconds — a real ISO-BMFF container
    with just a top-level `ftyp` + `moov` box, `moov` containing only an
    `mvhd` (movie header) atom with the given duration/timescale. This is
    NOT a real voice sample and must never be treated as one — it exists
    only to exercise upload validation + the queue lifecycle against
    portal-queue's actual (ffmpeg-free) duration parser. Real
    voice-similarity validation is model-feasibility's Phase 1 proof, not
    this suite's job.

    Default 35.0s sits inside the server's accepted range (20-60s, per
    PLAN.md's 30-45s target with slack) — see portal/app/config.py.
    """
    def box(box_type: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), box_type) + payload

    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isom")
    mvhd_payload = (
        bytes([0]) + bytes(3)  # version(0) + flags(0,0,0)
        + struct.pack(">I", 0)  # creation_time
        + struct.pack(">I", 0)  # modification_time
        + struct.pack(">I", timescale)
        + struct.pack(">I", int(duration_sec * timescale))
    )
    moov = box(b"moov", box(b"mvhd", mvhd_payload))
    return ftyp + moov


@pytest.fixture
def sample_clip_bytes():
    return build_minimal_mp4(35.0)


@pytest.fixture
def sample_clip_path(tmp_path, sample_clip_bytes):
    path = tmp_path / "synthetic_test_clip.mp4"
    path.write_bytes(sample_clip_bytes)
    return path


def login_form(http, base_url, username, password, allow_redirects=False):
    return http.post(
        f"{base_url}/login",
        data={"username": username, "password": password},
        allow_redirects=allow_redirects,
    )


@pytest.fixture(scope="session")
def admin_session(live_server):
    """A requests.Session authenticated as admin with a password that is
    NOT the default — i.e. one that can actually use the portal (every
    page/API route 403s on the default-but-unchanged password per
    deps.py:require_admin_api).

    Idempotent across repeated runs against the same (possibly reused)
    live_server: tries the default credential first; if that's already
    been rotated by an earlier run against the same data dir, falls back
    to TEST_ADMIN_NEW_PASSWORD. Session-scoped deliberately — the password
    change is a one-way side effect on the live server, shared by every
    test in this run that needs real access (lifecycle, worker-protocol).
    test_auth.py's own default-credential tests use a fresh, independent
    `http` session and run first in file-collection order, so they observe
    the pristine default-credential state before this fixture is ever
    invoked by a later test file.
    """
    s = requests.Session()
    s.request = _with_default_timeout(s.request)

    resp = login_form(s, live_server, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    if resp.status_code in (302, 303):
        # Default credential still active (fresh data dir) — rotate it now.
        change_resp = s.post(
            f"{live_server}/api/change-password",
            data={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": TEST_ADMIN_NEW_PASSWORD},
        )
        body = change_resp.json()
        if body.get("ok"):
            return s
        # Changed concurrently/already by something else between login and
        # this call — fall through to the rotated-password path below.
    else:
        # Default login itself failed (401): a previous run against this
        # same data dir already rotated the password out from under the
        # default credential. That's expected and fine, not an error.
        assert resp.status_code == 401, (
            f"unexpected status logging in with the default credential: {resp.status_code}"
        )

    # Either already rotated by a previous run, or the change-password call
    # above reported failure for some other reason — log in with the
    # password this fixture would have set last time.
    s2 = requests.Session()
    s2.request = _with_default_timeout(s2.request)
    resp2 = login_form(s2, live_server, DEFAULT_ADMIN_USER, TEST_ADMIN_NEW_PASSWORD)
    assert resp2.status_code in (302, 303), (
        "admin login failed with both the default password and this suite's "
        f"expected rotated password (default login returned {resp.status_code})"
    )
    return s2


@pytest.fixture(scope="module")
def mock_worker_process(live_server):
    """Launches portal-queue's reference `mock_worker.py` as a real
    subprocess against `live_server`, for the duration of the requesting
    test *module* (not the whole session — deliberately: test_lifecycle.py
    wants a worker continuously picking up jobs; test_worker_protocol.py
    drives jobs forward manually via direct API calls instead, and must
    NOT have this background worker racing it for the same job under the
    single shared worker token. Module scope means this process is
    torn down when test_lifecycle.py's tests finish, before
    test_worker_protocol.py's module even starts, as long as the latter
    never requests this fixture — which it doesn't).

    So lifecycle/worker-protocol tests exercise the actual outbound
    poll/claim/transcript/stage/result HTTP contract rather than the test
    suite impersonating a worker.

    Needs two env vars (deliberately not defaulted to a path/token baked
    into this repo — a worker token is a secret and its path is
    deployment-specific):

    - MOCK_WORKER_SCRIPT: path to portal/mock_worker.py
    - PORTAL_WORKER_TOKEN: the token that script should authenticate with
      (must match whatever the live server under test provisioned)

    Skips cleanly if either is unset, same philosophy as `live_server`.
    """
    script = os.environ.get("MOCK_WORKER_SCRIPT")
    token = os.environ.get("PORTAL_WORKER_TOKEN")
    if not script or not token:
        pytest.skip(
            "MOCK_WORKER_SCRIPT and/or PORTAL_WORKER_TOKEN not set — skipping "
            "tests that need a running worker against the live server"
        )
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, script, "--base-url", live_server, "--poll-interval", "1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        pytest.fail(f"mock worker exited immediately (code={proc.returncode}):\n{out[-2000:]}")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
