"""Exact, deterministic matching against one pinned treatment release."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .catalog import TreatmentCatalogue


_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def treatment_match_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", text)).strip()


class DeterministicMatchState(str, Enum):
    MATCHED = "matched"
    REVIEW_REQUIRED = "review_required"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class DeterministicTreatmentMatch:
    original_term: str
    normalized_key: str
    state: DeterministicMatchState
    target_concept_ids: tuple[str, ...] = ()
    target_search_intent_ids: tuple[str, ...] = ()
    matched_term_ids: tuple[str, ...] = ()
    campaign_selectable: bool = False
    recommendation_prompt_eligible: bool = False

    @property
    def is_deterministically_authoritative(self) -> bool:
        return self.state is DeterministicMatchState.MATCHED


class TreatmentMatcher:
    def __init__(self, catalogue: TreatmentCatalogue) -> None:
        self.catalogue = catalogue
        self._terms: dict[str, list] = {}
        self._intents: dict[str, list] = {}
        for item in catalogue.terms:
            self._terms.setdefault(treatment_match_key(item.exact_term), []).append(item)
        for item in catalogue.patient_search_intents:
            self._intents.setdefault(
                treatment_match_key(item.exact_patient_phrase), []
            ).append(item)

    def match(self, value: object) -> DeterministicTreatmentMatch:
        original = str(value or "").strip()
        key = treatment_match_key(original)
        if not key:
            return DeterministicTreatmentMatch(
                original_term=original,
                normalized_key=key,
                state=DeterministicMatchState.NO_MATCH,
            )

        terms = self._terms.get(key, [])
        intents = self._intents.get(key, [])
        candidates = [*terms, *intents]
        if not candidates:
            return DeterministicTreatmentMatch(
                original_term=original,
                normalized_key=key,
                state=DeterministicMatchState.NO_MATCH,
            )
        if any(not item.is_deterministically_authoritative for item in candidates):
            return DeterministicTreatmentMatch(
                original_term=original,
                normalized_key=key,
                state=DeterministicMatchState.REVIEW_REQUIRED,
                matched_term_ids=tuple(sorted(item.term_id for item in terms)),
            )

        intent_ids = tuple(sorted(item.patient_search_intent_id for item in intents))
        if len(intent_ids) > 1:
            return DeterministicTreatmentMatch(
                original_term=original,
                normalized_key=key,
                state=DeterministicMatchState.AMBIGUOUS,
                target_search_intent_ids=intent_ids,
                matched_term_ids=tuple(sorted(item.term_id for item in terms)),
            )

        concept_sets = {
            tuple(sorted(item.target_concept_ids))
            for item in candidates
            if item.target_concept_ids
        }
        if len(concept_sets) > 1:
            return DeterministicTreatmentMatch(
                original_term=original,
                normalized_key=key,
                state=DeterministicMatchState.AMBIGUOUS,
                target_search_intent_ids=intent_ids,
                matched_term_ids=tuple(sorted(item.term_id for item in terms)),
            )
        concept_ids = next(iter(concept_sets), ())
        intent = intents[0] if intents else None
        return DeterministicTreatmentMatch(
            original_term=original,
            normalized_key=key,
            state=DeterministicMatchState.MATCHED,
            target_concept_ids=concept_ids,
            target_search_intent_ids=intent_ids,
            matched_term_ids=tuple(sorted(item.term_id for item in terms)),
            campaign_selectable=bool(intent and intent.is_campaign_selectable),
            recommendation_prompt_eligible=bool(
                intent and intent.is_recommendation_prompt_eligible
            ),
        )


__all__ = [
    "DeterministicMatchState",
    "DeterministicTreatmentMatch",
    "TreatmentMatcher",
    "treatment_match_key",
]
