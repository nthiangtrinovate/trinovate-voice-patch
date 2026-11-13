#!/usr/bin/env python3
"""Real Mac mini worker — implements docs/worker-protocol.md against the
actual local STT/cloning/blend pipeline validated in Phase 1
(docs/feasibility-report.md), instead of portal_queue's mock_worker.py
stub passthrough.

Design constraints this file must keep (see PLAN.md + docs/worker-protocol.md):
  - Outbound-only: every request is GET/POST that THIS script initiates.
    Never binds a listening socket.
  - Isolated deps: this orchestrator itself needs only the Python stdlib
    (matches docs/worker-protocol.md's note that the reference mock worker
    needs "ideally none" extra deps). The heavy ML work (mlx-whisper,
    OpenVoice V2, MeloTTS) runs in two SEPARATE project-local venvs,
    invoked as subprocesses — see worker/README.md "Why two venvs" for why
    they can't be merged (incompatible pinned dependencies discovered in
    Phase 1).
  - No secrets in logs: the worker token is read from env or a 0600 file,
    passed only in the Authorization header, never printed/logged.
  - Limited concurrency: processes one job at a time; never spawns
    unbounded parallel subprocess work.

Usage:
    PORTAL_WORKER_TOKEN=<token> python3 worker/run_worker.py \
        --base-url http://127.0.0.1:8000

Or configure everything via env vars — see worker/README.md for the full
list (STT_VENV_PYTHON, TTS_VENV_PYTHON, OPENVOICE_CHECKPOINTS_DIR,
FFMPEG_BIN, WORKER_SCRATCH_DIR, CONTEXT_WORDS, CROSSFADE_MS).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import diffing, media  # noqa: E402

log = logging.getLogger("worker")

FEASIBILITY_HOME = Path(os.environ.get("FEASIBILITY_HOME", Path.home() / "voice-word-editor-feasibility"))


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default)))


STT_VENV_PYTHON = _env_path("STT_VENV_PYTHON", FEASIBILITY_HOME / "stt-venv" / "bin" / "python")
TTS_VENV_PYTHON = _env_path("TTS_VENV_PYTHON", FEASIBILITY_HOME / "tts-venv" / "bin" / "python")
OPENVOICE_CHECKPOINTS_DIR = _env_path("OPENVOICE_CHECKPOINTS_DIR", FEASIBILITY_HOME / "checkpoints_v2")
# Chatterbox/F5-TTS each get their own persistent, pinned venv (the same
# ones built during the throwaway spike evaluation — see
# docs/portal-notes.md "Model selection") — never the OpenVoice tts-venv,
# so a missing/broken engine venv fails loudly instead of silently
# running a different engine's code under another engine's dependencies.
CHATTERBOX_VENV_PYTHON = _env_path(
    "CHATTERBOX_VENV_PYTHON", FEASIBILITY_HOME / "tts-spike-chatterbox" / "bin" / "python3"
)
F5_VENV_PYTHON = _env_path("F5_VENV_PYTHON", FEASIBILITY_HOME / "tts-spike-f5" / "bin" / "python3")
WORKER_SCRATCH_DIR = _env_path("WORKER_SCRATCH_DIR", FEASIBILITY_HOME / "worker_jobs")
CONTEXT_WORDS = int(os.environ.get("CONTEXT_WORDS", 2))
CROSSFADE_MS = int(os.environ.get("CROSSFADE_MS", 25))

PIPELINE_SCRIPTS = Path(__file__).resolve().parent / "pipeline_scripts"

# Secrets that must never reach a child process's environment, even though
# none of pipeline_scripts/*.py currently reads or prints them. Defense in
# depth per PLAN.md "no secrets in logs" — a future debug dump of
# os.environ, or a library crash traceback that echoes its environment,
# should not be able to expose this. See _subprocess_env().
_ENV_SECRETS_TO_STRIP = ("PORTAL_WORKER_TOKEN",)

# Mirrors portal/app/model_registry.py's OpenVoice preset -> env override
# mapping. Deliberately duplicated rather than imported: this worker is
# intentionally stdlib-only and runs on a different host/venv from the
# portal (see module docstring, "Isolated deps") and must not depend on
# the portal's Python package. Keep these two in sync if either changes —
# docs/worker-protocol.md documents the voice_model/voice_preset job
# fields and this is the only other place the mapping lives.
_VOICE_PRESET_ENV_OVERRIDES = {
    "openvoice": {
        "balanced": {},
        "fast": {"VOICE_RESEMBLYZER_MATCH": "0", "VOICE_TAU": "0.60"},
    },
    "chatterbox": {"no_rubberband": {}},
    "f5tts": {"no_rubberband": {}},
}

# Which venv python + pipeline script runs each engine. Each engine is a
# fully separate persistent venv/process — never falls back to another
# engine's venv or script if its own is missing (see _dispatch_clone_stage).
_ENGINE_DISPATCH = {
    "openvoice": (lambda: TTS_VENV_PYTHON, "run_clone.py"),
    "chatterbox": (lambda: CHATTERBOX_VENV_PYTHON, "run_clone_chatterbox.py"),
    "f5tts": (lambda: F5_VENV_PYTHON, "run_clone_f5.py"),
}


def _voice_preset_overrides(voice_model: str, voice_preset: str) -> dict:
    model_presets = _VOICE_PRESET_ENV_OVERRIDES.get(voice_model)
    if model_presets is None:
        raise WorkerError(
            f"unsupported voice_model {voice_model!r} — only "
            f"{sorted(_VOICE_PRESET_ENV_OVERRIDES)} are wired into this worker today",
            retryable=False,
        )
    if voice_preset not in model_presets:
        raise WorkerError(
            f"unsupported voice_preset {voice_preset!r} for model {voice_model!r}",
            retryable=False,
        )
    return model_presets[voice_preset]


def _dispatch_clone_stage(voice_model: str, voice_preset: str, clone_input: Path, scratch: Path) -> None:
    """Run the chosen engine's pipeline script. Each engine is dispatched
    to its own venv/script — never substitutes a different engine, and
    raises a clear engine-specific WorkerError (not a silent OpenVoice
    fallback) if that engine's venv isn't actually present on this
    worker host."""
    dispatch = _ENGINE_DISPATCH.get(voice_model)
    if dispatch is None:
        raise WorkerError(f"unsupported voice_model {voice_model!r}", retryable=False)
    venv_python_fn, script_name = dispatch
    venv_python = venv_python_fn()
    if not venv_python.exists():
        raise WorkerError(
            f"engine {voice_model!r} is unavailable on this worker: "
            f"venv python not found at {venv_python}",
            retryable=False,
        )
    if voice_model == "openvoice":
        voice_env_overrides = _voice_preset_overrides(voice_model, voice_preset)
        run_subprocess(
            venv_python, PIPELINE_SCRIPTS / script_name,
            ["--input-json", str(clone_input), "--output-dir", str(scratch),
             "--checkpoints-dir", str(OPENVOICE_CHECKPOINTS_DIR)],
            timeout=600, extra_env=voice_env_overrides,
        )
    else:
        # Chatterbox/F5-TTS: no --checkpoints-dir (they load from the HF
        # cache themselves) and no env overrides (one preset each today —
        # see model_registry.py).
        run_subprocess(
            venv_python, PIPELINE_SCRIPTS / script_name,
            ["--input-json", str(clone_input), "--output-dir", str(scratch)],
            timeout=600,
        )


def _subprocess_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    for key in _ENV_SECRETS_TO_STRIP:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


class WorkerError(Exception):
    """A job-level failure to report via POST /worker/jobs/{id}/error.
    Never put the Authorization header or token value into this."""

    def __init__(self, message: str, detail: str | None = None, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.retryable = retryable


def run_subprocess(python_bin: Path, script: Path, args: list[str], timeout: int = 300,
                    extra_env: dict | None = None) -> str:
    """Run a pipeline_scripts/*.py under the given venv's interpreter.
    Returns stdout. Raises WorkerError with the stderr tail on failure.

    None of pipeline_scripts/*.py currently reads or prints
    PORTAL_WORKER_TOKEN, so this was never an active leak — but
    subprocess.run() inherits the full parent environment by default
    unless told otherwise, so the token WOULD have been there for a
    future debug dump of os.environ or a library crash traceback to
    expose (review-tests caught this exact docstring/code mismatch).
    _subprocess_env() strips it as defense in depth; see
    _ENV_SECRETS_TO_STRIP above."""
    if not python_bin.exists():
        raise WorkerError(
            f"venv python not found at {python_bin} — see worker/README.md to set it up "
            f"(or point STT_VENV_PYTHON/TTS_VENV_PYTHON at an existing one)",
            retryable=False,
        )
    cmd = [str(python_bin), str(script)] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True,
                               env=_subprocess_env(extra_env))
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"{script.name} timed out after {timeout}s", detail=str(exc))
    if proc.returncode != 0:
        raise WorkerError(
            f"{script.name} failed (exit {proc.returncode})",
            detail=f"stderr tail:\n{proc.stderr[-3000:]}\nstdout tail:\n{proc.stdout[-1000:]}",
        )
    log.info("%s: %s", script.name, proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "ok")
    return proc.stdout


class WorkerClient:
    def __init__(self, base_url: str, token: str, worker_id: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def _request(self, method: str, path: str, json_body=None, data=None, content_type=None, timeout=60):
        url = self.base_url + path
        headers = {"Authorization": f"Bearer {self.token}", "X-Worker-Id": self.worker_id}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data
            if content_type:
                headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def poll_next(self):
        status, raw = self._request("GET", "/worker/jobs/next")
        if status == 204:
            return None
        if status != 200:
            log.warning("poll_next unexpected status=%s", status)
            return None
        return json.loads(raw)

    def heartbeat(self, job_id: str):
        self._request("POST", f"/worker/jobs/{job_id}/heartbeat", json_body={})

    def submit_transcript(self, job_id: str, text: str, words: list, mock: bool = False):
        return self._request(
            "POST", f"/worker/jobs/{job_id}/transcript", json_body={"text": text, "words": words, "mock": mock}
        )

    def advance_stage(self, job_id: str, state: str):
        return self._request("POST", f"/worker/jobs/{job_id}/stage", json_body={"state": state})

    def download_input_to_file(self, job_id: str, download_url: str, dest_path: Path):
        url = self.base_url + download_url
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}", "X-Worker-Id": self.worker_id}
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 200:
                    raise WorkerError(f"failed to download input for job {job_id}: status={resp.status}")
                with open(dest_path, "wb") as out:
                    shutil.copyfileobj(resp, out)
        except urllib.error.HTTPError as e:
            raise WorkerError(f"failed to download input for job {job_id}: status={e.code}")

    def submit_result(self, job_id: str, file_path: Path, engine_metadata: dict | None = None):
        boundary = "----voicewordeditorworkerboundary"
        payload = file_path.read_bytes()
        parts = b""
        if engine_metadata:
            parts += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="engine_metadata"\r\n\r\n'
                f"{json.dumps(engine_metadata)}\r\n"
            ).encode("utf-8")
        parts += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="result.mp4"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8")
        body = parts + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
        return self._request(
            "POST", f"/worker/jobs/{job_id}/result",
            data=body, content_type=f"multipart/form-data; boundary={boundary}", timeout=180,
        )

    def submit_error(self, job_id: str, err: WorkerError):
        return self._request(
            "POST", f"/worker/jobs/{job_id}/error",
            json_body={"message": err.message[:500], "detail": (err.detail or "")[:4000], "retryable": err.retryable},
        )


def _read_engine_metadata(clone_result_path: Path) -> dict | None:
    """Normalize the engine-identifying top-level fields each clone script
    writes into clone_result.json (field names differ slightly per engine
    — e.g. Chatterbox's `engine_revision` vs F5's `model_hf_revision` —
    since each script's fork was written independently against the same
    contract). This is what actually ran, not what was merely selected,
    and gets attached to the job/output so that distinction is visible."""
    try:
        data = json.loads(clone_result_path.read_text())
    except (OSError, ValueError):
        return None
    return {
        "engine": data.get("engine"),
        "device": data.get("device"),
        "engine_revision": data.get("engine_revision") or data.get("model_hf_revision"),
        "model_ckpt_file": data.get("model_ckpt_file"),
        "license": data.get("license"),
    }


def _stage_transcribing(client: WorkerClient, job: dict, scratch: Path) -> None:
    job_id = job["id"]
    video_path = scratch / "input.mp4"
    audio_path = scratch / "audio.wav"

    client.download_input_to_file(job_id, job["input_download_url"], video_path)
    client.heartbeat(job_id)

    media.extract_audio(video_path, audio_path)
    client.heartbeat(job_id)

    # Measured on a 21.9s clip during integration testing: this stage (cold
    # model load + inference) took ~82s against a 120s default lease — not
    # over, but not a lot of margin either, especially for a longer
    # (up to 60s per PLAN.md) upload. Heartbeat right before the slow call.
    stt_json = scratch / "stt_result.json"
    run_subprocess(STT_VENV_PYTHON, PIPELINE_SCRIPTS / "run_stt.py", [str(audio_path), str(stt_json)])
    stt_result = json.loads(stt_json.read_text())
    words = stt_result["words"]
    if not words:
        raise WorkerError("STT produced no words — audio may be silent or unsupported", retryable=True)

    # Build the submitted text as the join of our own word tokens (NOT
    # Whisper's own `text` field) so later diffing against the user's edit
    # has 1:1 tokenization alignment — see pipeline/diffing.py's docstring.
    text = " ".join(w["word"] for w in words)
    status, _ = client.submit_transcript(job_id, text, words, mock=False)
    if status != 200:
        raise WorkerError(f"submit_transcript failed status={status}")
    log.info("job %s: transcript submitted (%d words), now awaiting user edit", job_id, len(words))


def _stage_generating(client: WorkerClient, job: dict, scratch: Path) -> None:
    job_id = job["id"]
    video_path = scratch / "input.mp4"
    audio_path = scratch / "audio.wav"

    client.download_input_to_file(job_id, job["input_download_url"], video_path)
    media.extract_audio(video_path, audio_path)
    client.heartbeat(job_id)

    original_words = job["transcript"]["words"]
    edited_text = job["edit"]["text"]
    spans = diffing.compute_edit_spans(original_words, edited_text, context_words=CONTEXT_WORDS)
    log.info("job %s: %d edit span(s) computed from diff", job_id, len(spans))

    edited_audio_path = scratch / "edited_audio.wav"
    engine_metadata = None

    if spans:
        clone_input = scratch / "clone_input.json"
        clone_input.write_text(
            json.dumps(
                {
                    "reference_audio": str(audio_path),
                    # Full word timeline so run_clone.py can build a clean,
                    # multi-segment target-speaker reference from this job's
                    # own audio (dynamic per video) instead of relying only
                    # on the ~2-3s local span clip.
                    "transcript_words": original_words,
                    "spans": [
                        {"id": i, "new_text": s.new_text, "span_start": s.span_start_sec, "span_end": s.span_end_sec}
                        for i, s in enumerate(spans)
                    ],
                }
            )
        )
        voice_model = job.get("voice_model") or "openvoice"
        voice_preset = job.get("voice_preset") or "balanced"
        _dispatch_clone_stage(voice_model, voice_preset, clone_input, scratch)
        clone_result_json = scratch / "clone_result.json"
        engine_metadata = _read_engine_metadata(clone_result_json)
        client.heartbeat(job_id)
        status, _ = client.advance_stage(job_id, "blending")
        if status != 200:
            raise WorkerError(f"advance_stage(blending) failed status={status}")

        run_subprocess(
            TTS_VENV_PYTHON, PIPELINE_SCRIPTS / "run_blend.py",
            [
                "--original-audio", str(audio_path),
                "--clone-result", str(clone_result_json),
                "--output-wav", str(edited_audio_path),
                "--crossfade-ms", str(CROSSFADE_MS),
            ],
        )
    else:
        # Diff found no actual change (edge case — shouldn't normally
        # happen since the portal only moves to "generating" once the
        # user submits an edit, but a no-op edit is a legitimate no-op).
        log.info("job %s: no edit spans found, passing audio through unchanged", job_id)
        shutil.copyfile(audio_path, edited_audio_path)
        client.heartbeat(job_id)
        status, _ = client.advance_stage(job_id, "blending")
        if status != 200:
            raise WorkerError(f"advance_stage(blending) failed status={status}")

    client.heartbeat(job_id)
    status, _ = client.advance_stage(job_id, "exporting")
    if status != 200:
        raise WorkerError(f"advance_stage(exporting) failed status={status}")

    output_video_path = scratch / "output.mp4"
    media.remux_audio_into_video(video_path, edited_audio_path, output_video_path)

    status, _ = client.submit_result(job_id, output_video_path, engine_metadata=engine_metadata)
    if status != 200:
        raise WorkerError(f"submit_result failed status={status}")
    log.info("job %s: done (%d span(s) edited, engine=%s)", job_id, len(spans),
              (engine_metadata or {}).get("engine"))


def process_job(client: WorkerClient, job: dict) -> None:
    job_id = job["id"]
    state = job["state"]
    WORKER_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"job-{job_id}-", dir=str(WORKER_SCRATCH_DIR)))
    try:
        if state == "transcribing" and not job.get("awaiting_user_edit"):
            _stage_transcribing(client, job, scratch)
        elif state == "generating":
            _stage_generating(client, job, scratch)
        else:
            log.info(
                "job %s: nothing to do for state=%s awaiting_user_edit=%s",
                job_id, state, job.get("awaiting_user_edit"),
            )
    except WorkerError as err:
        log.error("job %s failed: %s", job_id, err.message)
        client.submit_error(job_id, err)
    except media.MediaError as exc:
        log.exception("job %s failed (media error)", job_id)
        client.submit_error(job_id, WorkerError(str(exc)[:500], detail=repr(exc)[:2000], retryable=True))
    except Exception as exc:  # noqa: BLE001 - top-level job error boundary, mirrors mock_worker.py
        log.exception("job %s failed (unexpected)", job_id)
        client.submit_error(job_id, WorkerError(str(exc)[:500], detail=repr(exc)[:2000], retryable=True))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _load_token() -> str:
    token = os.environ.get("PORTAL_WORKER_TOKEN")
    if token:
        return token
    dev_token_path = Path(__file__).resolve().parent.parent / "portal" / "data" / "dev_worker_token.txt"
    if dev_token_path.exists():
        log.info("using dev worker token from %s", dev_token_path)
        return dev_token_path.read_text().strip()
    print(
        "ERROR: set PORTAL_WORKER_TOKEN, or run the portal server once first so it "
        "provisions portal/data/dev_worker_token.txt for local dev. Refusing to run "
        "with no token.",
        file=sys.stderr,
    )
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Real worker for the voice-word-editor portal")
    parser.add_argument("--base-url", default=os.environ.get("PORTAL_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--worker-id", default=os.environ.get("PORTAL_WORKER_ID", "mac-mini-worker-1"))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("PORTAL_POLL_INTERVAL", 2.0)))
    parser.add_argument("--once", action="store_true", help="process at most one job then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = _load_token()
    client = WorkerClient(args.base_url, token, args.worker_id)
    log.info("real worker starting against %s (worker_id=%s)", args.base_url, args.worker_id)
    log.info(
        "stt_venv=%s tts_venv=%s checkpoints=%s scratch=%s",
        STT_VENV_PYTHON, TTS_VENV_PYTHON, OPENVOICE_CHECKPOINTS_DIR, WORKER_SCRATCH_DIR,
    )

    processed_any = False
    while True:
        job = client.poll_next()
        if job is None:
            if args.once and processed_any:
                break
            time.sleep(args.poll_interval)
            continue
        process_job(client, job)
        processed_any = True
        if args.once:
            break


if __name__ == "__main__":
    main()
