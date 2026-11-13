"""The "random fruit replacement" demo/novelty action.

Heuristic (deliberately simple, per the mission brief — "no need for
heavy NLP"):

- Tokenize on whitespace, keep punctuation attached but strip it for the
  "is this word suitable" check.
- Skip: stopwords, words shorter than MIN_WORD_LEN, words that look like
  proper nouns (capitalized and not the first word of a sentence — a
  cheap, no-dependency proper-noun signal), and words that are purely
  numeric/punctuation.
- From the remaining "suitable" candidates, pick 1-2 at random and
  replace each with a random fruit name from a fixed list, preserving the
  original word's capitalization style (Title/UPPER/lower) and any
  trailing punctuation so the sentence still reads naturally.
"""
from __future__ import annotations

import random
import re
import string

FRUITS = [
    "apple", "banana", "cherry", "mango", "papaya", "kiwi", "pineapple",
    "blueberry", "raspberry", "grape", "peach", "plum", "apricot",
    "coconut", "lychee", "guava", "fig", "date", "pomegranate",
    "watermelon", "cantaloupe", "nectarine", "tangerine", "persimmon",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "because",
    "of", "to", "in", "on", "at", "by", "for", "with", "about", "as",
    "is", "am", "are", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their", "this",
    "that", "these", "those", "there", "here", "what", "which", "who",
    "whom", "when", "where", "why", "how", "do", "does", "did", "have",
    "has", "had", "not", "no", "yes", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must", "than", "too", "very",
    "just", "also", "up", "out", "off", "over", "under", "again",
    "further", "once", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "from",
    "into", "through", "during", "before", "after", "above", "below",
    "between",
}

MIN_WORD_LEN = 4

_WORD_RE = re.compile(r"[A-Za-z']+")


def _strip_punct(token: str) -> str:
    return token.strip(string.punctuation)


def _looks_proper_noun(word: str, is_sentence_start: bool) -> bool:
    core = _strip_punct(word)
    if not core:
        return False
    return core[0].isupper() and not is_sentence_start and not core.isupper()


def _apply_case_style(fruit: str, original_core: str) -> str:
    if original_core.isupper():
        return fruit.upper()
    if original_core[:1].isupper():
        return fruit.capitalize()
    return fruit


def find_suitable_word_indices(text: str) -> list[int]:
    """Return indices (into text.split()) of words judged "suitable" for
    fruit replacement, per the heuristic described above."""
    tokens = text.split()
    suitable = []
    sentence_start = True
    for idx, tok in enumerate(tokens):
        core = _strip_punct(tok)
        if not core or not _WORD_RE.fullmatch(core):
            sentence_start = tok.endswith((".", "!", "?"))
            continue
        core_lower = core.lower()
        is_start = sentence_start
        sentence_start = tok.endswith((".", "!", "?"))
        if len(core) < MIN_WORD_LEN:
            continue
        if core_lower in STOPWORDS:
            continue
        if _looks_proper_noun(tok, is_start):
            continue
        suitable.append(idx)
    return suitable


def apply_random_fruit_replacement(
    text: str, rng: random.Random | None = None, max_replacements: int = 2
) -> dict:
    """Pick 1-2 suitable words and replace with random fruit names.

    Returns a dict: {
      "text": <new text>,
      "replacements": [{"index": int, "original": str, "replacement": str}],
    }
    If there are no suitable candidates, returns the text unchanged with
    an empty replacements list (caller should surface that to the user).
    """
    rng = rng or random.Random()
    tokens = text.split()
    candidates = find_suitable_word_indices(text)
    if not candidates:
        return {"text": text, "replacements": []}

    n = min(max_replacements, len(candidates), rng.choice([1, 2]))
    n = max(1, n)
    chosen = sorted(rng.sample(candidates, n))

    replacements = []
    used_fruits: set[str] = set()
    for idx in chosen:
        original = tokens[idx]
        core = _strip_punct(original)
        trailing = original[len(original.rstrip(string.punctuation)):]
        leading = original[: len(original) - len(original.lstrip(string.punctuation))]
        available = [fr for fr in FRUITS if fr not in used_fruits] or FRUITS
        fruit = rng.choice(available)
        used_fruits.add(fruit)
        styled = _apply_case_style(fruit, core)
        new_token = f"{leading}{styled}{trailing}"
        tokens[idx] = new_token
        replacements.append({"index": idx, "original": original, "replacement": new_token})

    return {"text": " ".join(tokens), "replacements": replacements}
