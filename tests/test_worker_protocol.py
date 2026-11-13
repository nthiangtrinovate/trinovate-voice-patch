"""Worker poll/claim/upload protocol tests, per PLAN.md Phase 4 and
docs/worker-protocol.md (contract confirmed against portal-queue's commit
dad3a8d / master 75afb54, 2026-11-10).

- The Mac mini worker is outbound-only: every request here is one *this
  test* initiates against the server's `/worker/*` routes, mirroring what
  a real worker would do. Nothing here asserts or needs an inbound
  listener on any worker.
- There is no separate "claim" endpoint: `GET /worker/jobs/next` polls
  *and* atomically claims in one call. These tests drive jobs forward
  manually (not via mock_worker.py) so they have full control over when
  NOT to advance a stage — needed for the reclaim-after-lease-expiry test.
- Deliberately does NOT request the (module-scoped) `mock_worker_process`
  fixture from test_lifecycle.py — see conftest.py's docstring on that
  fixture for why a background worker racing these manual claims would
  make this file flaky under the single shared worker token.
"""
import os
import time

import pytest

pytestmark = pytest.mark.usefixtures("live_server")


@pytest.fixture
def worker_token():
    token = os.environ.get("PORTAL_WORKER_TOKEN")
    if not token:
        pytest.skip(
            "PORTAL_WORKER_TOKEN not set — need the real worker credential "
            "the live server under test provisioned"
        )
    return token


def _worker_headers(token, worker_id="test-worker"):
    return {"Authorization": f"Bearer {token}", "X-Worker-Id": worker_id}


def _create_job(admin_session, base_url, clip_bytes):
    resp = admin_session.post(
        f"{base_url}/upload",
        files={"file": ("clip.mp4", clip_bytes, "video/mp4")},
        allow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_poll_without_worker_auth_is_rejected(http, live_server):
    resp = http.get(f"{live_server}/worker/jobs/next")
    assert resp.status_code == 401


def test_poll_returns_204_when_nothing_queued(http, live_server, worker_token):
    """Not a guarantee in a shared/reused dev DB that nothing is ever
    queued, but immediately after draining with --once-style polls this
    should settle to 204. Mostly here to document the "no job" shape
    (204 No Content, not a JSON null) since that's easy to get wrong."""
    # Drain anything currently claimable first (best-effort, bounded).
    for _ in range(20):
        resp = http.get(f"{live_server}/worker/jobs/next", headers=_worker_headers(worker_token))
        if resp.status_code == 204:
            break
    assert resp.status_code == 204
    assert resp.content == b""


def test_claim_then_full_stage_sequence(http, live_server, worker_token, admin_session, sample_clip_bytes):
    """Drive a job from claim through transcript -> (edit, via the admin
    API) -> generating -> blending -> exporting -> result, asserting each
    worker-side transition is accepted."""
    headers = _worker_headers(worker_token, worker_id="w-sequence")
    job_id = _create_job(admin_session, live_server, sample_clip_bytes)

    # Poll/claim: queued -> transcribing.
    deadline = time.time() + 10
    claimed = None
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=headers)
        if resp.status_code == 200:
            body = resp.json()
            if body["id"] == job_id:
                claimed = body
                break
        time.sleep(0.2)
    assert claimed is not None, "worker never claimed the freshly uploaded job"
    assert claimed["state"] == "transcribing"
    assert claimed["input_download_url"] == f"/worker/jobs/{job_id}/input"

    # Pull input (the one outbound download path).
    input_resp = http.get(f"{live_server}{claimed['input_download_url']}", headers=headers)
    assert input_resp.status_code == 200
    assert input_resp.content == sample_clip_bytes

    # Push transcript.
    transcript_resp = http.post(
        f"{live_server}/worker/jobs/{job_id}/transcript",
        headers=headers,
        json={"text": "a worker-submitted transcript", "words": [], "mock": True},
    )
    assert transcript_resp.status_code == 200

    job_after_transcript = http.get(f"{live_server}/api/jobs/{job_id}", cookies=admin_session.cookies).json()
    assert job_after_transcript["awaiting_user_edit"] is True
    assert job_after_transcript["state"] == "transcribing"

    # A second poll must NOT reclaim this job while it's awaiting a human
    # edit, even though the lease hasn't expired — awaiting_user_edit jobs
    # are excluded from the eligible set entirely (docs/worker-protocol.md).
    resp = http.get(f"{live_server}/worker/jobs/next", headers=_worker_headers(worker_token, "w-other"))
    assert resp.status_code != 200 or resp.json()["id"] != job_id

    # User submits the edit -> generating.
    edit_resp = admin_session.post(
        f"{live_server}/api/jobs/{job_id}/submit-edit",
        json={"text": "edited text", "replacements": []},
    )
    assert edit_resp.status_code == 200
    assert edit_resp.json()["state"] == "generating"

    # The original worker's claim was cleared by submit_user_edit
    # (claimed_by=NULL) — re-poll to pick it back up for generating.
    deadline = time.time() + 10
    reclaimed = None
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=headers)
        if resp.status_code == 200 and resp.json()["id"] == job_id:
            reclaimed = resp.json()
            break
        time.sleep(0.2)
    assert reclaimed is not None
    assert reclaimed["state"] == "generating"
    assert reclaimed["edit"]["text"] == "edited text"

    for state in ["blending", "exporting"]:
        stage_resp = http.post(
            f"{live_server}/worker/jobs/{job_id}/stage", headers=headers, json={"state": state}
        )
        assert stage_resp.status_code == 200, f"advancing to {state} failed: {stage_resp.text[:200]}"

    result_resp = http.post(
        f"{live_server}/worker/jobs/{job_id}/result",
        headers=headers,
        files={"file": ("result.mp4", sample_clip_bytes, "video/mp4")},
    )
    assert result_resp.status_code == 200

    final = http.get(f"{live_server}/api/jobs/{job_id}", cookies=admin_session.cookies).json()
    assert final["state"] == "done"
    assert final["has_output"] is True


