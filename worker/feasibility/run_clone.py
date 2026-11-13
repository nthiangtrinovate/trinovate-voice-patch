"""
Phase 1 feasibility proof — stage 2: same-speaker voice cloning of replacement
phrases using OpenVoice V2 (MeloTTS base speaker + OpenVoice tone-color
converter), run on the reference clip's speaker.

Edits applied (word indices from stt_result.json, produced by run_stt.py):
  1. "retention" (idx 21) -> "satisfaction", regenerated with context
     "improve customer <word> and streamline" (idx 19-23)
  2. "dashboard" (idx 29) -> "spreadsheet", regenerated with context
     "the updated <word> will make" (idx 27-31)

For each edit this script:
  - builds the new context phrase text
  - extracts the target speaker embedding from the ORIGINAL reference clip
  - synthesizes the new phrase with MeloTTS (EN_NEWEST base speaker)
  - converts its tone color to the target speaker via OpenVoice's
    ToneColorConverter
  - records wall-clock time and peak RSS for each stage
"""
import json
import resource
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUT = ROOT / "outputs"
CKPT = ROOT / "checkpoints_v2"

REFERENCE_AUDIO = ASSETS / "reference_original.wav"
STT_JSON = OUT / "stt_result.json"

EDITS = [
    {
        "name": "retention_to_satisfaction",
        "context_word_indices": [19, 20, 21, 22, 23],
        "target_word_index": 21,
        "new_word": "satisfaction",
        "speed": 1.5,
    },
    {
        "name": "dashboard_to_spreadsheet",
        "context_word_indices": [27, 28, 29, 30, 31],
        "target_word_index": 29,
        "new_word": "spreadsheet",
        "speed": 1.28,
    },
]


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def main():
    timings = {}
    stt = json.loads(STT_JSON.read_text())
    words = stt["words"]

    device = "cpu"
    if torch.backends.mps.is_available():
        device = "mps"

    print(f"Using device: {device}")

    # ---- load models (one-time cost) ----
    t0 = time.time()
    from openvoice.api import ToneColorConverter

    ckpt_converter = str(CKPT / "converter")
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
        str(CKPT / "base_speakers" / "ses" / f"{speaker_key.lower().replace('_', '-')}.pth"),
        map_location=device,
    )
    timings["load_melo_seconds"] = time.time() - t0
    timings["melo_speaker_key"] = speaker_key

    # ---- extract target speaker embedding from the reference clip ----
    t0 = time.time()
    from openvoice import se_extractor

    target_se, audio_name = se_extractor.get_se(
        str(REFERENCE_AUDIO), tone_color_converter, target_dir=str(OUT / "se_processed"), vad=True
    )
    timings["extract_target_se_seconds"] = time.time() - t0
    timings["extract_target_se_peak_rss_mb"] = peak_rss_mb()

    results = []
    for edit in EDITS:
        name = edit["name"]
        ctx_words = [words[i]["word"] for i in edit["context_word_indices"]]
        new_words = [
            edit["new_word"] if i == edit["target_word_index"] else words[i]["word"]
            for i in edit["context_word_indices"]
        ]
        new_phrase_text = " ".join(new_words).rstrip(".,")
        old_phrase_text = " ".join(ctx_words)

        span_start = words[edit["context_word_indices"][0]]["start"]
        span_end = words[edit["context_word_indices"][-1]]["end"]

        print(f"\n=== Edit: {name} ===")
        print(f"  original phrase: {old_phrase_text!r}")
        print(f"  new phrase:      {new_phrase_text!r}")
        print(f"  original span:   {span_start:.2f}s - {span_end:.2f}s")

        tts_wav = OUT / f"{name}_base_tts.wav"
        cloned_wav = OUT / f"{name}_cloned.wav"

        t0 = time.time()
        melo_model.tts_to_file(new_phrase_text, speaker_id, str(tts_wav), speed=edit.get("speed", 1.0))
        base_tts_seconds = time.time() - t0

        t0 = time.time()
        tone_color_converter.convert(
            audio_src_path=str(tts_wav),
            src_se=source_se,
            tgt_se=target_se,
            output_path=str(cloned_wav),
            message="@voice-word-editor-feasibility",
        )
        convert_seconds = time.time() - t0
        stage_peak_rss_mb = peak_rss_mb()

        import soundfile as sf

        cloned_duration = sf.info(str(cloned_wav)).duration
        original_span_duration = span_end - span_start

        results.append(
            {
                "name": name,
                "old_phrase_text": old_phrase_text,
                "new_phrase_text": new_phrase_text,
                "original_span_start": span_start,
                "original_span_end": span_end,
                "original_span_duration": original_span_duration,
                "cloned_phrase_duration": cloned_duration,
                "tts_speed_param": edit.get("speed", 1.0),
                "base_tts_seconds": base_tts_seconds,
                "tone_convert_seconds": convert_seconds,
                "cumulative_peak_rss_mb": stage_peak_rss_mb,
                "base_tts_wav": str(tts_wav),
                "cloned_wav": str(cloned_wav),
            }
        )
        print(
            f"  base TTS: {base_tts_seconds:.2f}s | tone convert: {convert_seconds:.2f}s | "
            f"cloned duration: {cloned_duration:.2f}s vs original span: {original_span_duration:.2f}s"
        )

    out = {
        "device": device,
        "timings": timings,
        "edits": results,
        "final_peak_rss_mb": peak_rss_mb(),
    }
    (OUT / "clone_result.json").write_text(json.dumps(out, indent=2))
    print("\nFinal peak RSS (MB):", out["final_peak_rss_mb"])
    print("Wrote", OUT / "clone_result.json")


if __name__ == "__main__":
    main()
