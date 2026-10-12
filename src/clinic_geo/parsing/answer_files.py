"""Parse saved prompt-answer Markdown files.

The current browser engine writes one `NNN_<safe_prompt_id>.md` per prompt. These helpers
mirror the pure parsing parts of `clinic_geo.browser.extract_names` and
`clinic_geo.browser.check_failures` without file-system side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


FILENAME_RE = re.compile(r"^(\d{3})_.*\.md$")
FAILURE_MARKERS = (
    "Unusual activity has been detected",
    "Something went wrong",
)


@dataclass(frozen=True)
class ParsedAnswerMarkdown:
    prompt_id: str
    answer: str
    has_prompt_id_header: bool


def merge_sort_key(path):
    """Sort by leading numeric filename index, then by stem, matching the merge script."""
    path = Path(path)
    m = re.match(r"(\d+)", path.stem)
    return (0, int(m.group(1))) if m else (1, path.stem)


def parse_answer_markdown(text: str, fallback_id: str) -> ParsedAnswerMarkdown:
    """Return prompt id and answer body from one saved `.md` answer file."""
    m_id = re.search(r"^#\s*Prompt ID:\s*(.+)$", text, re.MULTILINE)
    prompt_id = m_id.group(1).strip() if m_id else fallback_id
    m_ans = re.search(r"^##\s*Answer\s*$", text, re.MULTILINE)
    answer = text[m_ans.end():].strip() if m_ans else ""
    return ParsedAnswerMarkdown(prompt_id, answer, bool(m_id))


def answer_body(text: str) -> str:
    """Return text under the `## Answer` heading, or an empty string when absent.

    Use this when a missing heading means "no answer" — the failure classifier and the
    fill/retry loop both depend on a marker-less file reading as empty.
    """
    _, _, after = text.partition("## Answer")
    return after.strip()


def answer_body_or_all(text: str) -> str:
    """Return text under the `## Answer` heading, falling back to the whole file when the
    heading is ABSENT. Use this where a heading-less file should still be salvaged.

    The fallback gates on heading PRESENCE, not on truthiness. A file that HAS the heading but an
    empty body must return "" — it is an empty answer. Falling back to the whole file there would
    hand back the prompt that every answer file echoes under `## Prompt`, and callers would parse
    that as if it were the answer: `merge_scores` would harvest the prompt's embedded database rows
    as fresh scores, `area_lib` would scrape its "uid | name | address" lines as area assignments.
    """
    if "## Answer" not in (text or ""):
        return (text or "").strip()
    return answer_body(text)


def classify_answer_text(text: str) -> str:
    """Classify saved answer text as `ok`, `empty`, or `blocked` without deleting files."""
    body = answer_body(text)
    if not body:
        return "empty"
    low = body.lower()
    if any(marker.lower() in low for marker in FAILURE_MARKERS):
        return "blocked"
    return "ok"
