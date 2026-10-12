"""Build and validate clinic-specific prospect visibility evidence.

This module is intentionally fail-closed.  It never assumes that the existing
rotating Chrome pool is logged out: every check is bound to a recently audited,
clean ephemeral guest profiles.  It prepares a deterministic fifteen-prompt basket
and repeats it in two independent logged-out runs, records all thirty raw outputs,
requires a human conclusion, and signs the complete artifact with SHA-256 hashes.

The module does not drive a browser.  That separation makes it impossible for a
caller to bypass the profile proof simply by selecting a browser regime whose
login state is unknown.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from clinic_geo.evidence.purpose import (
    EvidencePurpose,
    legacy_purpose_view,
    require_evidence_purpose,
)
from clinic_geo.paths import repo_root
from clinic_geo.parsing.answer_files import FAILURE_MARKERS as CANONICAL_FAILURE_MARKERS


SCHEMA_VERSION = "3.0"
PURPOSE_LEGACY_SCHEMA_VERSION = "2.0"
LEGACY_SCHEMA_VERSION = "1.0"
PROFILE_CONFIG_SCHEMA_VERSION = "1.0"
PROFILE_PROOF_MAX_AGE = timedelta(hours=2)
EVIDENCE_VALIDITY = timedelta(days=30)

VISIBILITY_CONTRACT_ID = "prospect_visibility_30_v1"
OUTREACH_EVIDENCE_PURPOSE = EvidencePurpose.OUTREACH_GAP_CONFIRMATION.value
RUN_IDS = ("run-a", "run-b")
DISCOVERY_TEMPLATE_IDS = tuple(f"discovery-{index:02d}" for index in range(1, 14))
VERIFICATION_TEMPLATE_IDS = ("clinic-verification-01", "clinic-verification-02")
TEMPLATE_PROMPT_IDS = (*DISCOVERY_TEMPLATE_IDS, *VERIFICATION_TEMPLATE_IDS)


def observation_id(run_id: str, template_id: str) -> str:
    """Return the stable identifier for one target-platform request."""

    return f"{run_id}--{template_id}"


def prompt_ids_for_run(run_id: str) -> tuple[str, ...]:
    if run_id not in RUN_IDS:
        raise VisibilityEvidenceError(f"unknown visibility run_id: {run_id}")
    return tuple(observation_id(run_id, template_id) for template_id in TEMPLATE_PROMPT_IDS)


DISCOVERY_PROMPT_IDS = tuple(
    observation_id(run_id, template_id)
    for run_id in RUN_IDS
    for template_id in DISCOVERY_TEMPLATE_IDS
)
VERIFICATION_PROMPT_IDS = tuple(
    observation_id(run_id, template_id)
    for run_id in RUN_IDS
    for template_id in VERIFICATION_TEMPLATE_IDS
)
# Kept as a source-compatibility alias for callers that imported the old singular
# name.  New code must use VERIFICATION_PROMPT_IDS.
VERIFICATION_PROMPT_ID = VERIFICATION_PROMPT_IDS[0]
EXPECTED_PROMPT_IDS = tuple(
    observation_id(run_id, template_id)
    for run_id in RUN_IDS
    for template_id in TEMPLATE_PROMPT_IDS
)
LEGACY_DISCOVERY_PROMPT_IDS = ("discovery-1", "discovery-2", "discovery-3")
LEGACY_VERIFICATION_PROMPT_ID = "clinic-verification"
LEGACY_EXPECTED_PROMPT_IDS = (*LEGACY_DISCOVERY_PROMPT_IDS, LEGACY_VERIFICATION_PROMPT_ID)

MIN_GAP_SUPPORTING_OBSERVATIONS = 8
MIN_PAIRED_SUPPORTING_TEMPLATES = 4
MIN_OVERALL_CONSISTENT_OBSERVATIONS = 18
MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS = 8

ALLOWED_PROFILE_TYPES = {"ephemeral_guest"}
ALLOWED_LOGIN_STATES = {"logged_out_verified"}
ALLOWED_VERIFICATION_METHODS = {
    "browser_ui_and_cookie_audit",
    "fresh_profile_ui_and_cookie_audit",
}
ALLOWED_CONCLUSIONS = {"confirmed_gap", "no_gap", "mixed", "inconclusive"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOKEN = re.compile(r"[a-z0-9]+")
# Browser map cards can flatten their one-decimal rating into an adjacent name,
# for example ``4.8Simply Clinics4.7Next Clinic``. Remove only those adjacent
# rating fragments; globally splitting letters and digits corrupts brands whose
# names legitimately contain numbers.
_RATING_BEFORE_NAME = re.compile(r"(?<!\d)(?:[0-4]\.[0-9]|5\.0)(?=[a-z])")
_RATING_AFTER_NAME = re.compile(r"(?<=[a-z])(?:[0-4]\.[0-9]|5\.0)(?!\d)")
_FAILURE_MARKERS = (
    *(marker.lower() for marker in CANONICAL_FAILURE_MARKERS),
    "[chatgpt error]",
    "[chatgpt timeout]",
    "[gemini error]",
    "[gemini timeout]",
    "too many requests",
    "unusual traffic",
    "verify you are human",
    "captcha",
    "something went wrong",
    "unusual activity has been detected",
    "an error occurred",
    "there was an error",
    "try again later",
    "service unavailable",
    "temporarily unavailable",
    "access denied",
    "log in to continue",
    "sign in to continue",
    "you have reached your limit",
    "you've reached your limit",
    "sign in to copilot",
)

_GENERIC_COMPETITOR_NAMES = {
    "clinic",
    "clinics",
    "a clinic",
    "local clinic",
    "aesthetic clinic",
    "medical clinic",
    "competitor",
    "competitors",
    "provider",
    "providers",
    "local provider",
    "option",
    "options",
    "local option",
}
_GENERIC_COMPETITOR_TOKENS = {
    "a",
    "and",
    "aesthetic",
    "aesthetics",
    "centre",
    "center",
    "clinic",
    "clinics",
    "competitor",
    "competitors",
    "doctor",
    "doctors",
    "hospital",
    "limited",
    "local",
    "ltd",
    "medical",
    "option",
    "options",
    "practice",
    "provider",
    "providers",
    "spa",
    "the",
}


class VisibilityEvidenceError(ValueError):
    """Raised when an evidence artifact would be unsafe or unsupported."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | datetime, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise VisibilityEvidenceError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VisibilityEvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, *, field: str, minimum: int = 1) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(cleaned) < minimum:
        raise VisibilityEvidenceError(f"{field} is required")
    return cleaned


