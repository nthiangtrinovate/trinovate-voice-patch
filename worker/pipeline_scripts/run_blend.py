#!/usr/bin/env python3
"""Phase 2 worker pipeline — blend stage. Runs inside the `tts-venv`
(pydub is installed there already from the cloning stage's dependency
install; see worker/README.md).

Splices every span's cloned phrase (or, for a pure-deletion span with no
cloned_wav, just cuts that range out) back into the original audio track,
with a short crossfade at each splice boundary — chosen deliberately short
because splice points sit at Whisper word boundaries, not mid-word.

Usage:
    python3 run_blend.py --original-audio <wav> --clone-result <json> \
        --output-wav <wav> [--crossfade-ms 25]

<json> is the clone_result.json written by run_clone.py (spans need
span_start/span_end/cloned_wav).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pydub import AudioSegment

TARGET_SR = 24000


def load_norm(path):
    seg = AudioSegment.from_file(path)
    return seg.set_frame_rate(TARGET_SR).set_channels(1).set_sample_width(2)


def splice_multi(original: AudioSegment, spans: list[dict], crossfade_ms: int) -> AudioSegment:
    """Apply every span (sorted by position) in one pass. A span with
    cloned_wav=None is a pure deletion — its original range is just cut,
    no replacement audio spliced in."""
    spans_sorted = sorted(spans, key=lambda s: s["span_start"])
    cursor_ms = 0
    pieces = [original[0:0]]  # seed with an empty segment of the right type

    for span in spans_sorted:
        start_ms = int(span["span_start"] * 1000)
        end_ms = int(span["span_end"] * 1000)
        pre_end = max(start_ms - crossfade_ms, cursor_ms)
        pre = original[cursor_ms:pre_end]
        cf = min(crossfade_ms, len(pieces[-1]), len(pre))
        pieces[-1] = pieces[-1].append(pre, crossfade=max(cf, 0))

        if span.get("cloned_wav"):
            cloned = load_norm(span["cloned_wav"])
            cf = min(crossfade_ms, len(pieces[-1]), len(cloned))
            pieces[-1] = pieces[-1].append(cloned, crossfade=max(cf, 0))
        # else: pure deletion, nothing to splice in for this span.

        cursor_ms = min(end_ms + crossfade_ms, len(original))

    tail = original[cursor_ms:]
    cf = min(crossfade_ms, len(pieces[-1]), len(tail))
    pieces[-1] = pieces[-1].append(tail, crossfade=max(cf, 0))
    return pieces[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-audio", required=True)
    parser.add_argument("--clone-result", required=True)
    parser.add_argument("--output-wav", required=True)
    parser.add_argument("--crossfade-ms", type=int, default=25)
    args = parser.parse_args()

    clone_result = json.loads(Path(args.clone_result).read_text())
    original = load_norm(args.original_audio)

    t0 = time.time()
    if clone_result["spans"]:
        combined = splice_multi(original, clone_result["spans"], args.crossfade_ms)
    else:
        combined = original  # no edits landed (shouldn't normally happen, but no-op is safe)
    elapsed = time.time() - t0

    combined.export(args.output_wav, format="wav")
    print(
        json.dumps(
            {
                "output_duration_seconds": len(combined) / 1000.0,
                "original_duration_seconds": len(original) / 1000.0,
                "span_count": len(clone_result["spans"]),
                "blend_seconds": elapsed,
            }
        )
    )


if __name__ == "__main__":
    main()
