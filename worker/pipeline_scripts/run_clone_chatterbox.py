#!/usr/bin/env python3
"""Chatterbox (Resemble AI) voice-cloning engine — drop-in alternative to
run_clone.py (OpenVoice). Same input/output JSON contract so run_blend.py
works unchanged regardless of which engine produced clone_result.json.

Runs inside the pre-existing spike venv
~/voice-word-editor-feasibility/tts-spike-chatterbox on the Mac mini
(chatterbox-tts==0.1.7, torch==2.6.0, torchaudio==2.6.0).

Deliberately reproduces the "no_rubberband_24k" recipe that was measured
(in a prior investigation) to sound closest to the original speaker for
this engine: trim silence, loudness-match via RMS-dB gain (clamped), pad
up to the replaced word's time slot if the synth came out short -- never
truncate or time-stretch if it came out long. There is NO pitch or tempo
("rubberband") correction anywhere in this script; that is the deliberate
point of this recipe, not an oversight. An overflow (synth longer than its
slot) is left as-is: run_blend.py's splice_multi() appends spans
sequentially rather than overwriting a fixed timeline slot, so a slightly
longer span just makes that part of the output video a little longer --
it does not truncate or overlap the surrounding original speech.

Per-span reference selection mirrors OpenVoice's own approach (and the
prior Chatterbox/F5 spike): try a span-local reference AND a clean
multi-segment reference built from elsewhere in this job's own audio,
synthesize against both, and keep whichever scores higher on Resemblyzer
similarity against the original span's own audio. If Resemblyzer isn't
available, falls back to the span-local reference deterministically.

Usage:
    python3 run_clone_chatterbox.py --input-json <path> --output-dir <dir>

Input JSON (identical shape to run_clone.py):
    {
      "reference_audio": "<path to original audio extracted from the job's video>",
      "transcript_words": [{"word": "...", "start": 0.72, "end": 1.36}, ...]  (optional),
      "spans": [{"id": 0, "new_text": "...", "span_start": 6.38, "span_end": 8.44}, ...]
    }

Output JSON (<output-dir>/clone_result.json, also printed to stdout):
    {"engine": "chatterbox", "engine_revision": "<hf commit or 'unknown'>",
     "device": "mps"|"cpu", "spans": [...]}
"""
from __future__ import annotations

# Force the already-cached model snapshot to be used as-is, never silently
# re-resolved to a newer upstream revision. This is how we pin Chatterbox's
# checkpoint version, since ChatterboxTTS.from_pretrained(device) takes no
# revision argument (confirmed by inspecting its signature in this venv).
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
SR = 24000

REF_PAD_SEC = 0.50
GAIN_CLAMP_DB = (-12.0, 12.0)
TRIM_TOP_DB = 35.0
REF_FILTER = "highpass=f=70,loudnorm=I=-16:TP=-1.0:LRA=11"

CLEAN_REF_GAP_SEC = 0.25
CLEAN_REF_MIN_RUN_SEC = 0.4
CLEAN_REF_MAX_RUN_SEC = 3.0
CLEAN_REF_TARGET_SEC = 8.0
CLEAN_REF_MAX_SEGMENTS = 12


class ChatterboxEngineError(RuntimeError):
    """A Chatterbox-specific failure. Must surface as-is -- never caught
    and silently swapped for a different engine."""


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def _ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def _clip(src: str, start: float, end: float | None, out: Path, sr: int, af: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", src, "-ss", f"{max(0.0, start):.3f}"]
    if end is not None:
        args += ["-to", f"{max(0.0, end):.3f}"]
    if af:
        args += ["-af", af]
    args += ["-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(out)]
    _ffmpeg(args)


def _rms_db(path: str) -> float:
    y, _ = librosa.load(path, sr=None, mono=True)
    if y.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(y))))
    return 20.0 * np.log10(r) if r > 1e-9 else -120.0


def _trim(src: Path, dst: Path, top_db: float = TRIM_TOP_DB) -> float:
    y, sr = librosa.load(str(src), sr=None, mono=True)
    if y.size == 0:
        sf.write(str(dst), y, sr)
        return 0.0
    yt, _ = librosa.effects.trim(y, top_db=top_db)
    if yt.size < int(0.05 * sr):
        yt = y
    sf.write(str(dst), yt, sr)
    return float(yt.size / float(sr))


def _load_resemblyzer():
    try:
        from resemblyzer import VoiceEncoder

        return VoiceEncoder(device="cpu", verbose=False)
    except Exception as exc:
        print(f"Resemblyzer unavailable ({exc}); falling back to span-local reference only", file=sys.stderr)
        return None


def _embed_safe(path: str, encoder) -> np.ndarray | None:
    if encoder is None:
        return None
    try:
        from resemblyzer import preprocess_wav

        wav = preprocess_wav(str(path))
        if wav.size < int(0.3 * 16000):
            return None
        return encoder.embed_utterance(wav)
    except Exception as exc:
        print(f"  resemblyzer embed failed for {path}: {exc}", file=sys.stderr)
        return None


