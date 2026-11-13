"""Full-pipeline integration test: upload -> transcribe -> edit (manual or
fruit-replacement) -> synthesize -> blend -> export -> download.

Runs against portal-queue's mock worker (portal/mock_worker.py), not the
real Mac mini model pipeline, so it isn't blocked on Phase 1/2 model work
landing. The mock worker does a byte-identical passthrough for
synthesis/blend/export and sends a fixed stub transcript (`mock: true`) —
this test asserts the plumbing, not voice quality (that's
model-feasibility's Phase 1 proof).

Contract confirmed against portal-queue's commit dad3a8d
(worktree agent-a5d6afa8b356b9508), merged to master as 75afb54, on
2026-11-10 (docs/worker-protocol.md, docs/portal-notes.md).

Needs a running mock worker (see conftest.py:mock_worker_process) — set
MOCK_WORKER_SCRIPT and PORTAL_WORKER_TOKEN env vars when running this
against a live server, or these tests skip cleanly.
"""
import time

import pytest

from conftest import LIFECYCLE_TIMEOUT

pytestmark = [pytest.mark.usefixtures("live_server"), pytest.mark.usefixtures("mock_worker_process")]

TERMINAL_STATES = {"done", "error"}
EXPECTED_STATE_ORDER = ["queued", "transcribing", "generating", "blending", "exporting", "done"]


def _upload(session, base_url, clip_bytes, filename="clip.mp4"):
    resp = session.post(
        f"{base_url}/upload",
        files={"file": (filename, clip_bytes, "video/mp4")},
        allow_redirects=False,
    )
    assert resp.status_code == 303, f"upload did not redirect as expected: {resp.status_code} {resp.text[:300]}"
    location = resp.headers["location"]
    assert location.startswith("/jobs/"), f"unexpected upload redirect target: {location}"
    return location.rsplit("/", 1)[-1]


def _poll_until(session, base_url, job_id, predicate, timeout=LIFECYCLE_TIMEOUT, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = session.get(f"{base_url}/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text[:300]
        last = resp.json()
        if predicate(last):
            return last
        time.sleep(0.4)
    pytest.fail(f"job {job_id} never reached {what} within {timeout}s; last seen: {last}")


def test_full_lifecycle_upload_to_download(admin_session, live_server, sample_clip_bytes):
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)

    job = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"] and j["transcript"],
        what="transcript ready for edit",
    )
    assert job["transcript"]["mock"] is True, "mock worker must flag its transcript as mock, per PLAN.md honesty requirement"
    transcript_text = job["transcript"]["text"]

    # Exercise the fruit-replacement preview (PLAN.md Phase 3: one-click
    # "random fruit replacement") before finalizing the edit.
    fruit_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/fruit-replace",
        json={"text": transcript_text},
    )
    assert fruit_resp.status_code == 200, fruit_resp.text[:300]
    fruit_body = fruit_resp.json()
    assert "text" in fruit_body and "replacements" in fruit_body

    # Submit the (possibly fruit-replaced) text as the final edit.
    edit_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": fruit_body["text"], "replacements": fruit_body["replacements"]},
    )
    assert edit_resp.status_code == 200, edit_resp.text[:300]
    edited_job = edit_resp.json()
    assert edited_job["state"] == "generating"
    assert edited_job["awaiting_user_edit"] is False

    final_job = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] in TERMINAL_STATES,
        what="a terminal state",
    )
    assert final_job["state"] == "done", f"job ended in {final_job['state']}: {final_job.get('error_message')}"
    assert final_job["has_output"] is True

    # Honest progress: no fabricated percentages, but elapsed time present
    # and the recorded history should be in the documented stage order.
    assert final_job["elapsed_total_sec"] >= 0
    seen_states = [h["state"] for h in final_job["state_history"]]
    order_index = {s: i for i, s in enumerate(EXPECTED_STATE_ORDER)}
    indices = [order_index[s] for s in seen_states if s in order_index]
    assert indices == sorted(indices), f"state_history out of documented order: {seen_states}"

    # Before/after playback: original + result media, both session-gated.
    original_resp = admin_session.get(f"{base_url}/jobs/{job_id}/media/original")
    assert original_resp.status_code == 200
    result_resp = admin_session.get(f"{base_url}/jobs/{job_id}/media/result")
    assert result_resp.status_code == 200

    # Download.
    download_resp = admin_session.get(f"{base_url}/jobs/{job_id}/download")
    assert download_resp.status_code == 200
    assert len(download_resp.content) > 0
    assert "attachment" in download_resp.headers.get("content-disposition", "").lower()