def test_stage_and_result_rejected_for_a_claim_held_by_another_worker(
    http, live_server, worker_token, admin_session, sample_clip_bytes
):
    job_id = _create_job(admin_session, live_server, sample_clip_bytes)
    headers_a = _worker_headers(worker_token, worker_id="w-a")
    headers_b = _worker_headers(worker_token, worker_id="w-b")

    deadline = time.time() + 10
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=headers_a)
        if resp.status_code == 200 and resp.json()["id"] == job_id:
            break
        time.sleep(0.2)
    else:
        pytest.fail("worker A never claimed the job")

    # Worker B (same shared token, different identity) must not be able to
    # advance a stage or submit a result on a claim it doesn't hold.
    stage_resp = http.post(
        f"{live_server}/worker/jobs/{job_id}/stage", headers=headers_b, json={"state": "generating"}
    )
    assert stage_resp.status_code == 409

    result_resp = http.post(
        f"{live_server}/worker/jobs/{job_id}/result",
        headers=headers_b,
        files={"file": ("result.mp4", sample_clip_bytes, "video/mp4")},
    )
    assert result_resp.status_code == 409


def test_error_with_retryable_requeues_until_max_attempts(
    http, live_server, worker_token, admin_session, sample_clip_bytes
):
    """Forces 3 retryable errors (PORTAL_JOB_MAX_ATTEMPTS default 3) and
    confirms: queued -> queued -> queued -> error, with attempts
    incrementing and error detail present on the terminal state — PLAN.md:
    "Errors are shown with enough detail to retry/recover.\""""
    job_id = _create_job(admin_session, live_server, sample_clip_bytes)
    headers = _worker_headers(worker_token, worker_id="w-retry")
    max_attempts = int(os.environ.get("PORTAL_JOB_MAX_ATTEMPTS", "3"))

    for attempt in range(1, max_attempts + 1):
        deadline = time.time() + 10
        claimed = None
        while time.time() < deadline:
            resp = http.get(f"{live_server}/worker/jobs/next", headers=headers)
            if resp.status_code == 200 and resp.json()["id"] == job_id:
                claimed = resp.json()
                break
            time.sleep(0.2)
        assert claimed is not None, f"attempt {attempt}: job never (re)claimed"
        assert claimed["attempts"] == attempt

        err_resp = http.post(
            f"{live_server}/worker/jobs/{job_id}/error",
            headers=headers,
            json={"message": f"forced failure #{attempt}", "detail": "test-induced", "retryable": True},
        )
        assert err_resp.status_code == 200

    final = http.get(f"{live_server}/api/jobs/{job_id}", cookies=admin_session.cookies).json()
    assert final["state"] == "error", f"expected terminal error after {max_attempts} retryable failures, got {final['state']}"
    assert final["error_message"]
    assert final["attempts"] == max_attempts


