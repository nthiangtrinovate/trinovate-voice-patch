from __future__ import annotations

import logging
import threading
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config, db, jobs as jobs_module, security
from .routers import auth, portal, worker

# Basic logging setup. IMPORTANT: no handler here ever receives request
# headers, form bodies, or cookies wholesale — each route logs specific,
# vetted fields only (see routers/*.py and security.py docstrings).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Voice Patch Portal")

app.mount(
    "/static",
    StaticFiles(directory=str(config.BASE_DIR / "app" / "static")),
    name="static",
)

app.include_router(auth.router)
app.include_router(portal.router)
app.include_router(worker.router)


def _purge_loop():
    """Background thread: periodically delete jobs (files + DB row) past
    config.TTL_HOURS. Runs once immediately, then on PURGE_INTERVAL_SECONDS
    — keeps a stopped-then-restarted dev instance from accumulating a
    backlog before the first scheduled tick. Any single failure is logged
    and the loop continues; it must never take the whole process down."""
    log = logging.getLogger("portal.purge")
    while True:
        try:
            jobs_module.purge_expired_jobs()
        except Exception:
            log.exception("purge_expired_jobs failed; will retry next interval")
        time.sleep(config.PURGE_INTERVAL_SECONDS)


@app.on_event("startup")
def on_startup():
    db.init_db()
    security.ensure_default_admin()
    token = security.ensure_worker_token()
    if config.REQUIRE_CHANGED_DEFAULT_CREDS:
        admin = security.get_admin(config.DEFAULT_ADMIN_USERNAME)
        if admin is not None and admin["must_change_password"]:
            raise RuntimeError(
                "PORTAL_PUBLIC_DEPLOYMENT is set but the default admin "
                "password has not been changed yet. Refusing to start in "
                "this mode until the password is rotated. See "
                "docs/portal-notes.md."
            )
    threading.Thread(target=_purge_loop, name="ttl-purge", daemon=True).start()
    logging.getLogger("portal.main").info(
        "startup complete (worker token provisioned: %s, ttl_hours=%s)",
        bool(token), config.TTL_HOURS,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}
