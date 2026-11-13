"""Browser-facing routes: upload, dashboard, job detail, transcript edit +
fruit replacement, before/after media streaming, download.

All routes here require an authenticated admin session (enforced via
require_admin_page / require_admin_api) per PLAN.md: "All uploads/jobs/
results/downloads require authentication."
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, jobs as jobs_module, media, model_registry
from ..deps import require_admin_api, require_admin_page
from ..fruit import apply_random_fruit_replacement
from ..templating import templates

log = logging.getLogger("portal.routes.portal")
router = APIRouter()


def _dashboard_context(admin, **extra) -> dict:
    rows = jobs_module.list_jobs_for_owner(admin["username"])
    ctx = {
        "jobs": [jobs_module.to_public_dict(r) for r in rows],
        "username": admin["username"],
        "min_duration": config.MIN_DURATION_SEC,
        "max_duration": config.MAX_DURATION_SEC,
        "word_replacement_modes": model_registry.WORD_REPLACEMENT_MODES,
        "default_word_replacement_mode": model_registry.DEFAULT_WORD_REPLACEMENT_MODE,
        "voice_models": model_registry.VOICE_MODELS,
        "default_voice_model": model_registry.DEFAULT_VOICE_MODEL,
        "ttl_hours": config.TTL_HOURS,
    }
    ctx.update(extra)
    return ctx


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        return admin
    extra = {}
    if request.query_params.get("purged"):
        extra["purge_notice"] = "Job purged — its upload, result, and record were deleted."
    purge_error = request.query_params.get("purge_error")
    if purge_error:
        extra["purge_notice"] = f"Could not purge: {purge_error}"
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context(admin, **extra))


@router.post("/jobs/{job_id}/purge")
def purge_job_page(request: Request, job_id: str):
    """User-initiated immediate purge (separate from the 72h automatic
    TTL purge in jobs.purge_expired_jobs). Page route (not /api/*) since
    it's triggered by a plain HTML form + confirm() dialog from both the
    dashboard job list and the job detail page — see templates. Always
    redirects to the dashboard: on success the job is gone, so there's
    nothing left at /jobs/{id} to show."""
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        return admin
    ok, reason = jobs_module.purge_single_job(job_id, admin["username"])
    if ok:
        return RedirectResponse("/?purged=1", status_code=303)
    return RedirectResponse(f"/?purge_error={urlquote(reason or 'could not purge job')}", status_code=303)


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    word_replacement_mode: str = Form(model_registry.DEFAULT_WORD_REPLACEMENT_MODE),
    voice_model: str = Form(model_registry.DEFAULT_VOICE_MODEL),
    voice_preset: str = Form(""),
):
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        return admin

    if not voice_preset:
        # Falls back to "" (not a valid preset id) if voice_model itself is
        # unrecognized — validate_voice_choice below will raise a clear
        # error for that case rather than silently guessing a preset.
        voice_preset = model_registry.VOICE_MODELS.get(voice_model, {}).get("default_preset", "")
    try:
        model_registry.validate_word_replacement_mode(word_replacement_mode)
        model_registry.validate_voice_choice(voice_model, voice_preset)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "dashboard.html", _dashboard_context(admin, upload_error=str(exc)), status_code=400,
        )

    suffix = "".join(ch for ch in (file.filename or "") if ch.isalnum() or ch in "._-")[-80:]
    dest_name = f"{uuid.uuid4()}_{suffix or 'upload.mp4'}"
    dest_path = config.UPLOAD_DIR / dest_name

    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    out.close()
                    dest_path.unlink(missing_ok=True)
                    return templates.TemplateResponse(
                        request,
                        "dashboard.html",
                        _dashboard_context(
                            admin,
                            upload_error=(
                                f"File exceeds max upload size of "
                                f"{config.MAX_UPLOAD_BYTES // (1024*1024)}MB."
                            ),
                        ),
                        status_code=413,
                    )
                out.write(chunk)
    finally:
        await file.close()

    try:
        duration = media.probe_duration_seconds(dest_path)
    except media.UnsupportedMediaError as exc:
        dest_path.unlink(missing_ok=True)
        log.info("upload rejected: unsupported media (%s)", exc)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _dashboard_context(
                admin,
                upload_error=(
                    f"Could not read video duration ({exc}). Please upload an "
                    f"MP4/MOV file."
                ),
            ),
            status_code=400,
        )

    if not (config.MIN_DURATION_SEC <= duration <= config.MAX_DURATION_SEC):
        dest_path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _dashboard_context(
                admin,
                upload_error=(
                    f"Video duration is {duration:.1f}s; must be between "
                    f"{config.MIN_DURATION_SEC:.0f}-{config.MAX_DURATION_SEC:.0f}s "
                    f"(target: {config.TARGET_DURATION_NOTE})."
                ),
            ),
            status_code=400,
        )

    job_id = jobs_module.create_job(
        owner_username=admin["username"],
        input_path=str(dest_path),
        input_filename=file.filename or dest_name,
        duration_sec=duration,
        size_bytes=size,
        word_replacement_mode=word_replacement_mode,
        voice_model=voice_model,
        voice_preset=voice_preset,
    )
    log.info("job %s created by %s (duration=%.1fs size=%dB)", job_id, admin["username"], duration, size)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _get_owned_job_or_404(job_id: str, username: str):
    job = jobs_module.get_job(job_id)
    if job is None or job["owner_username"] != username:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    admin = require_admin_page(request)
    if isinstance(admin, RedirectResponse):
        return admin
    job = jobs_module.get_job(job_id)
    if job is None or job["owner_username"] != admin["username"]:
        raise HTTPException(status_code=404, detail="job not found")
    return templates.TemplateResponse(
        request,
        "job.html",
        {
            "job": jobs_module.to_public_dict(job),
            "job_id": job_id,
            "word_replacement_modes": model_registry.WORD_REPLACEMENT_MODES,
            "voice_models": model_registry.VOICE_MODELS,
            # Pre-serialized (not the Jinja `tojson` filter, which isn't
            # registered on this plain Jinja2Templates instance) so
            # record.js/app.js can build the preset dropdown and show the
            # license note client-side without a second request. This is
            # static registry data, never user input, so `| safe` in the
            # template is fine.
            "voice_models_json": json.dumps(model_registry.VOICE_MODELS),
            "license_notes_json": json.dumps(model_registry.LICENSE_NOTE),
        },
    )


@router.get("/api/jobs/{job_id}")
def api_job_status(job_id: str, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    return jobs_module.to_public_dict(job)


@router.post("/api/jobs/{job_id}/fruit-replace")
async def api_fruit_replace(job_id: str, request: Request, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    result = apply_random_fruit_replacement(text, rng=random.Random())
    return result


@router.post("/api/jobs/{job_id}/submit-edit")
async def api_submit_edit(job_id: str, request: Request, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    body = await request.json()
    text = body.get("text", "")
    replacements = body.get("replacements", [])
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if not isinstance(replacements, list):
        raise HTTPException(status_code=400, detail="replacements must be a list")
    if job["state"] != "transcribing" or not job["awaiting_user_edit"]:
        raise HTTPException(
            status_code=409,
            detail=f"job is not awaiting an edit (state={job['state']})",
        )

    # Engine/preset/mode are chosen on THIS screen (job.html), right
    # before Run — not at upload time — so this is where it's actually
    # validated and persisted, atomically with the edit itself. Omitted
    # fields keep whatever was set at upload (backward compatible with
    # any caller that doesn't send them).
    word_replacement_mode = body.get("word_replacement_mode")
    voice_model = body.get("voice_model")
    voice_preset = body.get("voice_preset")
    if word_replacement_mode is not None:
        try:
            model_registry.validate_word_replacement_mode(word_replacement_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if voice_model is not None or voice_preset is not None:
        final_model = voice_model if voice_model is not None else job["voice_model"]
        final_preset = voice_preset if voice_preset is not None else job["voice_preset"]
        try:
            model_registry.validate_voice_choice(final_model, final_preset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        voice_model, voice_preset = final_model, final_preset

    # Bounds-check each replacement's index against transcript.words before
    # this ever reaches a worker — see docs/worker-protocol.md's "word-index
    # alignment" section. A replacement index is only meaningful positioned
    # against the ORIGINAL transcript's word list; if the user manually
    # edited the text afterward (changing word count), these indices can go
    # stale. We don't try to re-align them here (that's a real text-diff
    # problem), we just refuse to pass obviously-out-of-range indices
    # downstream so the worker never has to defend against an IndexError.
    transcript = json.loads(job["transcript_json"]) if job["transcript_json"] else None
    word_count = len(transcript["words"]) if transcript and transcript.get("words") else None
    if word_count is not None:
        for r in replacements:
            if not isinstance(r, dict) or not isinstance(r.get("index"), int):
                raise HTTPException(status_code=400, detail="each replacement needs an integer 'index'")
            if not (0 <= r["index"] < word_count):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"replacement index {r['index']} is out of range for the "
                        f"original transcript ({word_count} words) — the transcript "
                        f"text was likely edited manually after fruit-replace ran; "
                        f"resubmit without stale replacements or re-run fruit-replace "
                        f"on the current text"
                    ),
                )

    ok = jobs_module.submit_user_edit(
        job_id, admin["username"], {"text": text, "replacements": replacements},
        word_replacement_mode=word_replacement_mode,
        voice_model=voice_model,
        voice_preset=voice_preset,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="could not submit edit (job state changed)")
    return jobs_module.to_public_dict(jobs_module.get_job(job_id))


@router.get("/jobs/{job_id}/media/original")
def media_original(job_id: str, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    if not job["input_path"]:
        raise HTTPException(status_code=404, detail="no input media")
    return FileResponse(job["input_path"], media_type="video/mp4")


@router.get("/jobs/{job_id}/media/result")
def media_result(job_id: str, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    if not job["output_path"]:
        raise HTTPException(status_code=404, detail="no result media yet")
    return FileResponse(job["output_path"], media_type="video/mp4")


@router.get("/jobs/{job_id}/download")
def download(job_id: str, admin=Depends(require_admin_api)):
    job = _get_owned_job_or_404(job_id, admin["username"])
    if not job["output_path"]:
        raise HTTPException(status_code=404, detail="no result media yet")
    filename = f"edited_{job['input_filename'] or 'video.mp4'}"
    return FileResponse(job["output_path"], media_type="video/mp4", filename=filename)
