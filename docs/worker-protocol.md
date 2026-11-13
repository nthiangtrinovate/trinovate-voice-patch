# Worker protocol (Phase 4)

This is the contract between the portal server (`portal/app`) and any
worker that processes jobs — the mock worker today
(`portal/mock_worker.py`), and eventually the real Mac mini worker that
model-feasibility builds once the local STT/TTS stack is validated.
**Implement this same interface and you're a compatible worker** — no
other coupling to the portal's internals is required or assumed.

## The one constraint that shapes everything here

The Mac mini is **outbound-only**. It will never accept an inbound
connection from the server or the internet. Every single request below
is one the worker *initiates*. The server never calls the worker, never
needs to know its address, and never pushes anything to it. The worker:

- **polls** (`GET /worker/jobs/next`) to find out it has work,
- **pulls** its input (`GET /worker/jobs/{id}/input`),
- **pushes** progress and results (`POST` to `/transcript`, `/stage`,
  `/result`, `/error`, `/heartbeat`).

If you're extending this later, keep that invariant: nothing added here
should ever require the server to open a connection to the worker.

## Auth

Every `/worker/*` request requires:

```
Authorization: Bearer <worker token>
```

This is a **separate credential from the admin/browser login** — a long,
random, high-entropy token, not a password, never reused across the two
schemes. See `portal/app/security.py` for how it's provisioned:

- Set `PORTAL_WORKER_TOKEN` the same on the server and the worker for
  anything beyond a single local dev session.
- If unset, the server generates one at startup and writes it (mode
  0600) to `portal/data/dev_worker_token.txt` — **never** to stdout,
  stderr, or any logger, because those commonly get redirected into a
  persistent log by whatever runs the process. `mock_worker.py` reads
  that file automatically as a fallback if `PORTAL_WORKER_TOKEN` isn't
  set in its own environment.

An optional `X-Worker-Id: <name>` header identifies which worker claimed
a job (for logging/lease bookkeeping); it defaults to `"worker"` if
omitted. It is not itself a credential.

A bad/missing token gets `401` and is logged as an outcome only
(`"worker auth failed: ..."`) — never the header value itself.

## Job states and who drives each transition

States, exactly as scoped in PLAN.md:

```
queued -> transcribing -> generating -> blending -> exporting -> done
                                                            \
                                                             -> error
```

| Transition | Driven by | How |
|---|---|---|
| `queued` -> `transcribing` | worker | implicit: claiming a `queued` job via `GET /worker/jobs/next` moves it to `transcribing` |
| (within `transcribing`) worker finishes STT | worker | `POST /worker/jobs/{id}/transcript` — sets `awaiting_user_edit=1`, state stays `transcribing` |
| `transcribing` (awaiting edit) -> `generating` | **the human, via the browser** | user submits their edited transcript (`POST /api/jobs/{id}/submit-edit` on the portal API, not the worker API) |
| `generating` -> `blending` | worker | `POST /worker/jobs/{id}/stage {"state": "blending"}` |
| `blending` -> `exporting` | worker | `POST /worker/jobs/{id}/stage {"state": "exporting"}` |
| `exporting` -> `done` | worker | `POST /worker/jobs/{id}/result` (multipart file upload) |
| any non-terminal -> `error` or back to `queued` | worker | `POST /worker/jobs/{id}/error` — see retry logic below |

**Why `transcribing` covers two things**: PLAN.md's state enum has no
separate "awaiting human edit" state. Rather than invent one, the
awaiting-edit pause is layered on top as a boolean field
(`awaiting_user_edit`) while `state` stays `"transcribing"`. A job in
`transcribing` with `awaiting_user_edit=1` is **not** eligible for a
worker to claim — it's waiting on a person, not stalled. Check that flag
if you're deciding whether to act on a job.

## Endpoints

### `GET /worker/jobs/next`

Atomically claims the oldest eligible job. Eligible = `queued`, or any
stage (`transcribing` with `awaiting_user_edit=0`, `generating` with
`awaiting_user_edit=0`, `blending`, `exporting`) whose claim lease has
expired (previous worker died mid-stage) or was never claimed.