def _safe_identifier(value: Any, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise VisibilityEvidenceError(
            f"{field} must use only letters, numbers, dots, underscores, or hyphens"
        )
    return cleaned


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VisibilityEvidenceError(f"required artifact is missing: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise VisibilityEvidenceError(f"invalid JSON artifact: {path.name}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(_TOKEN.findall(str(value or "").lower()))


def _normalized_response_segments(value: str) -> tuple[str, ...]:
    """Normalize text without allowing removed ratings to join map cards.

    Browser-extracted map results can flatten ``4.8Clinic Name`` or
    ``Clinic Name4.8``.  Treating the rating as ordinary whitespace recovers
    those names, but it can also manufacture a name across two adjacent cards.
    Keeping each side of a removed rating in a separate segment preserves the
    useful recovery without crossing that boundary.
    """

    lowered = str(value or "").lower()
    lowered = _RATING_BEFORE_NAME.sub("\x00", lowered)
    lowered = _RATING_AFTER_NAME.sub("\x00", lowered)
    return tuple(
        normalized
        for segment in lowered.split("\x00")
        if (normalized := _normalized(segment))
    )


def _contains_name(text: str, name: str) -> bool:
    normalized_name = _normalized(name)
    if not normalized_name:
        return False
    exact = f" {normalized_name} " in f" {_normalized(text)} "
    if exact:
        return True
    needle = f" {normalized_name} "
    return any(needle in f" {segment} " for segment in _normalized_response_segments(text))


def _validate_competitor_name(
    name: Any,
    aliases: Sequence[str],
    *,
    area: str = "",
    treatments: Sequence[str] = (),
) -> str:
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    normalized = _normalized(cleaned)
    if len(normalized) < 3 or normalized in _GENERIC_COMPETITOR_NAMES:
        raise VisibilityEvidenceError(
            f"competitor mention is not a specific clinic or brand name: {cleaned or '<empty>'}"
        )
    alias_keys = {_normalized(alias) for alias in aliases if _normalized(alias)}
    if normalized in alias_keys:
        raise VisibilityEvidenceError("the prospect clinic cannot be annotated as its own competitor")
    context_tokens = set(_normalized(area).split())
    for treatment in treatments:
        context_tokens.update(_normalized(treatment).split())
    distinctive = [
        token
        for token in normalized.split()
        if len(token) >= 2
        and token not in _GENERIC_COMPETITOR_TOKENS
        and token not in context_tokens
    ]
    if not distinctive:
        raise VisibilityEvidenceError(
            f"competitor mention has no distinctive business-name token: {cleaned}"
        )
    return cleaned


def _profile_audit_path(
    check_dir: Path,
    check: Mapping[str, Any],
    proof: Mapping[str, Any] | None = None,
) -> Path:
    selected = proof or check.get("profile_proof", {})
    reference = str(selected.get("evidence_reference") or "").strip()
    relative = Path(reference)
    if not reference or relative.is_absolute() or ".." in relative.parts:
        raise VisibilityEvidenceError(
            "profile proof evidence_reference must be a safe path inside the check directory"
        )
    raw_candidate = check_dir / relative
    candidate = raw_candidate.resolve()
    try:
        candidate.relative_to(check_dir.resolve())
    except ValueError as exc:
        raise VisibilityEvidenceError("profile audit path escapes the check directory") from exc
    if raw_candidate.is_symlink() or not candidate.is_file():
        raise VisibilityEvidenceError("the logged-out profile audit artifact is missing")
    return raw_candidate


def _profile_audit_errors(check_dir: Path, check: Mapping[str, Any]) -> list[str]:
    """Validate both concrete audits referenced by the normalized run proofs."""

    proofs = check.get("profile_proofs")
    if not isinstance(proofs, Mapping):
        return ["check must contain profile_proofs for both independent runs"]
    errors: list[str] = []
    if tuple(proofs) != RUN_IDS:
        errors.append("profile_proofs must contain exactly run-a and run-b in order")
    profile_ids = [
        str(proofs.get(run_id, {}).get("profile_id") or "")
        for run_id in RUN_IDS
        if isinstance(proofs.get(run_id), Mapping)
    ]
    if len(profile_ids) != len(RUN_IDS) or len(set(profile_ids)) != len(RUN_IDS):
        errors.append("the two visibility runs must use different ephemeral profile_id values")
    audit_references = [
        str(proofs.get(run_id, {}).get("evidence_reference") or "")
        for run_id in RUN_IDS
        if isinstance(proofs.get(run_id), Mapping)
    ]
    if len(audit_references) != len(RUN_IDS) or len(set(audit_references)) != len(RUN_IDS):
        errors.append("the two visibility runs must use different profile audit artifacts")

    for run_id in RUN_IDS:
        proof = proofs.get(run_id)
        if not isinstance(proof, Mapping):
            errors.append(f"missing profile proof for {run_id}")
            continue
        try:
            path = _profile_audit_path(check_dir, check, proof)
            audit = _read_json(path)
        except VisibilityEvidenceError as exc:
            errors.append(f"{run_id}: {exc}")
            continue
        if not isinstance(audit, Mapping):
            errors.append(f"{run_id}: logged-out profile audit must contain a JSON object")
            continue

        forbidden = {
            "cookies",
            "cookie_values",
            "cookie_value",
            "tokens",
            "token_values",
            "authorization",
        }
        if any(str(key).strip().lower() in forbidden for key in audit):
            errors.append(f"{run_id}: logged-out profile audit must not contain cookie or token values")
        expected = {
            "platform": check.get("platform"),
            "profile_id": proof.get("profile_id"),
            "verified_at": proof.get("verified_at"),
            "verification_method": proof.get("verification_method"),
        }
        for field, value in expected.items():
            if audit.get(field) != value:
                errors.append(f"{run_id}: profile audit {field} does not match the check")
        required_booleans = {
            "guest_surface_detected": True,
            "auth_signals_detected": False,
            "cookie_values_recorded": False,
            "browser_user_data_ephemeral": True,
            "browser_profile_reused": False,
        }
        for field, value in required_booleans.items():
            if audit.get(field) is not value:
                errors.append(f"{run_id}: profile audit {field} must be {str(value).lower()}")
        auth_signals = audit.get("auth_signals")
        if not isinstance(auth_signals, list) or auth_signals:
            errors.append(f"{run_id}: profile audit auth_signals must be an empty list")
    return errors


def _live_collection_errors(check_dir: Path, check: Mapping[str, Any]) -> list[str]:
    """Require the terminal, cleanup-confirmed manifest from the isolated runner."""

    path = check_dir / "live-collection.json"
    try:
        manifest = _read_json(path)
    except VisibilityEvidenceError as exc:
        return [str(exc)]
    if not isinstance(manifest, Mapping):
        return ["live-collection.json must contain an object"]
    errors: list[str] = []
    expected = {
        "check_id": check.get("check_id"),
        "sales_account_id": check.get("sales_account_id"),
        "platform": check.get("platform"),
        "visibility_contract_id": VISIBILITY_CONTRACT_ID,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            errors.append(f"live collection {field} does not match the check")
    collection = manifest.get("collection")
    if not isinstance(collection, Mapping) or collection.get("collection_complete") is not True:
        errors.append("live collection is not complete")
    elif collection.get("target_platform_observation_count") != len(EXPECTED_PROMPT_IDS):
        errors.append("live collection does not contain exactly 30 target-platform observations")
    runs = manifest.get("runs")
    if not isinstance(runs, list) or [row.get("run_id") for row in runs if isinstance(row, Mapping)] != list(RUN_IDS):
        errors.append("live collection must contain exactly run-a and run-b")
        runs = []
    proofs = check.get("profile_proofs", {})
    for row in runs:
        if not isinstance(row, Mapping):
            errors.append("live collection run entry must be an object")
            continue
        run_id = str(row.get("run_id") or "")
        proof = proofs.get(run_id, {}) if isinstance(proofs, Mapping) else {}
        if row.get("profile_id") != proof.get("profile_id"):
            errors.append(f"live collection profile_id does not match {run_id}")
        if row.get("profile_audit_reference") != proof.get("evidence_reference"):
            errors.append(f"live collection profile audit reference does not match {run_id}")
        if row.get("profile_type") != "ephemeral_guest":
            errors.append(f"live collection {run_id} profile_type must be ephemeral_guest")
        if row.get("login_state") != "logged_out_verified":
            errors.append(f"live collection {run_id} login_state must be logged_out_verified")
        run_collection = row.get("collection")
        if not isinstance(run_collection, Mapping) or run_collection.get("collection_complete") is not True:
            errors.append(f"live collection {run_id} is not complete")
        elif run_collection.get("target_platform_observation_count") != len(TEMPLATE_PROMPT_IDS):
            errors.append(f"live collection {run_id} must contain 15 target-platform observations")
        if row.get("temporary_browser_state_cleanup_completed") is not True:
            errors.append(f"live collection {run_id} browser cleanup is not complete")
    required = {
        "human_review_required": True,
        "automatic_review": False,
        "automatic_finalize": False,
        "gap_claim_supported": False,
        "outreach_action_performed": False,
        "temporary_browser_state_cleanup_completed": True,
    }
    for field, value in required.items():
        if manifest.get(field) is not value:
            errors.append(f"live collection {field} must be {str(value).lower()}")
    return errors


def default_artifacts_root(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else repo_root()
    return base / "data" / "outreach" / "artifacts"


def validate_profile_proof(
    proof: Mapping[str, Any],
    *,
    at: str | datetime | None = None,
) -> dict[str, Any]:
    """Validate and normalize proof for a clean, ephemeral, logged-out profile.

    A regime name, a rotating-pool label, or cookie clearing alone is never proof.
    The caller must provide both the observed guest UI signal and the result of an
    authentication-cookie/account-signal audit.
    """

    if not isinstance(proof, Mapping):
        raise VisibilityEvidenceError("profile proof must be a JSON object")
    for key in ("regime", "profile_pool", "profile_source"):
        label = re.sub(r"[\s-]+", "_", str(proof.get(key) or "").strip().lower())
        if label in {"rotating", "rotating_pool", "shared_rotating_pool"}:
            raise VisibilityEvidenceError(
                "the rotating profile pool cannot be used as logged-out evidence"
            )
    profile_id = _safe_identifier(proof.get("profile_id"), field="profile_id")
    profile_type = str(proof.get("profile_type") or "").strip()
    if profile_type not in ALLOWED_PROFILE_TYPES:
        raise VisibilityEvidenceError(
            "profile_type must be ephemeral_guest; rotating or persistent pools are not accepted"
        )
    login_state = str(proof.get("login_state") or "").strip()
    if login_state not in ALLOWED_LOGIN_STATES:
        raise VisibilityEvidenceError("login_state must be explicitly logged_out_verified")
    method = str(proof.get("verification_method") or "").strip()
    if method not in ALLOWED_VERIFICATION_METHODS:
        raise VisibilityEvidenceError(
            "verification_method must include a browser UI and authentication-cookie audit"
        )
    if proof.get("auth_signals_detected") is not False:
        raise VisibilityEvidenceError("auth_signals_detected must be explicitly false")
    if proof.get("guest_surface_detected") is not True:
        raise VisibilityEvidenceError("guest_surface_detected must be explicitly true")
    evidence_reference = _clean_text(
        proof.get("evidence_reference"), field="evidence_reference", minimum=3
    )
    verified_at = _parse_timestamp(proof.get("verified_at"), field="verified_at")
    used_at = _parse_timestamp(at or _utc_now(), field="profile use time")
    if verified_at > used_at + timedelta(minutes=2):
        raise VisibilityEvidenceError("profile proof is dated in the future")
    age = used_at - verified_at
    if age > PROFILE_PROOF_MAX_AGE:
        raise VisibilityEvidenceError(
            f"logged-out profile proof is stale ({age}); verify it again before collection"
        )
    return {
        "profile_id": profile_id,
        "profile_type": profile_type,
        "login_state": login_state,
        "verified_at": _iso(verified_at),
        "valid_until": _iso(verified_at + PROFILE_PROOF_MAX_AGE),
        "verification_method": method,
        "auth_signals_detected": False,
        "guest_surface_detected": True,
        "evidence_reference": evidence_reference,
    }


def validate_logged_out_profile_config(
    path_or_value: Path | str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    at: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Read and validate ``config/logged-out-profiles.json`` shaped data.

    Accepted input is ``{"profiles": [...]}`` or a bare list.  This helper does
    not create or inspect browser profiles; it only validates independently
    captured proof.
    """

    if isinstance(path_or_value, (str, Path)):
        data = _read_json(Path(path_or_value))
    else:
        data = path_or_value
    if isinstance(data, Mapping):
        if data.get("schema_version") not in {None, PROFILE_CONFIG_SCHEMA_VERSION}:
            raise VisibilityEvidenceError("unsupported logged-out profile config schema")
        profiles = data.get("profiles")
    else:
        profiles = data
    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)) or not profiles:
        raise VisibilityEvidenceError("logged-out profile config must contain at least one profile")
    normalized = [validate_profile_proof(profile, at=at) for profile in profiles]
    ids = [profile["profile_id"] for profile in normalized]
    if len(ids) != len(set(ids)):
        raise VisibilityEvidenceError("logged-out profile config contains duplicate profile_id values")
    return normalized


def write_logged_out_profile_config(
    path: Path | str,
    profiles: Sequence[Mapping[str, Any]],
    *,
    at: str | datetime | None = None,
    replace: bool = False,
) -> Path:
    """Write a validated profile manifest without touching existing config by default."""

    destination = Path(path)
    if destination.exists() and not replace:
        raise VisibilityEvidenceError(f"profile config already exists: {destination}")
    normalized = validate_logged_out_profile_config(list(profiles), at=at)
    _atomic_json(
        destination,
        {
            "schema_version": PROFILE_CONFIG_SCHEMA_VERSION,
            "generated_at": _iso(_parse_timestamp(at or _utc_now(), field="generated_at")),
            "profiles": normalized,
        },
    )
    return destination


def _clean_treatments(treatments: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in treatments:
        treatment = re.sub(r"\s+", " ", str(value or "")).strip()
        key = treatment.casefold()
        if treatment and key not in seen:
            cleaned.append(treatment)
            seen.add(key)
    if not cleaned:
        raise VisibilityEvidenceError("at least one treatment is required")
    return cleaned


def build_prompt_basket(
    clinic_name: str,
    area: str,
    treatments: Iterable[str],
) -> list[dict[str, Any]]:
    """Return the frozen 15-template basket repeated across two runs.

    The thirteen discovery templates never name the prospect.  The final two
    templates name it only for identity and relevance verification; those four
    named observations are never accepted as evidence of an unbranded visibility
    gap.  Text is identical between runs so repeatability can be reviewed.
    """

    clinic = _clean_text(clinic_name, field="clinic_name")
    locality = _clean_text(area, field="area")
    methods = _clean_treatments(treatments)
    routed = [methods[index % len(methods)] for index in range(len(DISCOVERY_TEMPLATE_IDS))]
    discovery_texts = (
        f"Which clinics would you recommend for {routed[0]} in {locality}? Give a short, unranked list and explain the reasons using current public information.",
        f"A patient is comparing clinics for {routed[1]} near {locality}. Which clinics should they consider, and why? Use current public information.",
        f"What are the strongest clinic options for {routed[2]} in {locality}? Name the clinics you would shortlist and explain the evidence using current public information.",
        f"Which clinics in or near {locality} appear trustworthy for {routed[3]}? Explain the public signals behind the suggestions.",
        f"Where should a first-time patient look for {routed[4]} in {locality}? Suggest several clinics and explain the selection using current public information.",
        f"Which clinics around {locality} have credible clinicians for {routed[5]}? Give a concise shortlist based on current public information.",
        f"What clinics should a patient compare before booking a consultation for {routed[6]} in {locality}? Explain why each is relevant.",
        f"Which clinics near {locality} are worth considering for natural-looking {routed[7]} results? Use current public information and name the clinics.",
        f"Suggest clinics for someone prioritising safety and aftercare for {routed[8]} in {locality}. Explain the public evidence for each suggestion.",
        f"Which established clinics offer {routed[9]} in {locality}? Provide an unranked shortlist using current public information.",
        f"A patient wants high-quality {routed[10]} in {locality} and is deciding where to enquire. Which clinics should be on the shortlist, and why?",
        f"Which local clinics show strong public credibility for {routed[11]} around {locality}? Name several options and explain the signals used.",
        f"If you were helping a patient compare nearby providers for {routed[12]} in {locality}, which clinics would you include and why? Use current public information.",
    )
    templates: list[dict[str, Any]] = []
    for index, (template_id, text) in enumerate(
        zip(DISCOVERY_TEMPLATE_IDS, discovery_texts), start=1
    ):
        templates.append(
            {
                "template_id": template_id,
                "kind": "area_treatment_discovery",
                "run_sequence": index,
                "treatment": routed[index - 1],
                "clinic_named": False,
                "counts_toward_gap": True,
                "text": text,
            }
        )
    templates.extend(
        [
            {
                "template_id": VERIFICATION_TEMPLATE_IDS[0],
                "kind": "clinic_specific_verification",
                "run_sequence": 14,
                "treatment": None,
                "clinic_named": True,
                "counts_toward_gap": False,
                "text": (
                    f"Using current public information, what is {clinic} in {locality} known for? "
                    "Mention relevant treatments and evidence of credibility."
                ),
            },
            {
                "template_id": VERIFICATION_TEMPLATE_IDS[1],
                "kind": "clinic_specific_verification",
                "run_sequence": 15,
                "treatment": methods[0],
                "clinic_named": True,
                "counts_toward_gap": False,
                "text": (
                    f"Would {clinic} in {locality} be a relevant clinic to consider for {methods[0]}? "
                    "Explain what current public information supports or does not support that assessment."
                ),
            },
        ]
    )

    prompts: list[dict[str, Any]] = []
    for run_index, run_id in enumerate(RUN_IDS):
        for template in templates:
            prompts.append(
                template
                | {
                    "id": observation_id(run_id, str(template["template_id"])),
                    "run_id": run_id,
                    "sequence": run_index * len(TEMPLATE_PROMPT_IDS)
                    + int(template["run_sequence"]),
                    "observation_type": "target_platform_response",
                }
            )
    return prompts


def create_visibility_check(
    *,
    sales_account_id: str,
    clinic_name: str,
    area: str,
    treatments: Iterable[str],
    profile_proof: Mapping[str, Any] | None = None,
    profile_proofs: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] | None = None,
    platform: str = "chatgpt",
    artifacts_root: Path | str | None = None,
    check_id: str | None = None,
    created_at: str | datetime | None = None,
    clinic_aliases: Iterable[str] = (),
) -> Path:
    """Create a check directory and runner-compatible ``prompts.json`` file."""

    created = _parse_timestamp(created_at or _utc_now(), field="created_at")
    account_id = _safe_identifier(sales_account_id, field="sales_account_id")
    generated_check_id = check_id or f"vc-{created.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    normalized_check_id = _safe_identifier(generated_check_id, field="check_id")
    platform_key = _safe_identifier(str(platform).lower(), field="platform")
    if profile_proofs is None:
        if profile_proof is not None:
            raise VisibilityEvidenceError(
                "the 30-observation contract requires two independent profile_proofs; "
                "one legacy 3+1 profile cannot satisfy it"
            )
        raise VisibilityEvidenceError("profile_proofs is required for run-a and run-b")
    if isinstance(profile_proofs, Mapping):
        raw_proofs = {run_id: profile_proofs.get(run_id) for run_id in RUN_IDS}
        if set(profile_proofs) != set(RUN_IDS):
            raise VisibilityEvidenceError("profile_proofs must contain exactly run-a and run-b")
    else:
        rows = list(profile_proofs)
        if len(rows) != len(RUN_IDS):
            raise VisibilityEvidenceError("profile_proofs must contain exactly two independent proofs")
        raw_proofs = dict(zip(RUN_IDS, rows))
    proofs: dict[str, dict[str, Any]] = {}
    for run_id in RUN_IDS:
        raw_proof = raw_proofs.get(run_id)
        if not isinstance(raw_proof, Mapping):
            raise VisibilityEvidenceError(f"missing profile proof for {run_id}")
        # Each proof is captured immediately before its own run.  It may therefore
        # postdate the overall check start, so validate it at its own timestamp.
        proofs[run_id] = validate_profile_proof(
            raw_proof,
            at=raw_proof.get("verified_at"),
        )
    profile_ids = [proofs[run_id]["profile_id"] for run_id in RUN_IDS]
    if len(set(profile_ids)) != len(RUN_IDS):
        raise VisibilityEvidenceError(
            "run-a and run-b must use different disposable logged-out profiles"
        )
    evidence_references = [proofs[run_id]["evidence_reference"] for run_id in RUN_IDS]
    if len(set(evidence_references)) != len(RUN_IDS):
        raise VisibilityEvidenceError("each run must reference its own profile audit artifact")
    clinic = _clean_text(clinic_name, field="clinic_name")
    locality = _clean_text(area, field="area")
    methods = _clean_treatments(treatments)
    aliases = [clinic]
    for alias in clinic_aliases:
        cleaned = re.sub(r"\s+", " ", str(alias or "")).strip()
        if cleaned and cleaned.casefold() not in {value.casefold() for value in aliases}:
            aliases.append(cleaned)
    prompts = build_prompt_basket(clinic, locality, methods)

    base = (
        Path(artifacts_root) if artifacts_root is not None else default_artifacts_root()
    ).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    check_dir = base / account_id / normalized_check_id
    for candidate in (base / account_id, check_dir):
        if candidate.exists() and candidate.is_symlink():
            raise VisibilityEvidenceError("visibility artifact path must not use symlinks")
        try:
            candidate.resolve().relative_to(base)
        except ValueError as exc:
            raise VisibilityEvidenceError("visibility artifact path escapes the artifacts root") from exc
    if check_dir.exists():
        raise VisibilityEvidenceError(f"visibility check already exists: {check_dir}")
    check_dir.mkdir(parents=True)
    try:
        check_dir.resolve().relative_to(base)
    except ValueError as exc:
        raise VisibilityEvidenceError("visibility artifact directory escaped its trusted root") from exc

    check = {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "check_id": normalized_check_id,
        "sales_account_id": account_id,
        "created_at": _iso(created),
        "platform": platform_key,
        "clinic": {
            "name": clinic,
            "aliases": aliases,
            "area": locality,
            "treatments": methods,
        },
        # profile_proof is retained as a read-only compatibility pointer for
        # consumers that display one browser label.  Validation always uses both.
        "profile_proof": proofs[RUN_IDS[0]],
        "profile_proofs": proofs,
        "visibility_contract_id": VISIBILITY_CONTRACT_ID,
        "collection_policy": {
            "required_prompt_ids": list(EXPECTED_PROMPT_IDS),
            "required_run_ids": list(RUN_IDS),
            "prompt_templates_per_run": len(TEMPLATE_PROMPT_IDS),
            "unbranded_prompts_per_run": len(DISCOVERY_TEMPLATE_IDS),
            "clinic_named_prompts_per_run": len(VERIFICATION_TEMPLATE_IDS),
            "required_target_platform_observations": len(EXPECTED_PROMPT_IDS),
            "minimum_gap_supporting_observations": MIN_GAP_SUPPORTING_OBSERVATIONS,
            "minimum_paired_supporting_templates": MIN_PAIRED_SUPPORTING_TEMPLATES,
            "minimum_overall_consistent_observations": MIN_OVERALL_CONSISTENT_OBSERVATIONS,
            "minimum_overall_consistent_template_pairs": MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS,
            "require_competitor_mention_in_each_gap_supporting_prompt": True,
            "clinic_named_prompts_may_support_gap": False,
            "human_review_required": True,
            "fresh_for_days": EVIDENCE_VALIDITY.days,
        },
    }
    runner_prompts = {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "visibility_contract_id": VISIBILITY_CONTRACT_ID,
        "check_id": normalized_check_id,
        "prompts": [
            prompt
            | {
                "sales_account_id": account_id,
                "clinic": clinic,
                "area": locality,
                "platform": platform_key,
                "profile_id": proofs[str(prompt["run_id"])]["profile_id"],
            }
            for prompt in prompts
        ],
    }
    _atomic_json(check_dir / "check.json", check)
    _atomic_json(check_dir / "prompts.json", runner_prompts)
    (check_dir / "responses").mkdir()
    return check_dir


def _load_check(check_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    check = _read_json(check_dir / "check.json")
    prompts = _read_json(check_dir / "prompts.json")
    if not isinstance(check, dict) or not isinstance(prompts, dict):
        raise VisibilityEvidenceError("check and prompts artifacts must be JSON objects")
    return check, prompts


def record_response(
    check_dir: Path | str,
    *,
    prompt_id: str,
    output_text: str,
    platform: str,
    profile_id: str,
    collected_at: str | datetime | None = None,
    competitor_mentions: Iterable[str] = (),
    citations: Iterable[Mapping[str, Any] | str] = (),
    collection_status: str = "complete",
    replace: bool = False,
) -> Path:
    """Save one raw response and provenance sidecar.

    Mention claims are verified against the raw output.  Candidate competitor
    names that are not literally present are rejected instead of being stored as
    evidence.
    """

    directory = Path(check_dir)
    if (directory / "integrity.json").exists():
        raise VisibilityEvidenceError("a finalized visibility check is immutable")
    check, prompts_payload = _load_check(directory)
    prompt_key = str(prompt_id or "").strip()
    prompt_map = {
        str(prompt.get("id")): prompt
        for prompt in prompts_payload.get("prompts", [])
        if isinstance(prompt, dict)
    }
    if prompt_key not in prompt_map or prompt_key not in EXPECTED_PROMPT_IDS:
        raise VisibilityEvidenceError(f"unknown prompt_id: {prompt_key}")
    expected_platform = str(check.get("platform") or "")
    if str(platform or "").strip().lower() != expected_platform:
        raise VisibilityEvidenceError("response platform does not match the planned check")
    prompt_run_id = str(prompt_map[prompt_key].get("run_id") or "")
    expected_profile = str(
        check.get("profile_proofs", {}).get(prompt_run_id, {}).get("profile_id") or ""
    )
    if str(profile_id or "").strip() != expected_profile:
        raise VisibilityEvidenceError(
            f"response profile_id does not match the proven guest profile for {prompt_run_id}"
        )

    collected = _parse_timestamp(collected_at or _utc_now(), field="collected_at")
    validate_profile_proof(
        check.get("profile_proofs", {}).get(prompt_run_id, {}),
        at=collected,
    )
    created = _parse_timestamp(check.get("created_at"), field="created_at")
    if collected < created - timedelta(minutes=2):
        raise VisibilityEvidenceError("response predates the visibility check")

    status = str(collection_status or "").strip().lower()
    raw = str(output_text or "").strip()
    if status == "complete" and not raw:
        raise VisibilityEvidenceError("a complete response cannot be empty")
    if any(marker in raw.lower() for marker in _FAILURE_MARKERS):
        status = "blocked"

    response_dir = directory / "responses"
    raw_path = response_dir / f"{prompt_key}.txt"
    metadata_path = response_dir / f"{prompt_key}.json"
    if (raw_path.exists() or metadata_path.exists()) and not replace:
        raise VisibilityEvidenceError(f"response already exists for {prompt_key}")

    aliases = [str(value) for value in check.get("clinic", {}).get("aliases", []) if str(value).strip()]
    area = str(check.get("clinic", {}).get("area") or "")
    treatments = [
        str(value) for value in check.get("clinic", {}).get("treatments", []) if str(value).strip()
    ]
    clinic_matches = [alias for alias in aliases if _contains_name(raw, alias)]
    matched_competitors: list[str] = []
    unmatched_competitors: list[str] = []
    for candidate in competitor_mentions:
        name = _validate_competitor_name(
            candidate,
            aliases,
            area=area,
            treatments=treatments,
        )
        target = matched_competitors if _contains_name(raw, name) else unmatched_competitors
        if name.casefold() not in {item.casefold() for item in target}:
            target.append(name)
    if unmatched_competitors:
        raise VisibilityEvidenceError(
            "competitor mentions are not present in the raw output: "
            + ", ".join(unmatched_competitors)
        )

    citation_rows: list[Any] = []
    for citation in citations:
        if isinstance(citation, Mapping):
            citation_rows.append(dict(citation))
        else:
            citation_rows.append(str(citation))
    raw_with_newline = raw + ("\n" if raw else "")
    _atomic_text(raw_path, raw_with_newline)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "check_id": check.get("check_id"),
        "sales_account_id": check.get("sales_account_id"),
        "prompt_id": prompt_key,
        "run_id": prompt_run_id,
        "template_id": prompt_map[prompt_key].get("template_id"),
        "prompt_kind": prompt_map[prompt_key].get("kind"),
        "prompt_sha256": _sha256_text(str(prompt_map[prompt_key].get("text") or "")),
        "platform": expected_platform,
        "profile_id": expected_profile,
        "login_state": "logged_out_verified",
        "observation_type": "target_platform_response",
        "counts_as_visibility_observation": True,
        "target_platform_request_count": 1,
        "collected_at": _iso(collected),
        "collection_status": status,
        "raw_file": raw_path.relative_to(directory).as_posix(),
        "raw_sha256": _sha256(raw_path),
        "raw_bytes": raw_path.stat().st_size,
        "clinic_mentioned": bool(clinic_matches),
        "clinic_mentions": clinic_matches,
        "competitor_mentions": matched_competitors,
        "citations": citation_rows,
    }
    _atomic_json(metadata_path, metadata)
    return metadata_path


def _expected_prompt_payload(check: Mapping[str, Any]) -> list[dict[str, Any]]:
    clinic = check.get("clinic", {})
    return build_prompt_basket(
        str(clinic.get("name") or ""),
        str(clinic.get("area") or ""),
        clinic.get("treatments") or [],
    )


def _purpose_error(value: Mapping[str, Any], *, artifact_kind: str) -> str | None:
    """Validate current purpose or invoke the explicit v2 legacy read view."""

    try:
        schema_version = str(value.get("schema_version") or "").strip()
        if schema_version == SCHEMA_VERSION:
            require_evidence_purpose(
                value,
                EvidencePurpose.OUTREACH_GAP_CONFIRMATION,
                label=artifact_kind,
            )
        elif schema_version == PURPOSE_LEGACY_SCHEMA_VERSION and "purpose" in value:
            require_evidence_purpose(
                value,
                EvidencePurpose.OUTREACH_GAP_CONFIRMATION,
                label=f"historical purpose-bound {artifact_kind}",
            )
        elif schema_version == PURPOSE_LEGACY_SCHEMA_VERSION:
            legacy_purpose_view(
                value,
                EvidencePurpose.OUTREACH_GAP_CONFIRMATION,
                artifact_kind=artifact_kind,
                supported_schema_versions=(PURPOSE_LEGACY_SCHEMA_VERSION,),
            )
        else:
            raise ValueError(
                f"{artifact_kind} schema {schema_version!r} is not an allowlisted "
                "current or historical purpose contract"
            )
    except (TypeError, ValueError) as exc:
        return str(exc)
    return None


def _inspect_collection(check_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    responses: dict[str, dict[str, Any]] = {}
    try:
        check, prompts_payload = _load_check(check_dir)
    except VisibilityEvidenceError as exc:
        return {"errors": [str(exc)], "check": {}, "prompts": {}, "responses": {}}

    check_schema_version = str(check.get("schema_version") or "").strip()
    if check_schema_version not in {SCHEMA_VERSION, PURPOSE_LEGACY_SCHEMA_VERSION}:
        errors.append("unsupported check schema_version")
    purpose_error = _purpose_error(check, artifact_kind="outreach visibility check")
    if purpose_error:
        errors.append(purpose_error)
    if check.get("visibility_contract_id") != VISIBILITY_CONTRACT_ID:
        errors.append("check does not use the current 30-observation contract")
    policy = check.get("collection_policy")
    expected_policy = {
        "required_prompt_ids": list(EXPECTED_PROMPT_IDS),
        "required_run_ids": list(RUN_IDS),
        "prompt_templates_per_run": len(TEMPLATE_PROMPT_IDS),
        "unbranded_prompts_per_run": len(DISCOVERY_TEMPLATE_IDS),
        "clinic_named_prompts_per_run": len(VERIFICATION_TEMPLATE_IDS),
        "required_target_platform_observations": len(EXPECTED_PROMPT_IDS),
        "minimum_gap_supporting_observations": MIN_GAP_SUPPORTING_OBSERVATIONS,
        "minimum_paired_supporting_templates": MIN_PAIRED_SUPPORTING_TEMPLATES,
        "minimum_overall_consistent_observations": MIN_OVERALL_CONSISTENT_OBSERVATIONS,
        "minimum_overall_consistent_template_pairs": MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS,
        "require_competitor_mention_in_each_gap_supporting_prompt": True,
        "clinic_named_prompts_may_support_gap": False,
        "human_review_required": True,
        "fresh_for_days": EVIDENCE_VALIDITY.days,
    }
    if policy != expected_policy:
        errors.append("collection_policy does not match the current 30-observation contract")
    try:
        created = _parse_timestamp(check.get("created_at"), field="created_at")
        proofs = check.get("profile_proofs", {})
        if not isinstance(proofs, Mapping):
            raise VisibilityEvidenceError("check must contain profile_proofs")
        for run_id in RUN_IDS:
            run_proof = proofs.get(run_id)
            if not isinstance(run_proof, Mapping):
                raise VisibilityEvidenceError(f"missing profile proof for {run_id}")
            validate_profile_proof(run_proof, at=run_proof.get("verified_at"))
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))
    errors.extend(_profile_audit_errors(check_dir, check))
    errors.extend(_live_collection_errors(check_dir, check))

    stored_prompts = prompts_payload.get("prompts")
    if prompts_payload.get("schema_version") != check_schema_version:
        errors.append("prompts schema_version does not match check schema_version")
    purpose_error = _purpose_error(
        prompts_payload,
        artifact_kind="outreach visibility prompts",
    )
    if purpose_error:
        errors.append(purpose_error)
    if prompts_payload.get("visibility_contract_id") != VISIBILITY_CONTRACT_ID:
        errors.append("prompts do not use the current 30-observation contract")
    if prompts_payload.get("check_id") != check.get("check_id"):
        errors.append("prompts check_id does not match the check")
    if not isinstance(stored_prompts, list):
        errors.append("prompts.json does not contain a prompt list")
        stored_prompts = []
    try:
        expected_prompts = _expected_prompt_payload(check)
    except VisibilityEvidenceError as exc:
        expected_prompts = []
        errors.append(str(exc))
    if [row.get("id") for row in stored_prompts if isinstance(row, dict)] != list(EXPECTED_PROMPT_IDS):
        errors.append("prompt basket must contain the exact 30 required observations in order")
    for expected, stored in zip(expected_prompts, stored_prompts):
        if not isinstance(stored, dict):
            errors.append("prompt basket contains a non-object entry")
            continue
        for key in (
            "id",
            "run_id",
            "template_id",
            "kind",
            "sequence",
            "run_sequence",
            "treatment",
            "clinic_named",
            "counts_toward_gap",
            "observation_type",
            "text",
        ):
            if stored.get(key) != expected.get(key):
                errors.append(f"{stored.get('id', 'unknown')} has altered {key}")

    prompt_map = {
        str(row.get("id")): row for row in stored_prompts if isinstance(row, dict) and row.get("id")
    }
    for prompt_id in EXPECTED_PROMPT_IDS:
        raw_path = check_dir / "responses" / f"{prompt_id}.txt"
        metadata_path = check_dir / "responses" / f"{prompt_id}.json"
        if not raw_path.exists() or not metadata_path.exists():
            errors.append(f"missing response for {prompt_id}")
            continue
        try:
            metadata = _read_json(metadata_path)
        except VisibilityEvidenceError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(metadata, dict):
            errors.append(f"response metadata is invalid for {prompt_id}")
            continue
        responses[prompt_id] = metadata
        if metadata.get("schema_version") != check_schema_version:
            errors.append(f"response schema_version mismatch for {prompt_id}")
        purpose_error = _purpose_error(
            metadata,
            artifact_kind=f"outreach visibility response {prompt_id}",
        )
        if purpose_error:
            errors.append(purpose_error)
        raw = raw_path.read_text(encoding="utf-8")
        if metadata.get("prompt_id") != prompt_id:
            errors.append(f"response prompt_id mismatch for {prompt_id}")
        if metadata.get("check_id") != check.get("check_id"):
            errors.append(f"response check_id mismatch for {prompt_id}")
        if metadata.get("sales_account_id") != check.get("sales_account_id"):
            errors.append(f"response sales_account_id mismatch for {prompt_id}")
        expected_raw_file = f"responses/{prompt_id}.txt"
        if metadata.get("raw_file") != expected_raw_file:
            errors.append(f"raw response path mismatch for {prompt_id}")
        if metadata.get("collection_status") != "complete":
            errors.append(f"response is not complete for {prompt_id}")
        if not raw.strip():
            errors.append(f"response is empty for {prompt_id}")
        if any(marker in raw.lower() for marker in _FAILURE_MARKERS):
            errors.append(f"response contains a blocked/error marker for {prompt_id}")
        if metadata.get("raw_sha256") != _sha256(raw_path):
            errors.append(f"raw response hash mismatch for {prompt_id}")
        if metadata.get("raw_bytes") != raw_path.stat().st_size:
            errors.append(f"raw response byte count mismatch for {prompt_id}")
        prompt = prompt_map.get(prompt_id, {})
        if metadata.get("prompt_sha256") != _sha256_text(str(prompt.get("text") or "")):
            errors.append(f"prompt hash mismatch for {prompt_id}")
        if metadata.get("platform") != check.get("platform"):
            errors.append(f"platform mismatch for {prompt_id}")
        run_id = str(prompt.get("run_id") or "")
        expected_proof = check.get("profile_proofs", {}).get(run_id, {})
        if metadata.get("profile_id") != expected_proof.get("profile_id"):
            errors.append(f"profile mismatch for {prompt_id}")
        if metadata.get("run_id") != run_id:
            errors.append(f"run identity mismatch for {prompt_id}")
        if metadata.get("template_id") != prompt.get("template_id"):
            errors.append(f"template identity mismatch for {prompt_id}")
        if metadata.get("login_state") != "logged_out_verified":
            errors.append(f"login state is not proven for {prompt_id}")
        if metadata.get("observation_type") != "target_platform_response":
            errors.append(f"non-target response cannot count as an observation for {prompt_id}")
        if metadata.get("counts_as_visibility_observation") is not True:
            errors.append(f"response does not count as a target-platform observation for {prompt_id}")
        if metadata.get("target_platform_request_count") != 1:
            errors.append(f"response must represent exactly one target-platform request for {prompt_id}")
        try:
            collected = _parse_timestamp(metadata.get("collected_at"), field="collected_at")
            validate_profile_proof(expected_proof, at=collected)
        except VisibilityEvidenceError as exc:
            errors.append(f"{prompt_id}: {exc}")
        aliases = [
            str(value) for value in check.get("clinic", {}).get("aliases", []) if str(value).strip()
        ]
        area = str(check.get("clinic", {}).get("area") or "")
        treatments = [
            str(value)
            for value in check.get("clinic", {}).get("treatments", [])
            if str(value).strip()
        ]
        matches = [alias for alias in aliases if _contains_name(raw, alias)]
        if bool(matches) != bool(metadata.get("clinic_mentioned")):
            errors.append(f"clinic mention flag is not supported by raw output for {prompt_id}")
        if matches != metadata.get("clinic_mentions"):
            errors.append(f"clinic mention list is not supported by raw output for {prompt_id}")
        competitor_mentions = metadata.get("competitor_mentions")
        if not isinstance(competitor_mentions, list):
            errors.append(f"competitor mentions must be a list for {prompt_id}")
            competitor_mentions = []
        for competitor in competitor_mentions:
            try:
                specific_name = _validate_competitor_name(
                    competitor,
                    aliases,
                    area=area,
                    treatments=treatments,
                )
            except VisibilityEvidenceError as exc:
                errors.append(f"{prompt_id}: {exc}")
                continue
            if not _contains_name(raw, specific_name):
                errors.append(f"competitor mention is not supported by raw output for {prompt_id}")

    return {
        "errors": errors,
        "check": check,
        "prompts": prompts_payload,
        "responses": responses,
    }


def _conclusion_errors(
    conclusion: str,
    evidence_prompt_ids: Sequence[str],
    responses: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    unknown = [prompt_id for prompt_id in evidence_prompt_ids if prompt_id not in EXPECTED_PROMPT_IDS]
    if unknown:
        errors.append("review cites unknown prompts: " + ", ".join(unknown))
    if len(evidence_prompt_ids) != len(set(evidence_prompt_ids)):
        errors.append("review cites a prompt more than once")
    discovery_ids = [prompt_id for prompt_id in evidence_prompt_ids if prompt_id in DISCOVERY_PROMPT_IDS]

    def paired_support(ids: Sequence[str], *, mentioned: bool) -> set[str]:
        by_template: dict[str, set[str]] = {}
        for prompt_id in ids:
            response = responses.get(prompt_id, {})
            if response.get("clinic_mentioned") is mentioned:
                by_template.setdefault(str(response.get("template_id") or ""), set()).add(
                    str(response.get("run_id") or "")
                )
        return {
            template_id
            for template_id, run_ids in by_template.items()
            if template_id in DISCOVERY_TEMPLATE_IDS and run_ids == set(RUN_IDS)
        }

    if conclusion == "confirmed_gap":
        all_omissions = [
            prompt_id
            for prompt_id in DISCOVERY_PROMPT_IDS
            if responses.get(prompt_id, {}).get("clinic_mentioned") is False
        ]
        if len(all_omissions) < MIN_OVERALL_CONSISTENT_OBSERVATIONS:
            errors.append(
                "a visibility-gap claim requires omission in at least "
                f"{MIN_OVERALL_CONSISTENT_OBSERVATIONS} of the 26 unbranded observations"
            )
        if (
            len(paired_support(all_omissions, mentioned=False))
            < MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS
        ):
            errors.append(
                "a visibility-gap claim requires omission for at least "
                f"{MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS} prompt templates in both runs overall"
            )
        if len(discovery_ids) < MIN_GAP_SUPPORTING_OBSERVATIONS:
            errors.append(
                "a visibility-gap claim requires at least "
                f"{MIN_GAP_SUPPORTING_OBSERVATIONS} unbranded observations"
            )
        for prompt_id in discovery_ids:
            response = responses.get(prompt_id, {})
            if response.get("clinic_mentioned") is not False:
                errors.append(f"{prompt_id} does not support a clinic omission")
            if not response.get("competitor_mentions"):
                errors.append(f"{prompt_id} has no verified competitor mention")
        paired = paired_support(discovery_ids, mentioned=False)
        if len(paired) < MIN_PAIRED_SUPPORTING_TEMPLATES:
            errors.append(
                "a visibility-gap claim requires consistent omissions for at least "
                f"{MIN_PAIRED_SUPPORTING_TEMPLATES} prompt templates in both runs"
            )
        competitor_runs: dict[str, set[str]] = {}
        for prompt_id in discovery_ids:
            response = responses.get(prompt_id, {})
            for name in response.get("competitor_mentions") or []:
                competitor_runs.setdefault(_normalized(str(name)), set()).add(
                    str(response.get("run_id") or "")
                )
        if not any(run_ids == set(RUN_IDS) for run_ids in competitor_runs.values()):
            errors.append(
                "a visibility-gap claim requires at least one verified competitor to recur in both runs"
            )
    elif conclusion == "no_gap":
        all_mentions = [
            prompt_id
            for prompt_id in DISCOVERY_PROMPT_IDS
            if responses.get(prompt_id, {}).get("clinic_mentioned") is True
        ]
        if len(all_mentions) < MIN_OVERALL_CONSISTENT_OBSERVATIONS:
            errors.append(
                "a no-gap conclusion requires clinic mentions in at least "
                f"{MIN_OVERALL_CONSISTENT_OBSERVATIONS} of the 26 unbranded observations"
            )
        if (
            len(paired_support(all_mentions, mentioned=True))
            < MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS
        ):
            errors.append(
                "a no-gap conclusion requires mentions for at least "
                f"{MIN_OVERALL_CONSISTENT_TEMPLATE_PAIRS} prompt templates in both runs overall"
            )
        supporting = [
            prompt_id
            for prompt_id in discovery_ids
            if responses.get(prompt_id, {}).get("clinic_mentioned") is True
        ]
        if len(supporting) < MIN_GAP_SUPPORTING_OBSERVATIONS:
            errors.append(
                "a no-gap conclusion requires clinic mentions in at least "
                f"{MIN_GAP_SUPPORTING_OBSERVATIONS} unbranded observations"
            )
        if len(paired_support(supporting, mentioned=True)) < MIN_PAIRED_SUPPORTING_TEMPLATES:
            errors.append(
                "a no-gap conclusion requires consistent mentions for at least "
                f"{MIN_PAIRED_SUPPORTING_TEMPLATES} prompt templates in both runs"
            )
    elif conclusion == "mixed":
        mentioned = [
            prompt_id
            for prompt_id in discovery_ids
            if responses.get(prompt_id, {}).get("clinic_mentioned") is True
        ]
        omitted = [
            prompt_id
            for prompt_id in discovery_ids
            if responses.get(prompt_id, {}).get("clinic_mentioned") is False
        ]
        if not mentioned or not omitted:
            errors.append("a mixed conclusion requires both a discovery mention and omission")
        run_ids = {
            str(responses.get(prompt_id, {}).get("run_id") or "")
            for prompt_id in discovery_ids
        }
        if not set(RUN_IDS).issubset(run_ids):
            errors.append("a mixed conclusion must cite observations from both independent runs")
    return errors


def _tracked_artifact_paths(
    check_dir: Path,
    check: Mapping[str, Any] | None = None,
) -> list[Path]:
    if check is None:
        loaded = _read_json(check_dir / "check.json")
        if not isinstance(loaded, Mapping):
            raise VisibilityEvidenceError("check.json must contain an object")
        check = loaded
    paths = [check_dir / "check.json", check_dir / "prompts.json", check_dir / "evidence.json"]
    proofs = check.get("profile_proofs", {})
    if not isinstance(proofs, Mapping):
        raise VisibilityEvidenceError("check must contain profile_proofs for integrity tracking")
    for run_id in RUN_IDS:
        proof = proofs.get(run_id)
        if not isinstance(proof, Mapping):
            raise VisibilityEvidenceError(f"missing profile proof for {run_id}")
        paths.append(_profile_audit_path(check_dir, check, proof))
    paths.append(check_dir / "live-collection.json")
    profile_manifest = check_dir / "logged-out-profiles.json"
    if profile_manifest.exists():
        paths.append(profile_manifest)
    # Preserve and seal the browser runner's original Markdown artifacts as well
    # as the normalized raw response text used for review.
    for run_id in RUN_IDS:
        raw_answer_dir = check_dir / "runner" / run_id / "raw_answers"
        if raw_answer_dir.exists():
            paths.extend(sorted(raw_answer_dir.glob("*.md")))
    for prompt_id in EXPECTED_PROMPT_IDS:
        paths.extend(
            [
                check_dir / "responses" / f"{prompt_id}.json",
                check_dir / "responses" / f"{prompt_id}.txt",
            ]
        )
    return paths


def finalize_visibility_check(
    check_dir: Path | str,
    *,
    reviewer: str,
    conclusion: str,
    claim_summary: str,
    evidence_prompt_ids: Sequence[str],
    reviewed_at: str | datetime | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Review, summarize, and hash a complete visibility check."""

    directory = Path(check_dir)
    if (directory / "integrity.json").exists():
        raise VisibilityEvidenceError("visibility check has already been finalized")
    inspection = _inspect_collection(directory)
    if inspection["errors"]:
        raise VisibilityEvidenceError("collection is incomplete or invalid: " + "; ".join(inspection["errors"]))
    conclusion_key = str(conclusion or "").strip().lower()
    if conclusion_key not in ALLOWED_CONCLUSIONS:
        raise VisibilityEvidenceError(
            "conclusion must be one of: " + ", ".join(sorted(ALLOWED_CONCLUSIONS))
        )
    reviewer_name = _clean_text(reviewer, field="reviewer", minimum=2)
    summary = _clean_text(claim_summary, field="claim_summary", minimum=12)
    cited = [str(value).strip() for value in evidence_prompt_ids if str(value).strip()]
    conclusion_problems = _conclusion_errors(conclusion_key, cited, inspection["responses"])
    if conclusion_problems:
        raise VisibilityEvidenceError("unsupported review conclusion: " + "; ".join(conclusion_problems))

    collected_times = [
        _parse_timestamp(response["collected_at"], field="collected_at")
        for response in inspection["responses"].values()
    ]
    completed_at = max(collected_times)
    evidence_start = min(collected_times)
    reviewed = _parse_timestamp(reviewed_at or _utc_now(), field="reviewed_at")
    if reviewed < completed_at:
        raise VisibilityEvidenceError("reviewed_at cannot predate the completed collection")
    expires_at = evidence_start + EVIDENCE_VALIDITY
    if reviewed >= expires_at:
        raise VisibilityEvidenceError("the collected evidence expired before review")

    discovery = [inspection["responses"][prompt_id] for prompt_id in DISCOVERY_PROMPT_IDS]
    verification = [inspection["responses"][prompt_id] for prompt_id in VERIFICATION_PROMPT_IDS]
    profile_ids = [
        inspection["check"].get("profile_proofs", {}).get(run_id, {}).get("profile_id")
        for run_id in RUN_IDS
    ]
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "visibility_contract_id": VISIBILITY_CONTRACT_ID,
        "check_id": inspection["check"].get("check_id"),
        "sales_account_id": inspection["check"].get("sales_account_id"),
        "platform": inspection["check"].get("platform"),
        "profile_ids": profile_ids,
        "login_state": "logged_out_verified",
        "collection_complete": True,
        "completed_at": _iso(completed_at),
        "evidence_started_at": _iso(evidence_start),
        "expires_at": _iso(expires_at),
        "review": {
            "reviewer": reviewer_name,
            "reviewed_at": _iso(reviewed),
            "conclusion": conclusion_key,
            "claim_summary": summary,
            "evidence_prompt_ids": cited,
        },
        "observations": {
            "target_platform_observation_count": len(inspection["responses"]),
            "discovery_prompt_count": len(discovery),
            "clinic_named_verification_count": len(verification),
            "observations_by_run": {
                run_id: sum(
                    1
                    for response in inspection["responses"].values()
                    if response.get("run_id") == run_id
                )
                for run_id in RUN_IDS
            },
            "discovery_clinic_mention_count": sum(
                1 for response in discovery if response.get("clinic_mentioned") is True
            ),
            "discovery_clinic_omission_count": sum(
                1 for response in discovery if response.get("clinic_mentioned") is False
            ),
            "clinic_mentions_by_prompt": {
                prompt_id: inspection["responses"][prompt_id].get("clinic_mentions", [])
                for prompt_id in EXPECTED_PROMPT_IDS
            },
            "competitor_mentions_by_prompt": {
                prompt_id: inspection["responses"][prompt_id].get("competitor_mentions", [])
                for prompt_id in EXPECTED_PROMPT_IDS
            },
            "clinic_named_prompts_count_toward_gap": False,
            "non_target_analysis_requests_count_toward_observations": False,
        },
        "gap_claim_supported": conclusion_key == "confirmed_gap",
        "passes_outreach_evidence_gate": conclusion_key == "confirmed_gap",
    }
    _atomic_json(directory / "evidence.json", evidence)
    tracked = _tracked_artifact_paths(directory, inspection["check"])
    integrity = {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "hash_algorithm": "sha256",
        "created_at": _iso(reviewed),
        "files": {
            path.relative_to(directory).as_posix(): {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(tracked)
        },
    }
    _atomic_json(directory / "integrity.json", integrity)
    return evidence


def _verify_integrity(check_dir: Path, *, expected_schema_version: str) -> list[str]:
    errors: list[str] = []
    try:
        integrity = _read_json(check_dir / "integrity.json")
    except VisibilityEvidenceError as exc:
        return [str(exc)]
    if not isinstance(integrity, Mapping):
        return ["integrity.json must contain an object"]
    if integrity.get("schema_version") != expected_schema_version:
        errors.append("integrity schema_version does not match check schema_version")
    purpose_error = _purpose_error(
        integrity,
        artifact_kind="outreach visibility integrity manifest",
    )
    if purpose_error:
        errors.append(purpose_error)
    files = integrity.get("files") if isinstance(integrity, dict) else None
    if not isinstance(files, dict):
        return ["integrity.json does not contain a file manifest"]
    try:
        tracked = _tracked_artifact_paths(check_dir)
    except VisibilityEvidenceError as exc:
        return [str(exc)]
    expected = {path.relative_to(check_dir).as_posix() for path in tracked}
    if set(files) != expected:
        errors.append("integrity manifest does not cover the exact required artifact set")
    for relative in sorted(expected):
        path = check_dir / relative
        if not path.exists():
            errors.append(f"hashed artifact is missing: {relative}")
            continue
        row = files.get(relative)
        if not isinstance(row, dict):
            errors.append(f"integrity entry is missing: {relative}")
            continue
        if row.get("sha256") != _sha256(path):
            errors.append(f"artifact hash mismatch: {relative}")
        if row.get("bytes") != path.stat().st_size:
            errors.append(f"artifact byte count mismatch: {relative}")
    return errors


def _validate_legacy_visibility_check(
    check_dir: Path,
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    """Read a signed v1 3+1 artifact without letting it satisfy v2 readiness.

    This deliberately does not reinterpret four old requests as thirty new
    observations.  Basic file hashes and freshness are still reported so old
    evidence remains auditable and useful as historical context.
    """

    errors: list[str] = []
    check: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for name, destination in (("check.json", check), ("evidence.json", evidence)):
        try:
            loaded = _read_json(check_dir / name)
            if not isinstance(loaded, dict):
                errors.append(f"{name} must contain an object")
            else:
                destination.update(loaded)
        except VisibilityEvidenceError as exc:
            errors.append(str(exc))
    try:
        integrity = _read_json(check_dir / "integrity.json")
        files = integrity.get("files") if isinstance(integrity, Mapping) else None
        if not isinstance(files, Mapping):
            errors.append("integrity.json does not contain a file manifest")
        else:
            for relative, row in files.items():
                path = check_dir / str(relative)
                if not path.is_file():
                    errors.append(f"hashed artifact is missing: {relative}")
                    continue
                if not isinstance(row, Mapping) or row.get("sha256") != _sha256(path):
                    errors.append(f"artifact hash mismatch: {relative}")
                if not isinstance(row, Mapping) or row.get("bytes") != path.stat().st_size:
                    errors.append(f"artifact byte count mismatch: {relative}")
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))
    expires_at: datetime | None = None
    try:
        expires_at = _parse_timestamp(evidence.get("expires_at"), field="expires_at")
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))
    fresh = bool(expires_at and checked_at < expires_at)
    prompts: list[Any] = []
    try:
        prompt_payload = _read_json(check_dir / "prompts.json")
        prompts = prompt_payload.get("prompts", []) if isinstance(prompt_payload, Mapping) else []
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))
    ids = [str(row.get("id") or "") for row in prompts if isinstance(row, Mapping)]
    legacy_complete = ids == list(LEGACY_EXPECTED_PROMPT_IDS)
    if not legacy_complete:
        errors.append("legacy prompt basket is not the expected 3+1 set")
    return {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "required_schema_version": SCHEMA_VERSION,
        "visibility_contract_id": "legacy_3_plus_1",
        "check_dir": str(check_dir.resolve()),
        "check_id": check.get("check_id"),
        "sales_account_id": check.get("sales_account_id"),
        "validated_at": _iso(checked_at),
        "status": "legacy_insufficient" if not errors else "invalid",
        "artifact_valid": not errors,
        "collection_complete": legacy_complete and not errors,
        "fresh": fresh,
        "expires_at": _iso(expires_at) if expires_at else None,
        "review_conclusion": evidence.get("review", {}).get("conclusion"),
        "gap_claim_supported": False,
        "passes_outreach_evidence_gate": False,
        "contract_current": False,
        "errors": errors
        + ["legacy 3+1 evidence does not satisfy the 30-observation readiness contract"],
    }


