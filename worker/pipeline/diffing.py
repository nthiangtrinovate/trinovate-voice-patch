"""Map a transcript edit (arbitrary free-text edit OR the fruit-replacement
tool's structured output) onto the original audio's word-level timestamps.

Why we diff ourselves instead of trusting `edit["replacements"]`: the portal
only fills `replacements` reliably for the fruit-replacement path
(`portal/app/fruit.py`). If the user hand-edits the transcript textarea
afterward — or never clicks fruit-replace at all — `replacements` can be
empty or stale relative to the final submitted text (see
`portal/app/static/app.js`: `lastReplacements` is only ever set by the last
fruit-replace response, not recomputed on manual edits). The worker-protocol
contract (`docs/worker-protocol.md`) only guarantees `edit["text"]`; treat
`replacements` as an optional hint, never load-bearing. This module
recomputes the real diff ourselves so both paths (fruit tool and manual
edits) are handled identically and correctly.

This is pure stdlib (`difflib`) by design — it has to run in the lightweight
main worker process (see `worker/run_worker.py`), not in either of the heavy
ML venvs.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class EditSpan:
    """One contiguous region of the original audio to regenerate, expanded
    with surrounding context words for prosody continuity (per PLAN.md:
    "short phrase of surrounding context for prosody")."""

    start_word_idx: int  # inclusive index into the ORIGINAL words[] array
    end_word_idx: int  # exclusive
    original_text: str  # original context phrase (for logging/diagnostics)
    new_text: str  # the context phrase to actually synthesize
    span_start_sec: float  # original audio time of start_word_idx's start
    span_end_sec: float  # original audio time of (end_word_idx - 1)'s end
    changed_word_indices: list[int] = field(default_factory=list)  # original indices actually changed (for logging)


def _word_list(words: list[dict]) -> list[str]:
    return [w["word"] for w in words]


def compute_edit_spans(
    original_words: list[dict],
    new_text: str,
    context_words: int = 2,
) -> list[EditSpan]:
    """Diff the original word list against the edited full text and return
    the minimal set of (context-expanded, non-overlapping) spans that need
    to be regenerated.

    `original_words`: the exact list the worker submitted via
    `POST /worker/jobs/{id}/transcript` — each `{"word", "start", "end"}`.
    The worker MUST have built that submission's `text` field as
    `" ".join(w["word"] for w in words)` so this function's tokenization
    (`new_text.split()`) lines up 1:1 with `original_words` whenever nothing
    changed. See `worker/run_worker.py:_submit_transcript`.

    Returns spans sorted by position, already merged if two edits' context
    windows would otherwise overlap.
    """
    original_tokens = _word_list(original_words)
    new_tokens = new_text.split()

    sm = difflib.SequenceMatcher(a=original_tokens, b=new_tokens, autojunk=False)
    opcodes = sm.get_opcodes()

    # Raw (ctx_i1, ctx_i2, ctx_j1, ctx_j2, changed_i_range) tuples first —
    # index-space only, no text built yet. Each non-equal opcode's window
    # is expanded independently (clamped only to the token-array bounds);
    # if two edits are close enough that their expanded windows overlap,
    # that's intentional — _merge_overlapping_ranges below unions them
    # into a single combined span rather than trying to keep them
    # artificially separate (there's no clean way to synthesize two
    # overlapping context windows independently anyway).
    raw: list[tuple[int, int, int, int, range]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue

        ctx_i1 = max(i1 - context_words, 0)
        ctx_i2 = min(i2 + context_words, len(original_tokens))
        ctx_j1 = max(j1 - context_words, 0)
        ctx_j2 = min(j2 + context_words, len(new_tokens))

        if ctx_i1 >= ctx_i2:
            # Pure insertion with no surrounding original words available
            # for context (edit right at the very start/end). Fall back to
            # the nearest single original word so we still have a span to
            # anchor timestamps to.
            ctx_i1 = max(min(i1, len(original_tokens) - 1), 0)
            ctx_i2 = min(ctx_i1 + 1, len(original_tokens))

        raw.append((ctx_i1, ctx_i2, ctx_j1, ctx_j2, range(i1, i2)))

    merged = _merge_overlapping_ranges(raw)

    spans: list[EditSpan] = []
    for ctx_i1, ctx_i2, ctx_j1, ctx_j2, changed in merged:
        spans.append(
            EditSpan(
                start_word_idx=ctx_i1,
                end_word_idx=ctx_i2,
                original_text=" ".join(original_tokens[ctx_i1:ctx_i2]),
                new_text=" ".join(new_tokens[ctx_j1:ctx_j2]),
                span_start_sec=original_words[ctx_i1]["start"],
                span_end_sec=original_words[ctx_i2 - 1]["end"],
                changed_word_indices=sorted(changed),
            )
        )
    return spans


def _merge_overlapping_ranges(
    raw: list[tuple[int, int, int, int, range]]
) -> list[tuple[int, int, int, int, set]]:
    if not raw:
        return []
    raw = sorted(raw, key=lambda t: t[0])
    merged = [list(raw[0][:4]) + [set(raw[0][4])]]
    for ctx_i1, ctx_i2, ctx_j1, ctx_j2, changed in raw[1:]:
        last = merged[-1]
        if ctx_i1 <= last[1]:
            last[0] = min(last[0], ctx_i1)
            last[1] = max(last[1], ctx_i2)
            last[2] = min(last[2], ctx_j1)
            last[3] = max(last[3], ctx_j2)
            last[4] |= set(changed)
        else:
            merged.append([ctx_i1, ctx_i2, ctx_j1, ctx_j2, set(changed)])
    return [tuple(m) for m in merged]
