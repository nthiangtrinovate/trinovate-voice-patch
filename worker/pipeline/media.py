"""ffmpeg subprocess wrappers: extract the audio track from an uploaded
video for STT/cloning, and remux the edited audio back into the ORIGINAL
video container afterward.

Per PLAN.md Phase 2: "Video export: audio replaced, video stream otherwise
untouched. No lip syncing." We never re-encode or trim the video stream —
`-c:v copy` always. We deliberately do NOT pass `-shortest`: if a
synthesized replacement phrase ends up a little longer or shorter than the
original span (Phase 1's feasibility report measured drift of 0.04-0.24s
for short single-word edits), `-shortest` would risk truncating the tail
of a sentence. Letting the container hold mildly mismatched stream
durations is the safer tradeoff — video freezes on its last frame or audio
trails slightly past it, rather than cutting off speech.

Pure stdlib (`subprocess`) plus the `ffmpeg` binary — no Python package
dependency, so this can run in the lightweight main worker process rather
than needing either heavy ML venv.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

# Defense in depth, same rationale as worker/run_worker.py's
# _subprocess_env(): ffmpeg has no reason to see this, and subprocess.run()
# inherits the full parent environment unless told otherwise.
_ENV_SECRETS_TO_STRIP = ("PORTAL_WORKER_TOKEN",)


class MediaError(Exception):
    pass


def _subprocess_env() -> dict:
    env = dict(os.environ)
    for key in _ENV_SECRETS_TO_STRIP:
        env.pop(key, None)
    return env


def _run(cmd: list[str], timeout: int = 300) -> None:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False, env=_subprocess_env()
        )
    except FileNotFoundError as exc:
        raise MediaError(
            f"ffmpeg not found at {FFMPEG_BIN!r} — set FFMPEG_BIN to its full path "
            f"(see worker/README.md)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffmpeg timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        # stderr is ffmpeg's own diagnostic text, not request/token content —
        # safe to surface, but keep it bounded.
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        raise MediaError(f"ffmpeg failed (exit {proc.returncode}): {stderr_tail}")


def extract_audio(video_path: Path, out_wav_path: Path, sample_rate: int = 24000) -> None:
    """Extract the audio track as mono PCM16 wav at `sample_rate` Hz —
    matches what the STT/cloning pipeline scripts expect."""
    out_wav_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            FFMPEG_BIN, "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", str(sample_rate), "-acodec", "pcm_s16le",
            str(out_wav_path),
        ]
    )


def remux_audio_into_video(video_path: Path, new_audio_path: Path, out_video_path: Path) -> None:
    """Replace the video's audio stream with `new_audio_path`, re-encoding
    only the audio (AAC — required for a valid MP4 audio stream; raw PCM
    isn't a legal MP4 audio codec) and copying the video stream byte-for-
    byte untouched."""
    out_video_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-i", str(new_audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(out_video_path),
        ]
    )
