# Integration tests — Voice Patch

Owner: review-tests (Phase 5).

## Status (2026-11-10, updated)

Contract confirmed against portal-queue's Phase 3/4 implementation
(commit `dad3a8d`, merged to master as `75afb54`). No more `# ASSUMED`
markers — every endpoint/payload below was verified against a live
instance of the real server (ran it against an isolated scratch data dir,
not their committed `portal/data/`) plus their real `mock_worker.py`. See
`tests/CONTRACT.md` for the full confirmed shape, including one edge
case (reclaiming a job stalled mid-`blending` after its lease expires)
that was specifically exercised and confirmed working.

model-feasibility's real Phase 2 worker (`worker/run_worker.py` +
`worker/pipeline/`) now exists and they've verified it end to end
against a live portal themselves (manual free-text edit path, not just
fruit-replace). `test_lifecycle.py`/`test_worker_protocol.py` still run
against portal-queue's `mock_worker.py` by design, not the real worker —
that's the right default (no Mac mini / heavy ML deps required to run
this suite), not a gap. See "Known issue tracked, not auto-tested" below
for why the real pipeline isn't exercised here.

## Known issue tracked, not auto-tested: splice-boundary artifact

model-feasibility found (Phase 1, reproduced again with the general
Phase 2 pipeline): an independent re-transcription of the final edited
clip occasionally shows one hallucinated extra word at a splice boundary
— the crossfade/blend logic works, but Whisper's re-transcription of the
join point isn't always clean. This is a real, reproducible audio-quality
characteristic of the current crossfade tuning, not a protocol bug, and
not something this suite can meaningfully assert on: catching it needs
the real STT venv + real cloned audio, neither of which exists in this
sandbox (no Mac mini, no GPU, no model weights here). Deliberately NOT
building a Mac-mini-dependent test path for this right now — tracked
here instead so it isn't lost: whoever next tunes `CROSSFADE_MS` /
splice-boundary handling in `worker/pipeline_scripts/run_blend.py` should
verify against model-feasibility's repro (same two edits — "retention"->
"satisfaction", "dashboard"->"spreadsheet" — on the Phase 1 synthetic
clip) with an independent re-transcription before/after, the same way
model-feasibility validated it both times.

All tests still **skip cleanly** (not fail) when no server is configured
— verified: `pytest tests/` with no env vars set gives 30 skipped, 0
failed.

## Running

```
pip install -r tests/requirements.txt

# Point at a running portal (see portal/scripts/run_dev.sh):
BASE_URL=http://127.0.0.1:8000 python3 -m pytest tests/ -v

# Lifecycle tests additionally need a worker actually processing jobs —
# point them at portal-queue's reference mock worker:
BASE_URL=http://127.0.0.1:8000 \
MOCK_WORKER_SCRIPT=/path/to/portal/mock_worker.py \
PORTAL_WORKER_TOKEN=$(cat /path/to/portal/data/dev_worker_token.txt) \
python3 -m pytest tests/test_lifecycle.py -v

# test_worker_protocol.py drives jobs forward manually (no background
# worker — see its module docstring for why) and needs the same
# PORTAL_WORKER_TOKEN. Its stalled-job-reclaim test additionally needs a
# short lease configured on BOTH the server and this run, or it skips:
PORTAL_JOB_LEASE_SECONDS=2 \
# (restart the server with the same env var, then:)
BASE_URL=http://127.0.0.1:8000 \
PORTAL_WORKER_TOKEN=... \
PORTAL_JOB_LEASE_SECONDS=2 \
python3 -m pytest tests/test_worker_protocol.py -v
```

If `BASE_URL` is unset or unreachable, or `MOCK_WORKER_SCRIPT`/
`PORTAL_WORKER_TOKEN` are unset for the tests that need them, those tests
skip (not fail) with a clear reason.

## What's covered

- `test_lifecycle.py` — full pipeline: upload -> transcribe -> edit ->
  synthesize -> blend -> export -> download, against portal-queue's mock
  worker (not the real Mac mini model pipeline — that's covered separately
  once model-feasibility lands something testable).
- `test_auth.py` — admin login with default `admin`/`admin`, forced
  password-change flow, password stored hashed (never plaintext — checked
  via behavior/API, not direct DB inspection, since schema is portal-queue's
  to define), session-gated endpoints (uploads/jobs/results/downloads all
  require auth), and that the worker-auth scheme is a genuinely separate
  credential from the admin login (distinct header/token, admin creds don't
  authenticate as a worker and vice versa).
- `test_worker_protocol.py` — outbound-only poll/claim/upload shape: worker
  initiates every request (no inbound port asserted reachable from the
  "server" side in this test), job claim/complete/fail transitions.

## Known gaps (tracked, not yet actionable)

- No real Mac mini worker to test against — Phase 1/2 pipeline tests are
  blocked on model-feasibility's proof script landing an invokable CLI.
- Exact API routes/payloads are assumed, not confirmed, until portal-queue
  responds.
