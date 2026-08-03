"""Tailoring layer — truthfulness guards (tailoring_build.md WP2).

Two deterministic, no-I/O checks that ignore phrasing entirely and only
ever flag a CLAIM: a skill attribution the source item never authorised, or
a specific (numeral / named entity) absent from the source text
(tailoring.md §4). Both return a list of violation strings — empty means
the check passed — rather than raising, so `tailor()` (WP4) can collect
every violation from a single item before deciding to fail the job.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from profile.schema import ProfileItem, Skill
from tailoring.schema import TailoredItem

# ==========================================================================
# Skill-subset guard (invariant 2)
# ==========================================================================


def _detect_skills_by_surface_match(text: str, skills: list[Skill]) -> set[str]:
    """Default detector: a skill is 'detected' if any of its surface
    spellings (label + aliases) appears in the text as a whole word/phrase,
    case-insensitively. Word-boundary matching, not raw substring — a plain
    `in` check would match a short surface like 'C' or 'ROS' inside
    'pro**c**urement' or 'c**ros**s-functional', which a real skill scanner
    wouldn't. Deliberately simple and dependency-free otherwise — the
    `resume-ats-optimizer` domain-knowledge prompt constrains the LLM's
    vocabulary; this only has to catch the surface strings that vocabulary
    actually produces."""
    lowered = text.lower()
    detected = set()
    for skill in skills:
        for surface in skill.surfaces():
            pattern = r"\b" + re.escape(surface.lower()) + r"\b"
            if re.search(pattern, lowered):
                detected.add(skill.id)
                break
    return detected


def _get_skill_detector() -> Callable[[str, list[Skill]], set[str]]:
    """Sole construction point for the skill detector. Tests patch THIS,
    never the detection function directly, so `check_skill_subset` never
    needs to know whether it's talking to the real or a fake detector."""
    return _detect_skills_by_surface_match


def check_skill_subset(
    tailored_item: TailoredItem, source_item: ProfileItem, skills: list[Skill]
) -> list[str]:
    """Skill terms detected in `tailored_item`'s text must be a subset of
    `source_item.demonstrated_skills` (tailoring.md §3). Returns the sorted
    list of unauthorized skill ids — empty means the item passes."""
    detector = _get_skill_detector()
    text = " ".join(tailored_item.bullets)
    detected = detector(text, skills)
    authorized = set(source_item.demonstrated_skills)
    return sorted(detected - authorized)


# ==========================================================================
# No-new-specifics guard (invariant 3)
# ==========================================================================
# A known soft edge (tailoring.md §4): this catches new numerals and new
# named entities, not every possible fabricated specific. It is a light
# diff, not a formal proof.

_WORD_RE = re.compile(r"[A-Za-z]+")
_TOKEN_RE = re.compile(r"\w+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d(?:,?\d+)*(?![A-Za-z0-9])")  # "5", "2022",
# "20,000" as one unit — but NOT the "32" in "STM32": a digit run glued
# directly onto a letter (on EITHER side, hence lookbehind/lookahead both
# excluding letters/digits) is part of a model/part name (STM32, ARM7,
# RS232), not a quantity claim, so a punctuation change nearby must not
# make it look like a fabricated number.
_CLAUSE_BREAK_CHARS = (",", ";", ":")

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "by", "with", "for", "to", "and",
})


def _backward_anchor(text: str, match_start: int) -> str | None:
    """Walk backward word-by-word from `match_start`, skipping `_STOPWORDS`,
    and return the first content word (lowercased). Returns `None` if the
    numeral opens the text, or only stopwords precede it — same fallback as
    the bare-presence check for the clause-break case."""
    for word in reversed(_WORD_RE.findall(text[:match_start])):
        lowered = word.lower()
        if lowered not in _STOPWORDS:
            return lowered
    return None


def _numeral_contexts(text: str) -> set[tuple[str, str | None]]:
    """(numeral, next-word) pairs — anchoring a numeral to its neighbour
    catches it being reused in a NEW context (TC-GUARD-06: '5 engineers' ->
    '5 clients'), not just a bare digit that happens to also appear
    somewhere else in the source.

    But when a comma/semicolon/colon immediately follows the numeral (e.g.
    'S$20,000, ensuring...'), the next word starts a NEW CLAUSE — it is not
    what the numeral quantifies, unlike the direct-adjacency case ('5
    engineers'). Anchoring to the following word there would flag a plain
    synonym swap of that clause ('ensuring' -> 'maintaining') as if the
    number's own meaning had changed. Instead, anchor BACKWARD to the
    nearest preceding content word (still real context, just on the other
    side) — a `<-` prefix keeps these pairs from colliding with forward
    `(numeral, next_word)` pairs that happen to share the same word string.
    Falls back to bare presence (`None`) only when no content word precedes
    the numeral at all.
    """
    contexts: set[tuple[str, str | None]] = set()
    for match in _NUMBER_RE.finditer(text):
        rest = text[match.end():].lstrip()
        if rest and rest[0] in _CLAUSE_BREAK_CHARS:
            anchor = _backward_anchor(text, match.start())
            contexts.add((match.group(), f"<-{anchor}" if anchor else None))
            continue
        next_word = _WORD_RE.match(rest) or _TOKEN_RE.match(rest)
        contexts.add((match.group(), next_word.group().lower() if next_word else None))
    return contexts


def _new_numerals(tailored_text: str, source_text: str) -> list[str]:
    source_contexts = _numeral_contexts(source_text)
    source_numerals = {num for num, _ in source_contexts}

    new = []
    for num, next_word in _numeral_contexts(tailored_text):
        if next_word is None:
            is_new = num not in source_numerals
        else:
            is_new = (num, next_word) not in source_contexts
        if is_new:
            new.append((num, next_word))

    return sorted(f"new numeral: {num} {nxt or ''}".rstrip() for num, nxt in new)


def _new_named_entities(tailored_text: str, source_text: str) -> list[str]:
    """Capitalized tokens (excluding each SENTENCE's initial word, which is
    capitalized purely by position — `tailored_text` is normally several
    bullets joined together, so this must exempt every bullet's opening
    word, not just the very first one) not related to any source token by
    substring containment either way — a cheap stand-in for spelling
    variants of the same entity (e.g. 'Postgres' / 'PostgreSQL')."""
    source_words_lower = [w.lower() for w in _WORD_RE.findall(source_text)]

    violations = []
    for sentence in _SENTENCE_SPLIT_RE.split(tailored_text):
        for i, word in enumerate(_WORD_RE.findall(sentence)):
            if i == 0 or not (word[0].isupper() and len(word) > 1):
                continue
            lowered = word.lower()
            if not any(lowered in src or src in lowered for src in source_words_lower):
                violations.append(f"new named entity: {word}")
    return violations


def check_no_new_specifics(tailored_text: str, source_text: str) -> list[str]:
    """No numeral or named entity may appear in `tailored_text` that is
    absent from `source_text` (tailoring.md §4). Returns the list of
    violation descriptions — empty means the text passes."""
    return [
        *_new_numerals(tailored_text, source_text),
        *_new_named_entities(tailored_text, source_text),
    ]
