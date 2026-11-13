# Worker — real pipeline (Phase 2)

This is the real Mac mini worker: it implements `docs/worker-protocol.md`
against the actual local STT/cloning/blend pipeline validated in Phase 1
(`docs/feasibility-report.md`), replacing portal-queue's `portal/mock_worker.py`
stub (fixed transcript, byte-identical passthrough) with real transcription
and real same-speaker synthesis.

`worker/feasibility/` (Phase 1) is now historical — it proved the mechanics
by hand against a hardcoded two-edit demo clip. Everything here generalizes
that into a real CLI driven by whatever edits a real user actually submits,
for any video.

## Layout

```
worker/
  run_worker.py            main poll loop — implements the HTTP contract,
                            stdlib only (urllib/json/subprocess/tempfile),
                            no extra deps. This is what runs continuously
                            on the Mac mini.
  pipeline/
    diffing.py              pure-stdlib word-level diff: original transcript
                             words -> edited text -> minimal context-expanded
                             audio spans to regenerate. No deps, runs inside
                             run_worker.py directly.
    media.py                ffmpeg subprocess wrappers: extract the audio
                             track from the uploaded video, remux the edited
                             audio back in afterward (video stream copied
                             untouched, never re-encoded — PLAN.md: "no lip
                             syncing", video otherwise unchanged).
  pipeline_scripts/
    run_stt.py               STT stage — runs under STT_VENV_PYTHON
                              (mlx-whisper). Generalized from
                              worker/feasibility/run_stt.py.
    run_clone.py             Cloning stage — runs under TTS_VENV_PYTHON
                              (OpenVoice V2 + MeloTTS). Takes an arbitrary
                              list of edit spans (not a hardcoded demo),
                              and auto-tunes MeloTTS's `speed` parameter to
                              approximately match each span's original
                              duration (one retry, per Phase 1's finding
                              that manual speed-tuning works well).
    run_blend.py              Blend stage — splices every cloned span (or
                               cuts a pure-deletion span) back into the
                               original audio with short crossfades at
                               Whisper word boundaries.
  feasibility/                Phase 1 artifacts — unchanged, kept for the
                               historical record.
```

## Why two venvs (STT vs. cloning)

OpenVoice V2/MeloTTS's pinned dependencies (`numpy==1.22.0`, `librosa==0.9.1`,
etc.) are incompatible with the modern `numpy 2.x` `mlx-whisper` needs. Phase
1 found this empirically; rather than fight it, `run_worker.py` just invokes
each stage's script under its own venv's interpreter as a subprocess. This
maps directly onto the job state machine anyway — `transcribing` and
`generating` are already separate stages with separate worker-protocol
transitions.

## Why we re-diff instead of trusting `edit["replacements"]`

The portal's `edit` payload (`docs/worker-protocol.md`) only guarantees
`text`. `replacements` is populated reliably by the fruit-replacement tool
(`portal/app/fruit.py`), but the browser only keeps the *last* fruit-replace
response (`portal/app/static/app.js`: `lastReplacements`) — if the user
free-types further changes into the textarea afterward, or never clicks
fruit-replace at all, `replacements` is empty or stale relative to the final
submitted text. `pipeline/diffing.py` recomputes the real diff from
`job["transcript"]["words"]` (what we ourselves submitted) vs.
`job["edit"]["text"]` (whatever the user finally submitted) via
`difflib.SequenceMatcher`, so manual edits and fruit-replace edits are
handled identically and correctly. This only works because `run_worker.py`
builds the *submitted* transcript text as `" ".join(w["word"] for w in words)`
— never Whisper's own raw `text` field — guaranteeing 1:1 tokenization
alignment between the word array and the text a human (or `text.split()` in
the browser) sees.

