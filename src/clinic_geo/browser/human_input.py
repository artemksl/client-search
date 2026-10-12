"""Shared prompt-splitting helpers for human-paced browser input."""

from __future__ import annotations

import re


_SENTENCE_END = re.compile(r"[.!?](?:[\"')\]]*)(?=\s|$)")


def split_typed_lead(text: str, *, single_sentence_max_chars: int = 42) -> tuple[str, str]:
    """Split a prompt into a typed lead and one append operation.

    When a prompt contains multiple sentences, the complete first sentence is
    typed. Snapshot discovery prompts are usually one sentence, so those use a
    short opening phrase instead; otherwise "type the first sentence" would
    still type the entire question and provide no speed benefit. Concatenating
    the returned values always reconstructs the original text byte-for-byte.
    """

    value = str(text or "")
    if not value:
        return "", ""

    for match in _SENTENCE_END.finditer(value):
        if match.end() < len(value):
            return value[: match.end()], value[match.end() :]

    limit = max(1, int(single_sentence_max_chars))
    if len(value) <= limit:
        return value, ""

    boundary = value.rfind(" ", 1, limit + 1)
    if boundary < max(1, limit // 2):
        boundary = limit
    return value[:boundary], value[boundary:]
