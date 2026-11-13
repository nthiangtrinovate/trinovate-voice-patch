# Voice Patch

Voice Patch is a browser-based tool for fixing a word or two in a short
video without re-recording. Upload a short clip, edit the transcript (or
let it swap a word or two automatically as a demo), and get back a video
with the replacement spoken in the original speaker's own voice —
synthesized locally, with no cloud API calls for transcription or voice
cloning.

## How it works

1. **Upload** a short video (roughly 20–45s) through the browser portal.
2. **Transcribe** — the backend produces a word-level timestamped
   transcript using a local speech-to-text model.
3. **Edit** — change the transcript text directly, or use the built-in
   "random fruit replacement" demo action, which picks 1–2 suitable
   words and swaps them for a random fruit name.
4. **Generate** — a worker process clones the original speaker's voice
   to synthesize just the replaced word(s)/phrase(s), using a short
   context window for natural prosody.
5. **Blend** — the new audio is spliced into the original track with a
   short crossfade at each word boundary. The video stream itself is
   never re-encoded (audio-only replacement; **no lip sync** — this is
   explicitly out of scope).
6. **Review & download** — before/after playback in the browser, then
   download the edited video.

## Architecture

- **Portal** (`portal/`): Python 3, FastAPI, Jinja2 templates, vanilla
  JS (no frontend build step), SQLite (via the stdlib `sqlite3` module,
  no ORM). Owns uploads, the job queue, transcript editing, and two
  separate auth schemes (see below).
- **Worker** (`worker/`): a separate, stdlib-only polling process meant
  to run on whatever machine actually has the ML models installed (for
  example, a local Apple Silicon Mac). It is **outbound-only** — it
  polls the portal for queued jobs and pushes results back over
  connections it initiates. It never needs an inbound port open, so it
  can sit behind a home network or NAT with no port forwarding.
- **Job queue**: states are surfaced honestly (`queued` → `transcribing`
  → `awaiting edit` → `generating` → `blending` → `exporting` → `done`,
  or `error`), with real elapsed time — no fabricated progress bars.
- **Two independent auth schemes**: a browser admin login (bcrypt-hashed
  password, forced change away from the seeded default — see
  `docs/admin-credentials.md`) and a separate worker bearer token, so
  the worker's credential is never the same secret as the admin
  login.
- **Retention**: uploaded/generated files and their job records are
  purged automatically after a configurable TTL (default 72h), plus an
  on-demand manual purge per job.

See `docs/worker-protocol.md` for the full worker-facing API contract
and `docs/portal-notes.md` for detailed design notes.

## Models used (all run locally — no cloud inference calls)

| Stage | Engine | License | Notes |
|---|---|---|---|
| Speech-to-text | [`mlx-whisper`](https://github.com/ml-explore/mlx-examples) running `mlx-community/whisper-large-v3-turbo` | MIT | Apple-native (MLX), GPU-accelerated on Apple Silicon. |
| Voice cloning (default) | [OpenVoice V2](https://github.com/myshell-ai/OpenVoice) (tone-color conversion) + [MeloTTS](https://github.com/myshell-ai/MeloTTS) (base speaker) — both MyShell AI | MIT, free for commercial use | Runs via PyTorch's MPS backend on Apple Silicon. |
| Voice cloning (alternative) | [Chatterbox](https://github.com/resemble-ai/chatterbox) (Resemble AI) | MIT | Independently selectable per job; no fallback between engines. |
| Voice cloning (alternative, non-commercial only) | [F5-TTS](https://github.com/SWivid/F5-TTS) (SWivid) | Code: MIT. **Pretrained checkpoint: CC-BY-NC** (trained on the Emilia dataset) | Clearly labeled in the UI as non-commercial use only; not cleared for any commercial deployment. |
| Audio blending | [`pydub`](https://github.com/jiaaro/pydub) (ffmpeg-backed) | MIT | Crossfade splicing at word boundaries. |

The engine (and, for OpenVoice, a quality/speed preset) is chosen at
**run time**, per job, not at upload — see `portal/app/model_registry.py`
for the single source of truth on what's actually wired in. An
unavailable engine fails loudly with a clear error; it never silently
falls back to a different one.

## Status and known limitations

This is a working prototype, not a polished commercial product:

- Transcription and voice cloning are real (not mocked) when run
  against an actual worker with the models installed; `portal/mock_worker.py`
  is a stand-in for local portal/queue development without any ML
  dependencies.
- Measured on Apple Silicon: short-clip transcription in a few seconds,
  per-word-edit voice cloning in well under a second once models are
  warm, blending in single-digit milliseconds. See
  `docs/feasibility-report.md` for the full methodology, numbers, and
  model-selection research (including license research for several
  alternatives that were considered and rejected).
- Splice-boundary artifacts are possible at crossfade points and have
  been observed in testing; crossfade tuning is an open area for
  improvement.
- Browser-based recording (as an alternative to file upload) requires a
  secure context (HTTPS, or `localhost`) — recording will not work over
  plain HTTP in any browser, and is currently gated to browsers whose
  `MediaRecorder` can natively produce MP4.
- The default admin login (`admin` / `admin`) **must** be changed before
  this is exposed beyond local/private use — see
  `docs/admin-credentials.md`. Setting `PORTAL_PUBLIC_DEPLOYMENT=1`
  makes the server refuse to start if it hasn't been.

## Running it locally

```bash
cd portal
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/run_dev.sh            # binds 127.0.0.1:8000 only
```

This seeds the default admin login and a local dev worker token. To
exercise the full queue without installing any ML dependencies, run the
mock worker in another terminal — see `docs/portal-notes.md` for the
exact command. To run the real pipeline, set up `worker/` on a machine
with the models installed (Apple Silicon recommended) — see
`worker/README.md` and `worker/feasibility/README.md`.

## Deployment

Generic, non-prescriptive templates (no specific hosts/domains baked
in) live under `docs/deploy/`:

- `Dockerfile.portal` / `systemd/` — two ways to run the portal backend.
- `nginx/` + `APPLY_ON_GATEWAY.md` — a reverse-proxy template that
  defaults to LAN/VPN-only access until you explicitly decide to expose
  it publicly.
- `mac-mini/` — a `launchd` agent template and launch script for running
  the worker persistently on a Mac, outbound-only, no inbound ports.

All templates use `<<<PLACEHOLDER>>>` markers for anything
host-specific (domain, upstream address/port, file paths, service
account) — fill those in for your own environment. See
`docs/admin-credentials.md` before exposing anything beyond local use.

## Repository layout

```
portal/   FastAPI browser portal, job queue, auth, templates
worker/   Outbound-only worker: STT + voice cloning + blending pipeline
tests/    Integration tests (auth, job lifecycle, worker protocol)
docs/     Design notes, worker protocol, feasibility report, deploy templates
```

## Scope

Explicitly out of scope: lip syncing, multi-speaker diarization, and
any cloud-hosted inference. This project replaces short, isolated words
or phrases in otherwise-unedited audio/video — it is not a general
video-editing or dubbing tool.
