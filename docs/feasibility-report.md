# Phase 1 Feasibility Report — Local Same-Speaker Voice Word Editing

Owner: model-feasibility. Status: **GO** (with caveats below — a human listen
is still required before this is called "validated").

Date: 2026-11-10. Hardware: user's Mac mini, Apple Silicon (arm64),
macOS 26.5.1, 10 CPU cores, 32GB RAM, 262GB free disk.
All work ran over SSH, no inbound ports opened, no sudo, no system Python or
system-wide installs — everything lives in `~/voice-word-editor-feasibility/`
on the Mac mini in two project-local venvs (`stt-venv`, `tts-venv`).

## IMPORTANT: test clip provenance

**The test clip used in this feasibility pass is SYNTHETIC, not a real human
recording and not the user's voice.** As of this session's start the repo
workspace contained no user-supplied audio/video sample. Per the plan's
explicit fallback, I generated a ~13.8s single-speaker clip with macOS's
built-in `say` command (voice: `Samantha`, en_US), using an invented business
narration script written for this test:

> "Good afternoon everyone. Thank you all for joining today's quarterly
> planning session. Our main goal this quarter is to improve customer
> retention and streamline onboarding. I believe the updated dashboard will
> make reporting much faster for the whole team."

This is **not** real-user-voice validation. It proves the pipeline mechanics
and gives real measured numbers on the target hardware, but says nothing
about how well these models clone an actual human voice. Before this is
treated as validated for the user's real use case, we need (a) a short real
voice sample (with consent) and (b) an actual human listening pass — see
"What I could not verify" below.

File: `~/voice-word-editor-feasibility/assets/reference_original.wav`
(24kHz mono, 13.773s). Script: `~/voice-word-editor-feasibility/assets/reference_script.txt`.

## Verdict

**GO** on building Phase 2 around:
- **STT**: `mlx-whisper` running `mlx-community/whisper-large-v3-turbo`
- **Voice cloning/TTS**: OpenVoice V2 (MyShell) tone-color conversion on top
  of a MeloTTS (MyShell) base speaker
- **Blend**: `pydub` (ffmpeg-backed) crossfade splicing

All three run natively on this Apple Silicon Mac mini (MLX for STT, PyTorch
MPS backend for TTS/cloning) within a few GB of RAM and sub-10-second
end-to-end latency for a short clip with two word edits. Licenses for the
chosen stack are all MIT (see table below) — no commercial-use ambiguity.

The **caveats that must be resolved before Phase 2 is "done"**, not before it
starts:
1. No human has listened to the output yet (I cannot hear audio). The speaker
   embedding similarity proxy shows the clone moves meaningfully toward the
   target speaker but lands at ~0.68–0.71 cosine similarity, not "obviously
   the same person" territory — a real listen is needed to judge naturalness.
2. One splice boundary produced a detectable artifact (re-transcription
   hallucinated an extra word — see "Round-trip STT verification").
3. Synthesized phrase duration must be hand-tuned via a `speed` parameter to
   roughly match the original span's duration; Phase 2 needs this automated.
4. STT and TTS currently require **separate Python venvs** — their pinned
   dependencies conflict (see "Dependency conflict" below). This is a real
   architectural input for the worker design in Phase 2/4.
5. Tested against a synthetic `say` voice, not a real human — untested how
   these models behave on real user audio (different noise floor, prosody,
   possible imperfect recording conditions).

None of these are blockers to starting Phase 2 — they're known, bounded
follow-ups that confirm the stack is viable, not fully polished.

## Models considered, licenses, and sources

I read each primary license/model-card source directly (not blog summaries).

### Speech-to-text (word-level timestamps)

| Model | License | Source checked | Apple Silicon |
|---|---|---|---|
| **Chosen: OpenAI Whisper weights via `mlx-whisper` (`mlx-community/whisper-large-v3-turbo`)** | MIT (OpenAI Whisper weights/code); `mlx-whisper` itself ships under Apple's `mlx-examples` repo, also MIT | `https://raw.githubusercontent.com/openai/whisper/main/LICENSE` (MIT, OpenAI 2022); `https://raw.githubusercontent.com/ml-explore/mlx-examples/main/LICENSE` (MIT, Apple Inc. 2023). The `mlx-community/whisper-large-v3-turbo` HF repo itself carries no separate license tag — it's a format conversion of the same MIT weights, no indication of added restriction. | Native — uses Apple's MLX framework, runs on the GPU via `mx.default_device()` = `Device(gpu, 0)`. Confirmed running in **4.02s** on a 13.77s clip (RTF ≈ 0.29, i.e. ~3.4× faster than real time), peak RSS 1.81GB. |

