#!/usr/bin/env python3
"""Phase 2 worker pipeline — STT stage. Runs inside the `stt-venv`
(mlx-whisper) created during the Phase 1 feasibility pass; see
worker/README.md for how to set that venv up (or just point
STT_VENV_PYTHON at the one already built under
~/voice-word-editor-feasibility/stt-venv on the Mac mini).

Usage:
    python3 run_stt.py <input_audio_wav> <output_json>

Writes {engine, model, audio, elapsed_seconds, peak_rss_mb, text, words}
where `words` is [{"word": str, "start": float, "end": float}, ...] in
order. The CALLER (worker/run_worker.py), not this script, is responsible
for building the transcript text it submits to the portal as
" ".join(w["word"] for w in words) rather than this script's own `text`
field — that guarantees the word-array tokenization and the submitted
transcript text line up 1:1, which worker/pipeline/diffing.py depends on.
"""
import json
import os
import resource
import sys
import time

import mlx_whisper

MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


def main():
    if len(sys.argv) != 3:
        print("usage: run_stt.py <input_audio_wav> <output_json>", file=sys.stderr)
        sys.exit(2)
    audio_path, out_json = sys.argv[1], sys.argv[2]

    t0 = time.time()
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=MODEL,
        language="en",
        task="transcribe",
        word_timestamps=True,
        condition_on_previous_text=False,
        temperature=0.0,
        verbose=False,
    )
    elapsed = time.time() - t0
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024

    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"], "end": w["end"]})

    out = {
        "engine": "mlx-whisper",
        "model": MODEL,
        "audio": str(audio_path),
        "elapsed_seconds": elapsed,
        "peak_rss_mb": peak_rss_mb,
        "text": result.get("text", "").strip(),
        "words": words,
    }
    with open(out_json, "w") as f:
        json.dump(out, f, indent=2)
    # Only this one summary line goes to stdout/stderr — never the
    # transcript text itself, which could contain content the caller
    # doesn't want echoed into logs.
    print(json.dumps({"elapsed_seconds": elapsed, "peak_rss_mb": peak_rss_mb, "word_count": len(words)}))


if __name__ == "__main__":
    main()
