#!/usr/bin/env python3
"""Phase 2 worker pipeline — same-speaker cloning stage. Runs inside the
`tts-venv` (OpenVoice V2 + MeloTTS) created during the Phase 1 feasibility
pass; see worker/README.md for how to set that venv up (or just point
TTS_VENV_PYTHON at the one already built under
~/voice-word-editor-feasibility/tts-venv on the Mac mini).

This generalizes worker/feasibility/run_clone.py (which had two edits
hardcoded for the Phase 1 proof) into a real CLI driven by whatever edit
spans worker/pipeline/diffing.py computed for a given job — any number of
spans, any words, not a fixed demo.

Voice-match improvements (all tunable via env vars, see the knobs below):

  1. Local reference.  Tone-colour conversion used to extract the target
     speaker embedding from the WHOLE job audio.  If the clip has music,
     other voices, or long silences, that embedding is polluted and the
     cloned voice drifts.  We extract a short reference from the span's
     own neighbourhood (same speaker, same mic, same room) as one
     candidate target, and fall back to a denoised whole-clip reference
     only if the local clip is too short / VAD finds no speech.
  2. Clean multi-segment reference (biggest lever for timbre).  A ~2s
     span-local clip gives a noisy speaker embedding. Using this job's own
     transcript word timings, we stitch together several of the speaker's
     cleanest, most representative speech runs from ELSEWHERE in the same
     recording (same mic/room/speaker — still fully derived from this
     job's audio, nothing speaker-specific is hardcoded) and extract a
     second candidate embedding from that. Segment selection is itself
     guided by Resemblyzer: score every candidate run by its cosine
     similarity to the run-average embedding (favors clips that sound like
     "the speaker in general" over outliers) and by loudness (drops
     near-silent/noisy runs), then greedily keep the best ~6-10s.
  3. Resemblyzer-guided tau/reference selection. For each span we try
     every available reference (span-local, clean multi-segment, denoised
     whole-clip) at several OpenVoice `tau` values (how hard it pushes
     toward the target timbre) and score each resulting clone's
     Resemblyzer speaker-embedding similarity against the ORIGINAL span's
     own audio (the one piece of unimpeachable ground truth for "what this
     speaker actually sounds like saying something in this recording").
     The highest-scoring combination wins. If Resemblyzer isn't available
     for some reason, we fall back to the single fixed VOICE_TAU/span-first
     behavior rather than failing the job.
  4. Optional residual timbre correction. After the winning clone is
     chosen, we may nudge its long-term average spectrum toward the
     original span's spectrum (a crude formant/timbre correction) via
     ffmpeg's firequalizer. This is accepted ONLY if it measurably improves
     the Resemblyzer similarity score; otherwise it's discarded and the
     uncorrected clone is kept. Off by default caution: always auto-scored,
     never applied blindly.
  5. Pitch match. Measure the speaker's median F0 in the original span and
     the cloned phrase's F0, then pitch-shift (formant-preserving) to close
     the gap. UNCHANGED from the previous pass.
  6. Loudness match. Gain the cloned phrase to the original span's RMS so
     it doesn't jump out (clamped to +/-12 dB). UNCHANGED.
  7. Natural pace. MeloTTS `speed=` makes fast speech sound robotic. We
     synthesize at natural speed and time-stretch with ffmpeg's rubberband
     instead, allowing a small overflow (VOICE_MAX_OVERFLOW_SEC, default
     0.35s). UNCHANGED.

Nothing here is hardcoded to a particular speaker/F0/loudness/timbre —
every adaptation parameter (which reference, which tau, pitch ratio, gain,
tempo, spectral correction) is derived at runtime from THIS job's own
audio and transcript.

Usage:
    python3 run_clone.py --input-json <path> --output-dir <dir> \
        --checkpoints-dir <dir>

Input JSON:
    {
      "reference_audio": "<path to the original audio extracted from the
                            job's video>",
      "transcript_words": [{"word": "...", "start": 0.72, "end": 1.36}, ...]
                           (optional — enables the clean multi-segment
                           reference; if absent, that variant is skipped),
      "spans": [
        {"id": 0, "new_text": "...", "span_start": 6.38, "span_end": 8.44},
        ...
      ]
    }

Output JSON (written to <output-dir>/clone_result.json and also printed to
stdout): per-span natural/converted/final durations, tempo/pitch/gain that
were applied, F0 before/after, which reference+tau won and its Resemblyzer
score vs. the previous single-path baseline, whether spectral correction
was kept, timings, peak RSS.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from glob import glob
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no")


# --- tuning knobs (all env-overridable) ----------------------------------
TAU = float(os.environ.get("VOICE_TAU", "0.60"))
PITCH_MATCH = _env_bool("VOICE_PITCH_MATCH", "1")
LOUDNESS_MATCH = _env_bool("VOICE_LOUDNESS_MATCH", "1")
MAX_OVERFLOW_SEC = float(os.environ.get("VOICE_MAX_OVERFLOW_SEC", "0.35"))
REF_PAD_SEC = float(os.environ.get("VOICE_REF_PAD_SEC", "0.50"))

REF_MIN_SEC = 1.0
TEMPO_MIN, TEMPO_MAX = 0.7, 1.7
PITCH_DEADBAND = 0.05  # ignore corrections smaller than 5%
# Wide enough to track a genuinely different speaker (male/female, high/low)
# while still bounded so a bad F0 estimate can't produce a chipmunk/giant.
PITCH_CLAMP = (0.82, 1.18)
GAIN_CLAMP_DB = (-12.0, 12.0)
F0_MIN, F0_MAX = 65.0, 400.0
TRIM_TOP_DB = 35.0
REF_FILTER = "highpass=f=70,loudnorm=I=-16:TP=-1.0:LRA=11"

# --- timbre-match knobs (new) ---------------------------------------------
RESEMBLYZER_MATCH = _env_bool("VOICE_RESEMBLYZER_MATCH", "1")
TAU_SWEEP = [
    float(x) for x in os.environ.get("VOICE_TAU_SWEEP", "0.5,0.6,0.7,0.8,0.9").split(",") if x.strip()
]
if TAU not in TAU_SWEEP:
    TAU_SWEEP.append(TAU)  # always keep the single-path baseline comparable

CLEAN_REF_GAP_SEC = float(os.environ.get("VOICE_CLEAN_REF_GAP_SEC", "0.25"))
CLEAN_REF_MIN_RUN_SEC = float(os.environ.get("VOICE_CLEAN_REF_MIN_RUN_SEC", "0.4"))
# Cap on a single contiguous run before it's cut into a fresh segment, even
# if there's no pause there. Without this, fast/continuous narration with
# few pauses > CLEAN_REF_GAP_SEC collapses into one or two giant runs --
# defeating the point of sampling several genuinely distinct segments for
# Resemblyzer to pick from.
CLEAN_REF_MAX_RUN_SEC = float(os.environ.get("VOICE_CLEAN_REF_MAX_RUN_SEC", "3.0"))
CLEAN_REF_TARGET_SEC = float(os.environ.get("VOICE_CLEAN_REF_TARGET_SEC", "8.0"))
CLEAN_REF_MAX_SEGMENTS = int(os.environ.get("VOICE_CLEAN_REF_MAX_SEGMENTS", "12"))

SPECTRAL_MATCH = _env_bool("VOICE_SPECTRAL_MATCH", "1")
SPECTRAL_BANDS = int(os.environ.get("VOICE_SPECTRAL_BANDS", "12"))
SPECTRAL_MAX_GAIN_DB = float(os.environ.get("VOICE_SPECTRAL_MAX_GAIN_DB", "6.0"))
SPECTRAL_MIN_IMPROVEMENT = float(os.environ.get("VOICE_SPECTRAL_MIN_IMPROVEMENT", "0.01"))

# Sanity gate on any tone-converted/spectral candidate before it's allowed
# to be scored or chosen: guards against a silently-corrupted conversion
# (e.g. a near-silent or truncated render) winning a sweep just because
# nothing else was compared against it.
CANDIDATE_MIN_DURATION_FRAC = 0.5
CANDIDATE_MIN_RMS_DB = -55.0


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def _ffmpeg(args: list[str]) -> None:
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def _clip(src: str, start: float, end: float | None, out: Path, sr: int, af: str | None = None) -> None:
    """Extract [start, end) from `src` as mono PCM16 wav at `sr`, with an
    optional audio filter chain. `end=None` means "to the end of the file"."""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", src, "-ss", f"{max(0.0, start):.3f}"]
    if end is not None:
        args += ["-to", f"{max(0.0, end):.3f}"]
    if af:
        args += ["-af", af]
    args += ["-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(out)]
    _ffmpeg(args)


def _f0_median(path: str) -> float | None:
    """Median fundamental frequency over voiced frames (Hz), or None."""
    y, sr = librosa.load(path, sr=16000, mono=True)
    if y.size < int(0.2 * sr):
        return None
    try:
        f0, _voiced, _ = librosa.pyin(y, fmin=F0_MIN, fmax=F0_MAX, sr=sr, frame_length=1024)
    except Exception:
        return None
    f0 = f0[np.isfinite(f0)]
    return float(np.median(f0)) if f0.size else None


def _rms_db(path: str) -> float:
    y, _ = librosa.load(path, sr=None, mono=True)
    if y.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(y))))
    return 20.0 * np.log10(r) if r > 1e-9 else -120.0


def _trim(src: Path, dst: Path, top_db: float = TRIM_TOP_DB) -> float:
    """Trim leading/trailing silence from a TTS wav and write `dst`; returns
    the trimmed duration in seconds."""
    y, sr = librosa.load(str(src), sr=None, mono=True)
    if y.size == 0:
        sf.write(str(dst), y, sr)
        return 0.0
    yt, _ = librosa.effects.trim(y, top_db=top_db)
    if yt.size < int(0.05 * sr):
        yt = y
    sf.write(str(dst), yt, sr)
    return float(yt.size / float(sr))


def _safe_get_se(ref: Path, converter, target_dir: Path, split_seconds: float = 10.0):
    """Extract a target-speaker embedding from `ref`.

    OpenVoice's own `se_extractor.get_se(vad=True)` never threads a
    `split_seconds` through to `split_audio_vad` -- it's hardcoded to 10.0
    inside se_extractor.py itself. `split_audio_vad` then does
    `num_splits = round(active_speech_dur / split_seconds)` and asserts
    `num_splits > 0`, so any reference with less than ~5s of VAD-active
    speech (our ~2-3s per-span local references, always) trips that
    assert and get_se() always raises -- silently forcing every span back
    onto the single whole-video embedding regardless of how short/long
    the local reference actually is. Call split_audio_vad + extract_se
    directly instead so a short reference can use a short split_seconds.
    """
    from openvoice import se_extractor

    try:
        wavs_folder = se_extractor.split_audio_vad(
            str(ref), audio_name="ref", target_dir=str(target_dir), split_seconds=split_seconds
        )
        audio_segs = glob(f"{wavs_folder}/*.wav")
        if not audio_segs:
            return None
        return converter.extract_se(audio_segs, se_save_path=str(Path(target_dir) / "ref" / "se.pth"))
    except Exception as exc:  # no speech found / too short / etc.
        print(f"  se extract failed for {ref.name}: {exc}", file=sys.stderr)
        return None


def _extract_se_from_clips(clip_paths: list[Path], converter, se_save_path: Path):
    """Extract+average an embedding directly from a curated list of clean
    speech clips (bypassing OpenVoice's own VAD splitting entirely, since
    the caller already picked the clips)."""
    try:
        se_save_path.parent.mkdir(parents=True, exist_ok=True)
        return converter.extract_se([str(p) for p in clip_paths], se_save_path=str(se_save_path))
    except Exception as exc:
        print(f"  extract_se failed on {len(clip_paths)} curated clip(s): {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------- resemblyzer
def _load_resemblyzer():
    """Lazily load a Resemblyzer VoiceEncoder. Returns None (with a clear
    stderr note) if it isn't importable/usable -- callers must degrade to
    the old fixed-tau/span-first behavior rather than fail the job."""
    try:
        from resemblyzer import VoiceEncoder

        return VoiceEncoder(device="cpu", verbose=False)
    except Exception as exc:
        print(f"Resemblyzer unavailable ({exc}); falling back to fixed-tau/span-first selection", file=sys.stderr)
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


# ------------------------------------------------------- clean multi-ref
def _build_speech_runs(
    words: list[dict], gap_sec: float, min_dur: float, max_dur: float = CLEAN_REF_MAX_RUN_SEC
) -> list[tuple[float, float]]:
    """Group consecutive transcript words into contiguous speech runs
    (merge across gaps <= gap_sec), dropping runs shorter than min_dur.
    Also cuts a run once it would exceed max_dur even with no pause there,
    so continuous/fast narration still yields several distinct segments
    instead of one or two giant blobs."""
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


def build_clean_reference(
    reference_audio: str,
    words: list[dict],
    out_dir: Path,
    encoder,
) -> tuple[list[Path], dict]:
    """Build a clean, longer target-speaker reference from multiple of this
    job's own speech runs (Resemblyzer-scored for how representative +
    how loud/clean each run is), instead of a single noisy ~2s clip.

    Returns (selected_clip_paths, meta) -- selected_clip_paths is [] if no
    usable transcript/encoder/segments were available (caller must then
    skip this variant, not fail)."""
    meta: dict = {"runs_found": 0, "segments_selected": 0, "total_sec": 0.0}
    if not words or encoder is None:
        return [], meta

    runs = _build_speech_runs(words, CLEAN_REF_GAP_SEC, CLEAN_REF_MIN_RUN_SEC)
    meta["runs_found"] = len(runs)
    if not runs:
        return [], meta

    cand_dir = out_dir / "clean_ref_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)

    items = []
    for i, (s, e) in enumerate(runs):
        clip = cand_dir / f"run_{i}.wav"
        try:
            _clip(reference_audio, s, e, clip, 16000, af=REF_FILTER)
        except Exception:
            continue
        try:
            dur = sf.info(str(clip)).duration
        except Exception:
            continue
        if dur < CLEAN_REF_MIN_RUN_SEC:
            continue
        emb = _embed_safe(str(clip), encoder)
        if emb is None:
            continue
        items.append({"path": clip, "duration": dur, "rms_db": _rms_db(str(clip)), "embedding": emb})

    if not items:
        return [], meta

    embs = np.stack([it["embedding"] for it in items])
    anchor = embs.mean(axis=0)
    anchor_norm = np.linalg.norm(anchor)
    if anchor_norm < 1e-9:
        return [], meta
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
        return [], meta
    return [it["path"] for it in selected], meta


# ----------------------------------------------------- spectral correction
def _avg_spectrum_db(y: np.ndarray, n_fft: int = 512) -> tuple[np.ndarray, np.ndarray]:
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=n_fft // 4))
    avg = S.mean(axis=1) + 1e-8
    freqs = librosa.fft_frequencies(sr=16000, n_fft=n_fft)
    return freqs, 20.0 * np.log10(avg)


def _spectral_gain_entries(orig_path: str, clone_path: str) -> str | None:
    """Compare long-term average spectra of the original span and the
    candidate clone; return an ffmpeg firequalizer gain_entry string that
    nudges the clone's spectral envelope toward the original's, clamped
    per-band to avoid extreme/unnatural coloration. None if either clip is
    unusable."""
    try:
        y_o, _ = librosa.load(orig_path, sr=16000, mono=True)
        y_c, _ = librosa.load(clone_path, sr=16000, mono=True)
    except Exception:
        return None
    if y_o.size < 512 or y_c.size < 512:
        return None

    freqs, db_o = _avg_spectrum_db(y_o)
    _, db_c = _avg_spectrum_db(y_c)

    fmin, fmax = 80.0, min(7500.0, 16000 / 2 - 500)
    bands = np.geomspace(fmin, fmax, SPECTRAL_BANDS)
    entries = []
    for f in bands:
        idx = int(np.argmin(np.abs(freqs - f)))
        lo, hi = max(0, idx - 2), min(len(freqs), idx + 3)
        diff = float(np.mean(db_o[lo:hi] - db_c[lo:hi]))
        diff = max(-SPECTRAL_MAX_GAIN_DB, min(SPECTRAL_MAX_GAIN_DB, diff))
        entries.append(f"entry({f:.0f},{diff:.2f})")
    return ";".join(entries)


def _apply_firequalizer(src: Path, dst: Path, gain_entry: str) -> None:
    _ffmpeg(["-i", str(src), "-af", f"firequalizer=gain_entry='{gain_entry}'", "-c:a", "pcm_s16le", str(dst)])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--checkpoints-dir", required=True)
    args = parser.parse_args()

    spec = json.loads(Path(args.input_json).read_text())
    reference_audio = spec["reference_audio"]
    spans = spec["spans"]
    transcript_words = spec.get("transcript_words") or []

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt = Path(args.checkpoints_dir)

    device = "mps" if torch.backends.mps.is_available() else "cpu"

    encoder = _load_resemblyzer() if RESEMBLYZER_MATCH else None
    print(
        f"Using device: {device}; tau_sweep={TAU_SWEEP} resemblyzer={'on' if encoder is not None else 'off'} "
        f"pitch_match={PITCH_MATCH} loudness_match={LOUDNESS_MATCH} max_overflow={MAX_OVERFLOW_SEC}s "
        f"spectral_match={'on' if SPECTRAL_MATCH else 'off'}",
        file=sys.stderr,
    )

    timings = {}

    t0 = time.time()
    from openvoice.api import ToneColorConverter

    ckpt_converter = str(ckpt / "converter")
    tone_color_converter = ToneColorConverter(f"{ckpt_converter}/config.json", device=device)
    tone_color_converter.load_ckpt(f"{ckpt_converter}/checkpoint.pth")
    timings["load_tone_converter_seconds"] = time.time() - t0

    t0 = time.time()
    from melo.api import TTS

    melo_model = TTS(language="EN_NEWEST", device=device)
    speaker_ids = melo_model.hps.data.spk2id
    speaker_key = list(speaker_ids.keys())[0]
    speaker_id = speaker_ids[speaker_key]
    source_se = torch.load(
        str(ckpt / "base_speakers" / "ses" / f"{speaker_key.lower().replace('_', '-')}.pth"),
        map_location=device,
    )
    timings["load_melo_seconds"] = time.time() - t0
    timings["melo_speaker_key"] = speaker_key

    # Denoised whole-clip reference, computed lazily and only if needed.
    global_cache: dict[str, object] = {}

    def global_se():
        if "se" not in global_cache:
            g = out_dir / "global_ref.wav"
            _clip(reference_audio, 0.0, None, g, 16000, af=REF_FILTER)
            global_cache["se"] = _safe_get_se(g, tone_color_converter, out_dir / "se_global")
        return global_cache["se"]

    # Clean multi-segment reference (this job's own audio, Resemblyzer-
    # curated), computed lazily once and shared across all spans.
    clean_cache: dict[str, object] = {}

    def clean_se_and_meta():
        if "se" not in clean_cache:
            clips, meta = build_clean_reference(reference_audio, transcript_words, out_dir, encoder)
            se = None
            if clips:
                se = _extract_se_from_clips(clips, tone_color_converter, out_dir / "se_clean" / "se.pth")
            clean_cache["se"] = se
            clean_cache["meta"] = meta
        return clean_cache["se"], clean_cache["meta"]

    results = []
    for span in spans:
        span_id = span["id"]
        new_text = (span.get("new_text") or "").strip()
        start = float(span["span_start"])
        end = float(span["span_end"])
        slot = max(0.0, end - start)

        if not new_text:
            # Pure deletion: nothing to synthesize. The blend stage treats a
            # span with cloned_wav=None as "just cut this range out."
            results.append(
                {"id": span_id, "new_text": "", "cloned_wav": None, "skipped": "empty_new_text",
                 "span_start": start, "span_end": end}
            )
            continue

        # 1) Candidate target-speaker references: span-local, clean
        # multi-segment (this job's own audio), denoised whole-clip.
        t0 = time.time()
        ref_wav = out_dir / f"span_{span_id}_ref.wav"
        _clip(reference_audio, max(0.0, start - REF_PAD_SEC), end + REF_PAD_SEC, ref_wav, 16000, af=REF_FILTER)
        ref_dur = sf.info(str(ref_wav)).duration
        span_se = None
        if ref_dur >= REF_MIN_SEC:
            span_se = _safe_get_se(
                ref_wav, tone_color_converter, out_dir / f"se_span_{span_id}", split_seconds=1.5
            )
        clean_se, clean_meta = clean_se_and_meta()
        variants = [("span", span_se), ("clean", clean_se), ("global", global_se())]
        variants = [(name, se) for name, se in variants if se is not None]
        reference_seconds = time.time() - t0
        if not variants:
            raise RuntimeError(f"span {span_id}: no usable target-speaker reference (span/clean/global all failed)")

        # 2) Natural-speed base TTS, trimmed of edge silence (one synth per
        # span -- OpenVoice's convert() preserves duration, so every
        # reference/tau combo below reuses this same base clip).
        base_tts_wav = out_dir / f"span_{span_id}_base_tts.wav"
        base_trim_wav = out_dir / f"span_{span_id}_base_trim.wav"
        t0 = time.time()
        melo_model.tts_to_file(new_text, speaker_id, str(base_tts_wav), speed=1.0)
        natural_duration = _trim(base_tts_wav, base_trim_wav)
        base_tts_seconds = time.time() - t0

        orig_clip = out_dir / f"span_{span_id}_orig.wav"
        _clip(reference_audio, start, end, orig_clip, 16000)
        orig_embedding = _embed_safe(str(orig_clip), encoder)

        # 3) Resemblyzer-guided reference x tau sweep (skipped entirely if
        # Resemblyzer isn't usable -- no point paying for extra conversions
        # we can't score).
        t0 = time.time()
        sweep_results = []
        best = None  # {"variant","tau","path","score"}
        resemblyzer_usable = encoder is not None and orig_embedding is not None
        if resemblyzer_usable:
            for variant_name, variant_se in variants:
                for tau in TAU_SWEEP:
                    cand = out_dir / f"span_{span_id}_cand_{variant_name}_{tau:.2f}.wav"
                    try:
                        tone_color_converter.convert(
                            audio_src_path=str(base_trim_wav),
                            src_se=source_se,
                            tgt_se=variant_se,
                            output_path=str(cand),
                            tau=tau,
                            message="@voice-word-editor-worker",
                        )
                    except Exception as exc:
                        print(f"  span {span_id}: convert failed variant={variant_name} tau={tau}: {exc}", file=sys.stderr)
                        continue
                    try:
                        cand_dur = sf.info(str(cand)).duration
                    except Exception:
                        continue
                    sane = bool(
                        cand_dur >= CANDIDATE_MIN_DURATION_FRAC * natural_duration
                        and _rms_db(str(cand)) >= CANDIDATE_MIN_RMS_DB
                    )
                    score = _cosine(_embed_safe(str(cand), encoder), orig_embedding) if sane else None
                    entry = {"variant": variant_name, "tau": tau, "path": cand, "score": score, "sane": sane}
                    sweep_results.append(entry)
                    if sane and score is not None and (best is None or score > best["score"]):
                        best = entry
        tau_sweep_seconds = time.time() - t0

        # Baseline = the pre-this-task behavior (span reference at the
        # fixed default tau), looked up from the sweep if we ran one.
        baseline = next(
            (e for e in sweep_results if e["variant"] == "span" and abs(e["tau"] - TAU) < 1e-6 and e["sane"]),
            None,
        )

        if best is None:
            # Resemblyzer unavailable/unusable or every candidate failed
            # the sanity gate -- fall back to the pre-this-task behavior:
            # first available reference (span > clean > global) at the
            # fixed default tau.
            fallback_variant, fallback_se = variants[0]
            conv_wav = out_dir / f"span_{span_id}_conv.wav"
            tone_color_converter.convert(
                audio_src_path=str(base_trim_wav), src_se=source_se, tgt_se=fallback_se,
                output_path=str(conv_wav), tau=TAU, message="@voice-word-editor-worker",
            )
            ref_used, tau_used = fallback_variant, TAU
            resemblyzer_before = resemblyzer_after = None
        else:
            conv_wav = out_dir / f"span_{span_id}_conv.wav"
            best["path"].replace(conv_wav)
            ref_used, tau_used = best["variant"], best["tau"]
            resemblyzer_after = best["score"]
            resemblyzer_before = baseline["score"] if baseline and baseline["sane"] else None

        # Clean up the other sweep candidates (keep disk usage bounded).
        for entry in sweep_results:
            if entry["path"].exists() and entry["path"] != conv_wav:
                entry["path"].unlink(missing_ok=True)

        converted_duration = sf.info(str(conv_wav)).duration

        # 4) Pitch + tempo via rubberband (formant-preserving). UNCHANGED.
        f0_orig = _f0_median(str(orig_clip))
        f0_conv = _f0_median(str(conv_wav))
        pitch_ratio = 1.0
        if PITCH_MATCH and f0_orig and f0_conv and f0_conv > 0:
            ratio = f0_orig / f0_conv
            if abs(ratio - 1.0) > PITCH_DEADBAND:
                pitch_ratio = float(min(PITCH_CLAMP[1], max(PITCH_CLAMP[0], ratio)))

        if converted_duration <= slot:
            tempo = 1.0  # short enough; pad up to the slot to keep sync
        else:
            tempo = converted_duration / (slot + MAX_OVERFLOW_SEC)
            tempo = float(min(TEMPO_MAX, max(TEMPO_MIN, tempo)))

        rb_wav = out_dir / f"span_{span_id}_rb.wav"
        rb_filter = f"rubberband=tempo={tempo:.4f}:pitch={pitch_ratio:.4f}:formant=preserved"
        _ffmpeg(["-i", str(conv_wav), "-af", rb_filter, "-c:a", "pcm_s16le", str(rb_wav)])

        # 4.5) Optional residual timbre (spectral envelope) correction --
        # only kept if it measurably improves the Resemblyzer score.
        spectral_applied = False
        spectral_before = spectral_after = None
        final_source = rb_wav
        if SPECTRAL_MATCH and encoder is not None and orig_embedding is not None:
            gain_entry = _spectral_gain_entries(str(orig_clip), str(rb_wav))
            if gain_entry:
                spectral_wav = out_dir / f"span_{span_id}_spectral.wav"
                try:
                    _apply_firequalizer(rb_wav, spectral_wav, gain_entry)
                    score_before = _cosine(_embed_safe(str(rb_wav), encoder), orig_embedding)
                    score_after = _cosine(_embed_safe(str(spectral_wav), encoder), orig_embedding)
                    spectral_before, spectral_after = score_before, score_after
                    if score_before is not None and score_after is not None and score_after - score_before > SPECTRAL_MIN_IMPROVEMENT:
                        final_source = spectral_wav
                        spectral_applied = True
                    else:
                        spectral_wav.unlink(missing_ok=True)
                except Exception as exc:
                    print(f"  span {span_id}: spectral correction skipped: {exc}", file=sys.stderr)

        # 5) Loudness match + pad to the original slot (overflow allowed). UNCHANGED.
        cloned_wav = out_dir / f"span_{span_id}_cloned.wav"
        gain_db = 0.0
        if LOUDNESS_MATCH:
            gain_db = _rms_db(str(orig_clip)) - _rms_db(str(final_source))
            gain_db = float(min(GAIN_CLAMP_DB[1], max(GAIN_CLAMP_DB[0], gain_db)))
        final_filter = f"apad=whole_dur={max(slot, 0.01):.3f},volume={gain_db:.2f}dB"
        _ffmpeg(["-i", str(final_source), "-af", final_filter, "-c:a", "pcm_s16le", str(cloned_wav)])
        final_duration = sf.info(str(cloned_wav)).duration

        results.append(
            {
                "id": span_id,
                "new_text": new_text,
                "span_start": start,
                "span_end": end,
                "slot_duration": slot,
                "natural_duration": natural_duration,
                "converted_duration": converted_duration,
                "final_duration": final_duration,
                "overflow": final_duration - slot,
                "tempo": tempo,
                "pitch_ratio": pitch_ratio,
                "gain_db": gain_db,
                "f0_original": f0_orig,
                "f0_converted": f0_conv,
                "reference_used": ref_used,
                "tau_used": tau_used,
                "resemblyzer_before": resemblyzer_before,
                "resemblyzer_after": resemblyzer_after,
                "resemblyzer_sweep": [
                    {"variant": e["variant"], "tau": e["tau"], "score": e["score"], "sane": e["sane"]}
                    for e in sweep_results
                ],
                "clean_reference_meta": clean_meta,
                "spectral_match_applied": spectral_applied,
                "spectral_resemblyzer_before": spectral_before,
                "spectral_resemblyzer_after": spectral_after,
                "ref_wav": str(ref_wav),
                "cloned_wav": str(cloned_wav),
                "base_tts_seconds": base_tts_seconds,
                "tau_sweep_seconds": tau_sweep_seconds,
                "reference_seconds": reference_seconds,
            }
        )
        f0_note = f"f0 {f0_orig:.0f}->{f0_conv:.0f}Hz" if (f0_orig and f0_conv) else "f0 n/a"
        rsz_note = (
            f"resemblyzer {resemblyzer_before:.3f}->{resemblyzer_after:.3f}"
            if (resemblyzer_before is not None and resemblyzer_after is not None)
            else f"resemblyzer n/a (after={resemblyzer_after})"
        )
        print(
            f"span {span_id}: natural {natural_duration:.2f}s -> {final_duration:.2f}s "
            f"(slot {slot:.2f}s, tempo {tempo:.2f}, pitch {pitch_ratio:.2f}, {f0_note}, "
            f"ref={ref_used} tau={tau_used:.2f}, {rsz_note}, spectral={'kept' if spectral_applied else 'skipped'}, "
            f"gain {gain_db:+.1f}dB)",
            file=sys.stderr,
        )

    out = {
        "engine": "openvoice",
        "device": device,
        "tau_sweep": TAU_SWEEP,
        "resemblyzer_enabled": encoder is not None,
        "timings": timings,
        "spans": results,
        "final_peak_rss_mb": peak_rss_mb(),
    }
    (out_dir / "clone_result.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({"peak_rss_mb": out["final_peak_rss_mb"], "span_count": len(results)}))


if __name__ == "__main__":
    main()