def test_stalled_blending_job_is_reclaimed_after_lease_expiry(
    http, live_server, worker_token, admin_session, sample_clip_bytes
):
    """Edge case flagged during cross-review: claim_next_job's eligibility
    WHERE-clause must reclaim jobs stuck in `blending`/`exporting` (a
    worker died mid-stage), not just the initial `queued`/`generating`
    entry points. Needs PORTAL_JOB_LEASE_SECONDS set to a short value on
    BOTH the server under test and this test run (so the wait below is
    bounded) — skips cleanly otherwise rather than hanging for the
    120s production default.
    """
    lease_seconds = int(os.environ.get("PORTAL_JOB_LEASE_SECONDS", "0"))
    if not (0 < lease_seconds <= 10):
        pytest.skip(
            "set PORTAL_JOB_LEASE_SECONDS to a short value (e.g. 2) on both the "
            "live server and this test run to exercise the reclaim path without "
            "waiting out the 120s production default"
        )

    job_id = _create_job(admin_session, live_server, sample_clip_bytes)
    worker_a = _worker_headers(worker_token, worker_id="w-dies-mid-stage")

    # Drive it to generating (worker A).
    deadline = time.time() + 10
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=worker_a)
        if resp.status_code == 200 and resp.json()["id"] == job_id:
            break
        time.sleep(0.2)
    http.post(f"{live_server}/worker/jobs/{job_id}/transcript", headers=worker_a,
              json={"text": "t", "words": [], "mock": True})
    admin_session.post(f"{live_server}/api/jobs/{job_id}/submit-edit",
                        json={"text": "edit", "replacements": []})
    deadline = time.time() + 10
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=worker_a)
        if resp.status_code == 200 and resp.json()["id"] == job_id:
            break
        time.sleep(0.2)
    stage_resp = http.post(f"{live_server}/worker/jobs/{job_id}/stage", headers=worker_a, json={"state": "blending"})
    assert stage_resp.status_code == 200

    before = http.get(f"{live_server}/api/jobs/{job_id}", cookies=admin_session.cookies).json()
    assert before["state"] == "blending"
    attempts_before = before["attempts"]

    # Worker A now "dies" — no heartbeat, no further stage call. Wait out
    # the lease.
    time.sleep(lease_seconds + 1.5)

    worker_b = _worker_headers(worker_token, worker_id="w-reclaims")
    deadline = time.time() + 10
    reclaimed = None
    while time.time() < deadline:
        resp = http.get(f"{live_server}/worker/jobs/next", headers=worker_b)
        if resp.status_code == 200 and resp.json()["id"] == job_id:
            reclaimed = resp.json()
            break
        time.sleep(0.3)
    assert reclaimed is not None, (
        "a job stalled in 'blending' past its lease window was never reclaimed "
        "by a new poll — claim_next_job's WHERE clause must cover blending/"
        "exporting, not just queued/generating"
    )
    assert reclaimed["state"] == "blending", "reclaim must preserve the stage, not reset to queued/transcribing"
    assert reclaimed["attempts"] == attempts_before + 1, "reclaiming should still increment attempts"
