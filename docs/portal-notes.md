# Portal notes (Phase 3 + 4)

Owner: portal-queue. Covers the browser portal, job queue, and the two
separate auth schemes. See `docs/worker-protocol.md` for the
worker-facing API contract in detail.

## Stack

Python 3 + FastAPI + Jinja2 templates + vanilla JS (no frontend
framework/build step) + sqlite3 (stdlib, no ORM). Chosen per the mission
brief's "pick a reasonably lightweight stack... avoid heavy frameworks
the MVP doesn't need." Video duration is probed with a small hand-rolled
MP4/MOV (ISO-BMFF) box parser (`portal/app/media.py`) rather than
shelling out to ffmpeg/ffprobe, since neither is confirmed present on
either the portal host or the Mac mini (see PLAN.md's environment
notes) — this also means one less runtime dependency to install anywhere
this gets deployed.

## Running it locally

```bash
cd portal
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/run_dev.sh            # binds 127.0.0.1:8000 only — local dev
```

First run seeds `admin` / `admin` with a forced password-change flag,
and provisions a worker token at `portal/data/dev_worker_token.txt`
(0600, gitignored — see docs/worker-protocol.md for why it's a file and
not printed to any log stream). Open `http://127.0.0.1:8000`.

To exercise the full queue end-to-end, run the mock worker separately:

```bash
cd portal
PORTAL_WORKER_TOKEN=$(cat data/dev_worker_token.txt) .venv/bin/python mock_worker.py --base-url http://127.0.0.1:8000
```

Upload a 20-60s MP4/MOV (target 30-45s), wait for the mock transcript to
appear, edit it or click "Random fruit replacement", submit, and the
mock worker will carry the job through to `done` (as a passthrough — see
below).

## What's real vs. stubbed right now

**Real and fully working:**
- Upload validation (size + duration, pure-Python MP4/MOV parsing).
- The entire job state machine, outbound-only worker polling protocol,
  claim leasing + automatic retry/requeue, honest elapsed-time +
  measured-average estimates (never fabricated).
- Admin login with bcrypt-hashed passwords, forced password-change flow
  that blocks portal use (both page redirects and API 403s) until
  satisfied.
- Worker bearer-token auth, fully separate from admin auth.
- The "random fruit replacement" heuristic (stopword/short-word/cheap
  proper-noun filtering, 1-2 replacements from a fixed fruit list,
  case-preserving).
- Before/after video playback (with HTTP Range support for seeking) and
  authenticated download.

