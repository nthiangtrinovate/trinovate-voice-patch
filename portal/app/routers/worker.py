"""Worker-facing API: everything here is reachable only via outbound
requests *initiated by* the Mac mini worker. The server never calls out
to the worker — see docs/worker-protocol.md for the full contract.

Auth: Authorization: Bearer <worker token> (separate scheme from admin
login — see app/security.py and app/deps.py:require_worker).
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, Response

from .. import config, jobs as jobs_module
from ..deps import require_worker

log = logging.getLogger("portal.routes.worker")
router = APIRouter(prefix="/worker")


def _worker_job_dict(job) -> dict:
    """Job detail shaped for the worker (includes what it needs to act,
    excludes nothing — unlike the browser-facing dict this may include
    the transcript/edit payloads needed to perform the next stage)."""
    d = jobs_module.to_public_dict(job)
    d["input_download_url"] = f"/worker/jobs/{job['id']}/input"
    return d


@router.get("/jobs/next")
def poll_next_job(worker_id: str = Depends(require_worker)):
    job = jobs_module.claim_next_job(worker_id)
    if job is None:
        return Response(status_code=204)
    log.info("worker %s claimed job %s (state=%s)", worker_id, job["id"], job["state"])
    return _worker_job_dict(job)


@router.get("/jobs/{job_id}")
def get_job_detail(job_id: str, worker_id: str = Depends(require_worker)):
    job = jobs_module.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _worker_job_dict(job)


@router.get("/jobs/{job_id}/input")
def download_input(job_id: str, worker_id: str = Depends(require_worker)):
    job = jobs_module.get_job(job_id)
    if job is None or not job["input_path"]:
        raise HTTPException(status_code=404, detail="job or input not found")
    return FileResponse(job["input_path"], media_type="video/mp4")


@router.post("/jobs/{job_id}/heartbeat")
def heartbeat(job_id: str, worker_id: str = Depends(require_worker)):
    ok = jobs_module.heartbeat(job_id, worker_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job not claimed by this worker or already terminal")
    return {"ok": True}


@router.post("/jobs/{job_id}/transcript")
async def submit_transcript(job_id: str, request: Request, worker_id: str = Depends(require_worker)):
    body = await request.json()
    text = body.get("text")
    words = body.get("words", [])
    is_mock = bool(body.get("mock", False))
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    # Soft check, not a hard rejection: the portal's fruit-replacement and
    # submit-edit flows locate words by POSITION in `text.split()`, and
    # that position is only meaningful against `words` if the two line up
    # 1:1 (see docs/worker-protocol.md "word-index alignment"). A mismatch
    # doesn't break transcript display/manual editing, so we don't block
    # the worker over it — we just want it visible in the log rather than
    # silently producing stale replacement indices later.
    if words and len(text.split()) != len(words):
        log.warning(
            "job %s: transcript word count (%d) does not match text.split() "
            "length (%d) — fruit-replacement word indices will not reliably "
            "align with transcript.words for this job",
            job_id, len(words), len(text.split()),
        )
    ok = jobs_module.submit_transcript(
        job_id, worker_id, {"text": text, "words": words, "mock": is_mock}
    )
    if not ok:
        raise HTTPException(status_code=409, detail="job not claimed by this worker")
    log.info("worker %s submitted transcript for job %s (mock=%s)", worker_id, job_id, is_mock)
    return {"ok": True}


@router.post("/jobs/{job_id}/stage")
async def advance_stage(job_id: str, request: Request, worker_id: str = Depends(require_worker)):
    body = await request.json()
    new_state = body.get("state")
    if new_state not in config.JOB_STATES:
        raise HTTPException(status_code=400, detail=f"invalid state {new_state!r}")
    try:
        ok = jobs_module.advance_stage(job_id, worker_id, new_state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=409, detail="job not claimed by this worker or already terminal")
    log.info("worker %s advanced job %s to state=%s", worker_id, job_id, new_state)
    return {"ok": True}


@router.post("/jobs/{job_id}/result")
async def submit_result(job_id: str, worker_id: str = Depends(require_worker),
                         file: UploadFile = File(...),
                         engine_metadata: str | None = Form(None)):
    job = jobs_module.get_job(job_id)
    if job is None or job["claimed_by"] != worker_id:
        raise HTTPException(status_code=409, detail="job not claimed by this worker")
    parsed_metadata = None
    if engine_metadata:
        try:
            parsed_metadata = json.loads(engine_metadata)
        except (TypeError, ValueError):
            log.warning("job %s: worker sent unparseable engine_metadata, ignoring", job_id)
    dest_name = f"{uuid.uuid4()}_{job_id}.mp4"
    dest_path = config.OUTPUT_DIR / dest_name
    size = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            out.write(chunk)
    await file.close()
    ok = jobs_module.submit_result(job_id, worker_id, str(dest_path), engine_metadata=parsed_metadata)
    if not ok:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="job not claimed by this worker (result rejected)")
    log.info("worker %s submitted result for job %s (%d bytes, engine=%s)",
              worker_id, job_id, size, (parsed_metadata or {}).get("engine"))
    return {"ok": True}


@router.post("/jobs/{job_id}/error")
async def submit_error(job_id: str, request: Request, worker_id: str = Depends(require_worker)):
    body = await request.json()
    message = str(body.get("message", "unspecified error"))[:500]
    detail = body.get("detail")
    if detail is not None:
        detail = str(detail)[:4000]
    retryable = bool(body.get("retryable", False))
    ok = jobs_module.submit_error(job_id, worker_id, message, detail, retryable)
    if not ok:
        raise HTTPException(status_code=409, detail="job not claimed by this worker")
    # Log only the message (truncated, no secrets expected here since this
    # is our own worker's error text) — never log raw request bodies wholesale.
    log.warning("worker %s reported error for job %s: %s (retryable=%s)", worker_id, job_id, message, retryable)
    return {"ok": True}