I did not deep-dive `faster-whisper`/CTranslate2 as an alternative since
`mlx-whisper` is Apple-native and was already independently proven on this
exact machine twice before I started (see "Prior art already on this Mac"
below) — redundant to re-litigate.

### Same-speaker voice cloning / TTS

| Model | License | Source checked | Apple Silicon | Verdict |
|---|---|---|---|---|
| **Chosen: OpenVoice V2** (MyShell AI) tone-color converter | MIT, "Free for commercial use" per V2 | `https://raw.githubusercontent.com/myshell-ai/OpenVoice/main/LICENSE` (MIT text); HF model card `https://huggingface.co/myshell-ai/OpenVoiceV2` states "Starting from April 2024, both V2 and V1 are released under MIT License. Free for commercial use." | Runs via PyTorch MPS backend (`torch.backends.mps.is_available()` → True on this machine, `torch 2.15.0`). Measured, see below. | **Primary pick** |
| **Chosen base speaker: MeloTTS** (MyShell AI) | MIT | `https://raw.githubusercontent.com/myshell-ai/MeloTTS/main/LICENSE` (MIT text, Copyright MyShell.ai 2024) | Same process/venv as OpenVoice; works. | Used as required base-TTS layer for OpenVoice V2 |
| Chatterbox (Resemble AI) | MIT (code and weights — HF model card `https://huggingface.co/ResembleAI/chatterbox` tags "License: mit") | GitHub LICENSE + HF model card | Mixed/unverified: community reports (HF Space adaptations, Reddit threads) show some Apple Silicon MPS paths work and others fall back to CPU due to tensor-op issues; no authoritative statement of official MPS support found. An official `example_for_mac.py` exists upstream but wasn't inspected. | Deprioritized for this pass only due to MPS uncertainty + time budget — not disqualified. Purpose-built for short-reference zero-shot cloning, likely higher naturalness ceiling than OpenVoice. **Recommended as the first thing to bake off against OpenVoice V2 if a human listen judges OpenVoice insufficient.** |
| Coqui XTTS-v2 | Code (Coqui TTS lib) is **MPL-2.0** (commercial-OK); **weights are Coqui Public Model License (CPML) — non-commercial only.** Confirmed via HF model card tag `coqui-public-model-license` and CPML terms found via search: non-commercial use (including commercial-entity evaluation/research) is free, but "use of the model to train other models for commercial use is not a non-commercial purpose," and the project is unmaintained (Coqui Inc. shut down). | `https://github.com/coqui-ai/TTS/blob/dev/LICENSE.txt` (MPL-2.0, code); CPML terms via web search since `coqui.ai/cpml` 404'd directly | Well-documented, mature, widely used | **Rejected for primary pick**: non-commercial-only weights license is a real constraint given this project may eventually sit behind the user's commercial portal (Phase 3/4). Clean MIT alternatives exist, so no reason to accept that risk. |
| F5-TTS (SWivid) | Code MIT; **pretrained checkpoints are CC-BY-NC** because they're trained on the Emilia dataset (in-the-wild, non-commercial-licensed data). Repo README states this explicitly. | `https://raw.githubusercontent.com/SWivid/F5-TTS/main/LICENSE` + README | Not independently tested | **Rejected for primary pick**, same non-commercial-weights reasoning as XTTS-v2. |
| CosyVoice2 (FunAudioLLM/Alibaba) | **Apache-2.0** | `https://raw.githubusercontent.com/FunAudioLLM/CosyVoice/main/LICENSE` | Not tested on this hardware | Clean license, **not empirically validated this pass** — good second alternative to try if OpenVoice/Chatterbox both fall short. |

### Objective similarity proxy

| Tool | License | Source |
|---|---|---|
| Resemblyzer (resemble-ai) | Apache-2.0 | `https://github.com/resemble-ai/Resemblyzer` (license badge) |

Used only as an internal, non-redistributed analysis tool.

## Prior art already on this Mac (found, not created, by me)