def _cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return None
    return float(np.dot(a, b) / (na * nb))


def _build_speech_runs(words: list[dict], gap_sec: float, min_dur: float, max_dur: float) -> list[tuple[float, float]]:
    runs: list[tuple[float, float]] = []
    cur_start = cur_end = None
    for w in words:
        s, e = float(w["start"]), float(w["end"])
        if cur_start is None:
            cur_start, cur_end = s, e
            continue
        gap = s - cur_end
        if gap <= gap_sec and (e - cur_start) <= max_dur:
            cur_end = max(cur_end, e)
        else:
            if cur_end - cur_start >= min_dur:
                runs.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_start is not None and cur_end - cur_start >= min_dur:
        runs.append((cur_start, cur_end))
    return runs


def build_clean_reference_24k(reference_audio: str, words: list[dict], out_dir: Path, encoder) -> tuple[Path | None, dict]:
    """Adapted from run_clone.py's build_clean_reference(): score candidate
    speech runs (16k, for Resemblyzer) but build the actual reference audio
    handed to Chatterbox at native 24k, concatenated from the winning runs.

    NOTE: the original spike's exact clean-reference-building script
    (scratch_build_ref_24k.py) was not available to reconstruct this from —
    it was never captured in this session's recoverable context (unlike
    scratch_spike2_engine.py and scratch_build_spike2_outputs.py, which
    were). This rebuilds the same documented selection algorithm from
    run_clone.py's build_clean_reference() at 24k instead of inventing a
    different one. Flagged here explicitly as a disclosed deviation.
    """
    meta: dict = {"runs_found": 0, "segments_selected": 0, "total_sec": 0.0}
    if not words or encoder is None:
        return None, meta

    runs = _build_speech_runs(words, CLEAN_REF_GAP_SEC, CLEAN_REF_MIN_RUN_SEC, CLEAN_REF_MAX_RUN_SEC)
    meta["runs_found"] = len(runs)
    if not runs:
        return None, meta

    cand_dir = out_dir / "clean_ref_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for i, (s, e) in enumerate(runs):
        scoring_clip = cand_dir / f"run_{i}_16k.wav"
        try:
            _clip(reference_audio, s, e, scoring_clip, 16000, af=REF_FILTER)
            dur = sf.info(str(scoring_clip)).duration
        except Exception:
            continue
        if dur < CLEAN_REF_MIN_RUN_SEC:
            continue
        emb = _embed_safe(str(scoring_clip), encoder)
        if emb is None:
            continue
        items.append({"start": s, "end": e, "duration": dur, "rms_db": _rms_db(str(scoring_clip)), "embedding": emb})

    if not items:
        return None, meta

    embs = np.stack([it["embedding"] for it in items])
    anchor = embs.mean(axis=0)
    anchor_norm = np.linalg.norm(anchor)
    if anchor_norm < 1e-9:
        return None, meta
    anchor = anchor / anchor_norm
    for it in items:
        it["sim"] = _cosine(it["embedding"], anchor) or 0.0

    rms_floor = float(np.percentile([it["rms_db"] for it in items], 25))
    pool = [it for it in items if it["rms_db"] >= rms_floor] or items
    pool.sort(key=lambda it: it["sim"], reverse=True)

    selected = []
    total = 0.0
    for it in pool:
        if len(selected) >= CLEAN_REF_MAX_SEGMENTS:
            break
        selected.append(it)
        total += it["duration"]
        if total >= CLEAN_REF_TARGET_SEC:
            break

    meta["segments_selected"] = len(selected)
    meta["total_sec"] = total
    if not selected or total < 1.0:
        return None, meta

    # Build the actual 24k reference handed to Chatterbox by re-clipping
    # (at 24k, with the same denoise filter) and concatenating the winning
    # time ranges, in their original time order.
    selected_sorted = sorted(selected, key=lambda it: it["start"])
    piece_paths = []
    for i, it in enumerate(selected_sorted):
        piece = cand_dir / f"selected_{i}_24k.wav"
        _clip(reference_audio, it["start"], it["end"], piece, SR, af=REF_FILTER)
        piece_paths.append(piece)

    concat_list = cand_dir / "concat_list.txt"
    concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in piece_paths) + "\n")
    clean_ref = out_dir / "clean_ref_24k.wav"
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-ar", str(SR), "-ac", "1",
             "-c:a", "pcm_s16le", str(clean_ref)])
    return clean_ref, meta


def _load_model():
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    from chatterbox.tts import ChatterboxTTS

    model = ChatterboxTTS.from_pretrained(device)
    if model.sr != SR:
        raise ChatterboxEngineError(f"chatterbox native sample rate {model.sr} != expected {SR}")

    revision = "unknown"
    cache_refs = Path.home() / ".cache" / "huggingface" / "hub" / "models--ResembleAI--chatterbox" / "refs" / "main"
    if cache_refs.exists():
        revision = cache_refs.read_text().strip()

    return model, device, revision