def validate_visibility_check(
    check_dir: Path | str,
    *,
    at: str | datetime | None = None,
) -> dict[str, Any]:
    """Validate a finalized artifact and return a readiness-friendly result."""

    directory = Path(check_dir)
    checked_at = _parse_timestamp(at or _utc_now(), field="validation time")
    try:
        check_header = _read_json(directory / "check.json")
    except VisibilityEvidenceError:
        check_header = {}
    if isinstance(check_header, Mapping) and check_header.get("schema_version") == LEGACY_SCHEMA_VERSION:
        return _validate_legacy_visibility_check(directory, checked_at=checked_at)
    inspection = _inspect_collection(directory)
    errors = list(inspection["errors"])
    check_schema_version = str(inspection["check"].get("schema_version") or "").strip()
    errors.extend(
        _verify_integrity(
            directory,
            expected_schema_version=check_schema_version,
        )
    )
    evidence: dict[str, Any] = {}
    try:
        loaded = _read_json(directory / "evidence.json")
        if isinstance(loaded, dict):
            evidence = loaded
        else:
            errors.append("evidence.json must contain an object")
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))

    conclusion = str(evidence.get("review", {}).get("conclusion") or "")
    evidence_ids = evidence.get("review", {}).get("evidence_prompt_ids") or []
    if conclusion not in ALLOWED_CONCLUSIONS:
        errors.append("review conclusion is missing or invalid")
    else:
        errors.extend(_conclusion_errors(conclusion, evidence_ids, inspection["responses"]))
    if evidence.get("collection_complete") is not True:
        errors.append("evidence is not marked collection_complete")
    if evidence.get("login_state") != "logged_out_verified":
        errors.append("evidence login state is not logged_out_verified")
    if evidence.get("visibility_contract_id") != VISIBILITY_CONTRACT_ID:
        errors.append("evidence visibility contract does not match the 30-observation contract")
    if evidence.get("schema_version") != check_schema_version:
        errors.append("evidence schema_version does not match check schema_version")
    purpose_error = _purpose_error(
        evidence,
        artifact_kind="outreach visibility evidence",
    )
    if purpose_error:
        errors.append(purpose_error)
    for field, expected in (
        ("check_id", inspection["check"].get("check_id")),
        ("sales_account_id", inspection["check"].get("sales_account_id")),
        ("platform", inspection["check"].get("platform")),
        (
            "profile_ids",
            [
                inspection["check"].get("profile_proofs", {}).get(run_id, {}).get("profile_id")
                for run_id in RUN_IDS
            ],
        ),
    ):
        if evidence.get(field) != expected:
            errors.append(f"evidence {field} does not match the check")

    collected_times: list[datetime] = []
    for response in inspection["responses"].values():
        try:
            collected_times.append(
                _parse_timestamp(response.get("collected_at"), field="collected_at")
            )
        except VisibilityEvidenceError:
            pass
    if collected_times:
        expected_started = min(collected_times)
        expected_completed = max(collected_times)
        if evidence.get("evidence_started_at") != _iso(expected_started):
            errors.append("evidence_started_at does not match the raw collection")
        if evidence.get("completed_at") != _iso(expected_completed):
            errors.append("completed_at does not match the raw collection")
        try:
            reviewed_at = _parse_timestamp(
                evidence.get("review", {}).get("reviewed_at"), field="reviewed_at"
            )
            if reviewed_at < expected_completed:
                errors.append("review predates the completed collection")
        except VisibilityEvidenceError as exc:
            errors.append(str(exc))

    expires_at: datetime | None = None
    try:
        expires_at = _parse_timestamp(evidence.get("expires_at"), field="expires_at")
    except VisibilityEvidenceError as exc:
        errors.append(str(exc))
    if expires_at and collected_times and expires_at != min(collected_times) + EVIDENCE_VALIDITY:
        errors.append("expires_at does not apply the 30-day freshness policy")
    fresh = bool(expires_at and checked_at < expires_at)
    if expires_at and not fresh:
        errors.append("visibility evidence has expired")

    if evidence.get("gap_claim_supported") is not (conclusion == "confirmed_gap"):
        errors.append("stored gap_claim_supported does not match the review conclusion")
    if evidence.get("passes_outreach_evidence_gate") is not (conclusion == "confirmed_gap"):
        errors.append("stored outreach evidence gate does not match the review conclusion")
    observations = evidence.get("observations", {})
    if observations.get("target_platform_observation_count") != len(EXPECTED_PROMPT_IDS):
        errors.append("evidence does not contain exactly 30 target-platform observations")
    if observations.get("discovery_prompt_count") != len(DISCOVERY_PROMPT_IDS):
        errors.append("evidence discovery observation count is not 26")
    if observations.get("clinic_named_verification_count") != len(VERIFICATION_PROMPT_IDS):
        errors.append("evidence clinic-named verification count is not 4")
    if observations.get("observations_by_run") != {
        run_id: len(TEMPLATE_PROMPT_IDS) for run_id in RUN_IDS
    }:
        errors.append("evidence does not contain 15 observations from each independent run")
    artifact_valid = not errors
    gap_supported = artifact_valid and conclusion == "confirmed_gap"
    if not artifact_valid:
        status = "invalid"
    elif conclusion == "confirmed_gap":
        status = "approved_gap"
    elif conclusion == "no_gap":
        status = "reviewed_no_gap"
    elif conclusion == "mixed":
        status = "reviewed_mixed"
    else:
        status = "inconclusive"
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": OUTREACH_EVIDENCE_PURPOSE,
        "check_dir": str(directory.resolve()),
        "check_id": inspection["check"].get("check_id"),
        "sales_account_id": inspection["check"].get("sales_account_id"),
        "validated_at": _iso(checked_at),
        "status": status,
        "artifact_valid": artifact_valid,
        "collection_complete": len(inspection["responses"]) == len(EXPECTED_PROMPT_IDS)
        and not inspection["errors"],
        "fresh": fresh,
        "expires_at": _iso(expires_at) if expires_at else None,
        "review_conclusion": conclusion or None,
        "gap_claim_supported": gap_supported,
        "passes_outreach_evidence_gate": gap_supported,
        "contract_current": True,
        "errors": errors,
    }


