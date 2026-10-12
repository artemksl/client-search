"""Normalize configured treatments and tag prompts that explicitly mention them.

Treatment reporting must not infer a clinic's service mix from every procedure in
the basket.  The configured flagship treatments are the reporting dimensions;
this module only decides whether a prompt is explicitly about one or more of
those dimensions.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence


_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

# These are true patient-language aliases, not broader treatment categories.
# In particular, HIFU/RF are not silently folded into "skin tightening" and
# generic "filler" is not folded into every filler procedure.
_ALIASES = {
    # Fat freezing is patient wording for the cryolipolysis concept.  The
    # CoolSculpting brand keeps its own exact search intent and must not be
    # silently folded into this generic class.
    "fat freezing": ("cryolipolysis",),
    "coolsculpting": ("cool sculpting",),
    "laser hair removal": ("laser hair reduction", "laser epilation"),
    "non surgical nose job": (
        "non surgical rhinoplasty",
        "liquid rhinoplasty",
        "liquid nose job",
    ),
    "prp treatment": ("platelet rich plasma treatment", "prp therapy"),
    "rf microneedling": ("radiofrequency microneedling", "radio frequency microneedling"),
    "ipl photofacial": ("intense pulsed light photofacial", "ipl facial"),
    "pdo thread lift": ("pdo threadlift", "pdo threads"),
    "led light therapy": ("led therapy", "led facial"),
    "morpheus8": ("morpheus 8",),
}


def treatment_key(value: object) -> str:
    """Return a stable comparison key for a patient-language treatment name."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", text)).strip()


def configured_treatments(config: Mapping, procedures: Mapping | None = None) -> list[str]:
    """Resolve the three configured treatment slots, using workflow defaults.

    Blank and duplicate values are removed while preserving slot order.  This is
    intentionally the same fallback rule as the prompt builder.
    """
    defaults = (procedures or {}).get("defaults") or {}
    labels: list[str] = []
    seen: set[str] = set()
    for index in range(1, 4):
        value = str(config.get(f"treatment_{index}") or "").strip()
        if not value:
            value = str(defaults.get(f"CLIENT_TREATMENT_{index}") or "").strip()
        key = treatment_key(value)
        if value and key and key not in seen:
            seen.add(key)
            labels.append(value)
    return labels


def _forms(label: str) -> set[str]:
    key = treatment_key(label)
    forms = {key, *(_ALIASES.get(key) or ())}
    # Treat the alias groups as equivalence classes in both directions.  This
    # lets a clinic configured as "cryolipolysis" reuse basket prompts tagged
    # with the patient-language family "fat freezing", and vice versa.  Brand
    # groups remain separate from their broader treatment class.
    for canonical, aliases in _ALIASES.items():
        alias_keys = {treatment_key(value) for value in aliases}
        if key == canonical or key in alias_keys:
            forms.update({canonical, *aliases})
    normalized = {treatment_key(form) for form in forms if treatment_key(form)}

    # Common singular/plural wording should not create a different treatment.
    for form in tuple(normalized):
        words = form.split()
        if not words:
            continue
        last = words[-1]
        if last.endswith("ies") and len(last) > 3:
            normalized.add(" ".join([*words[:-1], last[:-3] + "y"]))
        elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
            normalized.add(" ".join([*words[:-1], last[:-1]]))
        elif len(last) > 3:
            normalized.add(" ".join([*words[:-1], last + "s"]))
    return normalized


def tag_prompt_treatments(text: object, treatments: Sequence[str]) -> list[str]:
    """Return configured treatment labels explicitly represented in ``text``.

    A prompt can map to more than one treatment (for example, a direct
    comparison).  The output follows configured treatment order.
    """
    normalized_text = f" {treatment_key(text)} "
    tagged: list[str] = []
    seen: set[str] = set()
    for label in treatments:
        key = treatment_key(label)
        if not key or key in seen:
            continue
        if any(f" {form} " in normalized_text for form in _forms(label)):
            seen.add(key)
            tagged.append(str(label).strip())
    return tagged


def prompt_treatment_tags(prompt: Mapping, treatments: Sequence[str]) -> list[str]:
    """Read explicit prompt tags, with text inference for historical packs."""
    explicit = prompt.get("treatments")
    if isinstance(explicit, list):
        return [
            label
            for label in treatments
            if any(tag_prompt_treatments(value, [label]) for value in explicit)
        ]
    return tag_prompt_treatments(prompt.get("text") or prompt.get("prompt") or "", treatments)
