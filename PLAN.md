# Voice Patch — Approved Plan

Status: APPROVED by user. This document is the source of truth for scope. Material
scope changes require user input; implementation-detail critique from the team is
welcome and expected.

## Phase 1 — Feasibility proof (gate for everything else)

On the user's Mac mini (local, private), using a short single-speaker clip:
- Replace 1–2 words in the transcript.
- Synthesize the replacement in the speaker's own voice using local, same-speaker
  cloning/synthesis with short phrase context (no cloud calls).
- Blend the new audio into the original clip.
- Report qualitative voice similarity/naturalness, elapsed time, and peak memory.

This must validate that a real (not toy) model actually runs acceptably on Apple
Silicon before anything else is built around it. If the sample clip is missing, use
a suitable licensed or synthetic test clip, clearly labeled as such — never claim
real-user voice validation without an actual user sample.

## Phase 2 — Core pipeline

- Transcription with word-level timestamps.
- Transcript editing (text-level operations map to audio spans).
- Local same-speaker synthesis of replacement words/phrases (short context window
  for prosody continuity).
- Audio blending (crossfade/splice at word boundaries) into the original track.
- Video export (audio replaced, video stream otherwise untouched). **No lip
  syncing** — explicitly out of scope.

## Phase 3 — Browser portal

- Upload a 30–45 second video.
- Manual transcript editing (user edits text directly).
- One-click "random fruit replacement": automatically pick 1–2 suitable words and
  replace them with a random fruit name, as a demo/novelty action.
- Before/after playback in-browser.
- Download the edited video.

## Phase 4 — Worker/server architecture

- The Mac mini runs a **private worker**: outbound-only. It polls the server for
  jobs and uploads/downloads over outbound connections it initiates.
- The Mac mini **never** exposes inbound ports to the server or the internet.
- Job states surfaced to the browser: `queued` → `transcribing` → `generating` →
  `blending` → `exporting` → `done` (or `error`), each with elapsed time and an
  honest estimate (no fabricated progress bars). Errors are shown with enough
  detail to retry/recover.

## Phase 5 — Tests & deployment

- Integration tests across transcription → edit → synthesis → blend → export →
  download.
- Deployment to the user's existing web services server and to the Mac mini.
- Git remote and public domain are supplied by the user later — do not invent a
  remote or push anywhere external in the meantime.

## Routing & infrastructure constraints

- Public routing: user's domain → existing gateway server → web services portal.
  Discover documented target metadata (server, domain, gateway config) safely;
  never guess, never provision paid resources, never alter unrelated
  infrastructure. If required info is missing, prepare deployment artifacts and
  report exactly what's missing and from whom/where it's needed.

## Portal authentication

- Simple admin login, initial credentials `admin` / `admin`.
- Forced password-change facility; password stored hashed (not plaintext).
- All uploads/jobs/results/downloads require authentication.
- **Before the portal is exposed on the public domain**, the default credentials
  must have been changed — default credentials are confined to private/local
  setup only.
- Worker authentication (Mac mini ↔ server) is a **separate** credential/scheme
  from the browser admin login.
- No secrets in logs, ever.

## Guardrails (apply to all phases)

- No destructive changes, no production replacement, no credential replacement
  for existing unrelated services, no firewall/port exposure, no external
  messages, no financial actions.
- Preserve unrelated files/services on the Mac mini and elsewhere.
- Isolated dependencies (venvs, not system Python/brew-wide changes), limited
  concurrency — do not disturb the user's existing workloads on the Mac mini.
- Research primary docs/licenses for any model used and validate actual
  compatibility (Apple Silicon, license terms for the intended use) before
  committing to it.

## Environment facts (confirmed 2026-11-10)

- Mac mini (accessed via SSH): Mac16,10, Apple Silicon (arm64),
  macOS 26.5.1, 10 CPU cores, 32GB RAM (`hw.memsize` = 34359738368), ~84% CPU idle
  at check time, 262GB free disk.
- No `ffmpeg` or `ollama` CLI found in the non-interactive SSH PATH. System Python
  is 3.9.6. These need isolated, non-destructive setup (project-local venv,
  vendored/brew-isolated ffmpeg) — do not modify system-wide tooling without
  checking first.

## Team ownership

- **Coordinator (this session)**: owns PLAN.md/STATUS.md, scope fidelity,
  cross-team integration decisions, verification. Does not write feature code.
- **Teammate: model-feasibility** — owns Phase 1 end-to-end: model research
  (STT + same-speaker TTS/cloning, licenses, Apple Silicon compatibility),
  Mac mini environment setup (isolated), the feasibility proof script, and its
  measured results.
- **Teammate: portal-queue** — owns Phase 3 + Phase 4: browser portal (upload,
  transcript edit UI, fruit-replacement action, before/after playback,
  download), job queue, progress states, admin auth + forced password change,
  worker-auth scheme, outbound-only worker polling protocol.
- **Teammate: review-tests** — owns Phase 5 + continuous review: integration
  tests, cross-review of the other two workstreams, deployment packaging/
  artifacts, and flagging scope or security concerns back to the coordinator.