def test_elapsed_time_freezes_after_completion(admin_session, live_server, sample_clip_bytes):
    """Regression test for the bug where a done job's elapsed time kept
    growing on every poll/reload (computed against wall-clock `now`
    instead of a recorded finish time) and still showed "estimating..."
    forever. Drives a real job through the mock worker to `done`, then
    polls twice more with a real sleep in between and asserts the elapsed
    fields are byte-identical across those polls."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"],
        what="transcript ready for edit",
    )
    edit_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "freeze check", "replacements": []},
    )
    assert edit_resp.status_code == 200

    first_done = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] in TERMINAL_STATES,
        what="a terminal state",
    )
    assert first_done["state"] == "done"
    assert first_done["is_terminal"] is True
    assert first_done["finished_at"] is not None
    assert first_done["estimate_label"] is None, "a finished job must not still say 'estimating...'"
    assert first_done["estimate_current_stage_sec"] is None

    time.sleep(2.5)
    second_poll = admin_session.get(f"{base_url}/api/jobs/{job_id}").json()
    assert second_poll["state"] == "done"
    assert second_poll["elapsed_total_sec"] == first_done["elapsed_total_sec"], (
        "elapsed_total_sec must be frozen at completion, not keep advancing with wall-clock time"
    )
    assert second_poll["elapsed_current_stage_sec"] == first_done["elapsed_current_stage_sec"]
    assert second_poll["finished_at"] == first_done["finished_at"]

    # A fresh page load (a brand-new GET, not a continued poll) must see
    # the exact same frozen numbers too — it's not an artifact of session
    # or connection reuse.
    third_poll = admin_session.get(f"{base_url}/api/jobs/{job_id}").json()
    assert third_poll["elapsed_total_sec"] == first_done["elapsed_total_sec"]


def test_engine_choice_is_made_at_edit_time_not_upload_time(admin_session, live_server, sample_clip_bytes):
    """Regression/requirement test: engine + preset + word-replacement
    mode are chosen on the submit-edit screen (right before Run), not
    locked in at upload time — upload only transcribes. Also covers:
    Chatterbox/F5-TTS are real, independently selectable, server-validated
    engine ids (not presets of OpenVoice), and an invalid combination is
    rejected without mutating the job."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    job = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"],
        what="transcript ready for edit",
    )
    # Upload-time defaults, confirmed present before any engine choice is made.
    assert job["voice_model"] == "openvoice"
    assert job["voice_preset"] == "balanced"
    assert job["engine_result"] is None

    # Invalid preset for a real engine must be rejected, job left untouched.
    bad_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "engine check", "replacements": [],
              "voice_model": "chatterbox", "voice_preset": "balanced"},
    )
    assert bad_resp.status_code == 400
    still_awaiting = admin_session.get(f"{base_url}/api/jobs/{job_id}").json()
    assert still_awaiting["state"] == "transcribing"
    assert still_awaiting["awaiting_user_edit"] is True
    assert still_awaiting["voice_model"] == "openvoice", "a rejected submit-edit must not mutate the job"

    # An unknown engine id entirely must also be rejected the same way.
    unknown_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "engine check", "replacements": [],
              "voice_model": "not-a-real-engine", "voice_preset": "no_rubberband"},
    )
    assert unknown_resp.status_code == 400

    # Real engine + its real preset, chosen HERE (not at upload) -> accepted,
    # persisted atomically with the edit, and this is what the worker will see.
    ok_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "engine check", "replacements": [],
              "voice_model": "chatterbox", "voice_preset": "no_rubberband",
              "word_replacement_mode": "fruit"},
    )
    assert ok_resp.status_code == 200
    updated = ok_resp.json()
    assert updated["state"] == "generating"
    assert updated["voice_model"] == "chatterbox"
    assert updated["voice_preset"] == "no_rubberband"
    assert updated["word_replacement_mode"] == "fruit"

    # And it's not just the immediate response -- a fresh GET sees the same
    # persisted choice (this is what worker/run_worker.py's dispatch reads).
    reread = admin_session.get(f"{base_url}/api/jobs/{job_id}").json()
    assert reread["voice_model"] == "chatterbox"
    assert reread["voice_preset"] == "no_rubberband"


