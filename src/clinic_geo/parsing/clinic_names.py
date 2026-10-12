"""Parse ChatGPT clinic-name extraction replies.

This is the single home for extraction-response parsing; `clinic_geo.browser.check_failures`
re-exports from here. Stdlib-only, with no browser, network, or pipeline imports.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


_BLOCK_RES = [
    re.compile(r"unusual activity has been detected", re.I),
    re.compile(r"verify you are human|are you a robot|checking your browser", re.I),
    re.compile(r"access denied|rate limit|too many requests", re.I),
    re.compile(r"you've reached your|message limit", re.I),
]


def looks_blocked(text):
    """Classify a captured reply as a diagnostic hint.

    Returns None (looks ok), "empty", "too_short", or "blocked". A parseable JSON object
    is still the final success criterion, matching the current extraction script.
    """
    if text is None:
        return "empty"
    t = text.strip()
    if not t:
        return "empty"
    if len(t) < 40:
        return "too_short"
    if any(r.search(t) for r in _BLOCK_RES):
        return "blocked"
    return None


def normalize_clinic_name(s):
    """Conservative clinic-name key: case, accents, and punctuation only.

    Removes NO words, so similar-but-distinct names stay distinct. This is the key the
    database matcher clusters on. Not to be confused with
    `clinic_geo.stats.share_of_voice.aggressive_match_key`, which additionally strips
    stopwords and would collapse "The London Clinic" to an empty string.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def iter_json_objects(text):
    """Yield top-level JSON object substrings using string-aware brace matching."""
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None


def parse_names(text):
    """Extract a `{id: [clinic names]}` object from ChatGPT's reply.

    Returns the dict on success, including valid empty lists, or None when no parseable
    object with list values exists.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else list(iter_json_objects(text))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and all(
            isinstance(values, list)
            and all(isinstance(name, str) for name in values)
            for values in obj.values()
        ):
            return obj
    return None


def dedupe_names(names):
    """Return non-empty clinic names once, preserving their first recommendation position."""
    seen, result = set(), []
    for value in names or []:
        if not isinstance(value, str):
            continue
        name = value.strip()
        key = normalize_clinic_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def names_for_prompt(prompt_id, parsed):
    """Pick this prompt's clinic list out of a `parse_names` result.

    ChatGPT usually keys the object by the prompt id, but when it returns a single-entry object
    under some other key that entry is still the answer to the one prompt we asked about.
    """
    key = str(prompt_id)
    if key in parsed:
        return dedupe_names(parsed[key])
    if len(parsed) == 1:
        return dedupe_names(next(iter(parsed.values())))
    return []


def load_failed_ids(failed_file):
    """Return de-duplicated failed extraction ids from current or legacy failure files."""
    failed_file = Path(failed_file)
    if not failed_file.exists():
        return []
    try:
        data = json.loads(failed_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("_failures", list(data.keys()))
    if not isinstance(data, list):
        return []
    seen, out = set(), []
    for x in data:
        s = str(x)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