# Short aliases are useful to callers that model other outreach evidence types.
create_check = create_visibility_check
finalize_check = finalize_visibility_check
validate_check = validate_visibility_check


def _load_profile_arguments(
    path: str,
    profile_ids: Sequence[str] | None,
    *,
    at: str | None,
) -> dict[str, dict[str, Any]]:
    profiles = validate_logged_out_profile_config(Path(path), at=at)
    requested = [str(value) for value in (profile_ids or []) if str(value).strip()]
    if requested:
        indexed = {profile["profile_id"]: profile for profile in profiles}
        missing = [profile_id for profile_id in requested if profile_id not in indexed]
        if missing:
            raise VisibilityEvidenceError(
                "profile_id is not in the proof config: " + ", ".join(missing)
            )
        profiles = [indexed[profile_id] for profile_id in requested]
    if len(profiles) != len(RUN_IDS):
        raise VisibilityEvidenceError(
            "choose exactly two independently audited --profile-id values for run-a and run-b"
        )
    return dict(zip(RUN_IDS, profiles))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    profiles = commands.add_parser("validate-profiles", help="validate logged-out profile proof")
    profiles.add_argument("--profiles-path", required=True)
    profiles.add_argument("--at")

    plan = commands.add_parser("plan", help="create a two-run, 30-observation visibility check")
    plan.add_argument("--sales-account-id", required=True)
    plan.add_argument("--clinic-name", required=True)
    plan.add_argument("--area", required=True)
    plan.add_argument("--treatment", action="append", required=True)
    plan.add_argument("--clinic-alias", action="append", default=[])
    plan.add_argument("--platform", default="chatgpt")
    plan.add_argument("--profiles-path", required=True)
    plan.add_argument("--profile-id", action="append")
    plan.add_argument("--artifacts-root")
    plan.add_argument("--check-id")
    plan.add_argument("--created-at")

    record = commands.add_parser("record", help="record one collected raw output")
    record.add_argument("--check-dir", required=True)
    record.add_argument("--prompt-id", required=True, choices=EXPECTED_PROMPT_IDS)
    record.add_argument("--output-file", required=True)
    record.add_argument("--platform", required=True)
    record.add_argument("--profile-id", required=True)
    record.add_argument("--collected-at")
    record.add_argument("--competitor-mention", action="append", default=[])
    record.add_argument("--status", default="complete")
    record.add_argument("--replace", action="store_true")

    finalize = commands.add_parser("finalize", help="review and hash a complete check")
    finalize.add_argument("--check-dir", required=True)
    finalize.add_argument("--reviewer", required=True)
    finalize.add_argument("--conclusion", required=True, choices=sorted(ALLOWED_CONCLUSIONS))
    finalize.add_argument("--claim-summary", required=True)
    finalize.add_argument("--evidence-prompt", action="append", default=[])
    finalize.add_argument("--reviewed-at")

    validate = commands.add_parser("validate", help="validate a finalized check")
    validate.add_argument("--check-dir", required=True)
    validate.add_argument("--at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-profiles":
            result: Any = {
                "profiles": validate_logged_out_profile_config(args.profiles_path, at=args.at),
            }
        elif args.command == "plan":
            proofs = _load_profile_arguments(args.profiles_path, args.profile_id, at=args.created_at)
            result = {
                "check_dir": str(
                    create_visibility_check(
                        sales_account_id=args.sales_account_id,
                        clinic_name=args.clinic_name,
                        area=args.area,
                        treatments=args.treatment,
                        clinic_aliases=args.clinic_alias,
                        profile_proofs=proofs,
                        platform=args.platform,
                        artifacts_root=args.artifacts_root,
                        check_id=args.check_id,
                        created_at=args.created_at,
                    ).resolve()
                )
            }
        elif args.command == "record":
            result = {
                "response_metadata": str(
                    record_response(
                        args.check_dir,
                        prompt_id=args.prompt_id,
                        output_text=Path(args.output_file).read_text(encoding="utf-8"),
                        platform=args.platform,
                        profile_id=args.profile_id,
                        collected_at=args.collected_at,
                        competitor_mentions=args.competitor_mention,
                        collection_status=args.status,
                        replace=args.replace,
                    ).resolve()
                )
            }
        elif args.command == "finalize":
            result = finalize_visibility_check(
                args.check_dir,
                reviewer=args.reviewer,
                conclusion=args.conclusion,
                claim_summary=args.claim_summary,
                evidence_prompt_ids=args.evidence_prompt,
                reviewed_at=args.reviewed_at,
            )
        else:
            result = validate_visibility_check(args.check_dir, at=args.at)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if args.command == "validate" and not result["artifact_valid"]:
            return 1
        return 0
    except (OSError, VisibilityEvidenceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