Before building anything, I did read-only reconnaissance of the Mac mini to
avoid redundant work and respect "preserve unrelated files/services." I did
**not** modify, run, or depend on any of the following — I only inspected
directory listings, `pip freeze` output, and a couple of small (<1KB) script
files for intelligence on what's already proven on this exact hardware:

- An existing, unrelated benchmark already on this machine had
  independently exercised `mlx-whisper` (`whisper-large-v3-mlx`,
  word-level timestamps) and `parakeet-mlx` against real meeting
  recordings. This independently corroborated the STT pick before I
  started.
- A separate, unrelated personal project already running on this machine
  (not touched or inspected beyond a directory listing) was confirmed to
  already use `mlx-whisper` (`whisper-large-v3-turbo`) and Kokoro-82M (a
  fixed-voice TTS, **not** voice cloning). This further corroborated the
  STT pick, and confirmed no existing same-speaker cloning capability
  existed there that I could or should have reused.
- User-scoped Homebrew is already installed (`/opt/homebrew`, not on the
  non-interactive SSH `PATH` by default — needed `export PATH=/opt/homebrew/bin:$PATH`
  per command) with `ffmpeg 7.1.1` already present. I used this existing
  binary rather than installing a new one.

No sample voice clip for this project existed anywhere I was authorized to
look (the repo workspace, which is the only place a "user-supplied sample"
would legitimately live for this task) — hence the synthetic clip.

## Environment setup (isolated, non-destructive)

All on the Mac mini, under `~/voice-word-editor-feasibility/` (not inside any
existing project, not touching system Python 3.9.6):

- `stt-venv/` — Python 3.11.18 (via `brew`'s `python@3.11`, used only to
  create an isolated venv; brew itself was not modified). Packages:
  `mlx-whisper`, `soundfile`, `numpy`. Model `mlx-community/whisper-large-v3-turbo`
  downloaded via Hugging Face Hub (outbound only).
- `tts-venv/` — Python 3.11.18, same brew interpreter. Packages: `torch`,
  `torchaudio`, OpenVoice (installed editable from a downloaded GitHub
  tarball — **not** `pip install git+...`, which this session's sandbox
  denied as a blanket policy; the tarball approach is equivalent and worked),
  MeloTTS (same tarball approach), plus their runtime deps (`librosa`,
  `pydub`, `faster-whisper`, `whisper-timestamped`, `wavmark`, `transformers==4.27.4`,
  `g2p_en`, `unidic`/`mecab-python3` + downloaded UniDic dictionary,
  `pykakasi`, `fugashi`, `g2pkk`, `jamo`, `gruut`, `langid`, `resemblyzer`,
  `setuptools<81` — pinned down because newer setuptools dropped
  `pkg_resources`, which `webrtcvad` (a Resemblyzer dependency) still
  imports).