def test_f5tts_is_independently_selectable_not_an_openvoice_preset(admin_session, live_server, sample_clip_bytes):
    """F5-TTS must be a real engine id the server accepts and persists
    verbatim -- never silently rewritten/substituted to openvoice."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"],
        what="transcript ready for edit",
    )
    resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "f5 check", "replacements": [],
              "voice_model": "f5tts", "voice_preset": "no_rubberband"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["voice_model"] == "f5tts", "must persist the exact engine requested, never substitute openvoice"
    assert body["voice_preset"] == "no_rubberband"


def test_manual_transcript_edit_is_honored_verbatim(admin_session, live_server, sample_clip_bytes):
    """A manual edit (no fruit-replacement) should flow through untouched —
    PLAN.md's "manual transcript editing" path, distinct from the
    one-click fruit action."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"],
        what="transcript ready for edit",
    )
    manual_text = "This is a manually edited transcript, not touched by fruit replacement."
    edit_resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": manual_text, "replacements": []},
    )
    assert edit_resp.status_code == 200
    final_job = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] in TERMINAL_STATES,
        what="a terminal state",
    )
    assert final_job["state"] == "done"
    assert final_job["edit"]["text"] == manual_text


def test_submit_edit_rejected_when_not_awaiting_edit(admin_session, live_server, sample_clip_bytes):
    """409 guard: a job that hasn't produced a transcript yet (still
    `queued`, or claimed but pre-transcript) must reject submit-edit."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    # Immediately, before the worker has had a chance to transcribe.
    resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "too early", "replacements": []},
    )
    assert resp.status_code == 409


def test_submit_edit_rejects_out_of_range_replacement_index(admin_session, live_server, sample_clip_bytes):
    """Server-side bounds-check added in portal-queue's commit 2bad758:
    `replacements[].index` is only meaningful against the ORIGINAL
    transcript's word list (docs/worker-protocol.md "word-index
    alignment"). A stale/out-of-range index (e.g. from fruit-replace run
    against different text, or hand-crafted) must be rejected with 400
    before it ever reaches a worker, rather than letting the worker hit
    an IndexError."""
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    job = _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"] and j["transcript"],
        what="transcript ready for edit",
    )
    word_count = len(job["transcript"]["words"])
    assert word_count > 0, "mock worker's transcript must carry non-empty word timestamps for this test to mean anything"

    resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={
            "text": "irrelevant, the index check runs first",
            "replacements": [{"index": word_count + 50, "original": "x", "replacement": "mango"}],
        },
    )
    assert resp.status_code == 400
    assert "out of range" in resp.json()["detail"].lower()

    # Job must still be awaiting edit — a rejected submit-edit must not
    # have mutated state.
    unchanged = admin_session.get(f"{base_url}/api/jobs/{job_id}").json()
    assert unchanged["state"] == "transcribing"
    assert unchanged["awaiting_user_edit"] is True


def test_submit_edit_rejects_non_integer_replacement_index(admin_session, live_server, sample_clip_bytes):
    base_url = live_server
    job_id = _upload(admin_session, base_url, sample_clip_bytes)
    _poll_until(
        admin_session, base_url, job_id,
        lambda j: j["state"] == "transcribing" and j["awaiting_user_edit"] and j["transcript"],
        what="transcript ready for edit",
    )
    resp = admin_session.post(
        f"{base_url}/api/jobs/{job_id}/submit-edit",
        json={"text": "t", "replacements": [{"index": "not-an-int", "original": "x", "replacement": "mango"}]},
    )
    assert resp.status_code == 400


def test_upload_rejects_duration_outside_accepted_range(admin_session, live_server):
    from conftest import build_minimal_mp4
    too_short = build_minimal_mp4(5.0)
    resp = admin_session.post(
        f"{live_server}/upload",
        files={"file": ("tooshort.mp4", too_short, "video/mp4")},
    )
    assert resp.status_code == 400
    assert "duration" in resp.text.lower()


def test_upload_rejects_unparseable_file(admin_session, live_server):
    resp = admin_session.post(
        f"{live_server}/upload",
        files={"file": ("bad.mp4", b"not-a-real-video-container", "video/mp4")},
    )
    assert resp.status_code == 400