**Deliberately stubbed, pending Phase 1/2 model validation
(model-feasibility's workstream):**
- Transcription is a fixed mock sentence with evenly-spaced fake word
  timestamps (`transcript.mock: true` is surfaced to the UI so this is
  never silently presented as real STT output).
- "Synthesis + blending + export" is a byte-identical passthrough of the
  original upload. Before/after playback will look and sound identical
  until a real worker (implementing the exact same
  `docs/worker-protocol.md` contract) replaces `mock_worker.py`.

This split is intentional: the queue/portal/auth architecture is fully
testable today without waiting on local STT/TTS model validation on the
Mac mini.

## Auth — two separate schemes, do not conflate

1. **Admin/browser login** (`portal/app/security.py`,
   `routers/auth.py`): bcrypt-hashed password, server-side session table,
   httponly cookie. Default `admin`/`admin` is seeded only on first run
   and only in a fresh local DB, with `must_change_password=1`. Every
   page route and every `/api/*` route is gated — unauthenticated ->
   redirect to `/login` (pages) or `401` (API); authenticated but still
   on the default/unchanged password -> redirect to `/change-password`
   (pages) or `403` (API), for every route except the change-password
   routes themselves. This cannot be dismissed or bypassed short of
   actually changing the password.
2. **Worker auth** (same file, `ensure_worker_token`/`verify_worker_token`):
   a long random bearer token, unrelated to the admin password, verified
   with a constant-time comparison. See docs/worker-protocol.md for full
   detail on why a fast hash (not bcrypt) is the right choice for this
   specific credential.

## Deployment gate — read before anything touches the public domain

**The default `admin`/`admin` credentials, and the auto-generated dev
worker token, must only ever be reachable on this local/private dev
setup.** Before the portal is exposed on the user's public domain (per
PLAN.md's routing section):

1. The admin password must have already been changed from the default
   (log in once locally and go through the forced change flow, or seed a
   different `PORTAL_DEFAULT_ADMIN_PASSWORD` before first boot).
2. `PORTAL_WORKER_TOKEN` must be set explicitly to a real random value
   (not left to the auto-generated dev-file fallback) on both the server
   and the Mac mini worker's config.
3. Set `PORTAL_PUBLIC_DEPLOYMENT=1` in the server's environment. On
   startup, if that flag is set and the admin account still has
   `must_change_password=1`, the app **refuses to start** rather than
   silently serving default credentials publicly (see
   `app/main.py:on_startup`).
4. Serve over HTTPS (the session cookie is only marked `secure` when the
   request scheme is `https`; plan to put this behind the user's
   existing gateway/TLS termination per PLAN.md's routing constraints,
   not expose it with plaintext HTTP beyond local dev).

## Logging — no secrets, ever

Every logging statement in `portal/app/*` was written (and re-checked
while assembling this) to log outcomes and identifiers only — usernames,
job ids, state names, boolean results — never passwords, never the raw
worker token, never a raw `Authorization` header. The one secret the
server itself generates (the dev worker token) is deliberately written
to a dedicated 0600 file instead of being printed anywhere, specifically
because stdout/stderr commonly end up redirected into a persistent log
by whatever process-supervisor runs this in a real deployment — see
docs/worker-protocol.md for the reasoning. If you add a new log
statement anywhere in this codebase, check it against this rule before
committing.

## Model selection (word-replacement mode, voice model + preset)

`portal/app/model_registry.py` is the single source of truth for every
selectable id. **Chosen at Run time (the submit-edit screen on the job
page), not at upload** — upload only transcribes; nothing is synthesized
until the user presses Run, and that's also where they pick the engine.
Validated server-side in `routers/portal.py:api_submit_edit`, persisted
atomically with the edit itself, surfaced to the worker via the existing
job-detail JSON (`routers/worker.py` reuses `jobs.to_public_dict`).

- **Word-replacement mode** (`manual` default / `fruit`): a *label/default*
  for which of the two already-existing editing paths (manual text
  editing vs. the fruit-replace heuristic) the UI leans toward. Both
  remain available regardless of this selection — it does not gate
  either API endpoint.
- **Voice model + preset — three real, independently dispatched engines**
  as of 2026-11-12, each with its own persistent, pinned Mac mini venv
  and its own pipeline script (`worker/run_worker.py`'s
  `_dispatch_clone_stage`/`_ENGINE_DISPATCH`):
  - `openvoice` (default) — `worker/pipeline_scripts/run_clone.py`, venv
    `tts-venv`. Presets `balanced` (default, zero overrides — exactly the
    script's own validated defaults) and `fast` (trades quality for
    speed via `VOICE_RESEMBLYZER_MATCH=0`/`VOICE_TAU=0.60`).
  - `chatterbox` — `worker/pipeline_scripts/run_clone_chatterbox.py`,
    venv `tts-spike-chatterbox` (`chatterbox-tts==0.1.7`, `torch==2.6.0`,
    `torchaudio==2.6.0`, HF checkpoint `ResembleAI/chatterbox` pinned to
    commit `5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18`). MIT-licensed,
    code and weights.
  - `f5tts` — `worker/pipeline_scripts/run_clone_f5.py`, venv
    `tts-spike-f5` (`f5-tts==1.1.22`, `torch==2.15.0`,
    `torchaudio==2.11.1`, HF checkpoint `SWivid/F5-TTS` pinned to commit
    `84e5a410d9cead4de2f847e7c9369a6440bdfaca`,
    `F5TTS_v1_Base/model_1250000.safetensors`). **Code is MIT; the
    pretrained checkpoint is CC-BY-NC (noncommercial), confirmed against
    the package's own shipped `METADATA`/README (trained on the Emilia
    dataset) — internal, non-commercial evaluation only, not cleared for
    any commercial product/service use.** The UI shows this restriction
    next to the engine choice (`model_registry.LICENSE_NOTE`); never
    omit it if this is ever discussed outside this internal deployment.
  - Chatterbox and F5-TTS each have exactly one preset, `no_rubberband`:
    reproduces the measured-best spike recipe (trim silence → RMS
    loudness-match, clamped ±12dB → pad up to the replaced word's slot
    if synthesized shorter — **never truncated or time-stretched if
    longer**; `run_blend.py`'s splice logic appends spans sequentially,
    so an overflow just makes that part of the output video a little
    longer rather than cutting or overlapping surrounding speech). No
    pitch/tempo/rubberband correction at all for these two engines — see
    "Why rubberband was dropped for these two engines" below.
  - An engine whose venv is missing/broken on the worker host raises a
    clear, engine-specific, non-retryable error (`WorkerError`) —
    confirmed live (temporarily pointed `CHATTERBOX_VENV_PYTHON` at a
    nonexistent path): the job lands in `error` with `voice_model` still
    `chatterbox` and `has_output: False`. It never silently substitutes
    OpenVoice.
  - What actually ran (vs. what was merely selected) is recorded in
    `jobs.engine_result_json` → `to_public_dict()`'s `engine_result`
    field (`engine`, `device`, `engine_revision`, `model_ckpt_file`,
    `license`) — read from each clone script's own `clone_result.json`,
    normalized by `run_worker.py:_read_engine_metadata`. Shown on the job
    page as "Actually ran: …".

