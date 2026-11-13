#!/usr/bin/env python3
"""F5-TTS same-speaker cloning stage -- drop-in alternative to
run_clone.py (OpenVoice) with the IDENTICAL input/output JSON contract,
so run_blend.py works unchanged regardless of which engine produced
clone_result.json.

Runs inside the pre-existing spike venv
`~/voice-word-editor-feasibility/tts-spike-f5` on the Mac mini (point
F5_VENV_PYTHON at it). Model: SWivid/F5-TTS, F5TTS_v1_Base checkpoint,
pinned to a specific on-disk checkpoint file + the HF snapshot commit it
came from (see MODEL_CKPT_FILE/MODEL_HF_REVISION below) rather than
whatever `-m F5TTS_v1_Base` would resolve to by default -- this can't
silently drift to a different weights file on a future `pip install -U`.

*** LICENSE -- READ BEFORE USING OUTSIDE THIS TEAM ***
F5-TTS's code is MIT, but the pretrained checkpoint used here is
CC-BY-NC -- NONCOMMERCIAL USE ONLY. Confirmed directly from the installed
package's own shipped README (f5_tts-1.1.22.dist-info/METADATA, "## License"
section, sourced from the official SWivid/F5-TTS GitHub repo):
    "Our code is released under MIT License. The pre-trained models are
    licensed under the CC-BY-NC license due to the training data Emilia,
    which is an in-the-wild dataset."
This engine must only be used for internal, non-commercial evaluation by
this team -- never offered as part of a commercial product/service, and
never used to process a customer's content. The portal UI must label it
accordingly (see docs/portal-notes.md and model_registry.py).

Recipe: deliberately reproduces the "no_rubberband" recipe measured (in
an earlier internal A/B, see docs/portal-notes.md "Model selection" and
INVESTIGATION.md) to sound the closest to the original speaker --
NO pitch correction, NO tempo-stretch, NO rubberband step at all. Only:
trim silence, loudness-match (RMS dB, clamped), then pad up to the span's
slot duration if the synthesized phrase is shorter. If it's LONGER than
the slot, it is left long, never truncated or time-stretched --
run_blend.py's splice_multi() appends segments sequentially rather than
overwriting a fixed timeline slot, so overflow just makes that part of
the output video a little longer; it never truncates or overlaps
surrounding original speech. Verified by reading run_blend.py directly
and by this script's own test harness (see bottom of this docstring's
sibling test run) with a deliberately long replacement phrase.

Reference selection: reproduces the actual prior spike recipe (recovered
from this session's history, not reinvented) -- per span, try BOTH a
"span"-local reference (padded window immediately around the span) and a
"clean" multi-segment reference (several of this job's own cleanest,
most representative speech runs elsewhere in the recording, Resemblyzer-
curated -- built once per job, shared across spans, mirroring
run_clone.py's build_clean_reference()/clean_se_and_meta() pattern), and
keep whichever variant scores higher on Resemblyzer similarity against
the ORIGINAL span's own audio, measured on the no-rubberband-processed
output (not the raw synth) -- same selection principle as the prior
spike's scratch_build_spike2_outputs.py.

Deliberately duplicates run_clone.py's generic audio helpers
(_clip/_trim/_rms_db/_load_resemblyzer/_embed_safe/_cosine/
_build_speech_runs/build_clean_reference-equivalent) rather than
importing them: this script runs in a completely separate venv with its
own pinned torch/torchaudio versions (2.15.0/2.11.1 vs whatever OpenVoice's
tts-venv has), and this codebase's own established pattern for that kind
of cross-venv boundary is duplication, not a shared import (see
worker/run_worker.py's deliberately-duplicated preset-env mirror of
portal/app/model_registry.py).

Usage:
    python3 run_clone_f5.py --input-json <path> --output-dir <dir>

Input JSON (identical to run_clone.py):
    {
      "reference_audio": "<path>",
      "transcript_words": [{"word": "...", "start": 0.72, "end": 1.36}, ...],
      "spans": [{"id": 0, "new_text": "...", "span_start": 6.38, "span_end": 8.44}, ...]
    }

Output JSON (<output-dir>/clone_result.json, also printed to stdout):
    {
      "engine": "f5tts",
      "model_hf_revision": "<pinned HF snapshot commit>",
      "model_ckpt_file": "<exact checkpoint path used>",
      "device": "mps"|"cpu",
      "license": "code MIT; pretrained checkpoint CC-BY-NC (noncommercial) -- internal use only",
      "spans": [{"id", "new_text", "span_start", "span_end", "cloned_wav", "ref_variant_used",
                 "resemblyzer_span": <score>, "resemblyzer_clean": <score>, ...}, ...]
    }
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
SR = 24000  # native throughout -- the whole point of the "no boxy artifact" fix.

# --- pinned model identity (do not let this drift silently) ---------------
MODEL_HF_REVISION = os.environ.get(
    "F5_MODEL_HF_REVISION", "84e5a410d9cead4de2f847e7c9369a6440bdfaca"
)
_DEFAULT_CKPT = (
    Path.home() / ".cache" / "huggingface" / "hub" / "models--SWivid--F5-TTS"
    / "snapshots" / MODEL_HF_REVISION / "F5TTS_v1_Base" / "model_1250000.safetensors"
)
MODEL_CKPT_FILE = Path(os.environ.get("F5_MODEL_CKPT_FILE", str(_DEFAULT_CKPT)))
MODEL_NAME = os.environ.get("F5_MODEL_NAME", "F5TTS_v1_Base")

LICENSE_NOTE = (
    "code MIT; pretrained checkpoint CC-BY-NC (noncommercial) per the "
    "official SWivid/F5-TTS README -- internal, non-commercial use only"
)

REF_PAD_SEC = float(os.environ.get("VOICE_REF_PAD_SEC", "0.50"))
REF_MIN_SEC = 1.0
GAIN_CLAMP_DB = (-12.0, 12.0)
TRIM_TOP_DB = 35.0
REF_FILTER = "highpass=f=70,loudnorm=I=-16:TP=-1.0:LRA=11"

CLEAN_REF_GAP_SEC = float(os.environ.get("VOICE_CLEAN_REF_GAP_SEC", "0.25"))
CLEAN_REF_MIN_RUN_SEC = float(os.environ.get("VOICE_CLEAN_REF_MIN_RUN_SEC", "0.4"))
CLEAN_REF_MAX_RUN_SEC = float(os.environ.get("VOICE_CLEAN_REF_MAX_RUN_SEC", "3.0"))
CLEAN_REF_TARGET_SEC = float(os.environ.get("VOICE_CLEAN_REF_TARGET_SEC", "8.0"))
CLEAN_REF_MAX_SEGMENTS = int(os.environ.get("VOICE_CLEAN_REF_MAX_SEGMENTS", "12"))


class F5EngineError(RuntimeError):
    """Raised for any F5-TTS-engine-specific failure (venv/CLI/synthesis).
    Callers must surface this as an honest engine failure -- never
    silently fall back to a different engine."""


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def _ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def _clip(src: str, start: float, end: float | None, out: Path, af: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", src, "-ss", f"{max(0.0, start):.3f}"]
    if end is not None:
        args += ["-to", f"{max(0.0, end):.3f}"]
    if af:
        args += ["-af", af]
    args += ["-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(out)]
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
        print(f"Resemblyzer unavailable ({exc}); reference-variant selection will default to 'span'", file=sys.stderr)
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


def _build_speech_runs(words, gap_sec, min_dur, max_dur=CLEAN_REF_MAX_RUN_SEC):
    runs = []
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


def build_clean_reference(reference_audio: str, words: list[dict], out_dir: Path, encoder):
    """Returns (ref_wav_path_or_None, ref_text, meta). Builds ONE
    concatenated 24k reference wav (+ matching transcript text, in the
    same order) from this job's own cleanest/most representative speech
    runs, Resemblyzer-curated -- same selection principle as
    run_clone.py's build_clean_reference(), adapted to produce a single
    file+text pair since F5's CLI takes one reference file."""
    meta = {"runs_found": 0, "segments_selected": 0, "total_sec": 0.0}
    if not words or encoder is None:
        return None, "", meta

    runs = _build_speech_runs(words, CLEAN_REF_GAP_SEC, CLEAN_REF_MIN_RUN_SEC)
    meta["runs_found"] = len(runs)
    if not runs:
        return None, "", meta

    cand_dir = out_dir / "clean_ref_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)

    items = []
    for i, (s, e) in enumerate(runs):
        clip = cand_dir / f"run_{i}.wav"
        try:
            _clip(reference_audio, s, e, clip, af=REF_FILTER)
            dur = sf.info(str(clip)).duration
        except Exception:
            continue
        if dur < CLEAN_REF_MIN_RUN_SEC:
            continue
        emb = _embed_safe(str(clip), encoder)
        if emb is None:
            continue
        run_words = [w["word"] for w in words if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
        items.append({"path": clip, "duration": dur, "rms_db": _rms_db(str(clip)),
                      "embedding": emb, "start": s, "text": " ".join(run_words)})

    if not items:
        return None, "", meta

    embs = np.stack([it["embedding"] for it in items])
    anchor = embs.mean(axis=0)
    anchor_norm = np.linalg.norm(anchor)
    if anchor_norm < 1e-9:
        return None, "", meta
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
        return None, "", meta

    # Keep chronological order for a more natural concatenated reference.
    selected.sort(key=lambda it: it["start"])
    concat_list = out_dir / "clean_ref_concat_list.txt"
    concat_list.write_text("\n".join(f"file '{it['path']}'" for it in selected) + "\n")
    clean_wav = out_dir / "clean_ref.wav"
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(clean_wav)])
    clean_text = " ".join(it["text"] for it in selected if it["text"])
    return clean_wav, clean_text, meta