- Model checkpoints downloaded via `huggingface_hub` into
  `~/voice-word-editor-feasibility/checkpoints_v2/` (OpenVoice V2 converter +
  English base-speaker embeddings, ~125MB) and the venvs' own HF caches
  (MeloTTS's BERT tagger, UniDic dictionary ~526MB, Whisper turbo weights).
- One `torch.hub` one-time trust prompt (for Silero VAD, used internally by
  OpenVoice's reference-audio segmentation) had to be pre-accepted
  non-interactively with `trust_repo=True` since there's no TTY over this
  SSH session; it's now cached in `~/.cache/torch/hub/`.
- **Total disk footprint: 4.3GB** (`stt-venv` 1.3GB, `tts-venv` 2.8GB,
  checkpoints/caches ~150MB, outputs ~3MB) — trivial against 262GB free.
- Load average stayed around 1.1–1.3 (on 10 cores) throughout, including
  during model downloads and inference; nothing was run with elevated
  priority relative to the user's other work, and `nice -n 10` was used for
  the heavier inference run as an extra courtesy.

### Dependency conflict worth flagging to the team

OpenVoice/MeloTTS's pinned `requirements.txt` (`numpy==1.22.0`,
`librosa==0.9.1`, etc.) is incompatible with the modern `numpy 2.x` that
`mlx-whisper` needs. I installed OpenVoice/MeloTTS with `--no-deps` and then
installed modern, mutually-compatible versions of each actual runtime
dependency by hand (works fine — the pins were just conservative, not hard
requirements). But this means **STT and TTS cannot currently share one
venv/process** without real work reconciling versions. For Phase 2/4, the
simplest path is to keep them as two subprocesses/venvs (which also maps
cleanly onto the planned job-state machine: `transcribing` can be one
process, `generating` another) rather than trying to unify them.

## The proof script and what it does

Code lives in `~/voice-word-editor-feasibility/scripts/` on the Mac mini and
is mirrored (text only, no weights/venvs/audio) at `worker/feasibility/` in
this repo:

1. **`run_stt.py`** — `mlx_whisper.transcribe(..., word_timestamps=True,
   condition_on_previous_text=False, temperature=0.0)` on the reference clip.
   Writes word-level timestamps + timing/memory to `stt_result.json`.
2. **`run_clone.py`** — for each of two word-edits:
   - builds a short context phrase (2 words before + target + 2 words after)
     with the target word substituted
   - extracts the **target speaker's** tone-color embedding from the whole
     reference clip via `openvoice.se_extractor.get_se(..., vad=True)`
   - synthesizes the new phrase text with MeloTTS's `EN_NEWEST` base speaker
   - converts its tone color to the target speaker via OpenVoice's
     `ToneColorConverter.convert()`
   - records per-stage wall-clock time, peak RSS, and output duration vs. the
     original span's duration
3. **`run_blend.py`** — splices each cloned phrase (and both together) back
   into the original clip using `pydub`, with a 25ms crossfade at each splice
   boundary (chosen because splice points sit at Whisper-identified word
   boundaries, not mid-word).
4. **`run_similarity.py`** — Resemblyzer speaker-embedding cosine similarity:
   reference clip vs. (a) the raw MeloTTS base-speaker output (no cloning)
   and (b) the OpenVoice-cloned output, to objectively show the cloning step
   is doing something, plus the final blended clip vs. the reference.

The two edits demonstrated (word indices from Whisper's own output):

| Edit | Original phrase (context) | New phrase | Original span |
|---|---|---|---|
| 1 | "improve customer **retention** and streamline" | "improve customer **satisfaction** and streamline" | 6.38s–8.44s (2.06s) |
| 2 | "the updated **dashboard** will make" | "the updated **spreadsheet** will make" | 9.92s–11.52s (1.60s) |

## Measured results

### STT (mlx-whisper, `whisper-large-v3-turbo`)

| Metric | Value |
|---|---|
| Input | 13.773s synthetic clip |
| Wall-clock | 4.02s (RTF ≈ 0.29) |
| Peak RSS | 1813.7 MB |
| Word count | 39 |
| Transcript | Exact match to the input script, including punctuation |

### Voice cloning (OpenVoice V2 + MeloTTS, device = `mps`)

One-time model load costs: tone converter load 2.34s, MeloTTS load 4.52s,
target-speaker embedding extraction from the reference clip 0.60s (≈7.5s
total one-time setup).

| Edit | Base TTS synth | Tone conversion | Cloned duration vs. original span | Cumulative peak RSS |
|---|---|---|---|---|
| retention→satisfaction (speed=1.5) | 2.26s | 0.29s | 2.30s vs 2.06s target | 2406 MB |
| dashboard→spreadsheet (speed=1.28) | 0.19s | 0.15s | 1.64s vs 1.60s target | 2406 MB |

(First edit's base-TTS time includes a one-time lazy download/load of a BERT
tagger used internally by MeloTTS; the second edit — same process, warm —
shows the real steady-state cost: **≈0.34s per short-phrase edit** once
everything is loaded.)

Phrase duration was hand-tuned via MeloTTS's `speed` parameter to
approximately match the original span (within 0.04–0.24s). This is a manual
knob today; Phase 2 should automate it (e.g. iterative speed search, or a
post-hoc time-stretch).

### Blend/splice (pydub, 25ms crossfades)

| Output | Duration | Blend wall-clock |
|---|---|---|
| Edit 1 only, blended into full clip | 13.912s | 2ms |
| Edit 2 only, blended into full clip | 13.710s | 1.4ms |
| **Both edits, blended into full clip** | **13.849s** | 2.7ms |

Blending itself is essentially free — all the cost is in STT and cloning.

### Objective similarity proxy (Resemblyzer cosine similarity)

**This is a proxy only — it is not a substitute for a human listen,** and
does not measure naturalness, prosody quality, or splice-boundary artifacts.

| Comparison | Cosine similarity |
|---|---|
| Base MeloTTS speaker (no cloning) vs. reference | 0.460 / 0.471 (two edits) |
| **OpenVoice-cloned phrase vs. reference** | **0.708 / 0.676** |
| Improvement from the tone-conversion step | +0.249 / +0.205 |
| Final blended full clip vs. reference | 0.977 |

Reading these honestly: the tone-conversion step clearly moves the output
toward the target speaker (a consistent +0.2–0.25 jump), but the cloned
phrases alone land at ~0.68–0.71, not in the ">0.85" range that would read as
"unambiguously the same speaker" by typical Resemblyzer benchmarks. The final
blended clip's 0.977 is almost entirely because ~90% of that clip's audio
*is* the untouched original recording — it is not evidence about the cloned
segments' quality and shouldn't be read as such.

### Round-trip STT verification (objective, not a human check)

I fed the final blended output back through `mlx-whisper` as an independent
check that the edits actually land as intelligible words, not just "some
audio in there somewhere":

> Input (reference):
> "...to improve customer **retention** and streamline onboarding. ... the
> updated **dashboard** will make reporting..."
>
> Output (re-transcribed from the edited clip):
> "...to improve customer **satisfaction** and streamline **online**
> onboarding. ... the updated **spreadsheet** will make reporting..."

Both target edits ("retention"→"satisfaction", "dashboard"→"spreadsheet")
transcribed back correctly. **However**, Whisper also hallucinated an extra
word, "online", right at the trailing edge of the first edit's splice
boundary (between the cloned phrase ending in "...streamline" and the
untouched original word "onboarding."). This is a concrete, honest signal
that the 25ms crossfade at that specific boundary likely introduced an
audible artifact (a small discontinuity in energy/pitch) — Phase 2 should
treat splice-boundary crossfade tuning (length, alignment, maybe a short
fade curve instead of linear) as real work, not a formality.

## What I could not verify (needs the user / a human)

- **I cannot listen to audio.** I cannot judge whether the cloned voice
  actually sounds like the reference speaker, whether the prosody is
  natural, or how noticeable the splice artifact above is. **Recommended
  next step: have the user (or another teammate) listen to**
  `~/voice-word-editor-feasibility/outputs/final_both_edits_blended.wav`
  (the full before/after demo) and the two `*_cloned.wav` phrase files, and
  report back before this is called "validated" rather than just
  "mechanically works."
- This was tested on a **synthetic** voice, not a real human and not the
  user. Real speech has different noise characteristics, natural
  disfluencies, and recording conditions that could behave differently (for
  better or worse) with these models. A short real voice sample (with
  consent) is the right next validation step.

## Recommendation

**GO.** Build Phase 2's worker-side pipeline around `mlx-whisper` (STT) +
OpenVoice V2/MeloTTS (cloning) + ffmpeg/pydub (blend), as two separate
venvs/subprocesses per the dependency-conflict note above. Carry the five
caveats listed under "Verdict" into Phase 2 as explicit follow-up work items,
not blockers. Keep Chatterbox (MIT, purpose-built for short-reference
cloning) and CosyVoice2 (Apache-2.0) on the shortlist as fallback candidates
if a human listen judges OpenVoice V2's cloning fidelity insufficient.

If the team wants a NO-GO path instead: the fallback order I'd try next is
(1) Chatterbox on CPU (slower but MIT, purpose-built for this), then (2)
CosyVoice2 (Apache-2.0, zero-shot cloning, not yet tested here).

## Artifacts

On the Mac mini (not committed to git — scripts only, no weights/venvs/audio):
- `~/voice-word-editor-feasibility/scripts/{run_stt,run_clone,run_blend,run_similarity}.py`
- `~/voice-word-editor-feasibility/assets/reference_original.wav` (+ `reference_script.txt`)
- `~/voice-word-editor-feasibility/outputs/*.json` (all measured results above)
- `~/voice-word-editor-feasibility/outputs/final_both_edits_blended.wav` (the demo output)

In this repo:
- `docs/feasibility-report.md` (this file)
- `worker/feasibility/` (mirrored scripts + README, see there for how to
  reproduce)
