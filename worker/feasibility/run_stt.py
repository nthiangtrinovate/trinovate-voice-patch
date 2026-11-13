import json
import resource
import sys
import time
from pathlib import Path

import mlx_whisper

audio_path = sys.argv[1]
out_json = sys.argv[2]

t0 = time.time()
result = mlx_whisper.transcribe(
    audio_path,
    path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
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
    "model": "mlx-community/whisper-large-v3-turbo",
    "audio": str(audio_path),
    "elapsed_seconds": elapsed,
    "peak_rss_mb": peak_rss_mb,
    "text": result.get("text", "").strip(),
    "words": words,
}
Path(out_json).write_text(json.dumps(out, indent=2))
print(json.dumps({"elapsed_seconds": elapsed, "peak_rss_mb": peak_rss_mb, "word_count": len(words)}, indent=2))
print("TEXT:", out["text"])
