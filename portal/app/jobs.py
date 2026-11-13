"""Job queue model and state machine.

States (exactly as specified in PLAN.md):
    queued -> transcribing -> generating -> blending -> exporting -> done
    (or -> error from any state)

The worker drives forward transitions via the worker API (see
routers/worker.py and docs/worker-protocol.md); the one exception is
queued -> transcribing -> (pause for human edit) -> generating, where the
*user* unblocks the transcribing -> generating edge by submitting their
edited transcript (routers/portal.py: submit_edit). We model that pause
as `awaiting_user_edit=1` while state is still "transcribing" (the plan's
state enum has no separate "awaiting edit" state, so we layer this as a
flag rather than inventing a new top-level state) so UI copy can say
"transcript ready — edit below" without claiming a job stage the plan
doesn't define.

Progress honesty: every stage transition is timestamped. Elapsed time for
the *current* stage is `now - stage_started_at`. The estimate for how
long a stage will take is a running mean of previously *measured* stage
durations (stage_stats table) — never a fabricated number. Until we have
ESTIMATE_MIN_SAMPLES completed samples for a given state, the API reports
estimate=None and the UI shows "estimating...".
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from . import config, db, security
from . import model_registry

log = logging.getLogger("portal.jobs")

TERMINAL_STATES = {"done", "error"}


def _now_iso() -> str:
    return security.iso(security.utcnow())


def create_job(owner_username: str, input_path: str, input_filename: str,
                duration_sec: float, size_bytes: int,
                word_replacement_mode: str = model_registry.DEFAULT_WORD_REPLACEMENT_MODE,
                voice_model: str = model_registry.DEFAULT_VOICE_MODEL,
                voice_preset: str | None = None) -> str:
    if voice_preset is None:
        voice_preset = model_registry.VOICE_MODELS[voice_model]["default_preset"]
    # Validate here too (not just at the router) so any future direct
    # caller can't silently persist an invalid combination.
    model_registry.validate_word_replacement_mode(word_replacement_mode)
    model_registry.validate_voice_choice(voice_model, voice_preset)

    job_id = str(uuid.uuid4())
    now = _now_iso()
    with db.tx() as c:
        c.execute(
            """INSERT INTO jobs (
                id, owner_username, state, awaiting_user_edit, input_path,
                input_filename, input_duration_sec, input_size_bytes,
                transcript_json, edit_json, output_path,
                word_replacement_mode, voice_model, voice_preset,
                claimed_by, claim_expires_at, attempts, max_attempts,
                error_message, error_detail, retryable, created_at,
                updated_at, stage_started_at, state_history_json
            ) VALUES (?, ?, 'queued', 0, ?, ?, ?, ?, NULL, NULL, NULL,
                ?, ?, ?, NULL, NULL, 0, ?, NULL, NULL, NULL, ?, ?, ?, '[]')""",
            (
                job_id, owner_username, input_path, input_filename,
                duration_sec, size_bytes,
                word_replacement_mode, voice_model, voice_preset,
                config.JOB_MAX_ATTEMPTS,
                now, now, now,
            ),
        )
    return job_id


def get_job(job_id: str):
    conn = db.get_conn()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs_for_owner(owner_username: str):
    conn = db.get_conn()
    return conn.execute(
        "SELECT * FROM jobs WHERE owner_username = ? ORDER BY created_at DESC",
        (owner_username,),
    ).fetchall()


def _record_stage_duration(state: str, duration_sec: float) -> None:
    with db.tx() as c:
        row = c.execute(
            "SELECT sample_count, total_duration_sec FROM stage_stats WHERE state = ?",
            (state,),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO stage_stats (state, sample_count, total_duration_sec) "
                "VALUES (?, 1, ?)",
                (state, duration_sec),
            )
        else:
            c.execute(
                "UPDATE stage_stats SET sample_count = sample_count + 1, "
                "total_duration_sec = total_duration_sec + ? WHERE state = ?",
                (duration_sec, state),
            )


def estimate_for_state(state: str) -> float | None:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT sample_count, total_duration_sec FROM stage_stats WHERE state = ?",
        (state,),
    ).fetchone()
    if row is None or row["sample_count"] < config.ESTIMATE_MIN_SAMPLES:
        return None
    return row["total_duration_sec"] / row["sample_count"]


def _append_history(job_row, finished_state: str, duration_sec: float) -> str:
    history = json.loads(job_row["state_history_json"] or "[]")
    history.append({"state": finished_state, "duration_sec": round(duration_sec, 3)})
    return json.dumps(history)


def claim_next_job(worker_id: str) -> "sqlite3.Row | None":
    """Atomically claim the oldest eligible job for a worker.

    Eligible = any non-terminal, non-human-blocked state with no active
    (unexpired) claim, OR a claim whose lease expired (worker died
    mid-stage — this also covers resuming a crashed worker's job stuck in
    `blending`/`exporting`, not just the initial `queued`/`generating`
    entry points). `transcribing` jobs with awaiting_user_edit=1 are NOT
    eligible — the worker already did its part; it's waiting on a human,
    not stalled.
    """
    now = security.utcnow()
    now_iso = security.iso(now)
    conn = db.get_conn()
    with db.tx() as c:
        row = c.execute(
            """SELECT * FROM jobs
               WHERE (
                 (state = 'queued')
                 OR (state = 'transcribing' AND awaiting_user_edit = 0)
                 OR (state = 'generating' AND awaiting_user_edit = 0)
                 OR (state = 'blending')
                 OR (state = 'exporting')
               )
               AND (claimed_by IS NULL OR claim_expires_at < ?)
               ORDER BY created_at ASC
               LIMIT 1""",
            (now_iso,),
        ).fetchone()
        if row is None:
            return None

        lease_expiry = security.iso(now + timedelta(seconds=config.JOB_CLAIM_LEASE_SECONDS))
        next_state = "transcribing" if row["state"] == "queued" else row["state"]
        c.execute(
            """UPDATE jobs SET state = ?, claimed_by = ?, claim_expires_at = ?,
               stage_started_at = ?, updated_at = ?, attempts = attempts + 1
               WHERE id = ?""",
            (next_state, worker_id, lease_expiry, now_iso, now_iso, row["id"]),
        )
    return get_job(row["id"])


def heartbeat(job_id: str, worker_id: str) -> bool:
    now = security.utcnow()
    lease_expiry = security.iso(now + timedelta(seconds=config.JOB_CLAIM_LEASE_SECONDS))
    with db.tx() as c:
        cur = c.execute(
            "UPDATE jobs SET claim_expires_at = ?, updated_at = ? "
            "WHERE id = ? AND claimed_by = ? AND state NOT IN ('done', 'error')",
            (lease_expiry, security.iso(now), job_id, worker_id),
        )
    return cur.rowcount > 0


def advance_stage(job_id: str, worker_id: str, new_state: str) -> bool:
    """Worker reports it has moved to `new_state`. Records the duration of
    the stage just completed into stage_stats for future estimates."""
    if new_state not in config.JOB_STATES:
        raise ValueError(f"unknown state {new_state!r}")
    job = get_job(job_id)
    if job is None or job["claimed_by"] != worker_id:
        return False
    if job["state"] in TERMINAL_STATES:
        return False

    now = security.utcnow()
    started = security.parse_iso(job["stage_started_at"])
    duration = (now - started).total_seconds()
    _record_stage_duration(job["state"], duration)
    history_json = _append_history(job, job["state"], duration)

    lease_expiry = security.iso(now + timedelta(seconds=config.JOB_CLAIM_LEASE_SECONDS))
    awaiting = 1 if (new_state == "transcribing") else 0
    with db.tx() as c:
        c.execute(
            """UPDATE jobs SET state = ?, awaiting_user_edit = ?,
               stage_started_at = ?, claim_expires_at = ?, updated_at = ?,
               state_history_json = ? WHERE id = ?""",
            (new_state, awaiting, security.iso(now), lease_expiry, security.iso(now),
             history_json, job_id),
        )
    return True


def submit_transcript(job_id: str, worker_id: str, transcript: dict) -> bool:
    job = get_job(job_id)
    if job is None or job["claimed_by"] != worker_id:
        return False
    with db.tx() as c:
        c.execute(
            "UPDATE jobs SET transcript_json = ?, awaiting_user_edit = 1, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(transcript), _now_iso(), job_id),
        )
    return True


def submit_user_edit(job_id: str, owner_username: str, edit: dict,
                      word_replacement_mode: str | None = None,
                      voice_model: str | None = None,
                      voice_preset: str | None = None) -> bool:
    """User finalizes their transcript edit (post fruit-replacement or
    manual tweaks) and releases the job for synthesis — this is the
    actual Run/Generate action. Engine/preset/mode are chosen HERE, not
    at upload time (upload only transcribes), so the caller may pass
    overrides that replace whatever was set at job creation; callers must
    validate these against model_registry themselves before calling (same
    convention as create_job/the /upload route) — this function trusts
    its caller and persists them atomically with the state transition so
    the worker picks up exactly what was chosen at Run time, never a
    stale upload-time default."""
    job = get_job(job_id)
    if job is None or job["owner_username"] != owner_username:
        return False
    if job["state"] != "transcribing" or not job["awaiting_user_edit"]:
        return False
    now = security.utcnow()
    started = security.parse_iso(job["stage_started_at"])
    duration = (now - started).total_seconds()
    _record_stage_duration("transcribing", duration)
    history_json = _append_history(job, "transcribing", duration)
    final_mode = word_replacement_mode if word_replacement_mode is not None else job["word_replacement_mode"]
    final_model = voice_model if voice_model is not None else job["voice_model"]
    final_preset = voice_preset if voice_preset is not None else job["voice_preset"]
    with db.tx() as c:
        c.execute(
            """UPDATE jobs SET edit_json = ?, state = 'generating',
               awaiting_user_edit = 0, claimed_by = NULL, claim_expires_at = NULL,
               stage_started_at = ?, updated_at = ?, state_history_json = ?,
               word_replacement_mode = ?, voice_model = ?, voice_preset = ?
               WHERE id = ?""",
            (json.dumps(edit), _now_iso(), _now_iso(), history_json,
             final_mode, final_model, final_preset, job_id),
        )
    return True


def submit_result(job_id: str, worker_id: str, output_path: str,
                   engine_metadata: dict | None = None) -> bool:
    job = get_job(job_id)
    if job is None or job["claimed_by"] != worker_id:
        return False
    now = security.utcnow()
    started = security.parse_iso(job["stage_started_at"])
    duration = (now - started).total_seconds()
    _record_stage_duration(job["state"], duration)
    history_json = _append_history(job, job["state"], duration)
    finished_at = _now_iso()
    engine_result_json = json.dumps(engine_metadata) if engine_metadata else None
    with db.tx() as c:
        c.execute(
            """UPDATE jobs SET output_path = ?, state = 'done',
               claimed_by = NULL, claim_expires_at = NULL, updated_at = ?,
               state_history_json = ?, finished_at = ?, engine_result_json = ?
               WHERE id = ?""",
            (output_path, finished_at, history_json, finished_at, engine_result_json, job_id),
        )
    return True


def submit_error(job_id: str, worker_id: str, message: str, detail: str | None,
                  retryable: bool) -> bool:
    job = get_job(job_id)
    if job is None or job["claimed_by"] != worker_id:
        return False
    now_iso = _now_iso()
    if retryable and job["attempts"] < job["max_attempts"]:
        # Requeue: back to 'queued' so another (or the same) worker can retry.
        with db.tx() as c:
            c.execute(
                """UPDATE jobs SET state = 'queued', claimed_by = NULL,
                   claim_expires_at = NULL, error_message = ?, error_detail = ?,
                   retryable = 1, updated_at = ? WHERE id = ?""",
                (message, detail, now_iso, job_id),
            )
    else:
        with db.tx() as c:
            c.execute(
                """UPDATE jobs SET state = 'error', claimed_by = NULL,
                   claim_expires_at = NULL, error_message = ?, error_detail = ?,
                   retryable = ?, updated_at = ?, finished_at = ? WHERE id = ?""",
                (message, detail, 1 if retryable else 0, now_iso, now_iso, job_id),
            )
    return True


def to_public_dict(job) -> dict:
    """Serialize a job row for API/UI consumption. Never includes
    filesystem paths or worker identifiers (internal detail).

    Elapsed-time freeze: for a terminal job (done/error), elapsed is
    computed against the recorded `finished_at` rather than wall-clock
    `now`, so it stops advancing once the job actually finished instead of
    growing on every poll/reload. `finished_at` is set exactly once, at
    the moment `submit_result`/`submit_error` transitions a job into a
    terminal state (and is backfilled from `updated_at` for older rows —
    see db.py's `_backfill_finished_at`). If a terminal job somehow has no
    finished_at (shouldn't happen after that backfill), we fall back to
    `updated_at` — never to `now`, which would silently reintroduce the
    same bug — and that fallback is reported via `elapsed_basis` so it's
    visible rather than silent.
    """
    now = security.utcnow()
    is_terminal = job["state"] in TERMINAL_STATES
    elapsed_basis = "live"
    clock_end = now
    if is_terminal:
        if job["finished_at"]:
            clock_end = security.parse_iso(job["finished_at"])
            elapsed_basis = "finished_at"
        else:
            clock_end = security.parse_iso(job["updated_at"])
            elapsed_basis = "updated_at_fallback"
    stage_started = security.parse_iso(job["stage_started_at"])
    elapsed_current_stage = (clock_end - stage_started).total_seconds()
    elapsed_total = (clock_end - security.parse_iso(job["created_at"])).total_seconds()
    estimate = None if is_terminal else estimate_for_state(job["state"])
    expires_at = security.parse_iso(job["created_at"]) + timedelta(hours=config.TTL_HOURS)
    claimed_active = bool(job["claimed_by"]) and job["claim_expires_at"] is not None and (
        security.parse_iso(job["claim_expires_at"]) > now
    )
    return {
        "id": job["id"],
        "state": job["state"],
        "awaiting_user_edit": bool(job["awaiting_user_edit"]),
        "claimed_active": claimed_active,
        "input_filename": job["input_filename"],
        "input_duration_sec": job["input_duration_sec"],
        "word_replacement_mode": job["word_replacement_mode"],
        "voice_model": job["voice_model"],
        "voice_preset": job["voice_preset"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "finished_at": job["finished_at"],
        "engine_result": json.loads(job["engine_result_json"]) if job["engine_result_json"] else None,
        "expires_at": security.iso(expires_at),
        "is_terminal": is_terminal,
        "elapsed_basis": elapsed_basis,
        "elapsed_current_stage_sec": round(elapsed_current_stage, 1),
        "elapsed_total_sec": round(elapsed_total, 1),
        "estimate_current_stage_sec": (
            round(estimate, 1) if estimate is not None else None
        ),
        "estimate_label": (
            None if is_terminal
            else (f"~{round(estimate)}s" if estimate is not None else "estimating...")
        ),
        "transcript": json.loads(job["transcript_json"]) if job["transcript_json"] else None,
        "edit": json.loads(job["edit_json"]) if job["edit_json"] else None,
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "error_message": job["error_message"],
        "error_detail": job["error_detail"],
        "retryable": bool(job["retryable"]) if job["retryable"] is not None else None,
        "has_output": job["output_path"] is not None,
        "state_history": json.loads(job["state_history_json"] or "[]"),
    }


def _delete_job_files(row) -> None:
    """Best-effort delete of a job's upload + result files. A missing file
    is not an error — purging is idempotent and never fatal over a file
    that's already gone."""
    for path in (row["input_path"], row["output_path"]):
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def purge_expired_jobs() -> int:
    """Delete uploads, generated results, and the DB row for every job
    older than config.TTL_HOURS. Storage-light, no soft-delete. Returns
    the number of jobs purged. Safe to call from a background thread:
    each call gets its own thread-local DB connection via db.get_conn()."""
    cutoff = security.iso(security.utcnow() - timedelta(hours=config.TTL_HOURS))
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, input_path, output_path FROM jobs WHERE created_at < ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        _delete_job_files(row)
        with db.tx() as c:
            c.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
    if rows:
        log.info("purge_expired_jobs: removed %d job(s) older than %.0fh", len(rows), config.TTL_HOURS)
    return len(rows)


def purge_single_job(job_id: str, owner_username: str) -> tuple[bool, str | None]:
    """User-initiated immediate purge of exactly one job they own: its
    upload, its result, and its DB row — nothing else. Refuses while the
    worker currently holds a live claim lease on it (state is actively
    being processed right now) so we never delete a file the worker is
    mid-stage on, or race its next "job not found" write; everything else
    (queued/unclaimed, paused awaiting a human edit, or terminal
    done/error) is safe to purge immediately. Returns (ok, reason);
    reason is a user-facing message when ok is False."""
    job = get_job(job_id)
    if job is None or job["owner_username"] != owner_username:
        return False, "job not found"
    now = security.utcnow()
    if job["claimed_by"] and job["claim_expires_at"] and security.parse_iso(job["claim_expires_at"]) > now:
        return False, (
            "this job is actively being processed by the worker right now — "
            "try again once it finishes, or in a few minutes once its "
            "processing lease expires"
        )
    _delete_job_files(job)
    with db.tx() as c:
        c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    log.info("job %s manually purged by %s", job_id, owner_username)
    return True, None
