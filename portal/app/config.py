"""Central configuration for the portal.

All secrets/tunables are read from environment variables so nothing
sensitive needs to live in source control. Sane *local-dev-only* defaults
are provided; see docs/portal-notes.md for the production/deployment gate
(default admin credentials and dev worker token must never reach a public
deployment unchanged).
"""
from __future__ import annotations

import os
from pathlib import Path

# Base data directory: uploads, outputs, and the sqlite DB all live here.
# Gitignored — never commit real user media or the database file.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PORTAL_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / os.environ.get("PORTAL_DB_FILENAME", "portal.sqlite3")

for _d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Upload validation ---
MAX_UPLOAD_BYTES = int(os.environ.get("PORTAL_MAX_UPLOAD_BYTES", 250 * 1024 * 1024))  # 250MB
MIN_DURATION_SEC = float(os.environ.get("PORTAL_MIN_DURATION_SEC", 20.0))
MAX_DURATION_SEC = float(os.environ.get("PORTAL_MAX_DURATION_SEC", 60.0))
TARGET_DURATION_NOTE = "30-45 seconds (20-60s accepted with slack)"

# --- Session / admin auth ---
SESSION_COOKIE_NAME = "portal_session"
# Absolute ceiling on a session's life, regardless of activity. Raised from
# the original 12h to 30 days: the real-world expiry rule teammates should
# feel is PORTAL_INACTIVITY_TTL_SECONDS below (24h of *inactivity*) — a
# 12h absolute cap would log someone out mid-use during one long active
# session, which contradicts "log out on inactivity." This absolute cap
# stays only as defense-in-depth against a session that somehow never gets
# its last_seen_at checked, not as the primary expiry mechanism. Anyone who
# already set PORTAL_SESSION_TTL_SECONDS explicitly keeps their own value.
SESSION_TTL_SECONDS = int(os.environ.get("PORTAL_SESSION_TTL_SECONDS", 30 * 24 * 3600))
# Sliding inactivity expiry: a session is invalidated if unused for this
# long, independent of the absolute cap above. Resets on every
# authenticated request (throttled — see security.get_session).
SESSION_INACTIVITY_TTL_SECONDS = int(os.environ.get("PORTAL_INACTIVITY_TTL_SECONDS", 24 * 3600))
# Dev-only default admin password. MUST be changed before any non-local use.
# Never read from a committed file — only from env or the hardcoded dev
# fallback, which forces a password change on first login (see auth.py).
# Configurable per deployment via PORTAL_DEFAULT_ADMIN_PASSWORD — see
# docs/admin-credentials.md for the full first-login / change-password
# runbook. This file never reads or stores any deployment's actual current
# admin password; it only ever sees the *seed* value used on a fresh DB.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = os.environ.get("PORTAL_DEFAULT_ADMIN_PASSWORD", "admin")

# Set this truthy in a real deployment to refuse to boot with unchanged
# default credentials still active. Local dev leaves it unset.
REQUIRE_CHANGED_DEFAULT_CREDS = os.environ.get("PORTAL_PUBLIC_DEPLOYMENT", "") not in (
    "",
    "0",
    "false",
    "False",
)

# --- Worker auth (SEPARATE scheme from admin login, never reused) ---
# A worker bearer token. In dev, if unset, a random one is generated at
# startup and written ONLY to a dedicated gitignored file
# (DATA_DIR/dev_worker_token.txt, mode 0600) — deliberately never printed
# to stdout/stderr or passed through any logger, since those streams
# commonly get redirected into a persistent log by whatever supervises
# the process. See app/security.py:ensure_worker_token for the logic. In
# any real deployment this must be set explicitly via env/secret store on
# both the server and the Mac mini worker.
WORKER_TOKEN_ENV_VAR = "PORTAL_WORKER_TOKEN"

# --- Job queue tuning ---
# The real Mac mini worker (worker/run_worker.py) measured ~82s for the
# `transcribing` stage (cold STT model load + inference) on a 21.9s test
# clip, against the old 120s default — only ~38s of margin, and PLAN.md
# allows uploads up to 60s (with slack around a 30-45s target). Cold-start
# dominates that number more than per-second audio cost, so a longer clip
# likely doesn't add much — but the Mac mini may also be running the
# user's other workloads concurrently (a real constraint per PLAN.md's
# guardrails), which could slow inference further. 300s gives ~3.6x
# headroom over the measured figure while still being short enough that a
# genuinely crashed/stuck worker gets noticed and retried within a few
# minutes rather than tying up a job indefinitely. The worker also
# heartbeats right before its slow calls (see its own comment in
# run_worker.py), which independently resets this clock — this default is
# a safety margin on top of that, not a replacement for it.
JOB_CLAIM_LEASE_SECONDS = int(os.environ.get("PORTAL_JOB_LEASE_SECONDS", 300))
JOB_MAX_ATTEMPTS = int(os.environ.get("PORTAL_JOB_MAX_ATTEMPTS", 3))
# Minimum samples before we trust a stage's measured average enough to show
# an estimate instead of "estimating...".
ESTIMATE_MIN_SAMPLES = int(os.environ.get("PORTAL_ESTIMATE_MIN_SAMPLES", 3))

# --- Retention / TTL ---
# Uploads, generated results, and the job's DB row are purged this long
# after the job was created. Storage-light by design: no soft-delete, no
# archive tier, just a clean delete of files-then-row. See
# jobs.purge_expired_jobs() and main.py's background purge loop.
TTL_HOURS = float(os.environ.get("PORTAL_TTL_HOURS", 72))
PURGE_INTERVAL_SECONDS = int(os.environ.get("PORTAL_PURGE_INTERVAL_SECONDS", 3600))

JOB_STATES = (
    "queued",
    "transcribing",
    "generating",
    "blending",
    "exporting",
    "done",
    "error",
)
