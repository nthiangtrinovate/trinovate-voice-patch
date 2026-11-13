"""
Phase 1 feasibility proof — stage 3: blend the cloned replacement phrases
back into the original audio track with crossfades at the splice boundaries.

Reads outputs/clone_result.json (produced by run_clone.py) for the edit spans
and cloned-phrase wav paths, then for each edit (and for both edits applied
together) produces a full-length edited wav:

  [ original audio before span ] --xfade-- [ cloned phrase ] --xfade-- [ original audio after span ]

Crossfade length is kept short (25 ms) since the splice points sit at word
boundaries identified by Whisper, not mid-word.
"""
import json
import time
from pathlib import Path

from pydub import AudioSegment

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUT = ROOT / "outputs"

REFERENCE_AUDIO = ASSETS / "reference_original.wav"
CROSSFADE_MS = 25
TARGET_SR = 24000


def load_norm(path):
    seg = AudioSegment.from_file(path)
    return seg.set_frame_rate(TARGET_SR).set_channels(1).set_sample_width(2)


def splice_one(original, edit, out_path):
    start_ms = int(edit["original_span_start"] * 1000)
    end_ms = int(edit["original_span_end"] * 1000)

    before = original[: max(start_ms - CROSSFADE_MS, 0)]
    after = original[min(end_ms + CROSSFADE_MS, len(original)) :]
    cloned = load_norm(edit["cloned_wav"])

    combined = before.append(cloned, crossfade=min(CROSSFADE_MS, len(before), len(cloned)))
    combined = combined.append(after, crossfade=min(CROSSFADE_MS, len(combined), len(after)))

    combined.export(out_path, format="wav")
    return len(combined) / 1000.0


def splice_multi(original, edits, out_path):
    """Apply multiple non-overlapping edits (sorted by time) in one pass."""
    edits_sorted = sorted(edits, key=lambda e: e["original_span_start"])
    cursor_ms = 0
    pieces = []
    for edit in edits_sorted:
        start_ms = int(edit["original_span_start"] * 1000)
        end_ms = int(edit["original_span_end"] * 1000)
        pre_end = max(start_ms - CROSSFADE_MS, cursor_ms)
        pieces.append(("orig", original[cursor_ms:pre_end]))
        pieces.append(("cloned", load_norm(edit["cloned_wav"])))
        cursor_ms = min(end_ms + CROSSFADE_MS, len(original))
    pieces.append(("orig", original[cursor_ms:]))

    combined = pieces[0][1]
    for _, seg in pieces[1:]:
        cf = min(CROSSFADE_MS, len(combined), len(seg))
        combined = combined.append(seg, crossfade=max(cf, 0))

    combined.export(out_path, format="wav")
    return len(combined) / 1000.0


def main():
    clone_result = json.loads((OUT / "clone_result.json").read_text())
    original = load_norm(REFERENCE_AUDIO)
    original_duration = len(original) / 1000.0

    timings = {}
    report = {"original_duration_seconds": original_duration, "crossfade_ms": CROSSFADE_MS, "edits": []}

    for edit in clone_result["edits"]:
        out_path = OUT / f"{edit['name']}_blended_single_edit.wav"
        t0 = time.time()
        dur = splice_one(original, edit, out_path)
        elapsed = time.time() - t0
        report["edits"].append(
            {
                "name": edit["name"],
                "output": str(out_path),
                "output_duration_seconds": dur,
                "blend_seconds": elapsed,
            }
        )
        print(f"{edit['name']}: blended single-edit output -> {out_path} ({dur:.2f}s, blend {elapsed:.3f}s)")

    # Both edits applied together to one final clip
    final_path = OUT / "final_both_edits_blended.wav"
    t0 = time.time()
    final_dur = splice_multi(original, clone_result["edits"], final_path)
    final_elapsed = time.time() - t0
    report["final_both_edits"] = {
        "output": str(final_path),
        "output_duration_seconds": final_dur,
        "blend_seconds": final_elapsed,
    }
    print(f"BOTH edits combined -> {final_path} ({final_dur:.2f}s, blend {final_elapsed:.3f}s)")

    (OUT / "blend_result.json").write_text(json.dumps(report, indent=2))
    print("Wrote", OUT / "blend_result.json")


if __name__ == "__main__":
    main()
