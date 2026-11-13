#!/usr/bin/env bash
# Local dev convenience: start the portal server with the project venv.
# Binds to 127.0.0.1 only — local dev testing, not exposed beyond this
# machine (see PLAN.md guardrails).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORTAL_PORT:-8000}" --reload