Each changed region is expanded by `CONTEXT_WORDS` (default 2) words on each
side for prosody continuity (PLAN.md: "short phrase of surrounding context
for prosody"); overlapping windows from nearby edits are merged into one
combined span rather than kept artificially separate.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `PORTAL_BASE_URL` | `http://127.0.0.1:8000` | the portal server |
| `PORTAL_WORKER_TOKEN` | (reads `portal/data/dev_worker_token.txt` if unset) | worker bearer token — separate scheme from admin login |
| `PORTAL_WORKER_ID` | `mac-mini-worker-1` | sent as `X-Worker-Id` |
| `PORTAL_POLL_INTERVAL` | `2.0` seconds | poll delay on `204` |
| `FEASIBILITY_HOME` | `~/voice-word-editor-feasibility` | base dir for the defaults below |
| `STT_VENV_PYTHON` | `$FEASIBILITY_HOME/stt-venv/bin/python` | the Phase 1 STT venv |
| `TTS_VENV_PYTHON` | `$FEASIBILITY_HOME/tts-venv/bin/python` | the Phase 1 cloning venv |
| `OPENVOICE_CHECKPOINTS_DIR` | `$FEASIBILITY_HOME/checkpoints_v2` | OpenVoice V2 converter + base-speaker weights |
| `WORKER_SCRATCH_DIR` | `$FEASIBILITY_HOME/worker_jobs` | per-job temp dirs (video/audio/intermediate wavs); each job's dir is deleted after it's processed, success or failure |
| `FFMPEG_BIN` | `ffmpeg` (PATH) | set to the full path if ffmpeg isn't on PATH in whatever launches this (e.g. a launchd agent's minimal PATH — see `docs/deploy/mac-mini/`) |
| `CONTEXT_WORDS` | `2` | prosody-context words on each side of an edit |
| `CROSSFADE_MS` | `25` | splice crossfade length |

The defaults point straight at the exact venvs/checkpoints Phase 1 already
built and validated on the Mac mini — no reinstall needed to run this for
real there.

## Running it

```bash
PORTAL_WORKER_TOKEN=<token> python3 worker/run_worker.py --base-url http://127.0.0.1:8000
# or --once to process at most one job then exit (useful for testing)
```

## Verified against the real portal (not just unit-tested in isolation)

I ran this for real: stood up `portal/app` (dev mode, `127.0.0.1`-only,
torn down afterward — never exposed beyond that) on the Mac mini, uploaded a
~22s synthetic test clip (same `say`-generated voice as Phase 1, wrapped in
an MP4 with a trivial color video track so it clears the portal's 20-60s
MP4-only validation), and drove the full lifecycle:

1. `run_worker.py --once` claimed the `queued` job, downloaded the video,
   extracted audio, ran real `mlx-whisper` STT, submitted the transcript
   (62 words, exact match to the script). **82.3s** for this stage
   (cold model load dominates; see the note in `_stage_transcribing` about
   lease margin for longer clips).
2. Submitted a **manual free-text edit** directly to
   `/api/jobs/{id}/submit-edit` with `replacements: []` — deliberately
   *not* using the fruit-replace tool — to exercise the real diff path
   described above, not the easy structured-replacements case.
3. `run_worker.py --once` claimed the `generating` job, diffed the edit
   (correctly found 2 spans from the free-text change alone), cloned both
   replacement phrases (auto speed-tuned), blended them in, advanced
   through `blending`/`exporting`, remuxed into the original MP4, and
   submitted the result. **12.2s** total for generating, **0.04s**
   blending, **0.22s** exporting.
4. Downloaded the result, extracted its audio, and re-transcribed it
   independently: both edits landed correctly in the final video. The
   video stream was confirmed byte-identical to the input (same codec,
   frame count, bitrate — `-c:v copy`, never re-encoded).
5. **Same splice-boundary artifact Phase 1 found reappeared** (an
   extra hallucinated word at one join, on re-transcription) — this is now
   confirmed reproducible with the general pipeline, not a fluke of the
   Phase 1 demo's specific hardcoded span. Crossfade/splice-boundary tuning
   is real follow-up work, not a one-off fix.
6. Tore down the test server and deleted all test artifacts (venv, test
   video, scratch dirs) from the Mac mini afterward — nothing from this
   test was left behind.

This is still the same **synthetic** `say`-voice clip as Phase 1, not a real
human sample — see `docs/feasibility-report.md` for why, and for everything
that still needs a human listen before this is "validated" rather than
"mechanically works."

## Known limitations / next steps

- **Lease margin**: cold STT model load took most of the 82s observed above,
  against a 120s default lease. Fine for a 22s clip; worth re-measuring
  against a full 60s upload (PLAN.md's max) before relying on the default
  lease without raising `PORTAL_JOB_LEASE_SECONDS` or investigating what's
  behind the cold-start cost (network metadata checks on each process start
  are a likely contributor).
- **Splice artifacts**: see point 5 above — needs real tuning once someone
  can listen to the output.
- **One-worker-at-a-time**: `run_worker.py` processes jobs serially, by
  design (PLAN.md: don't disturb the user's other Mac mini workloads).
- `docs/deploy/mac-mini/run_worker.sh.template`'s entrypoint placeholder can
  now be filled in — it's `worker/run_worker.py` with the env vars above.
  That template is review-tests' file; flagging this to them rather than
  editing it directly.