def _assert_checkpoint_present() -> None:
    if not MODEL_CKPT_FILE.exists():
        raise F5EngineError(
            f"F5-TTS checkpoint not found at pinned path {MODEL_CKPT_FILE} "
            f"(HF revision {MODEL_HF_REVISION}) -- engine unavailable, not falling back"
        )


def synthesize(text: str, ref_wav: Path, ref_text: str, out_path: Path, device: str) -> None:
    """Invoke f5-tts_infer-cli as a subprocess, pinned to MODEL_CKPT_FILE.
    Raises F5EngineError on any failure -- never falls back silently."""
    f5_bin = str(Path(sys.executable).parent / "f5-tts_infer-cli")
    if not Path(f5_bin).exists():
        raise F5EngineError(f"f5-tts_infer-cli not found at {f5_bin} -- F5 venv unavailable")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        f5_bin,
        "-m", MODEL_NAME,
        "-p", str(MODEL_CKPT_FILE),
        "-r", str(ref_wav),
        "-s", ref_text,
        "-t", text,
        "-o", str(out_path.parent),
        "-w", out_path.name,
        "--device", device,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_path.exists():
        raise F5EngineError(
            f"f5-tts_infer-cli failed (rc={proc.returncode}) for text={text!r}: "
            f"{proc.stderr[-2000:] or proc.stdout[-2000:]}"
        )


