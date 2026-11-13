# Confirmed interface contract

Confirmed against portal-queue's commit `dad3a8d` (worktree
`agent-a5d6afa8b356b9508`), merged to master as `75afb54`, on 2026-11-10 —
via portal-queue's own message, their `docs/worker-protocol.md` and
`docs/portal-notes.md`, direct code review of `portal/app/`, and a live
smoke test (ran their real server + real `mock_worker.py` against an
isolated scratch data dir and exercised every endpoint below end to end).
No more `# ASSUMED` markers in the test files — if you find one, that's
stale, flag it.

## Job lifecycle states

Exactly as PLAN.md specifies: `queued -> transcribing -> generating ->
blending -> exporting -> done` (or `error`). The "awaiting a human edit"
pause is layered as a boolean `awaiting_user_edit` field while `state`
stays `"transcribing"` — there's no separate top-level state for it.

Job JSON (`GET /api/jobs/{id}`): `id, state, awaiting_user_edit,
input_filename, input_duration_sec, created_at, updated_at, finished_at
(null until the job reaches done/error), is_terminal,
elapsed_current_stage_sec, elapsed_total_sec, estimate_current_stage_sec
(null until >=3 measured samples, or once `is_terminal`), estimate_label
(null once `is_terminal` — no more "estimating..." for a finished job),
elapsed_basis ("live" | "finished_at" | "updated_at_fallback" — which
clock elapsed was computed against), word_replacement_mode, voice_model,
voice_preset, engine_result (null until done — `{"engine", "device",
"engine_revision", "model_ckpt_file", "license"}`, what *actually* ran,
read from the worker's own clone_result.json, independent of
voice_model/voice_preset which are what was *selected*), transcript,
edit, attempts, max_attempts, error_message, error_detail, retryable,
has_output, state_history`.

**Real, independently selectable voice engines (added 2026-11-12):**
`voice_model` is one of `openvoice` (default), `chatterbox`, `f5tts` —
see `portal/app/model_registry.py`. Each has its own pinned Mac mini
venv and pipeline script; none falls back to another if unavailable
(confirmed live: an unavailable engine venv produces a clear,
engine-specific, non-retryable `error` state, `voice_model` unchanged).
`f5tts`'s checkpoint is CC-BY-NC/noncommercial — internal use only, see
`model_registry.LICENSE_NOTE`.

**Engine/preset/mode are chosen at Run time, not upload time (added
2026-11-12):** `POST /upload` still accepts and defaults these three
fields for backward compatibility, but the real UI collects them on the
submit-edit screen instead — see `POST /api/jobs/{id}/submit-edit` below.

**Elapsed freeze (added 2026-11-12, fixing a bug where a done/error job's
elapsed time kept growing on every poll/reload):** once `state` is `done`
or `error`, `elapsed_current_stage_sec`/`elapsed_total_sec` are computed
against the recorded `finished_at` timestamp, not wall-clock `now` — they
stop changing. `finished_at` is set exactly once, at the same DB write
that transitions a job into `done` (`submit_result`) or non-retryable
`error` (`submit_error`); it is never touched again. Jobs that reached a
terminal state before this fix shipped had `finished_at` backfilled from
`updated_at` (verified safe: nothing in jobs.py writes `updated_at` on an
already-terminal job).

## Admin/browser API (session cookie `portal_session`, httponly)

- `POST /login` — **form** fields `username`, `password` (not JSON).
  Succeeds (303) for any correct password, including the still-default
  one — the forced-change gate applies to subsequent requests, not login
  itself.
- `POST /logout`.
- `GET /change-password`, `POST /change-password` — page routes (form:
  `current_password`, `new_password`, `confirm_password`).
- `POST /api/change-password` — form: `current_password`, `new_password`
  (no confirm field). Returns `{"ok": true}` or `{"ok": false, "error":
  "..."}` — failures are reported in-band with 200, not via status code.
- `POST /upload` — multipart, field name **`file`** (not `video`). 303 to
  `/jobs/{id}` on success; 400/413 (re-rendered dashboard) on bad
  duration/size/unparseable container.
- `GET /api/jobs/{id}`.
- `POST /api/jobs/{id}/fruit-replace` — JSON `{"text": "..."}` -> preview
  `{"text", "replacements": [{"index","original","replacement"}]}`, does
  not persist anything.