def _synth_and_process(model, text: str, ref_wav: Path, orig_clip: Path, slot: float, out_path: Path) -> dict:
    """Synthesize `text` against `ref_wav`, then apply the no-rubberband
    recipe (trim, loudness-match, pad-only), writing the final wav to
    `out_path`. Returns stage metadata."""
    import torchaudio

    raw_path = out_path.with_name(out_path.stem + "_raw.wav")
    try:
        wav = model.generate(text, audio_prompt_path=str(ref_wav))
        torchaudio.save(str(raw_path), wav, model.sr)
    except Exception as exc:
        raise ChatterboxEngineError(f"chatterbox synthesis failed for text={text!r}: {exc}") from exc

    trimmed_path = out_path.with_name(out_path.stem + "_trim.wav")
    natural_duration = _trim(raw_path, trimmed_path)

    gain_db = _rms_db(str(orig_clip)) - _rms_db(str(trimmed_path))
    gain_db = float(min(GAIN_CLAMP_DB[1], max(GAIN_CLAMP_DB[0], gain_db)))

    # Pad up to the slot if short; NEVER truncate/stretch if long (apad is
    # a no-op when the input is already >= whole_dur).
    _ffmpeg(["-i", str(trimmed_path), "-af", f"apad=whole_dur={max(slot, 0.01):.3f},volume={gain_db:.2f}dB",
             "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(out_path)])

    return {
        "raw_duration": sf.info(str(raw_path)).duration,
        "natural_duration": natural_duration,
        "final_duration": sf.info(str(out_path)).duration,
        "gain_db": gain_db,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    spec = json.loads(Path(args.input_json).read_text())
    reference_audio = spec["reference_audio"]
    spans = spec["spans"]
    transcript_words = spec.get("transcript_words") or []

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    model, device, revision = _load_model()
    load_seconds = time.time() - t0
    encoder = _load_resemblyzer()

    print(f"chatterbox engine_revision={revision} device={device} resemblyzer={'on' if encoder else 'off'}",
          file=sys.stderr)

    clean_ref_cache: dict[str, object] = {}

    def clean_reference():
        if "ref" not in clean_ref_cache:
            ref, meta = build_clean_reference_24k(reference_audio, transcript_words, out_dir, encoder)
            clean_ref_cache["ref"] = ref
            clean_ref_cache["meta"] = meta
        return clean_ref_cache["ref"], clean_ref_cache["meta"]

    results = []
    for span in spans:
        span_id = span["id"]
        new_text = (span.get("new_text") or "").strip()
        start = float(span["span_start"])
        end = float(span["span_end"])
        slot = max(0.0, end - start)

        if not new_text:
            results.append({"id": span_id, "new_text": "", "cloned_wav": None, "skipped": "empty_new_text",
                             "span_start": start, "span_end": end})
            continue

        orig_clip = out_dir / f"span_{span_id}_orig.wav"
        _clip(reference_audio, start, end, orig_clip, SR)
        orig_embedding = _embed_safe(str(orig_clip), encoder)

        span_ref = out_dir / f"span_{span_id}_ref.wav"
        _clip(reference_audio, max(0.0, start - REF_PAD_SEC), end + REF_PAD_SEC, span_ref, SR, af=REF_FILTER)

        variants = [("span", span_ref)]
        clean_ref, _clean_meta = clean_reference()
        if clean_ref is not None:
            variants.append(("clean", clean_ref))

        candidates = []
        for variant_name, ref_wav in variants:
            final_path = out_dir / f"span_{span_id}_{variant_name}_final.wav"
            stage_meta = _synth_and_process(model, new_text, ref_wav, orig_clip, slot, final_path)
            score = _cosine(_embed_safe(str(final_path), encoder), orig_embedding)
            candidates.append({"variant": variant_name, "path": final_path, "score": score, "meta": stage_meta})

        scored = [c for c in candidates if c["score"] is not None]
        best = max(scored, key=lambda c: c["score"]) if scored else candidates[0]

        cloned_wav = out_dir / f"span_{span_id}_cloned.wav"
        best["path"].replace(cloned_wav)

        results.append({
            "id": span_id,
            "new_text": new_text,
            "span_start": start,
            "span_end": end,
            "cloned_wav": str(cloned_wav),
            "reference_variant_used": best["variant"],
            "resemblyzer_score": best["score"],
            "candidates_scored": {c["variant"]: c["score"] for c in candidates},
            **best["meta"],
        })

    out = {
        "engine": "chatterbox",
        "engine_revision": revision,
        "device": device,
        "load_seconds": load_seconds,
        "final_peak_rss_mb": peak_rss_mb(),
        "spans": results,
    }
    (out_dir / "clone_result.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"engine": "chatterbox", "engine_revision": revision, "span_count": len(results)}))


if __name__ == "__main__":
    main()
