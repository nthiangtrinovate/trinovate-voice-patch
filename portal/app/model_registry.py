"""Registry of selectable word-replacement modes and voice-simulation
models/presets.

Deliberately small and honest: every entry here maps to a code path that
already exists and has actually been exercised, not an aspirational
integration. See docs/portal-notes.md "Model selection" for the evidence
behind each one.

- Word-replacement mode: which of the two existing, already-implemented
  ways of producing edited transcript text the UI defaults to. Both
  remain available regardless of this selection (the fruit-replace
  endpoint and manual text editing already coexist in job.html/app.js) —
  this is a stated default/label, not a hard gate, so it can't take away
  a capability a user already had.
- Voice model + preset: the TTS/cloning engine and its tuning.
  `openvoice`, `chatterbox`, and `f5tts` are all real, independently
  dispatched engines (see worker/run_worker.py's `_stage_generating`) —
  each runs in its own persistent, pinned Mac mini venv with its own
  pipeline script (`run_clone.py`, `run_clone_chatterbox.py`,
  `run_clone_f5.py`). None silently substitutes another engine: an
  unavailable venv raises a clear engine-specific error instead of
  falling back to OpenVoice. See docs/portal-notes.md "Model selection"
  for the measured evidence behind each one and exact pinned
  checkpoint/library versions.

  `f5tts`'s pretrained checkpoints (SWivid/F5-TTS) are licensed
  **CC-BY-NC — noncommercial use only** (trained on the Emilia dataset;
  confirmed against the primary source, the F5-TTS GitHub repo's
  LICENSE file and Hugging Face model card — see docs/portal-notes.md).
  This is wired in for internal, non-commercial evaluation only. It is
  NOT cleared for any commercial product/service use, and this
  deployment must stay internal/private — see "Before this is exposed
  beyond local/private use" in docs/portal-notes.md. `LICENSE_NOTE`
  below is shown in the UI next to this engine so that restriction is
  never silently omitted.
"""
from __future__ import annotations

WORD_REPLACEMENT_MODES: dict[str, dict] = {
    "manual": {
        "label": "Manual edit (default)",
        "description": "Edit the transcript text yourself.",
    },
    "fruit": {
        "label": "Random fruit replacement (demo)",
        "description": "Automatically swap 1-2 suitable words for a random fruit name, then you can still edit further.",
    },
}
DEFAULT_WORD_REPLACEMENT_MODE = "manual"


# Each preset's "env" dict is passed as per-job environment overrides to
# the run_clone.py subprocess (see worker/run_worker.py) — never as global
# process env, so different jobs can use different presets concurrently.
_OPENVOICE_PRESETS: dict[str, dict] = {
    "balanced": {
        "label": "Balanced (recommended, default)",
        "description": (
            "Full Resemblyzer-guided search across reference variants and "
            "tau values, plus pitch/loudness/spectral matching. This is "
            "exactly run_clone.py's own validated default behavior (no "
            "overrides applied) — the same configuration used for every "
            "prior measured result in docs/portal-notes.md."
        ),
        "env": {},
    },
    "fast": {
        "label": "Fast (draft quality)",
        "description": (
            "Skips the multi-reference/tau search (fixed tau=0.60) and "
            "therefore the spectral correction pass too (it only runs "
            "when the search path is active). Faster per edit; measured "
            "lower speaker-similarity than Balanced — see "
            "docs/portal-notes.md."
        ),
        "env": {"VOICE_RESEMBLYZER_MATCH": "0", "VOICE_TAU": "0.60"},
    },
}

_CHATTERBOX_PRESETS: dict[str, dict] = {
    "no_rubberband": {
        "label": "Default (no pitch/tempo correction)",
        "description": (
            "Reproduces the measured-best spike recipe: trim silence, "
            "RMS loudness-match to the original span (clamped +/-12dB), "
            "pad up to the replaced word's slot if the synth came out "
            "shorter — never truncated or time-stretched if it came out "
            "longer. No rubberband pitch/tempo correction (that step is "
            "what measurably hurt speaker similarity in testing)."
        ),
        "env": {},
    },
}
_F5TTS_PRESETS: dict[str, dict] = {
    "no_rubberband": {
        "label": "Default (no pitch/tempo correction)",
        "description": _CHATTERBOX_PRESETS["no_rubberband"]["description"],
        "env": {},
    },
}

LICENSE_NOTE: dict[str, str] = {
    "f5tts": (
        "Checkpoint license: CC-BY-NC (noncommercial) — SWivid/F5-TTS, "
        "trained on the Emilia dataset. Internal/non-commercial "
        "evaluation only; not cleared for any commercial product or "
        "service use. Keep this deployment internal/private."
    ),
}

VOICE_MODELS: dict[str, dict] = {
    "openvoice": {
        "label": "OpenVoice V2 + MeloTTS",
        "presets": _OPENVOICE_PRESETS,
        "default_preset": "balanced",
    },
    "chatterbox": {
        "label": "Chatterbox (Resemble AI)",
        "presets": _CHATTERBOX_PRESETS,
        "default_preset": "no_rubberband",
    },
    "f5tts": {
        "label": "F5-TTS (internal/non-commercial use only)",
        "presets": _F5TTS_PRESETS,
        "default_preset": "no_rubberband",
    },
}
DEFAULT_VOICE_MODEL = "openvoice"


def validate_word_replacement_mode(mode: str) -> str:
    if mode not in WORD_REPLACEMENT_MODES:
        raise ValueError(f"unknown word_replacement_mode {mode!r}")
    return mode


def validate_voice_choice(model_id: str, preset_id: str) -> tuple[str, str]:
    model = VOICE_MODELS.get(model_id)
    if model is None:
        raise ValueError(f"unknown voice_model {model_id!r}")
    if preset_id not in model["presets"]:
        raise ValueError(f"unknown voice_preset {preset_id!r} for model {model_id!r}")
    return model_id, preset_id


def preset_env_overrides(model_id: str, preset_id: str) -> dict:
    """Return the env var overrides for this (validated) model/preset pair.
    Caller must validate first; this never silently falls back."""
    return dict(VOICE_MODELS[model_id]["presets"][preset_id]["env"])
