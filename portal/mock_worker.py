#!/usr/bin/env python3
"""Mock worker — reference implementation of the outbound-only worker
protocol described in docs/worker-protocol.md.

This stands in for the real Mac mini worker (owned by the
model-feasibility teammate) until the real STT/TTS pipeline is validated.
It performs *no* real transcription or synthesis: transcripts are a fixed
stub sentence, and "synthesis + blending + export" is a byte-identical
passthrough of the original upload. The only things it exercises for
real are the HTTP contract: polling, claiming, stage transitions,
heartbeats, transcript/result upload, and error reporting.

Everything this script does is an OUTBOUND HTTPS request it initiates —
GET to poll/download, POST to report progress/results/errors. It never
listens on a port and the server never calls it. That mirrors the real
constraint: the Mac mini is outbound-only.

Usage:
    PORTAL_WORKER_TOKEN=<token> python3 mock_worker.py --base-url http://127.0.0.1:8000

The token must match what the server has provisioned (see app/security.py
ensure_worker_token). For local dev, if PORTAL_WORKER_TOKEN is unset here,
this script falls back to reading portal/data/dev_worker_token.txt, which
the server writes (mode 0600, gitignored) the first time it provisions a
token without an env var set — never printed to stdout/stderr/logs.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("mock_worker")

MOCK_TRANSCRIPT_TEXT = (
    "This is a mock transcript used to exercise the job queue end to end "
    "before the real transcription model is wired in."
)


def build_mock_words(text: str, duration_sec: float) -> list[dict]:
    words = text.split()
    if not words:
        return []
    span = duration_sec / len(words)
    out = []
    for i, w in enumerate(words):
        out.append({"word": w, "start": round(i * span, 3), "end": round((i + 1) * span, 3)})
    return out


class WorkerClient:
    def __init__(self, base_url: str, token: str, worker_id: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def _request(self, method: str, path: str, json_body=None, data=None,
                  content_type=None, timeout=30):
        url = self.base_url + path
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Worker-Id": self.worker_id,
        }
        body = None
        if json_body is not None:
            import json as _json
            body = _json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data
            if content_type:
                headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
                return status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def poll_next(self):
        status, raw = self._request("GET", "/worker/jobs/next")
        if status == 204:
            return None
        if status != 200:
            log.warning("poll_next unexpected status=%s", status)
            return None
        import json
        return json.loads(raw)

    def heartbeat(self, job_id: str):
        self._request("POST", f"/worker/jobs/{job_id}/heartbeat", json_body={})

    def submit_transcript(self, job_id: str, text: str, words: list, mock: bool = True):
        return self._request(
            "POST", f"/worker/jobs/{job_id}/transcript",
            json_body={"text": text, "words": words, "mock": mock},
        )

    def advance_stage(self, job_id: str, state: str):
        return self._request("POST", f"/worker/jobs/{job_id}/stage", json_body={"state": state})

    def download_input(self, job_id: str, download_url: str) -> bytes:
        status, raw = self._request("GET", download_url)
        if status != 200:
            raise RuntimeError(f"failed to download input for job {job_id}: status={status}")
        return raw

    def submit_result(self, job_id: str, payload: bytes):
        boundary = "----mockworkerboundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="result.mp4"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
        return self._request(
            "POST", f"/worker/jobs/{job_id}/result",
            data=body, content_type=f"multipart/form-data; boundary={boundary}",
            timeout=120,
        )

    def submit_error(self, job_id: str, message: str, detail: str | None, retryable: bool):
        return self._request(
            "POST", f"/worker/jobs/{job_id}/error",
            json_body={"message": message, "detail": detail, "retryable": retryable},
        )


def process_job(client: WorkerClient, job: dict, simulated_stage_seconds: float):
    job_id = job["id"]
    state = job["state"]
    try:
        if state == "transcribing" and not job.get("awaiting_user_edit"):
            log.info("job %s: transcribing (mock)", job_id)
            # Download input just to prove the outbound fetch path works;
            # the mock STT doesn't actually look at the bytes.
            client.download_input(job_id, job["input_download_url"])
            time.sleep(simulated_stage_seconds)
            duration = job.get("input_duration_sec") or 30.0
            words = build_mock_words(MOCK_TRANSCRIPT_TEXT, duration)
            status, _ = client.submit_transcript(job_id, MOCK_TRANSCRIPT_TEXT, words, mock=True)
            if status != 200:
                raise RuntimeError(f"submit_transcript failed status={status}")
            log.info("job %s: transcript submitted, now awaiting user edit", job_id)
            return

        if state == "generating":
            log.info("job %s: generating (mock synthesis)", job_id)
            time.sleep(simulated_stage_seconds)
            client.heartbeat(job_id)
            status, _ = client.advance_stage(job_id, "blending")
            if status != 200:
                raise RuntimeError(f"advance_stage(blending) failed status={status}")

            log.info("job %s: blending (mock)", job_id)
            time.sleep(simulated_stage_seconds)
            client.heartbeat(job_id)
            status, _ = client.advance_stage(job_id, "exporting")
            if status != 200:
                raise RuntimeError(f"advance_stage(exporting) failed status={status}")

            log.info("job %s: exporting (mock passthrough)", job_id)
            payload = client.download_input(job_id, job["input_download_url"])
            time.sleep(simulated_stage_seconds)
            status, _ = client.submit_result(job_id, payload)
            if status != 200:
                raise RuntimeError(f"submit_result failed status={status}")
            log.info("job %s: done (mock output = passthrough of original)", job_id)
            return

        log.info("job %s: nothing to do for state=%s awaiting_user_edit=%s",
                  job_id, state, job.get("awaiting_user_edit"))
    except Exception as exc:  # noqa: BLE001 - this is the top-level job error boundary
        log.exception("job %s failed", job_id)
        # Never include secrets (tokens/headers) in the error payload — this
        # is our own stack trace text about a mock-processing failure, not
        # request contents.
        client.submit_error(job_id, message=str(exc)[:300], detail=repr(exc)[:2000], retryable=True)


def main():
    parser = argparse.ArgumentParser(description="Mock worker for the voice-word-editor portal")
    parser.add_argument("--base-url", default=os.environ.get("PORTAL_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--worker-id", default=os.environ.get("PORTAL_WORKER_ID", "mock-worker-1"))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("PORTAL_POLL_INTERVAL", 2.0)))
    parser.add_argument("--simulated-stage-seconds", type=float,
                         default=float(os.environ.get("PORTAL_SIMULATED_STAGE_SECONDS", 0.5)))
    parser.add_argument("--once", action="store_true", help="process at most one job then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("PORTAL_WORKER_TOKEN")
    if not token:
        dev_token_path = Path(__file__).resolve().parent / "data" / "dev_worker_token.txt"
        if dev_token_path.exists():
            token = dev_token_path.read_text().strip()
            log.info("using dev worker token from %s", dev_token_path)
    if not token:
        print(
            "ERROR: set PORTAL_WORKER_TOKEN, or run the server once first so it "
            "provisions portal/data/dev_worker_token.txt for local dev. Refusing "
            "to run with no token.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = WorkerClient(args.base_url, token, args.worker_id)
    log.info("mock worker starting against %s (worker_id=%s)", args.base_url, args.worker_id)

    processed_any = False
    while True:
        job = client.poll_next()
        if job is None:
            if args.once and processed_any:
                break
            time.sleep(args.poll_interval)
            continue
        process_job(client, job, args.simulated_stage_seconds)
        processed_any = True
        if args.once:
            break


if __name__ == "__main__":
    main()