- `POST /api/jobs/{id}/submit-edit` — JSON `{"text", "replacements"}`,
  plus optional `word_replacement_mode`/`voice_model`/`voice_preset`
  (added 2026-11-12) — omitted fields keep whatever was set at upload;
  provided ones are validated against `model_registry` (400 on an
  unknown engine/preset combo, job left untouched) and persisted
  atomically with the edit itself. This is where the real UI lets the
  user choose/change the engine, right before Run. Only valid when
  `state=="transcribing" && awaiting_user_edit`; 409 otherwise.
  Transitions to `generating`.
- `GET /jobs/{id}/media/original`, `GET /jobs/{id}/media/result`,
  `GET /jobs/{id}/download` — session-gated, Range-seekable (FileResponse
  from a server-stored path, never a user-supplied path — no traversal
  surface).

Auth gating: no session -> `401` (API) / `303` to `/login` (pages).
Session but `must_change_password` -> `403` (API, except
`/api/change-password` itself) / `303` to `/change-password` (pages).

## Worker API (`Authorization: Bearer <token>`, separate scheme)

No separate "claim" call — `GET /worker/jobs/next` polls *and* atomically
claims in one request (`204` = nothing eligible, `200` = claimed job
JSON incl. `input_download_url`). Optional `X-Worker-Id` header for
lease/logging bookkeeping (defaults to `"worker"`); not itself a
credential.

- `GET /worker/jobs/next`
- `GET /worker/jobs/{id}` (re-fetch without claiming)
- `GET /worker/jobs/{id}/input` (download original, binary)
- `POST /worker/jobs/{id}/heartbeat` — body `{}`, extends the lease
- `POST /worker/jobs/{id}/transcript` — `{"text", "words": [...],
  "mock": bool}` -> sets `awaiting_user_edit=1`
- `POST /worker/jobs/{id}/stage` — `{"state": "blending"|"exporting"}`
- `POST /worker/jobs/{id}/result` — multipart, field `file` -> `done`
- `POST /worker/jobs/{id}/error` — `{"message", "detail", "retryable"}`.
  `retryable && attempts < max_attempts` -> back to `queued`; otherwise
  terminal `error`.

Claim lease: `PORTAL_JOB_LEASE_SECONDS` (default 120s). Eligible-to-claim
= `queued`, or `transcribing`/`generating` with `awaiting_user_edit=0`, or
`blending`/`exporting` — in all non-`queued` cases only if unclaimed or
the existing claim's lease has expired. **Verified live** (short-lease
server, `PORTAL_JOB_LEASE_SECONDS=2`): a job stalled in `blending` past
its lease (simulated worker death — no heartbeat, no further stage call)
was correctly reclaimed by a fresh poll, state preserved as `blending`,
`attempts` incremented. This was the specific edge case portal-queue
flagged as easy to get wrong in the WHERE clause; confirmed fixed.

## What's deliberately stubbed right now

`mock_worker.py` sends a fixed transcript (`mock: true`) and does a
byte-identical passthrough for synthesis/blend/export — these tests
exercise the queue/auth/protocol plumbing, not voice quality. The real
worker (model-feasibility's pipeline, once wired into this same contract)
is what Phase 1/2 validation covers.

## Resolved: dead-code note (was not a security issue)

~~`routers/auth.py` used to check the 8-char minimum before the
"cannot equal the default" check, making the latter unreachable with the
stock 5-char default.~~ Fixed by portal-queue in commit `2bad758`: the
default-password check now runs first in both `change_password_submit`
and `api_change_password`. Verified in this suite (see
`test_auth.py::test_password_change_rejects_the_default_as_the_new_password`,
which passes without any assumption about which check fires).

## Word-index alignment + bounds-check (added in portal-queue's 2bad758)

`POST /api/jobs/{id}/submit-edit` now bounds-checks every
`replacements[].index` against the length of the original transcript's
`words` array (when one exists) *before* the edit is accepted — 400 with
a message explaining the index is out of range if it fails, or if an
entry's `index` isn't an integer. This exists because a replacement
index is only meaningful positioned against the ORIGINAL transcript's
word list (`docs/worker-protocol.md`'s new "word-index alignment"
section); a manual edit that changes word count can make previously
valid indices stale, and the server now refuses those outright instead
of letting a worker hit an `IndexError` downstream. `submit_transcript`
also now logs a warning (not an error) if a worker's `words` count
doesn't match `text.split()`, surfacing a worker-side alignment bug
without blocking the pipeline.

Verified live (`test_lifecycle.py::test_submit_edit_rejects_out_of_range_replacement_index`,
`::test_submit_edit_rejects_non_integer_replacement_index`): both get
400, and a rejected submit-edit leaves the job's state/awaiting_user_edit
unchanged (no partial mutation).
