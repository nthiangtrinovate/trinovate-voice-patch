"""
Phase 1 feasibility proof — objective proxy metric: speaker-embedding cosine
similarity between the original reference clip and the synthesized/cloned
replacement phrases, computed via Resemblyzer.

IMPORTANT: this is a PROXY signal only, not a substitute for a human listen.
Cosine similarity of d-vector speaker embeddings correlates with perceived
speaker identity but does not capture naturalness, prosody quality, or
artifacts at splice boundaries.
"""
import json
from pathlib import Path

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUT = ROOT / "outputs"


def cos_sim(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def main():
    encoder = VoiceEncoder()
    reference_wav = preprocess_wav(str(ASSETS / "reference_original.wav"))
    reference_embed = encoder.embed_utterance(reference_wav)

    clone_result = json.loads((OUT / "clone_result.json").read_text())

    report = {"note": "PROXY METRIC ONLY — not a substitute for a human listen.", "edits": []}

    for edit in clone_result["edits"]:
        base_tts_wav = preprocess_wav(edit["base_tts_wav"])
        cloned_wav = preprocess_wav(edit["cloned_wav"])

        base_embed = encoder.embed_utterance(base_tts_wav)
        cloned_embed = encoder.embed_utterance(cloned_wav)

        sim_base_to_ref = cos_sim(base_embed, reference_embed)
        sim_cloned_to_ref = cos_sim(cloned_embed, reference_embed)

        entry = {
            "name": edit["name"],
            "cosine_similarity_base_tts_vs_reference": sim_base_to_ref,
            "cosine_similarity_cloned_vs_reference": sim_cloned_to_ref,
            "improvement_from_tone_conversion": sim_cloned_to_ref - sim_base_to_ref,
        }
        report["edits"].append(entry)
        print(
            f"{edit['name']}: base-TTS-vs-ref sim={sim_base_to_ref:.3f} | "
            f"cloned-vs-ref sim={sim_cloned_to_ref:.3f} | "
            f"delta={entry['improvement_from_tone_conversion']:+.3f}"
        )

    # Also compare the final blended full clip's overall embedding to the reference
    final_wav = preprocess_wav(str(OUT / "final_both_edits_blended.wav"))
    final_embed = encoder.embed_utterance(final_wav)
    sim_final_to_ref = cos_sim(final_embed, reference_embed)
    report["final_blended_vs_reference_similarity"] = sim_final_to_ref
    print(f"final blended clip vs reference: sim={sim_final_to_ref:.3f}")

    (OUT / "similarity_result.json").write_text(json.dumps(report, indent=2))
    print("Wrote", OUT / "similarity_result.json")


if __name__ == "__main__":
    main()
