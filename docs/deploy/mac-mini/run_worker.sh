#!/usr/bin/env bash
# Launch script for the voice-word-editor Mac mini worker.
# Invoked by com.example.voice-word-editor-worker.plist.
#
# Real entrypoint (worker/README.md, verified end to end against a live
# portal instance): `worker/run_worker.py` itself is stdlib-only
# (urllib/json/subprocess/tempfile — no venv of its own needed), but it
# shells out to TWO SEPARATE heavy-ML venvs for the STT and cloning stages
# (their pinned deps conflict — see worker/README.md "Why two venvs").
# STT_VENV_PYTHON/TTS_VENV_PYTHON default to the two venvs Phase 1 already
# built and validated under FEASIBILITY_HOME on the Mac mini.
#
# Constraints this script must respect (PLAN.md):
#  - Outbound-only: never start anything that binds a listening socket.
#  - Isolated dependencies: the two ML venvs are project-local.
#  - Limited concurrency: don't disturb the user's existing Mac mini
#    workloads (Ollama etc.) — worker/run_worker.py processes one job at a
#    time, by design, never spawning unbounded parallel subprocess work.
#  - No secrets in logs: never `echo`/print the worker-auth token or job
#    content; read the token from file, export it only as an env var the
#    Python process reads directly — never pass it as a CLI argument
#    (would be visible in `ps`/process listings).

set -euo pipefail

WORKER_HOME="${HOME}/voice-word-editor"
TOKEN_FILE="${VOICE_WORD_EDITOR_WORKER_TOKEN_FILE:-${WORKER_HOME}/config/worker_auth_token}"

if [[ ! -f "${TOKEN_FILE}" ]]; then
    echo "worker auth token file not found at ${TOKEN_FILE} — see docs/deployment-notes.md" >&2
    exit 1
fi

# Exported, never echoed/printed — worker/run_worker.py reads this
# directly from its environment (see its _load_token()).
export PORTAL_WORKER_TOKEN
PORTAL_WORKER_TOKEN="$(cat "${TOKEN_FILE}")"

# Required: where the portal server actually is (set by the plist's
# VOICE_WORD_EDITOR_SERVER_URL env var — translate it to the name
# worker/run_worker.py expects).
export PORTAL_BASE_URL="${VOICE_WORD_EDITOR_SERVER_URL:?set VOICE_WORD_EDITOR_SERVER_URL in the plist}"

# Everything below has a working default in worker/README.md pointing at
# the exact Phase 1 venvs/checkpoints already built on the Mac mini
# (under ~/voice-word-editor-feasibility by default). FFMPEG_BIN is set
# explicitly: a launchd agent's PATH is minimal and won't find
# Homebrew-installed ffmpeg on its own.
export FFMPEG_BIN="/opt/homebrew/bin/ffmpeg"
# mlx_whisper (inside the STT venv) shells out to a bare `ffmpeg` on PATH
# internally — it does not honor FFMPEG_BIN. launchd's default PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) doesn't include Homebrew's bin dir, so
# that lookup fails even though FFMPEG_BIN above is set correctly for our
# own pipeline/media.py calls. Prepend it here so every subprocess this
# script's children spawn can find ffmpeg by name too.
export PATH="/opt/homebrew/bin:${PATH}"
export PORTAL_WORKER_ID="${PORTAL_WORKER_ID:-mac-mini-worker-1}"

WORKER_PY="${WORKER_HOME}/worker/run_worker.py"

# plain python3 — run_worker.py itself needs no venv (stdlib only); the
# two heavy ML venvs above are what actually need isolation.
exec python3 "${WORKER_PY}" --base-url "${PORTAL_BASE_URL}" --worker-id "${PORTAL_WORKER_ID}"