- `204 No Content` — nothing to do right now. Poll again after a short
  delay (the mock worker defaults to 2s; pick whatever's reasonable for
  your worker's resource budget).
- `200 OK` — job detail JSON:

```json
{
  "id": "uuid",
  "state": "transcribing",
  "awaiting_user_edit": false,
  "input_filename": "clip.mp4",
  "input_duration_sec": 35.0,
  "input_download_url": "/worker/jobs/{id}/input",
  "word_replacement_mode": "manual",
  "voice_model": "openvoice",
  "voice_preset": "balanced",
  "transcript": null,
  "edit": null,
  "finished_at": null,
  "engine_result": null,
  "is_terminal": false,
  "elapsed_basis": "live",
  "elapsed_current_stage_sec": 0.0,
  "elapsed_total_sec": 1.2,
  "estimate_current_stage_sec": null,
  "estimate_label": "estimating...",
  "attempts": 1,
  "max_attempts": 3,
  "state_history": [],
  "expires_at": "2026-11-15T12:00:00+00:00"
}
```

`word_replacement_mode`/`voice_model`/`voice_preset` are chosen at **Run
time** — `POST /api/jobs/{id}/submit-edit` on the portal API (the same
call that releases the job from `awaiting_user_edit` into `generating`),
not at upload — see `portal/app/model_registry.py`, the single source of
truth for valid ids, and `routers/portal.py:api_submit_edit` for the
server-side validation. `word_replacement_mode` is informational (both
the fruit-replace and manual-edit API paths stay available regardless)
but **binding** for `voice_model`/`voice_preset`: at `generating`, the
worker dispatches to that engine's own venv/pipeline script
(`worker/run_worker.py:_dispatch_clone_stage`/`_ENGINE_DISPATCH` —
`openvoice` → `run_clone.py`, `chatterbox` → `run_clone_chatterbox.py`,
`f5tts` → `run_clone_f5.py`, each with its own persistent pinned venv)
and must fail the job (non-retryable `WorkerError`) rather than guess or
substitute a different engine if it doesn't recognize the
model/preset, or if that engine's venv isn't present on this worker
host. `engine_result` (set only once the job reaches `done`) is what
*actually* ran, read back from that engine's own `clone_result.json` by
`run_worker.py:_read_engine_metadata` and passed to
`POST /worker/jobs/{id}/result` as an additional `engine_metadata` form
field (JSON string) alongside the result file — `{"engine", "device",
"engine_revision", "model_ckpt_file", "license"}`, letting a caller
distinguish the engine selected from the engine that ran, and surfacing
the exact pinned checkpoint/license for engines like `f5tts` whose
weights carry a use restriction. `finished_at`/`is_terminal`/
`elapsed_basis` describe the elapsed-time freeze once a job reaches
`done`/`error` — see `jobs.py:to_public_dict`'s docstring. `expires_at`
reflects `PORTAL_TTL_HOURS` (default 72h from `created_at`) — a
background purge loop in `portal/app/main.py` deletes the job's files
and DB row once past this.

When a job is claimed at `generating`, `edit` is populated:
`{"text": "...", "replacements": [{"index": 3, "original": "mock", "replacement": "cherry"}]}`
— this is what to actually synthesize/blend against.

Claiming sets a lease (`PORTAL_JOB_LEASE_SECONDS`, default **300s** — raised
from an initial 120s default after the real worker measured ~82s for a cold
STT model load + inference on a 21.9s test clip, which left only ~38s of
margin; see the comment in `portal/app/config.py` for the full reasoning).
If your worker needs longer per stage, call the heartbeat endpoint
periodically or another worker (or a retry) may reclaim the job out from
under you.

**Heartbeat timing matters, not just the lease ceiling.** A single
heartbeat called right before a long blocking call (e.g. right before
invoking an STT subprocess) resets the clock at that moment, but if the
call itself runs long — slower hardware, a bigger clip, the Mac mini busy
with something else — nothing refreshes the lease again until that call
returns. The 300s default is a safety margin on top of this, not a
substitute for it. If your worker's single slowest call could plausibly
run past a couple of minutes under adverse conditions, consider
heartbeating periodically *during* that call (e.g. a lightweight
background thread that pings `/heartbeat` every 30-60s while a subprocess
runs) rather than relying solely on the pre-call heartbeat + a generous
lease. Neither side (server default vs. worker-side periodic heartbeats)
is sufficient alone against a truly pathological case; use both.

#### Word-index alignment between `transcript.words` and `edit.replacements[].index`

This is the one place the wire format has a non-obvious invariant, worth
spelling out precisely before you write code against it (per
`docs/feasibility-report.md`, the real pipeline builds a short context
phrase — 2 words before/after the target — and needs the target word's
original `start`/`end` to know where to splice, so this alignment matters
for real synthesis, not just for display).

- `edit.replacements[i].index` is a position into `text.split()` as
  evaluated against the **original, pristine transcript text** (i.e. the
  same text you submitted via `POST /transcript`) — it is *not* an id or a
  timestamp. The intended (and currently only implemented) way this gets
  produced is: the portal's "random fruit replacement" heuristic runs
  against the still-untouched transcript text and returns indices
  positioned in that same text's `.split()`.
- **The contract this requires of you, the worker, submitting the
  transcript**: `transcript.words` must correspond to `transcript.text`
  positionally and 1:1 — i.e. `len(words) == len(text.split())` and
  `words[i]` is the i-th whitespace-delimited token of `text` in order.
  The simplest way to guarantee this is to literally derive
  `text = " ".join(w["word"] for w in words)` rather than deriving the two
  independently from your STT output. `mock_worker.py` does exactly this
  (see `build_mock_words`). If your STT's native tokenization doesn't line
  up 1:1 with whitespace-splitting (e.g. it splits contractions or
  attaches punctuation differently), normalize `words` to match before
  submitting — don't submit a `words` list whose length disagrees with
  `text.split()`. The server logs a warning (not a hard rejection) if it
  ever sees a mismatch, specifically so this is visible rather than
  silently producing wrong splice points downstream.
- Given that invariant holds, `transcript.words[replacements[i].index]`
  gives you the original word's `{word, start, end}` to build your context
  phrase and locate the splice boundary.
- **Known limitation, not yet solved by this contract**: if the user
  manually edits the transcript text (inserting/deleting words) *before or
  after* clicking fruit-replace, word positions can shift and
  `replacements[].index` is no longer guaranteed to align with
  `transcript.words`. The portal server bounds-checks `index` against
  `len(transcript.words)` at submit time and rejects (`400`) anything
  clearly out of range, but it does **not** attempt to re-align indices
  after a manual edit — that's a real text-diff problem PLAN.md scopes to
  Phase 2 ("text-level operations map to audio spans"), not yet solved
  here. If you receive an `edit` whose indices you can't trust (e.g. you
  want to double check `edit.text.split()[idx] != replacements[idx].replacement`
  as a sanity check), the safe fallback is to treat it as a full-utterance
  resynthesis rather than a targeted splice, or to report a `retryable:
  false` error asking the user to redo the edit — don't guess at
  mismatched positions.

