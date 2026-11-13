"""Auth primitives.

Two *separate* credential schemes live here on purpose:

- Admin/browser login: bcrypt-hashed password + server-side session table,
  session id delivered via an httponly cookie. Slow hash is correct here
  because the secret (a human-chosen password) is low entropy.
- Worker auth: a long, random, high-entropy bearer token (never a
  password, never reused from the admin scheme). It is hashed with
  SHA-256 for storage/comparison, which is appropriate *only* because the
  token itself has enough entropy that fast-hash brute forcing is not a
  realistic threat — unlike a human password. This is documented so
  nobody "fixes" it into bcrypt-for-everything and then wonders why worker
  polling got slow, or conversely reuses a weak token with this scheme.

Nothing in this module ever logs a raw password, token, or Authorization
header. Keep it that way: log booleans/outcomes, not secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from . import config, db

log = logging.getLogger("portal.security")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- passwords
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        # Malformed hash in storage; never leak details, just fail closed.
        log.warning("password verify: stored hash malformed for a user")
        return False


def ensure_default_admin() -> None:
    """Seed the admin/admin account on first run only. Idempotent."""
    db.init_db()
    conn = db.get_conn()
    row = conn.execute(
        "SELECT username FROM admin_users WHERE username = ?",
        (config.DEFAULT_ADMIN_USERNAME,),
    ).fetchone()
    if row is not None:
        return
    now = iso(utcnow())
    with db.tx() as c:
        c.execute(
            "INSERT INTO admin_users (username, password_hash, must_change_password, "
            "created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (
                config.DEFAULT_ADMIN_USERNAME,
                hash_password(config.DEFAULT_ADMIN_PASSWORD),
                now,
                now,
            ),
        )
    log.info("seeded default admin account (must_change_password=1)")


def get_admin(username: str):
    conn = db.get_conn()
    return conn.execute(
        "SELECT * FROM admin_users WHERE username = ?", (username,)
    ).fetchone()


def set_password(username: str, new_plain: str) -> None:
    now = iso(utcnow())
    with db.tx() as c:
        c.execute(
            "UPDATE admin_users SET password_hash = ?, must_change_password = 0, "
            "updated_at = ? WHERE username = ?",
            (hash_password(new_plain), now, username),
        )


# ----------------------------------------------------------------- sessions
# Only re-write last_seen_at if it's at least this stale, so an active user
# polling every couple seconds (see app.js) doesn't turn every request into
# a DB write.
_LAST_SEEN_UPDATE_THROTTLE_SECONDS = 60


def create_session(username: str) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + timedelta(seconds=config.SESSION_TTL_SECONDS)
    with db.tx() as c:
        c.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, username, iso(now), iso(expires), iso(now)),
        )
    return token, expires


def get_session(token: str):
    """Return the session row, or None if it's missing/expired.

    Two independent expiry checks: the absolute `expires_at` ceiling set
    once at login (defense-in-depth, see config.SESSION_TTL_SECONDS), and
    a sliding inactivity window based on `last_seen_at` (the one PLAN.md's
    teammate-packaging requirement actually asks for: 24h of inactivity
    requires logging in again). Either one expires the session.
    """
    if not token:
        return None
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    now = utcnow()
    if parse_iso(row["expires_at"]) < now:
        delete_session(token)
        return None
    last_seen = parse_iso(row["last_seen_at"]) if row["last_seen_at"] else parse_iso(row["created_at"])
    if (now - last_seen).total_seconds() > config.SESSION_INACTIVITY_TTL_SECONDS:
        delete_session(token)
        return None
    if (now - last_seen).total_seconds() > _LAST_SEEN_UPDATE_THROTTLE_SECONDS:
        with db.tx() as c:
            c.execute("UPDATE sessions SET last_seen_at = ? WHERE token = ?", (iso(now), token))
    return row


def delete_session(token: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ------------------------------------------------------------- worker auth
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_worker_token() -> str:
    """Return the active worker token, provisioning one if needed.

    Resolution order:
    1. PORTAL_WORKER_TOKEN env var (preferred; required for any real
       deployment — set it the same on the server and the worker config).
    2. A dev-only random token generated once and written, in plaintext,
       ONLY to a dedicated gitignored file (DATA_DIR/dev_worker_token.txt,
       mode 0600) — deliberately never to stdout/stderr/print/logging,
       because those streams commonly get redirected into a persistent
       log file (systemd journal, supervisor, etc.) by whoever runs this,
       which would violate "no secrets in logs, ever." A dedicated
       secret-bearing file is the correct place for it, same as a .env
       file would be.
    """
    db.init_db()
    dev_token_path = config.DATA_DIR / "dev_worker_token.txt"
    env_token = os.environ.get(config.WORKER_TOKEN_ENV_VAR)
    conn = db.get_conn()
    if env_token:
        th = _token_hash(env_token)
        row = conn.execute(
            "SELECT token_hash FROM worker_tokens WHERE token_hash = ?", (th,)
        ).fetchone()
        if row is None:
            # Replace any previously provisioned token with the one from env.
            with db.tx() as c:
                c.execute("DELETE FROM worker_tokens")
                c.execute(
                    "INSERT INTO worker_tokens (token_hash, worker_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (th, "env-configured", iso(utcnow())),
                )
        # An explicit env var always wins; remove any stale dev-token file
        # so nobody later picks up an inactive secret by mistake.
        dev_token_path.unlink(missing_ok=True)
        return env_token

    row = conn.execute("SELECT token_hash FROM worker_tokens LIMIT 1").fetchone()
    if row is not None and dev_token_path.exists():
        existing = dev_token_path.read_text().strip()
        if existing and hmac.compare_digest(_token_hash(existing), row["token_hash"]):
            return existing
        # File missing/stale relative to DB row: fall through and reissue
        # both consistently rather than returning a token that won't verify.

    new_token = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("DELETE FROM worker_tokens")
        c.execute(
            "INSERT INTO worker_tokens (token_hash, worker_id, created_at) VALUES (?, ?, ?)",
            (_token_hash(new_token), "dev-default", iso(utcnow())),
        )
    dev_token_path.write_text(new_token + "\n")
    os.chmod(dev_token_path, 0o600)
    log.info(
        "provisioned a new dev worker token; raw value written to %s (0600, "
        "gitignored, not logged) for the mock worker to read",
        dev_token_path,
    )
    return new_token


def verify_worker_token(presented: str) -> bool:
    if not presented:
        return False
    conn = db.get_conn()
    th = _token_hash(presented)
    rows = conn.execute("SELECT token_hash FROM worker_tokens").fetchall()
    for row in rows:
        if hmac.compare_digest(row["token_hash"], th):
            return True
    return False
