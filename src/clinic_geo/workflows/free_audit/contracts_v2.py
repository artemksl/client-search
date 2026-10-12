"""Versioned contracts for the Free Visibility Snapshot v2 workflow.

The values in this module are deliberately immutable inputs to hashing.  A
change to a prompt template or an evidence threshold is therefore a contract
change rather than a silent change to an in-flight Snapshot.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from clinic_geo.integrity import canonical_json, canonical_sha256


SCOPE_V2_VERSION = "2.0"
SCOPE_V4_VERSION = "4.0"
SCOPE_VERSION = SCOPE_V4_VERSION
LEGACY_SCOPE_V2_SCHEMA_VERSION = "free_snapshot.scope.v2"
LEGACY_SCOPE_V4_SCHEMA_VERSION = "free_snapshot.scope.v4"
SCOPE_V2_SCHEMA_VERSION = "free_snapshot.scope.v2.purpose.v1"
SCOPE_V4_SCHEMA_VERSION = "free_snapshot.scope.v4.purpose.v1"
SCOPE_SCHEMA_VERSION = SCOPE_V4_SCHEMA_VERSION
LEGACY_PROMPT_PACK_V2_SCHEMA_VERSION = "free_snapshot.prompts.v2"
LEGACY_PROMPT_PACK_V3_SCHEMA_VERSION = "free_snapshot.prompts.v3"
LEGACY_PROMPT_PACK_V4_SCHEMA_VERSION = "free_snapshot.prompts.v4"
PROMPT_PACK_V2_SCHEMA_VERSION = "free_snapshot.prompts.v2.purpose.v1"
PROMPT_PACK_V3_SCHEMA_VERSION = "free_snapshot.prompts.v3.purpose.v1"
PROMPT_PACK_V4_SCHEMA_VERSION = "free_snapshot.prompts.v4.purpose.v1"
PROMPT_PACK_SCHEMA_VERSION = PROMPT_PACK_V4_SCHEMA_VERSION
LEGACY_OBSERVATION_PLAN_V2_SCHEMA_VERSION = "free_snapshot.observation_plan.v2"
LEGACY_OBSERVATION_PLAN_V4_SCHEMA_VERSION = "free_snapshot.observation_plan.v4"
OBSERVATION_PLAN_V2_SCHEMA_VERSION = "free_snapshot.observation_plan.v2.purpose.v1"
OBSERVATION_PLAN_V4_SCHEMA_VERSION = "free_snapshot.observation_plan.v4.purpose.v1"
OBSERVATION_PLAN_SCHEMA_VERSION = OBSERVATION_PLAN_V4_SCHEMA_VERSION
ANALYSIS_V4_SCHEMA_VERSION = "free_snapshot.analysis.v4"
PRESENTATION_V4_SCHEMA_VERSION = "free_snapshot.presentation.v4"
PRESENTATION_SCHEMA_VERSION = "free_snapshot.presentation.v2"

PROMPT_PACK_V2_VERSION = "snapshot-prompts-v2"
PROMPT_PACK_V3_VERSION = "snapshot-prompts-v3"
PROMPT_PACK_V4_VERSION = "snapshot-prompts-v4"
PROMPT_PACK_VERSION = PROMPT_PACK_V4_VERSION
EVIDENCE_POLICY_V3_VERSION = "snapshot-evidence-v3"
EVIDENCE_POLICY_V4_VERSION = "snapshot-evidence-v4"
EVIDENCE_POLICY_VERSION = EVIDENCE_POLICY_V4_VERSION
COPY_POLICY_VERSION = "snapshot-copy-v1"
RECOMMENDATION_RUBRIC_VERSION = "recommendation-rubric-v1"
LINK_VERIFICATION_POLICY_VERSION = "snapshot-clinic-link-verification-v2"
LINK_VERIFICATION_SCHEMA_VERSION = "free_snapshot.clinic_link_verification.v1"
NAME_RECONCILIATION_POLICY_VERSION = "snapshot-name-reconciliation-v1"
NAME_RECONCILIATION_SCHEMA_VERSION = "free_snapshot.name_reconciliation.v1"

DEFAULT_PLATFORMS = (
    "chatgpt",
    "gemini",
    "google_ai_overview",
    "copilot",
)
BROWSER_MEASUREMENT_MODE = "browser_measured"
COPILOT_DERIVED_MEASUREMENT_MODE = "derived_floor_average_v1"
DEFAULT_PLANNED_RUNS = 3
TEST_MODE_PLANNED_RUNS = 1
MIN_SELECTED_AREAS = 1
MAX_SELECTED_AREAS = 2


class StringEnum(str, Enum):
    """A JSON-friendly string enum that works on Python 3.10."""

    def __str__(self) -> str:
        return self.value


class PromptIntent(StringEnum):
    LOCAL_RECOMMENDATION = "local_recommendation"
    REPUTABLE_SHORTLIST = "reputable_shortlist"
    FIRST_TIME_CONSIDERATION = "first_time_consideration"
    LANDMARK_SHORTLIST = "landmark_shortlist"
    QUALITY_COMPARISON = "quality_comparison"
    PRACTITIONER_EXPERTISE = "practitioner_expertise"
    CONSULTATION_CONFIDENCE = "consultation_confidence"


PROMPT_INTENTS_V2 = tuple(
    intent.value
    for intent in (
        PromptIntent.LOCAL_RECOMMENDATION,
        PromptIntent.REPUTABLE_SHORTLIST,
        PromptIntent.FIRST_TIME_CONSIDERATION,
        PromptIntent.LANDMARK_SHORTLIST,
        PromptIntent.QUALITY_COMPARISON,
    )
)
DEFAULT_PROMPT_INTENTS = tuple(intent.value for intent in PromptIntent)


class ObservationStatus(StringEnum):
    COLLECTED = "collected"
    NOT_SHOWN = "not_shown"
    EMPTY = "empty"
    TRANSIENT_ERROR = "transient_error"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class RecommendationClass(StringEnum):
    STRONG_RECOMMENDATION = "strong_recommendation"
    SHORTLISTED_POSITIVE = "shortlisted_positive"
    NEUTRAL_MENTION = "neutral_mention"
    CAVEATED_NEGATIVE = "caveated_negative"
    NOT_NAMED = "not_named"
    UNCLASSIFIABLE = "unclassifiable"


NAMED_RECOMMENDATION_CLASSES = frozenset(
    classification.value
    for classification in RecommendationClass
    if classification is not RecommendationClass.NOT_NAMED
)
QUALIFIED_RECOMMENDATION_CLASSES = frozenset(
    {
        RecommendationClass.STRONG_RECOMMENDATION.value,
        RecommendationClass.SHORTLISTED_POSITIVE.value,
    }
)


class FindingVerdict(StringEnum):
    MATERIAL_GAP_DEMONSTRATED = "material_gap_demonstrated"
    COMPETITIVE_OR_LEADING = "competitive_or_leading"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class OverallVerdict(StringEnum):
    MATERIAL_OPPORTUNITY = "Material opportunity demonstrated"
    MORE_EVIDENCE = "More evidence required"
    NO_STRONG_OPPORTUNITY = "No strong opportunity currently visible"


@dataclass(frozen=True)
class EvidencePolicy:
    """Release-gate and materiality thresholds for evidence policy v1."""

    version: str = EVIDENCE_POLICY_VERSION
    required_runs_per_prompt_family: int = 3
    min_complete_prompt_families: int = 4
    min_valid_observations: int = 12
    min_coverage: float = 0.80
    min_pattern_prompt_ids: int = 2
    min_pattern_runs: int = 2
    material_gap: float = 0.20
    material_ratio: float = 2.0
    material_min_competitor_rate: float = 0.25
    max_collection_attempts: int = 3

    def to_dict(self) -> dict:
        return asdict(self)


EVIDENCE_POLICY_V3 = EvidencePolicy(version=EVIDENCE_POLICY_V3_VERSION)
EVIDENCE_POLICY_V4 = EvidencePolicy(version=EVIDENCE_POLICY_V4_VERSION)
DEFAULT_EVIDENCE_POLICY = EVIDENCE_POLICY_V4


PROMPT_TEMPLATES_V2 = (
    MappingProxyType(
        {
            "sequence": 1,
            "intent": PromptIntent.LOCAL_RECOMMENDATION.value,
            "variant_type": "area",
            "template": "Which clinics would you recommend for {treatment} in {area}?",
        }
    ),
    MappingProxyType(
        {
            "sequence": 2,
            "intent": PromptIntent.REPUTABLE_SHORTLIST.value,
            "variant_type": "station",
            "template": "Give me a shortlist of reputable clinics for {treatment} near {station_or_area}.",
        }
    ),
    MappingProxyType(
        {
            "sequence": 3,
            "intent": PromptIntent.FIRST_TIME_CONSIDERATION.value,
            "variant_type": "postcode",
            "template": (
                "I live in {postcode_or_area} and am considering {treatment} for the first time. "
                "Which clinics should I consider?"
            ),
        }
    ),
    MappingProxyType(
        {
            "sequence": 4,
            "intent": PromptIntent.LANDMARK_SHORTLIST.value,
            "variant_type": "landmark",
            "template": (
                "Where should I go for {treatment} near {landmark_or_area}, and why would you "
                "shortlist those clinics?"
            ),
        }
    ),
    MappingProxyType(
        {
            "sequence": 5,
            "intent": PromptIntent.QUALITY_COMPARISON.value,
            "variant_type": "area",
            "template": (
                "Compare suitable clinics for {treatment} serving {area}. Which would you "
                "recommend based on quality and suitability?"
            ),
        }
    ),
)


PROMPT_TEMPLATES_V3 = PROMPT_TEMPLATES_V2 + (
    MappingProxyType(
        {
            "sequence": 6,
            "intent": PromptIntent.PRACTITIONER_EXPERTISE.value,
            "variant_type": "area",
            "template": (
                "Which clinics for {treatment} in {area} would you shortlist based on relevant "
                "practitioner expertise and experience?"
            ),
        }
    ),
    MappingProxyType(
        {
            "sequence": 7,
            "intent": PromptIntent.CONSULTATION_CONFIDENCE.value,
            "variant_type": "area",
            "template": (
                "Which clinics for {treatment} serving {area} would you recommend for a "
                "consultation, considering suitability assessment, transparency, and aftercare?"
            ),
        }
    ),
)
PROMPT_TEMPLATES = PROMPT_TEMPLATES_V3


# This is deliberately not one of the seven report-eligible prompt templates.
# It is sent only after the measured answer has been persisted, and its output
# may enrich competitor verification but can never change an observation,
# recommendation class, ranking, or denominator.
LINK_FOLLOWUP_TEMPLATE = (
    "Return JSON only. For every clinic named in your immediately preceding answer, "
    "provide the clinic name exactly as written and its official website links. "
    "Do not add clinics, revise the earlier answer, or use directories, social profiles, "
    "search-result pages, or booking marketplaces. Use null when you are not confident. "
    "Return exactly this shape, replacing the observation placeholder with the supplied value: "
    '{"observation_id":"{source_observation_id}","clinics":[{"clinic_name":"",'
    '"homepage_url":null,'
    '"location_url":null,"treatment_url":null}]}'
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PROMPT_CATALOG_PATH = _REPOSITORY_ROOT / "resources/free-audit/prompt_catalog.json"
PROMPT_GEOGRAPHY_PATH = _REPOSITORY_ROOT / "resources/free-audit/prompt-geography-v1.json"


def _read_prompt_catalog() -> Mapping:
    raw = json.loads(PROMPT_CATALOG_PATH.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "free_snapshot.prompt_content_spec.v1":
        raise RuntimeError("the v4 prompt catalog has an unsupported schema")
    if raw.get("runtime_enabled") is not False:
        raise RuntimeError("the research prompt catalog must remain runtime_enabled=false")
    content = {
        key: raw[key]
        for key in ("platforms", "rendering_policy", "baskets", "slots")
    }
    if canonical_sha256(content) != raw.get("content_sha256"):
        raise RuntimeError("the v4 prompt catalog content hash does not match its declaration")
    slots = raw.get("slots") or []
    platforms = tuple(raw.get("platforms") or [])
    if len(slots) != 21 or len(platforms) != 4:
        raise RuntimeError("the v4 prompt catalog must contain 21 slots and four platforms")
    basket_counts = {
        basket_id: sum(row.get("basket_id") == basket_id for row in slots)
        for basket_id in ("treatment_local", "treatment_expanded", "clinic_archetype")
    }
    if set(basket_counts.values()) != {7}:
        raise RuntimeError("the v4 prompt catalog must contain seven slots in each basket")
    for slot in slots:
        if set((slot.get("renderings") or {}).keys()) != set(platforms):
            raise RuntimeError(f"v4 slot {slot.get('id')} lacks an exact platform rendering")
    return _deep_freeze(raw)


def _read_prompt_geography() -> Mapping:
    raw = json.loads(PROMPT_GEOGRAPHY_PATH.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "free_snapshot.prompt_geography.v1":
        raise RuntimeError("the prompt geography taxonomy has an unsupported schema")
    if "Patient-facing geography choices" not in str(raw.get("purpose") or ""):
        raise RuntimeError("the prompt geography taxonomy must remain patient-facing only")
    return _deep_freeze(raw)


PROMPT_CATALOG_V4 = _read_prompt_catalog()
PROMPT_CATALOG_V4_VERSION = str(PROMPT_CATALOG_V4["catalog_version"])
PROMPT_CATALOG_V4_CONTENT_HASH = str(PROMPT_CATALOG_V4["content_sha256"])
PROMPT_GEOGRAPHY_V1 = _read_prompt_geography()
PROMPT_GEOGRAPHY_VERSION = str(PROMPT_GEOGRAPHY_V1["version"])
PROMPT_GEOGRAPHY_HASH = canonical_sha256(PROMPT_GEOGRAPHY_V1)
PROMPT_INTENTS_V4 = tuple(str(row["semantic_intent"]) for row in PROMPT_CATALOG_V4["slots"])


PROMPT_TEMPLATE_ARTIFACT_V2 = MappingProxyType(
    {
        "version": PROMPT_PACK_V2_VERSION,
        "prompt_intents": PROMPT_INTENTS_V2,
        "templates": PROMPT_TEMPLATES_V2,
    }
)
PROMPT_TEMPLATE_ARTIFACT_HASH_V2 = canonical_sha256(PROMPT_TEMPLATE_ARTIFACT_V2)

PROMPT_TEMPLATE_ARTIFACT_V3 = MappingProxyType(
    {
        "version": PROMPT_PACK_V3_VERSION,
        "prompt_intents": DEFAULT_PROMPT_INTENTS,
        "templates": PROMPT_TEMPLATES_V3,
    }
)
PROMPT_TEMPLATE_ARTIFACT_HASH_V3 = canonical_sha256(PROMPT_TEMPLATE_ARTIFACT_V3)

PROMPT_TEMPLATE_ARTIFACT_V4 = MappingProxyType(
    {
        "version": PROMPT_PACK_V4_VERSION,
        # The template artifact describes prompt semantics, which did not
        # change when the surrounding prompt-pack envelope became purpose
        # bound.  Retain its historical schema byte so its content hash and
        # every already-frozen scope remain stable.
        "schema_version": LEGACY_PROMPT_PACK_V4_SCHEMA_VERSION,
        "runtime_contract_version": "snapshot-prompts-v4-runtime-v1",
        "catalog_version": PROMPT_CATALOG_V4_VERSION,
        "catalog_content_sha256": PROMPT_CATALOG_V4_CONTENT_HASH,
        "platforms": PROMPT_CATALOG_V4["platforms"],
        "baskets": PROMPT_CATALOG_V4["baskets"],
        "slots": PROMPT_CATALOG_V4["slots"],
    }
)
PROMPT_TEMPLATE_ARTIFACT_HASH_V4 = canonical_sha256(PROMPT_TEMPLATE_ARTIFACT_V4)

PROMPT_TEMPLATE_ARTIFACT = PROMPT_TEMPLATE_ARTIFACT_V4
PROMPT_TEMPLATE_ARTIFACT_HASH = PROMPT_TEMPLATE_ARTIFACT_HASH_V4

EVIDENCE_POLICY_ARTIFACT_V3 = MappingProxyType(EVIDENCE_POLICY_V3.to_dict())
EVIDENCE_POLICY_ARTIFACT_V4 = MappingProxyType(EVIDENCE_POLICY_V4.to_dict())
EVIDENCE_POLICY_ARTIFACT_HASH_V3 = canonical_sha256(EVIDENCE_POLICY_ARTIFACT_V3)
EVIDENCE_POLICY_ARTIFACT_HASH_V4 = canonical_sha256(EVIDENCE_POLICY_ARTIFACT_V4)
EVIDENCE_POLICY_ARTIFACT = EVIDENCE_POLICY_ARTIFACT_V4
EVIDENCE_POLICY_ARTIFACT_HASH = EVIDENCE_POLICY_ARTIFACT_HASH_V4

PROMPT_PACK_CONTRACTS = MappingProxyType(
    {
        PROMPT_PACK_V2_VERSION: MappingProxyType(
            {
                "schema_version": PROMPT_PACK_V2_SCHEMA_VERSION,
                "prompt_intents": PROMPT_INTENTS_V2,
                "templates": PROMPT_TEMPLATES_V2,
                "template_artifact_hash": PROMPT_TEMPLATE_ARTIFACT_HASH_V2,
                "scope_version": SCOPE_V2_VERSION,
                "scope_schema_version": SCOPE_V2_SCHEMA_VERSION,
                "legacy_scope_schema_versions": (LEGACY_SCOPE_V2_SCHEMA_VERSION,),
                "legacy_schema_versions": (LEGACY_PROMPT_PACK_V2_SCHEMA_VERSION,),
                "observation_plan_schema_version": OBSERVATION_PLAN_V2_SCHEMA_VERSION,
                "legacy_observation_plan_schema_versions": (
                    LEGACY_OBSERVATION_PLAN_V2_SCHEMA_VERSION,
                ),
                "evidence_policy_version": EVIDENCE_POLICY_V3_VERSION,
                "evidence_policy_artifact_hash": EVIDENCE_POLICY_ARTIFACT_HASH_V3,
            }
        ),
        PROMPT_PACK_V3_VERSION: MappingProxyType(
            {
                "schema_version": PROMPT_PACK_V3_SCHEMA_VERSION,
                "prompt_intents": DEFAULT_PROMPT_INTENTS,
                "templates": PROMPT_TEMPLATES_V3,
                "template_artifact_hash": PROMPT_TEMPLATE_ARTIFACT_HASH_V3,
                "scope_version": SCOPE_V2_VERSION,
                "scope_schema_version": SCOPE_V2_SCHEMA_VERSION,
                "legacy_scope_schema_versions": (LEGACY_SCOPE_V2_SCHEMA_VERSION,),
                "legacy_schema_versions": (LEGACY_PROMPT_PACK_V3_SCHEMA_VERSION,),
                "observation_plan_schema_version": OBSERVATION_PLAN_V2_SCHEMA_VERSION,
                "legacy_observation_plan_schema_versions": (
                    LEGACY_OBSERVATION_PLAN_V2_SCHEMA_VERSION,
                ),
                "evidence_policy_version": EVIDENCE_POLICY_V3_VERSION,
                "evidence_policy_artifact_hash": EVIDENCE_POLICY_ARTIFACT_HASH_V3,
            }
        ),
        PROMPT_PACK_V4_VERSION: MappingProxyType(
            {
                "schema_version": PROMPT_PACK_V4_SCHEMA_VERSION,
                "prompt_intents": PROMPT_INTENTS_V4,
                "templates": (),
                "slots": PROMPT_CATALOG_V4["slots"],
                "baskets": PROMPT_CATALOG_V4["baskets"],
                "platforms": PROMPT_CATALOG_V4["platforms"],
                "catalog_version": PROMPT_CATALOG_V4_VERSION,
                "catalog_content_sha256": PROMPT_CATALOG_V4_CONTENT_HASH,
                "template_artifact_hash": PROMPT_TEMPLATE_ARTIFACT_HASH_V4,
                "scope_version": SCOPE_V4_VERSION,
                "scope_schema_version": SCOPE_V4_SCHEMA_VERSION,
                "legacy_scope_schema_versions": (LEGACY_SCOPE_V4_SCHEMA_VERSION,),
                "legacy_schema_versions": (LEGACY_PROMPT_PACK_V4_SCHEMA_VERSION,),
                "observation_plan_schema_version": OBSERVATION_PLAN_V4_SCHEMA_VERSION,
                "legacy_observation_plan_schema_versions": (
                    LEGACY_OBSERVATION_PLAN_V4_SCHEMA_VERSION,
                ),
                "evidence_policy_version": EVIDENCE_POLICY_V4_VERSION,
                "evidence_policy_artifact_hash": EVIDENCE_POLICY_ARTIFACT_HASH_V4,
            }
        ),
    }
)

LINK_VERIFICATION_ARTIFACT = MappingProxyType(
    {
        "version": LINK_VERIFICATION_POLICY_VERSION,
        "schema_version": LINK_VERIFICATION_SCHEMA_VERSION,
        "measurement_role": "competitor_verification",
        "report_eligible": False,
        "template": LINK_FOLLOWUP_TEMPLATE,
        "conversational_platforms": ("chatgpt", "gemini", "copilot"),
        "non_conversational_platforms": ("google_ai_overview",),
        "google_ai_overview_fallback": "captured_citations",
    }
)
LINK_VERIFICATION_ARTIFACT_HASH = canonical_sha256(LINK_VERIFICATION_ARTIFACT)

# This replaces conversational link follow-ups for new snapshots. The actual
# prompt includes a frozen run's occurrence IDs, while these immutable rules
# define what the platform is and is not allowed to decide.
NAME_RECONCILIATION_ARTIFACT = MappingProxyType(
    {
        "version": NAME_RECONCILIATION_POLICY_VERSION,
        "schema_version": NAME_RECONCILIATION_SCHEMA_VERSION,
        "measurement_role": "name_variant_reconciliation",
        "report_eligible": False,
        "source_name_policy": "exact_raw_answer_spelling",
        "basic_normalization": "case_accents_ampersand_punctuation_whitespace",
        "scoring_unit": "distinct_source_answer",
        "conversational_platforms": ("chatgpt", "gemini", "copilot"),
        "non_conversational_platforms": ("google_ai_overview",),
        "google_ai_overview_fallback": "deterministic_basic_normalization",
        "interaction_contract": "one_prompt_in_a_fresh_chat",
        "model_authority": "closed_set_alias_partition_only",
        "count_authority": "deterministic_local_recomputation",
        "uncertain_variant_policy": "keep_separate",
    }
)
NAME_RECONCILIATION_ARTIFACT_HASH = canonical_sha256(NAME_RECONCILIATION_ARTIFACT)