### Why rubberband was dropped for these two engines

An earlier investigation (`INVESTIGATION.md`, root-caused 2026-11-12)
measured that ffmpeg's `rubberband` pitch/tempo correction — used by
`run_clone.py` to time-align a synthesized replacement to its original
slot — costs real, measured Resemblyzer similarity whenever it does real
correction (deltas of -0.05 to -0.17 across 8 controlled data points; see
that document for the full numbers). This is still present and
unconditional in OpenVoice's path (`VOICE_PITCH_MATCH` defaults on) — not
changed here, since the user only asked for OpenVoice to be *preserved*,
not modified. For Chatterbox/F5-TTS, the ask was specifically to
reproduce the demonstrated-best recipe, so their scripts never invoke
rubberband at all.

## Retention (TTL)

A background thread (`portal/app/main.py:_purge_loop`, started at
app startup) calls `jobs.purge_expired_jobs()` once immediately and then
every `PORTAL_PURGE_INTERVAL_SECONDS` (default 1h). It deletes a job's
upload file, output file (best-effort — a missing file is not an error),
and its DB row once `created_at` is older than `PORTAL_TTL_HOURS`
(default 72h). No soft-delete, no archive tier — storage-light by
design. `expires_at` is computed (not stored) in `jobs.to_public_dict`
and shown in the dashboard/job UI so users know results aren't permanent.

## Session inactivity + logout

Explicit logout (`POST /logout`) already existed. Added: a sliding
24h-inactivity expiry (`PORTAL_INACTIVITY_TTL_SECONDS`) on top of the
existing absolute per-session ceiling, which was raised from 12h to 30
days (`PORTAL_SESSION_TTL_SECONDS`) specifically so the *inactivity* rule
is what a teammate actually experiences — the old 12h absolute cap would
otherwise log someone out mid-use during one long continuous session,
which contradicts "log out on inactivity," not "log out on use." Tracked
via a `sessions.last_seen_at` column, updated on each authenticated
request but throttled to at most once per 60s to avoid a DB write on
every poll (`app.js` polls every 2s).

## Manual purge (per-job, user-initiated)

`POST /jobs/{id}/purge` (page route, admin session auth, triggered from a
plain HTML form + `confirm()` on both the dashboard job list and the job
detail page — see `jobs.purge_single_job`). Separate from, and in
addition to, the automatic 72h TTL purge. Deletes exactly that job's
upload, result, and DB row — never touches another job. Refuses (409
surfaced as a redirect + banner, not a raw error page) while the worker
currently holds a live, unexpired claim lease on the job (`claimed_by`
set and `claim_expires_at` in the future) — that's the only "actively
processing" condition the schema already tracks, so purge is blocked
exactly during the window the worker might still make a call referencing
this job id. It's safe outside that window (queued-but-unclaimed, paused
awaiting a human edit, or terminal `done`/`error`) — in the queued/paused
cases the row simply disappears before the worker (or the user,
resubmitting an edit) ever gets to touch it again, which fails cleanly
(404/"job not found") rather than corrupting anything.