### `GET /worker/jobs/{id}` (optional)

Same job detail shape as above, for re-fetching state without claiming
anything new.

### `GET /worker/jobs/{id}/input`

Streams the original uploaded video (binary, `video/mp4`). This is the
one and only outbound *download* path.

### `POST /worker/jobs/{id}/heartbeat`

Body: `{}`. Extends the claim lease. Call this if a stage is going to
take longer than the lease window.

### `POST /worker/jobs/{id}/transcript`

Only valid while the job is in `transcribing` and claimed by you.

```json
{"text": "full transcript text", "words": [{"word": "This", "start": 0.0, "end": 0.4}], "mock": true}
```

`words` should have word-level timestamps once the real STT model is
wired in (per PLAN.md Phase 2); the mock worker sends evenly-spaced
fake timestamps. `mock: true` is a signal to the UI ("this transcript is
a stub, not real STT") — the real worker should send `mock: false` (or
omit it).

Sets `awaiting_user_edit=1`. The job now sits still until a human
submits an edit through the browser.

### `POST /worker/jobs/{id}/stage`

Body: `{"state": "blending"}` or `{"state": "exporting"}`. Moves the job
forward and records how long the *previous* stage actually took into a
running average used for future honest time estimates
(`portal/app/jobs.py:_record_stage_duration`). Only valid for jobs you
currently hold the claim on.

### `POST /worker/jobs/{id}/result`

Multipart upload, field name `file`, the final exported video. Marks the
job `done`, records the `exporting` stage's duration, and makes the
result available for in-browser playback/download.

### `POST /worker/jobs/{id}/error`

```json
{"message": "short human-readable summary", "detail": "more detail for diagnosis", "retryable": true}
```

- `message` is shown in the UI; keep it short (truncated server-side to
  500 chars) and **never include secrets** (tokens, headers, raw
  exception text that might echo an Authorization header, etc.) — this
  is enforced by convention, not automatically, so worker authors must
  scrub their own exception messages before sending this.
- `detail` (truncated to 4000 chars) is for a human/operator to diagnose
  and retry, not shown prominently in the main UI.
- `retryable: true` and `attempts < max_attempts` (default 3) -> the job
  goes back to `queued` for another attempt. Otherwise -> terminal
  `error` state.

## Honest progress, never fabricated

- `elapsed_current_stage_sec` / `elapsed_total_sec` are always real wall
  clock measurements (`now - stage_started_at` / `now - created_at`).
- `estimate_current_stage_sec` is `None` (and `estimate_label` is
  `"estimating..."`) until at least `PORTAL_ESTIMATE_MIN_SAMPLES` (default
  3) completed stage durations have been recorded for that state. After
  that it's a plain running mean of real measured durations — never a
  hardcoded or guessed number. This naturally starts honest-but-vague and
  becomes more informative as the mock (or real) worker accumulates
  history.

## Mock worker (reference implementation)

`portal/mock_worker.py` implements this entire contract using only the
Python standard library (`urllib`) — no extra dependencies, matching
what a from-scratch Mac mini worker would need to add (ideally none).

```bash
# one-shot, local dev, against the portal on :8000
PORTAL_WORKER_TOKEN=<token> python3 portal/mock_worker.py \
  --base-url http://127.0.0.1:8000 --once

# long-running, polling every 2s (defaults)
PORTAL_WORKER_TOKEN=<token> python3 portal/mock_worker.py \
  --base-url http://127.0.0.1:8000
```

It does **no real transcription or synthesis**:
- "Transcription" is a fixed stub sentence with evenly-spaced fake word
  timings (`mock: true`).
- "Synthesis + blending + export" is a byte-identical passthrough of the
  original upload — it downloads the input and re-uploads it unchanged
  as the result. Before/after playback will look and sound identical
  until the real model pipeline (Phase 1/2, owned by model-feasibility)
  is wired in to replace this with a real worker implementing the same
  endpoints.

This is enough to exercise and test the entire queue lifecycle,
auth, lease/retry logic, and honest progress estimation today —
independent of model validation.