def no_rubberband_finish(raw_wav: Path, orig_clip: Path, slot: float, out_path: Path) -> dict:
    """Trim silence, loudness-match (RMS dB, clamped), pad up to `slot` if
    shorter. NEVER truncates/time-stretches if longer -- left long, which
    run_blend.py's sequential splice handles safely (see module docstring)."""
    trimmed = out_path.parent / f"{out_path.stem}_trim.wav"
    natural_duration = _trim(raw_wav, trimmed)
    gain_db = _rms_db(str(orig_clip)) - _rms_db(str(trimmed))
    gain_db = float(min(GAIN_CLAMP_DB[1], max(GAIN_CLAMP_DB[0], gain_db)))
    # apad only pads a shorter input up to whole_dur; it is a no-op (does
    # NOT truncate) if the input is already longer than whole_dur.
    _ffmpeg(["-i", str(trimmed), "-af", f"apad=whole_dur={max(slot, 0.01):.3f},volume={gain_db:.2f}dB",
             "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(out_path)])
    final_duration = sf.info(str(out_path)).duration
    return {
        "natural_duration": natural_duration,
        "final_duration": final_duration,
        "gain_db": gain_db,
        "padded": final_duration > natural_duration + 1e-3,
        "overflowed_slot": natural_duration > slot + 1e-3,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    _assert_checkpoint_present()

    import torch
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    spec = json.loads(Path(args.input_json).read_text())
    reference_audio = spec["reference_audio"]
    spans = spec["spans"]
    transcript_words = spec.get("transcript_words") or []

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"F5-TTS engine: device={device} ckpt={MODEL_CKPT_FILE} revision={MODEL_HF_REVISION}", file=sys.stderr)

    encoder = _load_resemblyzer()

    clean_cache: dict = {}

    def clean_ref_and_text():
        if "wav" not in clean_cache:
            wav, text, meta = build_clean_reference(reference_audio, transcript_words, out_dir, encoder)
            clean_cache["wav"], clean_cache["text"], clean_cache["meta"] = wav, text, meta
        return clean_cache["wav"], clean_cache["text"], clean_cache["meta"]

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
        _clip(reference_audio, start, end, orig_clip)
        orig_embedding = _embed_safe(str(orig_clip), encoder)

        span_ref = out_dir / f"span_{span_id}_ref.wav"
        _clip(reference_audio, max(0.0, start - REF_PAD_SEC), end + REF_PAD_SEC, span_ref, af=REF_FILTER)
        span_ref_words = [
            w["word"] for w in transcript_words
            if w["start"] >= start - REF_PAD_SEC - 0.05 and w["end"] <= end + REF_PAD_SEC + 0.05
        ]
        span_ref_text = " ".join(span_ref_words)
        span_ref_dur = sf.info(str(span_ref)).duration

        clean_wav, clean_text, clean_meta = clean_ref_and_text()

        variants = []
        if span_ref_dur >= REF_MIN_SEC and span_ref_text:
            variants.append(("span", span_ref, span_ref_text))
        if clean_wav is not None and clean_text:
            variants.append(("clean", clean_wav, clean_text))
        if not variants:
            # Fall back to the span reference regardless of the length/text
            # heuristics above rather than having nothing to synthesize
            # against -- still the F5 engine, still no silent engine swap.
            variants.append(("span", span_ref, span_ref_text or new_text))

        scored = {}
        errors = {}
        for variant_name, ref_wav, ref_text in variants:
            raw_wav = out_dir / f"span_{span_id}_{variant_name}_raw.wav"
            try:
                synthesize(new_text, ref_wav, ref_text, raw_wav, device)
            except F5EngineError as exc:
                errors[variant_name] = str(exc)
                continue
            final_wav = out_dir / f"span_{span_id}_{variant_name}_final.wav"
            finish_meta = no_rubberband_finish(raw_wav, orig_clip, slot, final_wav)
            score = _cosine(_embed_safe(str(final_wav), encoder), orig_embedding)
            scored[variant_name] = {"wav": final_wav, "score": score, "meta": finish_meta}

        if not scored:
            raise F5EngineError(
                f"F5-TTS synthesis failed for every reference variant on span {span_id}: {errors}"
            )

        best_variant = max(scored, key=lambda k: (scored[k]["score"] if scored[k]["score"] is not None else -1.0))
        best = scored[best_variant]
        cloned_wav = out_dir / f"span_{span_id}_cloned.wav"
        best["wav"].replace(cloned_wav)

        results.append({
            "id": span_id,
            "new_text": new_text,
            "span_start": start,
            "span_end": end,
            "cloned_wav": str(cloned_wav),
            "ref_variant_used": best_variant,
            "resemblyzer_span": scored.get("span", {}).get("score"),
            "resemblyzer_clean": scored.get("clean", {}).get("score"),
            "natural_duration": best["meta"]["natural_duration"],
            "final_duration": best["meta"]["final_duration"],
            "overflowed_slot": best["meta"]["overflowed_slot"],
            "slot_sec": slot,
            "variant_errors": errors or None,
        })
        print(
            f"span {span_id}: variant={best_variant} score={best['score']} "
            f"natural={best['meta']['natural_duration']:.2f}s slot={slot:.2f}s "
            f"overflowed={best['meta']['overflowed_slot']}",
            file=sys.stderr,
        )

    out = {
        "engine": "f5tts",
        "model_hf_revision": MODEL_HF_REVISION,
        "model_ckpt_file": str(MODEL_CKPT_FILE),
        "device": device,
        "license": LICENSE_NOTE,
        "peak_rss_mb": peak_rss_mb(),
        "spans": results,
    }
    (out_dir / "clone_result.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"engine": "f5tts", "peak_rss_mb": out["peak_rss_mb"], "span_count": len(results)}))


if __name__ == "__main__":
    main()