To purge one record manually right now: open the job (or find its row on
the dashboard) and click "Purge" / "Purge this job now", confirm the
dialog. To verify the 72h automatic expiry later without waiting 72
hours: temporarily set `PORTAL_TTL_HOURS=0` (or a small number) in the
environment, restart the portal, wait up to `PORTAL_PURGE_INTERVAL_SECONDS`
(default 1h — also reducible for a faster check) for the background
purge loop to run, and confirm the job's row/files are gone; or call
`jobs.purge_expired_jobs()` directly from a Python shell against the same
`PORTAL_DATA_DIR`.

## Browser recording (optional alternative to file upload)

`portal/app/static/record.js`, loaded only from `dashboard.html`. Lets a
teammate record directly from their device's camera/mic instead of
uploading a file, reusing the exact same `/upload` endpoint and the same
duration/model-selector validation — recording produces a `File`-like
`Blob` that's POSTed as `multipart/form-data` via `fetch`, not a new
upload path on the backend.

**Explicit-action and privacy requirements, all enforced in `record.js`:**
the camera/mic permission prompt fires only after the user clicks "Start
recording" (never on page load); a visible timer and "Stop" button track
the in-progress recording; stopping shows a local preview (via
`URL.createObjectURL`, i.e. an in-memory blob reference — nothing is
written to disk, IndexedDB, or `localStorage`); "Discard / re-record" and
"Upload this recording" are two separate, explicit actions — nothing is
uploaded until the second one is clicked; camera/mic tracks are released
(`track.stop()`) as soon as recording stops or the user navigates away.

**Known limitation #1, confirmed root cause (2026-11-12) — the deployed
portal is plain HTTP, which blocks this on every browser, not just
non-Safari ones.** `navigator.mediaDevices`/`getUserMedia` only exist in
a secure context (`https:`, or `http://localhost`/`127.0.0.1`); a portal
reachable only via a private-network address over plain HTTP does not
qualify in any browser. If your private network provider offers
certificate issuance for its own internal hostnames (e.g. Tailscale's
`tailscale cert` + MagicDNS), that's the easiest fix — it typically
requires a one-time operator/cert-issuance command on that host, run
interactively by whoever holds the credentials for it. **This is the
actual blocker for recording in that scenario — fix HTTPS before
worrying about browser/codec support at all.**

**Known limitation #2 — even once HTTPS is fixed, recording stays
Safari-only.** The capability gate also checks
`MediaRecorder.isTypeSupported("video/mp4")`, only true on browsers
whose `MediaRecorder` can natively encode to MP4 (Safari/macOS and iOS,
roughly 14.1+). Chrome and Firefox only offer WebM via `MediaRecorder`,
and `media.py`'s duration validator deliberately only parses ISO-BMFF
(MP4/MOV), not WebM/EBML — teaching it a second container format is a
real, separate change deliberately not made here, to avoid risking the
stable file-upload path.

**UX (added 2026-11-12):** rather than silently vanishing when
unavailable, the record section now shows a short explanation —
distinguishing "needs HTTPS" from "this browser doesn't support it" —
instead of just disappearing with no trace. File upload is unaffected
either way, on any browser/HTTPS state.

**This was verified only by reading the spec and compile/syntax checking
the JS (`node --check`); there was no real browser or camera available
in the environment this was built in to actually click through it.**
Treat the recording flow itself (not the HTTPS/codec gating logic) as
implemented-but-unverified until someone tries it on an actual Safari
browser, after HTTPS is fixed.

## Known simplifications / follow-ups

- Worker auth currently supports one shared token (not per-worker
  identity/revocation); `worker_tokens` table and the
  `verify_worker_token` function are structured so multiple named
  workers could be added later without changing the wire protocol
  (`X-Worker-Id` header is already accepted).
- MP4/MOV duration probing doesn't handle every exotic container
  (fragmented/streaming MP4, WebM/EBML); unsupported files are rejected
  with a clear error rather than mis-measured or silently accepted.
- Sessions are stored in the same sqlite file as everything else; fine
  for this scale, would want a proper session store if this ever needs
  multiple server processes/horizontal scaling.
- Still a single shared `admin` account, no per-teammate accounts — see
  `docs/admin-credentials.md` for the tradeoffs this implies for a team
  deployment.
- Only one voice model (`openvoice`) is actually selectable today; the
  registry/dispatch plumbing (`model_registry.py`,
  `run_worker.py:_voice_preset_overrides`) is structured so adding a
  second engine later doesn't require re-plumbing the job schema or
  worker protocol, but no second engine is wired in yet.
