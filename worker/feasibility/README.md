# Phase 1 feasibility scripts

These four scripts are a straight mirror of what was actually run over SSH on
the user's Mac mini for the Phase 1 feasibility proof. See
`docs/feasibility-report.md` at the repo root for the full writeup, measured
results, license citations, and GO/NO-GO call. This directory holds **code
only** — no model weights, no venvs, no audio. Those artifacts live on the
Mac mini under `~/voice-word-editor-feasibility/` and are not committed here
(see `.gitignore` in this directory, which guards against accidentally doing
so if these scripts are ever run from inside a repo checkout on the Mac
mini).

## Pipeline

```
run_stt.py        audio in  -> word-level timestamps (mlx-whisper)
run_clone.py       picks 1-2 words to replace, synthesizes the replacement
                   phrase in the source speaker's voice (OpenVoice V2 +
                   MeloTTS), using a short surrounding-context phrase for
                   prosody
run_blend.py       splices the cloned phrase(s) back into the original
                   audio with short crossfades at the (word-boundary)
                   splice points
run_similarity.py  objective speaker-embedding similarity proxy
                   (Resemblyzer) — NOT a substitute for a human listen
```

## Why two environments

`run_stt.py` needs an `mlx-whisper` venv (modern `numpy`/`mlx`).
`run_clone.py`, `run_blend.py`, `run_similarity.py` need an OpenVoice
V2/MeloTTS venv, whose pinned dependencies (`numpy==1.22.0` et al.) conflict
with `mlx-whisper`'s. In practice both venvs just need modern, mutually
compatible versions of the same packages (the exact pins in OpenVoice's/
MeloTTS's `requirements.txt` are conservative, not hard requirements) — but
until that's reconciled, treat STT and cloning as two separate subprocesses.
This maps cleanly onto the Phase 2/4 job state machine anyway
(`transcribing` and `generating` are already distinct states).

## Reproducing (on an Apple Silicon Mac with user-scoped Homebrew + `ffmpeg`)

```bash
# STT venv
python3.11 -m venv stt-venv
./stt-venv/bin/pip install mlx-whisper soundfile numpy
PATH="/opt/homebrew/bin:$PATH" ./stt-venv/bin/python run_stt.py <audio.wav> stt_result.json

# TTS venv (see docs/feasibility-report.md "Environment setup" for the full
# dependency list — OpenVoice and MeloTTS need to be installed from source,
# e.g. a GitHub tarball, with --no-deps, then their runtime deps installed
# with modern versions by hand)
python3.11 -m venv tts-venv
# ... install OpenVoice + MeloTTS + deps + download checkpoints_v2/ ...
PATH="/opt/homebrew/bin:$PATH" ./tts-venv/bin/python run_clone.py
./tts-venv/bin/python run_blend.py
./tts-venv/bin/python run_similarity.py
```

`run_clone.py` expects `stt_result.json` (from `run_stt.py`) and a
`checkpoints_v2/` directory (OpenVoice V2 converter + base-speaker
embeddings, downloaded from `huggingface_hub`) next to it; see the script's
`EDITS` list for how word indices map to edits — this is hardcoded to the
specific two-edit demo described in the feasibility report, not yet a
general CLI. Turning this into a general `transcript diff -> edit list`
driven tool, with proper args in/out, is Phase 2 work — review-tests has
been told this mirror is a mechanics reference, not the final worker
interface.

## Licenses of everything invoked here

See `docs/feasibility-report.md` for the full table with primary sources.
Summary: `mlx-whisper`/Whisper weights (MIT), OpenVoice V2 (MIT), MeloTTS
(MIT), Resemblyzer (Apache-2.0). No CUDA-only or cloud-API dependencies.
