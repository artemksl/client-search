"""Fail-closed review and immutable publication for versioned Visibility Snapshots.

The collection/analysis pipeline owns the evidence in ``data/free-audit/runs``.
This module owns only publication state.  It deliberately does not calculate a
metric or infer a verdict: it hashes the candidate it was given, records the
founder's decision, and promotes an exact byte-for-byte bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from clinic_geo.execution.worker_registry import publish_json

from clinic_geo.workflows.free_audit.contracts_v2 import (
    ANALYSIS_V4_SCHEMA_VERSION,
    LEGACY_SCOPE_V2_SCHEMA_VERSION,
    LEGACY_SCOPE_V4_SCHEMA_VERSION,
    PRESENTATION_V4_SCHEMA_VERSION,
    PROMPT_GEOGRAPHY_HASH,
    PROMPT_GEOGRAPHY_VERSION,
    SCOPE_V2_SCHEMA_VERSION,
    SCOPE_V2_VERSION,
    SCOPE_V4_SCHEMA_VERSION,
    SCOPE_V4_VERSION,
    canonical_sha256,
)
from clinic_geo.workflows.free_audit.purpose_adapter_v1 import (
    FREE_SNAPSHOT_PURPOSE,
    SnapshotPurposeError,
    adapt_legacy_snapshot_artifact,
    reject_market_evidence_references,
    validate_current_snapshot_artifact,
)


PUBLICATION_STATES = frozenset(
    {
        "draft",
        "partial",
        "inconclusive",
        "awaiting_founder_review",
        "prospect_ready",
        "rejected",
    }
)

FOUNDER_CONFIRMATIONS = (
    "clinic_identity",
    "branch_treatment_matrix",
    "treatment_relevance",
    "catchment",
    "focused_competitors",
    "evidence_quality",
    "client_wording",
)

V4_FOUNDER_CONFIRMATIONS = (
    *FOUNDER_CONFIRMATIONS,
    "clinic_archetype_wording",
    "prompt_geography",
)

LEGACY_FOUNDER_CONFIRMATIONS = tuple(
    key for key in FOUNDER_CONFIRMATIONS if key != "branch_treatment_matrix"
)

SUPPORTED_SCOPE_SCHEMAS = {
    SCOPE_V2_VERSION: SCOPE_V2_SCHEMA_VERSION,
    SCOPE_V4_VERSION: SCOPE_V4_SCHEMA_VERSION,
}
LEGACY_SCOPE_SCHEMAS = {
    SCOPE_V2_VERSION: LEGACY_SCOPE_V2_SCHEMA_VERSION,
    SCOPE_V4_VERSION: LEGACY_SCOPE_V4_SCHEMA_VERSION,
}

REQUIRED_CANDIDATE_ARTIFACTS = frozenset({"pdf", "html", "data", "validation"})

APPROVABLE_STATES = frozenset({"awaiting_founder_review", "inconclusive"})
RELEASE_MANIFEST_VERSION = "free_snapshot.release.v3"
LEGACY_RELEASE_MANIFEST_VERSIONS = frozenset({"free_snapshot.release.v2"})
RELEASE_POINTER_VERSION = "free_snapshot.current.v3"
LEGACY_RELEASE_POINTER_VERSIONS = frozenset({"free_snapshot.current.v2"})
CURRENT_RUN_MANIFEST_SCHEMA_VERSION = "free_snapshot.run_manifest.v2"
CURRENT_RUN_MANIFEST_LOCK_SCHEMA_VERSION = "free_snapshot.run_manifest_lock.v2"


class SnapshotPublicationError(RuntimeError):
    """Base class for a publication contract failure."""


class SnapshotNotFoundError(SnapshotPublicationError):
    """A report ID does not resolve to a supported frozen Snapshot run."""


class SnapshotConflictError(SnapshotPublicationError):
    """The requested transition is stale or conflicts with immutable state."""


class SnapshotNotApprovableError(SnapshotPublicationError):
    """The candidate is incomplete, test-only, or otherwise cannot publish."""


def _assert_snapshot_publication_payload(value: object, *, label: str) -> None:
    """Reject Market references and any explicit non-Snapshot purpose."""

    try:
        reject_market_evidence_references(value, label=label)
        if isinstance(value, Mapping) and "purpose" in value:
            validate_current_snapshot_artifact(value, label=label)
    except SnapshotPurposeError as exc:
        raise SnapshotPublicationError(str(exc)) from exc


def _bind_current_snapshot_publication_payload(
    value: object,
    *,
    label: str,
) -> dict:
    """Bind the system-owned Snapshot purpose without accepting an override."""

    if not isinstance(value, Mapping):
        raise SnapshotPublicationError(f"{label} must be a JSON object")
    try:
        reject_market_evidence_references(value, label=label)
        if "purpose" in value:
            return validate_current_snapshot_artifact(value, label=label)
        result = deepcopy(dict(value))
        result["purpose"] = FREE_SNAPSHOT_PURPOSE.value
        return result
    except SnapshotPurposeError as exc:
        raise SnapshotPublicationError(str(exc)) from exc


def _bind_candidate_payloads(
    analysis: Mapping,
    presentation: Mapping,
    quality: Mapping,
) -> tuple[dict, dict, dict]:
    """Purpose-bind current candidate artifacts and preserve their hash graph."""

    bound_analysis = _bind_current_snapshot_publication_payload(
        analysis,
        label="Snapshot analysis",
    )
    bound_presentation = _bind_current_snapshot_publication_payload(
        presentation,
        label="Snapshot presentation",
    )
    bound_quality = _bind_current_snapshot_publication_payload(
        quality,
        label="Snapshot quality",
    )

    if "purpose" not in analysis:
        old_results_hash = str(bound_analysis.pop("results_hash", ""))
        try:
            from clinic_geo.workflows.free_audit.analysis_v2 import compute_results_hash

            new_results_hash = compute_results_hash(bound_analysis)
        except (ImportError, AttributeError):
            payload = deepcopy(bound_analysis)
            for key in (
                "results_hash",
                "presentation",
                "quality",
                "publication_state",
                "founder_review",
                "approval",
            ):
                payload.pop(key, None)
            new_results_hash = sha256_json(payload)
        bound_analysis["results_hash"] = new_results_hash
        if old_results_hash and old_results_hash != new_results_hash:
            bound_presentation["results_hash"] = new_results_hash
            for field in ("scope_definition", "artifact_identity", "presentation"):
                nested = bound_presentation.get(field)
                if isinstance(nested, Mapping) and "results_hash" in nested:
                    updated = dict(nested)
                    updated["results_hash"] = new_results_hash
                    bound_presentation[field] = updated

    return bound_analysis, bound_presentation, bound_quality


def _assert_scope_manifest_hash(value: Mapping) -> None:
    """Validate a raw scope's historical hash without changing its payload."""

    declared = str(value.get("scope_hash") or "").strip()
    unsigned = deepcopy(dict(value))
    unsigned.pop("scope_hash", None)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", declared)
        or declared != canonical_sha256(unsigned)
    ):
        raise SnapshotConflictError(
            "Snapshot scope hash does not match the frozen manifest"
        )


def _publication_release_manifest(value: Mapping) -> dict:
    """Validate a current release or explicitly adapt a historical v2 view."""

    version = str(value.get("release_version") or "").strip()
    try:
        if version == RELEASE_MANIFEST_VERSION:
            return validate_current_snapshot_artifact(
                value,
                label="Snapshot release manifest",
            )
        if version in LEGACY_RELEASE_MANIFEST_VERSIONS:
            if "purpose" in value:
                return validate_current_snapshot_artifact(
                    value,
                    label="purpose-bound historical Snapshot release manifest",
                )
            return adapt_legacy_snapshot_artifact(
                value,
                label="historical Snapshot release manifest",
                artifact_kind="release_manifest",
                supported_schema_versions=LEGACY_RELEASE_MANIFEST_VERSIONS,
                schema_field="release_version",
            )
    except SnapshotPurposeError as exc:
        raise SnapshotPublicationError(str(exc)) from exc
    raise SnapshotPublicationError(
        f"Snapshot release manifest version {version!r} is unsupported"
    )


def _publication_release_pointer(
    value: Mapping,
    *,
    manifest: Mapping,
    manifest_path: Path,
) -> dict:
    """Validate a current pointer or explicitly adapt a genuine legacy pointer."""

    version = str(value.get("pointer_version") or "").strip()
    try:
        if version == RELEASE_POINTER_VERSION:
            pointer = validate_current_snapshot_artifact(
                value,
                label="Snapshot current-release pointer",
            )
            if pointer.get("release_version") != RELEASE_MANIFEST_VERSION:
                raise SnapshotPurposeError(
                    "Current Snapshot pointer does not require the current release schema"
                )
            if manifest.get("release_version") != RELEASE_MANIFEST_VERSION:
                raise SnapshotPurposeError(
                    "Current Snapshot pointer cannot downgrade to a legacy release manifest"
                )
            declared_hash = str(pointer.get("release_manifest_sha256") or "")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", declared_hash)
                or declared_hash != sha256_file(manifest_path)
            ):
                raise SnapshotPurposeError(
                    "Current Snapshot pointer release-manifest hash does not verify"
                )
        elif version in LEGACY_RELEASE_POINTER_VERSIONS:
            if "purpose" in value:
                pointer = validate_current_snapshot_artifact(
                    value,
                    label="purpose-bound historical Snapshot current-release pointer",
                )
                if manifest.get("release_version") not in (
                    RELEASE_MANIFEST_VERSION,
                    *LEGACY_RELEASE_MANIFEST_VERSIONS,
                ):
                    raise SnapshotPurposeError(
                        "Purpose-bound historical Snapshot pointer names an unsupported release"
                    )
            else:
                pointer = adapt_legacy_snapshot_artifact(
                    value,
                    label="historical Snapshot current-release pointer",
                    artifact_kind="release_pointer",
                    supported_schema_versions=LEGACY_RELEASE_POINTER_VERSIONS,
                    schema_field="pointer_version",
                )
            if (
                "purpose" not in value
                and manifest.get("release_version") not in LEGACY_RELEASE_MANIFEST_VERSIONS
            ):
                raise SnapshotPurposeError(
                    "Historical Snapshot pointer cannot name a current release manifest"
                )
        else:
            raise SnapshotPurposeError(
                f"Snapshot current-release pointer version {version!r} is unsupported"
            )
    except SnapshotPurposeError as exc:
        raise SnapshotPublicationError(str(exc)) from exc

    mismatches = [
        field
        for field in ("release_id", "report_id", "clinic_id")
        if value.get(field) is not None
        and manifest.get(field) is not None
        and value.get(field) != manifest.get(field)
    ]
    if mismatches:
        raise SnapshotPublicationError(
            "Snapshot current-release pointer disagrees with its manifest: "
            + ", ".join(mismatches)
        )
    return pointer


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _nested(mapping: Mapping, path: str) -> object:
    value: object = mapping
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _declared_hash(mapping: Mapping, *paths: str) -> str:
    for path in paths:
        value = _nested(mapping, path)
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _verified_results_hash(analysis: Mapping) -> str:
    declared = _declared_hash(analysis, "results_hash", "artifact_identity.results_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise SnapshotPublicationError("Analysis has no valid declared results_hash")

    # Prefer the core verifier when available.  The fallback is the v2 canonical
    # convention: hash the complete analysis object before results_hash is added.
    try:
        from clinic_geo.workflows.free_audit.analysis_v2 import verify_results_hash
    except (ImportError, AttributeError):
        verify_results_hash = None
    if verify_results_hash is not None:
        if not verify_results_hash(analysis):
            raise SnapshotConflictError("Analysis results_hash does not verify")
        return declared

    payload = deepcopy(dict(analysis))
    for key in (
        "results_hash",
        "presentation",
        "quality",
        "publication_state",
        "founder_review",
        "approval",
    ):
        payload.pop(key, None)
    if sha256_json(payload) != declared:
        raise SnapshotConflictError("Analysis results_hash does not match the canonical results payload")
    return declared


def _verified_copy_hash(presentation: Mapping) -> str:
    declared = _declared_hash(
        presentation,
        "presentation.copy_hash",
        "artifact_identity.copy_hash",
        "copy_hash",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise SnapshotPublicationError("Presentation has no valid deterministic copy_hash")
    try:
        from clinic_geo.reporting.presentation import build_presentation_model

        computed = str(build_presentation_model(presentation).get("copy_hash") or "")
    except Exception as exc:
        raise SnapshotPublicationError(f"Presentation copy_hash cannot be verified: {exc}") from exc
    if computed != declared:
        raise SnapshotConflictError("Presentation copy_hash does not match its deterministic copy deck")
    return declared


def _verify_artifact_identity(
    presentation: Mapping,
    *,
    scope_hash: str,
    results_hash: str,
    copy_hash: str,
) -> None:
    expected = {
        "scope_hash": scope_hash,
        "results_hash": results_hash,
        "copy_hash": copy_hash,
    }
    for key, value in expected.items():
        declared = _declared_hash(
            presentation,
            key,
            f"scope_definition.{key}",
            f"artifact_identity.{key}",
            f"presentation.{key}",
        )
        if not declared or declared != value:
            raise SnapshotConflictError(f"Presentation {key} disagrees with the reviewed candidate")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SnapshotNotFoundError(f"Missing Snapshot file: {path.name}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotPublicationError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotPublicationError(f"{path.name} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: object) -> None:
    """Replace a JSON file atomically without exposing a partial pointer/state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def safe_slug(value: object, *, fallback: str = "unknown-clinic") -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return clean[:100] or fallback


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _scope_version(manifest: Mapping) -> str:
    return str(manifest.get("scope_version") or "").strip()


def _review_confirmations_for_manifest(manifest: Mapping) -> tuple[str, ...]:
    if _scope_version(manifest) == SCOPE_V4_VERSION:
        if manifest.get("scope_kind") == "area_treatments" and manifest.get("clinic_archetype") is None:
            return tuple(key for key in V4_FOUNDER_CONFIRMATIONS if key != "clinic_archetype_wording")
        return V4_FOUNDER_CONFIRMATIONS
    if isinstance(manifest.get("areas"), list) and manifest.get("areas"):
        return FOUNDER_CONFIRMATIONS
    return LEGACY_FOUNDER_CONFIRMATIONS


def _v4_confirmation_context(manifest: Mapping) -> dict:
    """Return the exact patient-facing v4 choices bound into founder review."""

    if _scope_version(manifest) != SCOPE_V4_VERSION:
        return {}

    clinic = manifest.get("clinic") if isinstance(manifest.get("clinic"), Mapping) else {}
    archetype = (
        manifest.get("clinic_archetype")
        if isinstance(manifest.get("clinic_archetype"), Mapping)
        else clinic.get("archetype")
        if isinstance(clinic.get("archetype"), Mapping)
        else None
    )
    frozen_archetype = None
    treatment_only = manifest.get("scope_kind") == "area_treatments" and manifest.get("clinic_archetype") is None
    if not treatment_only:
        if not isinstance(archetype, Mapping):
            raise SnapshotPublicationError(
                "A v4 Snapshot must freeze the clinic archetype wording before publication"
            )
        frozen_archetype = {
            key: str(archetype.get(key) or "").strip()
            for key in ("id", "singular", "plural")
        }
        if not all(frozen_archetype.values()) or archetype.get("confirmation_status") != "confirmed":
            raise SnapshotPublicationError(
                "A v4 Snapshot must contain confirmed singular and plural clinic archetype wording"
            )

    geography_version = str(manifest.get("prompt_geography_version") or "").strip()
    geography_hash = str(manifest.get("prompt_geography_hash") or "").strip()
    if geography_version != PROMPT_GEOGRAPHY_VERSION:
        raise SnapshotPublicationError(
            "The v4 prompt-geography version does not match the publication contract"
        )
    if geography_hash != PROMPT_GEOGRAPHY_HASH:
        raise SnapshotPublicationError(
            "The v4 prompt-geography hash does not match the publication contract"
        )

    areas = manifest.get("areas")
    if not isinstance(areas, list) or not areas:
        raise SnapshotPublicationError(
            "A v4 Snapshot must freeze its prompt-geography selections by area"
        )

    selections = []
    for index, area in enumerate(areas, 1):
        if not isinstance(area, Mapping):
            raise SnapshotPublicationError(f"v4 area {index} is not a frozen object")
        geographies = area.get("geographies")
        if not isinstance(geographies, Mapping):
            raise SnapshotPublicationError(
                f"v4 area {index} has no frozen prompt-geography selections"
            )

        targets = {}
        for tier in ("local", "greater_area", "city"):
            raw = geographies.get(tier)
            if not isinstance(raw, Mapping):
                raise SnapshotPublicationError(
                    f"v4 area {index} has no {tier} prompt-geography selection"
                )
            target = {
                "id": str(raw.get("id") or "").strip(),
                "name": str(raw.get("name") or "").strip(),
                "country_code": str(raw.get("country_code") or "").strip().upper(),
            }
            if (
                not target["id"]
                or not target["name"]
                or len(target["country_code"]) != 2
                or raw.get("confirmation_status") != "confirmed"
            ):
                raise SnapshotPublicationError(
                    f"v4 area {index} has an unconfirmed or incomplete {tier} geography"
                )
            targets[tier] = target

        branch = area.get("branch") if isinstance(area.get("branch"), Mapping) else {}
        catchment = (
            area.get("catchment")
            if isinstance(area.get("catchment"), Mapping)
            else {}
        )
        selections.append(
            {
                "area_id": str(
                    catchment.get("area_id") or targets["local"]["id"]
                ).strip(),
                "branch_id": str(branch.get("id") or "").strip() or None,
                **targets,
            }
        )

    return {
        **({"clinic_archetype_wording": frozen_archetype} if frozen_archetype is not None else {}),
        "prompt_geography": {
            "version": geography_version,
            "hash": geography_hash,
            "selections": selections,
            "selections_sha256": sha256_json(selections),
        },
    }


def _assert_candidate_schema_compatibility(
    manifest: Mapping,
    analysis: Mapping,
    presentation: Mapping,
) -> None:
    """Fail closed on cross-version v4 artifacts without tightening legacy v2/v3."""

    if _scope_version(manifest) != SCOPE_V4_VERSION:
        return
    analysis_schema = str(analysis.get("schema_version") or "").strip()
    if analysis_schema == "free_snapshot.analysis.v3.presence":
        pass
    elif analysis_schema != ANALYSIS_V4_SCHEMA_VERSION:
        raise SnapshotPublicationError(
            f"A scope {SCOPE_V4_VERSION} Snapshot requires {ANALYSIS_V4_SCHEMA_VERSION} analysis"
        )
    presentation_schema = str(presentation.get("schema_version") or "").strip()
    if presentation_schema not in {
        ANALYSIS_V4_SCHEMA_VERSION,
        PRESENTATION_V4_SCHEMA_VERSION,
    }:
        raise SnapshotPublicationError(
            "A scope 4.0 Snapshot requires a v4 presentation artifact"
        )


def _analysis_verdict(analysis: Mapping) -> str:
    value = analysis.get("overall_verdict")
    if isinstance(value, Mapping):
        value = value.get("label") or value.get("status")
    return str(value or "").strip()


def _quality_blockers(quality: Mapping) -> list[str]:
    # An explicit release-blocker list distinguishes hard integrity/collection
    # failures from evidence limitations that legitimately support an
    # inconclusive, founder-approvable report.
    if "release_blockers" in quality:
        raw = quality.get("release_blockers")
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [
            str(item.get("message") or item.get("code") or "").strip()
            if isinstance(item, Mapping)
            else str(item or "").strip()
            for item in raw
            if (
                str(item.get("message") or item.get("code") or "").strip()
                if isinstance(item, Mapping)
                else str(item or "").strip()
            )
        ]
    for key in ("release_blockers", "blockers", "blocking_reasons"):
        values = _as_string_list(quality.get(key))
        if values:
            return values
    return []


def publication_state_for_candidate(
    analysis: Mapping,
    *,
    test_mode: bool,
    quality: Optional[Mapping] = None,
) -> tuple[str, list[str]]:
    """Map already-computed quality/verdict data to a publication state.

    Missing quality is treated as blocking.  This prevents a legacy/v1 result or
    an underspecified candidate from becoming approvable by accident.
    """
    quality = dict(quality or {})
    blockers = _quality_blockers(quality)
    if test_mode:
        blockers.append("Test mode uses one run and is permanently barred from prospect release.")

    required_complete_flags = (
        "collection_complete",
        "extraction_complete",
        "identity_complete",
        "integrity_complete",
        "copy_check_passed",
        "render_complete",
        "artifact_agreement_complete",
    )
    missing_flags = [key for key in required_complete_flags if quality.get(key) is not True]
    for key in missing_flags:
        blockers.append(f"{key.replace('_', ' ').capitalize()} is not confirmed.")

    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return "partial", blockers

    verdict = _analysis_verdict(analysis)
    if verdict == "More evidence required":
        return "inconclusive", []
    if verdict in {"Material opportunity demonstrated", "No strong opportunity currently visible"}:
        return "awaiting_founder_review", []

    return "partial", ["The analysis does not contain a recognized v2 overall verdict."]


@dataclass(frozen=True)
class CandidateHashes:
    scope_hash: str
    results_hash: str
    copy_hash: str
    review_bundle_sha256: str

    def as_dict(self) -> dict:
        return {
            "scope_hash": self.scope_hash,
            "results_hash": self.results_hash,
            "copy_hash": self.copy_hash,
            "review_bundle_sha256": self.review_bundle_sha256,
        }


class SnapshotPublicationStore:
    """Filesystem-backed lifecycle for supported Snapshot candidates and releases."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        release_artifact_builder: Optional[
            Callable[[Path, Mapping, Mapping], Mapping[str, str | Path]]
        ] = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.runs_root = self.repo_root / "data" / "free-audit" / "runs"
        self.releases_root = self.repo_root / "audit-results" / "releases"
        self.release_artifact_builder = release_artifact_builder or self._render_prospect_release

    def run_dir(self, report_id: str) -> Path:
        clean = str(report_id or "").strip()
        if not clean or clean in {".", ".."} or Path(clean).name != clean:
            raise SnapshotNotFoundError("Invalid report ID")
        candidate = (self.runs_root / clean).resolve()
        if candidate.parent != self.runs_root.resolve():
            raise SnapshotNotFoundError("Invalid report ID")
        return candidate

    def _versioned_manifest(self, report_id: str) -> tuple[Path, dict]:
        run_dir = self.run_dir(report_id)
        raw_manifest = read_json(run_dir / "scope_manifest.json")
        _assert_snapshot_publication_payload(
            raw_manifest,
            label="Snapshot scope manifest",
        )
        _assert_scope_manifest_hash(raw_manifest)
        declared_schema = str(raw_manifest.get("schema_version") or "").strip()
        current_schema_era = declared_schema in frozenset(
            SUPPORTED_SCOPE_SCHEMAS.values()
        )
        legacy_schema_era = not declared_schema or declared_schema in frozenset(
            LEGACY_SCOPE_SCHEMAS.values()
        )
        if not current_schema_era and not legacy_schema_era:
            raise SnapshotNotFoundError(
                f"Snapshot {report_id!r} has unsupported scope schema {declared_schema!r}"
            )

        run_manifest_path = run_dir / "run_manifest.json"
        run_manifest_lock_path = run_dir / "run_manifest.lock.json"
        current_sentinel = False
        for sentinel_path, current_schema in (
            (run_manifest_path, CURRENT_RUN_MANIFEST_SCHEMA_VERSION),
            (run_manifest_lock_path, CURRENT_RUN_MANIFEST_LOCK_SCHEMA_VERSION),
        ):
            if sentinel_path.is_file():
                sentinel = read_json(sentinel_path)
                if sentinel.get("schema_version") == current_schema:
                    current_sentinel = True

        if current_schema_era and "purpose" not in raw_manifest:
            raise SnapshotPublicationError(
                "Current Snapshot scope manifest has no explicit evidence purpose"
            )
        if "purpose" in raw_manifest:
            try:
                manifest = validate_current_snapshot_artifact(
                    raw_manifest,
                    label="Snapshot scope manifest",
                )
            except SnapshotPurposeError as exc:
                raise SnapshotPublicationError(str(exc)) from exc
        else:
            if current_sentinel:
                raise SnapshotPublicationError(
                    "Current Snapshot scope manifest has no explicit evidence purpose"
                )
            try:
                schema_field = (
                    "schema_version"
                    if str(raw_manifest.get("schema_version") or "").strip()
                    else "scope_version"
                )
                supported_versions = (
                    tuple(LEGACY_SCOPE_SCHEMAS.values())
                    if schema_field == "schema_version"
                    else tuple(LEGACY_SCOPE_SCHEMAS)
                )
                manifest = adapt_legacy_snapshot_artifact(
                    raw_manifest,
                    label="historical Snapshot scope manifest",
                    artifact_kind="scope_manifest",
                    supported_schema_versions=supported_versions,
                    schema_field=schema_field,
                )
            except SnapshotPurposeError as exc:
                raise SnapshotPublicationError(str(exc)) from exc
        scope_version = _scope_version(manifest)
        if scope_version not in SUPPORTED_SCOPE_SCHEMAS:
            raise SnapshotNotFoundError(
                f"Snapshot {report_id!r} has unsupported scope version {scope_version!r}"
            )
        is_legacy = legacy_schema_era
        expected_schema = (
            LEGACY_SCOPE_SCHEMAS if is_legacy else SUPPORTED_SCOPE_SCHEMAS
        )[scope_version]
        declared_schema = str(manifest.get("schema_version") or "").strip()
        if declared_schema and declared_schema != expected_schema:
            raise SnapshotNotFoundError(
                f"Snapshot {report_id!r} is not a valid {expected_schema} manifest"
            )
        if not is_legacy and not declared_schema:
            raise SnapshotNotFoundError(
                f"Snapshot {report_id!r} has no current purpose-era scope schema"
            )
        manifest_id = str(manifest.get("snapshot_id") or "")
        target_binding = self._report_target_binding(run_dir, manifest, report_id)
        if manifest_id and manifest_id != report_id and target_binding is None:
            raise SnapshotConflictError("Report ID does not match the frozen scope")
        return run_dir, manifest

    def _report_target_binding(self, run_dir: Path, manifest: Mapping, report_id: str) -> Optional[dict]:
        """Read a separate report identity without editing measurement scope."""
        path = run_dir / "report_target.json"
        if path.is_symlink():
            raise SnapshotConflictError("Report target must not be a symbolic link")
        if not path.exists():
            if manifest.get("scope_kind") == "area_treatments":
                raise SnapshotConflictError("Area measurement requires a separate report target")
            return None
        from clinic_geo.visibility_reports.api import verify_report_target_binding
        try:
            return verify_report_target_binding(read_json(path), manifest, report_id)
        except ValueError as exc:
            raise SnapshotConflictError(str(exc)) from exc

    def _assert_report_target_analysis(self, run_dir: Path, manifest: Mapping,
                                       analysis: Optional[Mapping]) -> Optional[dict]:
        binding = self._report_target_binding(run_dir, manifest, run_dir.name)
        # The publication reader adds an explicit purpose compatibility view to
        # runner scopes. Compare the analysis with the hash-bound source, not
        # adapter-only metadata, while retaining the read-only effective view.
        source_manifest = manifest
        if manifest.get("purpose_compatibility") is not None:
            from clinic_geo.evidence.purpose import legacy_source_payload
            try:
                source_manifest = legacy_source_payload(manifest)
            except (TypeError, ValueError) as exc:
                raise SnapshotConflictError("Invalid scope compatibility view") from exc
        if analysis is not None:
            if binding is None:
                if analysis.get("report_target") is not None or analysis.get("report_target_binding") is not None:
                    raise SnapshotConflictError("Analysis report target has no immutable binding")
            elif (analysis.get("report_target_binding") != binding
                  or analysis.get("report_target") != binding["report_target"]
                  or analysis.get("report_id") != binding["report_id"]
                  or analysis.get("run_id") != binding["measurement_run_id"]
                  or analysis.get("scope_hash") != binding["scope_hash"]
                  or analysis.get("frozen_scope") != source_manifest):
                raise SnapshotConflictError("Analysis report target differs from its immutable binding")
        return binding

    def list_snapshots(self) -> list[dict]:
        if not self.runs_root.is_dir():
            return []
        rows = []
        for path in self.runs_root.iterdir():
            if not path.is_dir():
                continue
            try:
                detail = self.get_snapshot(path.name)
            except SnapshotPublicationError:
                continue
            rows.append(self._summary(detail))
        rows.sort(key=lambda row: (row.get("updated_at") or "", row["report_id"]), reverse=True)
        return rows

    def get_snapshot(self, report_id: str) -> dict:
        run_dir, manifest = self._versioned_manifest(report_id)
        state_path = run_dir / "snapshot_state.json"
        state = read_json(state_path) if state_path.is_file() else self._initial_state(report_id, manifest)
        _assert_snapshot_publication_payload(state, label="Snapshot publication state")
        if state.get("publication_state") not in PUBLICATION_STATES:
            raise SnapshotPublicationError("Snapshot has an invalid publication state")

        analysis = read_json(run_dir / "analysis.json") if (run_dir / "analysis.json").is_file() else None
        competitor_review = (
            read_json(run_dir / "competitor_review.json")
            if (run_dir / "competitor_review.json").is_file()
            else None
        )
        competitor_resolutions = (
            read_json(run_dir / "competitor_resolutions.json")
            if (run_dir / "competitor_resolutions.json").is_file()
            else None
        )
        founder_review = (
            read_json(run_dir / "founder_review.json")
            if (run_dir / "founder_review.json").is_file()
            else None
        )
        presentation = (
            read_json(run_dir / "presentation.json")
            if (run_dir / "presentation.json").is_file()
            else None
        )
        for label, value in (
            ("stored Snapshot analysis", analysis),
            ("stored Snapshot presentation", presentation),
            ("stored Snapshot founder review", founder_review),
            ("stored Snapshot competitor review", competitor_review),
            ("stored Snapshot competitor resolutions", competitor_resolutions),
        ):
            if value is not None:
                _assert_snapshot_publication_payload(value, label=label)
        stored_artifacts = (
            state.get("candidate_artifacts")
            if isinstance(state.get("candidate_artifacts"), Mapping)
            else {}
        )
        self._assert_json_candidate_artifacts(run_dir, stored_artifacts)
        current_hashes = self._current_hashes(run_dir, manifest, analysis, presentation, state)
        approval_stale = bool(
            founder_review
            and founder_review.get("decision") == "approved"
            and any(founder_review.get(key) != value for key, value in current_hashes.as_dict().items())
        )

        target_binding = self._assert_report_target_analysis(run_dir, manifest, analysis)
        clinic = (target_binding["report_target"] if target_binding else
                  manifest.get("clinic") if isinstance(manifest.get("clinic"), dict) else {})
        catchment = manifest.get("catchment") if isinstance(manifest.get("catchment"), dict) else {}
        confirmation_context = _v4_confirmation_context(manifest)
        areas = manifest.get("areas") if isinstance(manifest.get("areas"), list) else []
        if not areas and catchment:
            areas = [
                {
                    "branch": {"id": catchment.get("area_id"), "name": catchment.get("name")},
                    "catchment": catchment,
                    "treatments": manifest.get("treatments") or [],
                    "validation_status": "confirmed",
                }
            ]
        detail = {
            **state,
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "snapshot_id": report_id,
            "scope_version": _scope_version(manifest),
            "scope": manifest,
            "clinic": clinic,
            **({"report_target": clinic, "measurement_run_id": target_binding["measurement_run_id"]}
               if target_binding else {}),
            "catchment": catchment,
            "treatments": manifest.get("treatments") or [],
            "areas": areas,
            "platforms": manifest.get("platforms") or [],
            "analysis": analysis,
            "competitor_review": competitor_review,
            "competitor_resolutions": competitor_resolutions,
            "presentation": presentation,
            "founder_review": founder_review,
            "approval_stale": approval_stale,
            "current_hashes": current_hashes.as_dict(),
            "review_confirmations": list(_review_confirmations_for_manifest(manifest)),
            **(
                {"review_confirmation_context": confirmation_context}
                if confirmation_context
                else {}
            ),
        }
        return detail

    def register_candidate(
        self,
        report_id: str,
        *,
        analysis: Mapping,
        presentation: Mapping,
        quality: Mapping,
        candidate_artifacts: Optional[Mapping[str, str | Path]] = None,
        test_mode: bool = False,
        operational_state: str = "complete",
    ) -> dict:
        run_dir, manifest = self._versioned_manifest(report_id)
        if type(test_mode) is not bool:
            raise SnapshotPublicationError("Snapshot test_mode must be a Boolean")
        bound_analysis, bound_presentation, bound_quality = _bind_candidate_payloads(
            analysis,
            presentation,
            quality,
        )
        self._assert_report_target_analysis(run_dir, manifest, bound_analysis)
        if not isinstance(candidate_artifacts or {}, Mapping):
            raise SnapshotPublicationError(
                "Snapshot candidate artifact manifest must be a JSON object"
            )
        _assert_snapshot_publication_payload(
            candidate_artifacts or {},
            label="Snapshot candidate artifact manifest",
        )
        _assert_candidate_schema_compatibility(
            manifest,
            bound_analysis,
            bound_presentation,
        )

        state_path = run_dir / "snapshot_state.json"
        existing = read_json(state_path) if state_path.is_file() else self._initial_state(report_id, manifest)
        if existing.get("test_mode") is True and test_mode is not True:
            raise SnapshotConflictError("Snapshot test mode is permanent and cannot be removed")
        _assert_snapshot_publication_payload(
            existing,
            label="existing Snapshot publication state",
        )
        if existing.get("publication_state") == "prospect_ready":
            raise SnapshotConflictError("A published Snapshot is immutable; create a new Snapshot")

        artifact_manifest = self._artifact_manifest(run_dir, candidate_artifacts or {})
        self._assert_json_candidate_artifacts(run_dir, artifact_manifest)
        missing_artifacts = sorted(REQUIRED_CANDIDATE_ARTIFACTS - set(artifact_manifest))
        effective_quality = dict(bound_quality)
        if missing_artifacts:
            existing_blockers = _quality_blockers(effective_quality)
            effective_quality["release_blockers"] = [
                *existing_blockers,
                "Candidate report bundle is incomplete: " + ", ".join(missing_artifacts) + ".",
            ]
        state_name, blockers = publication_state_for_candidate(
            bound_analysis,
            test_mode=test_mode,
            quality=effective_quality,
        )
        results_hash = _verified_results_hash(bound_analysis)
        if existing.get("report_revision_id") == results_hash:
            if existing.get("test_mode") is not test_mode:
                raise SnapshotConflictError("Report revision publication mode changed")
            # Delivery of an identical candidate does not make a second report
            # revision or change the review timestamps.
            if existing.get("candidate_artifacts") != artifact_manifest:
                raise SnapshotConflictError("Report revision artifacts changed")
            return self.get_snapshot(report_id)
        copy_hash = _verified_copy_hash(bound_presentation)
        scope_hash = str(manifest.get("scope_hash") or "")
        if not scope_hash:
            raise SnapshotPublicationError("Frozen scope has no scope_hash")
        _verify_artifact_identity(
            bound_presentation,
            scope_hash=scope_hash,
            results_hash=results_hash,
            copy_hash=copy_hash,
        )
        prompt_pack_hash = str(manifest.get("prompt_pack_hash") or "")
        presented_prompt_hash = _declared_hash(
            bound_presentation,
            "prompt_pack_hash",
            "scope_definition.prompt_pack_hash",
            "artifact_identity.prompt_pack_hash",
        )
        if not prompt_pack_hash or presented_prompt_hash != prompt_pack_hash:
            raise SnapshotConflictError(
                "Presentation prompt_pack_hash disagrees with the frozen scope"
            )
        confirmation_context = _v4_confirmation_context(manifest)
        review_hash = self._review_bundle_hash(
            report_id,
            scope_hash=scope_hash,
            results_hash=results_hash,
            copy_hash=copy_hash,
            artifact_manifest=artifact_manifest,
            confirmation_context=confirmation_context,
        )
        now = utc_now()
        history = list(existing.get("state_history") or [])
        if not history or history[-1].get("publication_state") != state_name:
            history.append(
                {
                    "at": now,
                    "operational_state": operational_state,
                    "publication_state": state_name,
                    "reason": "candidate_registered",
                }
            )
        state = {
            **existing,
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "scope_version": _scope_version(manifest),
            "operational_state": operational_state,
            "publication_state": state_name,
            "test_mode": bool(test_mode),
            "scope_hash": scope_hash,
            "results_hash": results_hash,
            "copy_hash": copy_hash,
            "review_bundle_sha256": review_hash,
            "quality": effective_quality,
            "blockers": blockers,
            "candidate_artifacts": artifact_manifest,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "report_revision_id": results_hash,
            "prior_report_revision_id": existing.get("report_revision_id") or existing.get("results_hash"),
            "state_history": history,
            **(
                {
                    "prompt_geography_version": confirmation_context[
                        "prompt_geography"
                    ]["version"],
                    "prompt_geography_hash": confirmation_context[
                        "prompt_geography"
                    ]["hash"],
                    "prompt_geography_selections_sha256": confirmation_context[
                        "prompt_geography"
                    ]["selections_sha256"],
                }
                if confirmation_context
                else {}
            ),
        }
        # Report files are immutable revision bundles. Preserve the old review
        # envelope before updating these compatibility projections as well.
        # This does not relax approval or permit replacing a published release.
        if existing.get("results_hash") and (run_dir / "analysis.json").is_file():
            prior = {
                "schema_version": "free_snapshot.report_registration.v1",
                "analysis": read_json(run_dir / "analysis.json"),
                "presentation": read_json(run_dir / "presentation.json"),
                "state": existing,
            }
            prior_hash = sha256_json(prior)
            prior_path = run_dir / "report-history" / prior_hash / "registration.json"
            try:
                publish_json(prior_path, prior)
            except FileExistsError:
                if read_json(prior_path) != prior:
                    raise SnapshotConflictError("Prior report registration is corrupt") from None
            state["prior_registration_sha256"] = prior_hash
        # Old review/approval data belongs to the archived revision. A new
        # candidate must pass the existing founder review again.
        for key in ("approval", "founder_review", "release_id", "approved_at", "approved_by"):
            state.pop(key, None)
        revision = {
            "schema_version": "free_snapshot.report_registration.v1",
            "analysis": bound_analysis,
            "presentation": bound_presentation,
            "state": state,
        }
        revision_path = run_dir / "report-revisions" / results_hash / "registration.json"
        if revision_path.exists():
            saved = read_json(revision_path)
            if saved.get("analysis") != bound_analysis or saved.get("presentation") != bound_presentation:
                raise SnapshotConflictError("Uncommitted report registration conflicts")
            # A crash after immutable publication may leave old mutable
            # projections. Reuse the exact committed timestamps and lineage.
            state = saved["state"]
            revision = saved
        else:
            publish_json(revision_path, revision)
        commit = {"schema_version": "free_snapshot.report_registration_commit.v1", "revision_id": results_hash, "sha256": sha256_json(revision)}
        commit_path = revision_path.with_name("registration-commit.json")
        try:
            publish_json(commit_path, commit)
        except FileExistsError:
            if read_json(commit_path) != commit:
                raise SnapshotConflictError("Report registration commit is corrupt") from None
        atomic_write_json(run_dir / "analysis.json", bound_analysis)
        atomic_write_json(run_dir / "presentation.json", bound_presentation)
        atomic_write_json(state_path, state)
        _refresh_telemetry(self.repo_root)
        return self.get_snapshot(report_id)

    def record_operational_state(
        self,
        report_id: str,
        operational_state: str,
        *,
        detail: str = "",
    ) -> dict:
        run_dir, manifest = self._versioned_manifest(report_id)
        state_path = run_dir / "snapshot_state.json"
        state = read_json(state_path) if state_path.is_file() else self._initial_state(report_id, manifest)
        _assert_snapshot_publication_payload(
            state,
            label="existing Snapshot publication state",
        )
        now = utc_now()
        state["purpose"] = FREE_SNAPSHOT_PURPOSE.value
        state["operational_state"] = str(operational_state)
        state["updated_at"] = now
        history = list(state.get("state_history") or [])
        history.append(
            {
                "at": now,
                "operational_state": str(operational_state),
                "publication_state": state.get("publication_state", "draft"),
                "reason": detail or "workflow_transition",
            }
        )
        state["state_history"] = history
        atomic_write_json(state_path, state)
        return state

    def mark_partial(
        self,
        report_id: str,
        *,
        blockers: Iterable[str],
        operational_state: str = "failed",
        quality: Optional[Mapping] = None,
    ) -> dict:
        """Expose an incomplete run without ever making it approvable."""
        run_dir, manifest = self._versioned_manifest(report_id)
        state_path = run_dir / "snapshot_state.json"
        state = read_json(state_path) if state_path.is_file() else self._initial_state(report_id, manifest)
        _assert_snapshot_publication_payload(
            state,
            label="existing Snapshot publication state",
        )
        if state.get("publication_state") in {"prospect_ready", "rejected"}:
            raise SnapshotConflictError("A finalized Snapshot cannot be changed in place")
        now = utc_now()
        state.update(
            {
                "purpose": FREE_SNAPSHOT_PURPOSE.value,
                "operational_state": str(operational_state),
                "publication_state": "partial",
                "blockers": list(dict.fromkeys(str(item) for item in blockers if str(item).strip())),
                "quality": dict(quality or state.get("quality") or {}),
                "updated_at": now,
            }
        )
        state.setdefault("state_history", []).append(
            {
                "at": now,
                "operational_state": str(operational_state),
                "publication_state": "partial",
                "reason": "workflow_incomplete",
            }
        )
        atomic_write_json(state_path, state)
        _refresh_telemetry(self.repo_root)
        return self.get_snapshot(report_id)

    def approve(
        self,
        report_id: str,
        *,
        reviewer: str,
        confirmations: Mapping[str, object],
        expected_review_bundle_sha256: Optional[str] = None,
    ) -> dict:
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise SnapshotNotApprovableError("Reviewer is required")
        detail = self.get_snapshot(report_id)
        state_name = detail["publication_state"]
        if state_name not in APPROVABLE_STATES:
            raise SnapshotNotApprovableError(
                f"Snapshot is {state_name!r}; only complete candidates awaiting review can be approved"
            )
        if detail.get("test_mode"):
            raise SnapshotNotApprovableError("Test-mode Snapshots can never become prospect-ready")
        if detail.get("blockers"):
            raise SnapshotNotApprovableError("Snapshot still has release blockers")
        if detail.get("approval_stale"):
            raise SnapshotConflictError("The previous approval is stale")

        required = tuple(detail.get("review_confirmations") or LEGACY_FOUNDER_CONFIRMATIONS)
        normalized = {key: confirmations.get(key) is True for key in required}
        missing = [key for key, confirmed in normalized.items() if not confirmed]
        if missing:
            raise SnapshotNotApprovableError(
                f"All {len(required)} founder confirmations are required: "
                + ", ".join(missing)
            )

        run_dir = self.run_dir(report_id)
        state = read_json(run_dir / "snapshot_state.json")
        _assert_snapshot_publication_payload(
            state,
            label="existing Snapshot publication state",
        )
        state["purpose"] = FREE_SNAPSHOT_PURPOSE.value
        current = detail["current_hashes"]
        if any(state.get(key) != value for key, value in current.items()):
            raise SnapshotConflictError("Candidate hashes changed after the review bundle was prepared")
        if expected_review_bundle_sha256 and expected_review_bundle_sha256 != current["review_bundle_sha256"]:
            raise SnapshotConflictError("Review bundle changed; reload the evidence before approving")

        now = utc_now()
        approval = {
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "decision": "approved",
            "reviewer": reviewer,
            "reviewed_at": now,
            "confirmations": normalized,
            **current,
            **(
                {
                    "scope_version": detail["scope_version"],
                    "confirmation_context": deepcopy(
                        detail["review_confirmation_context"]
                    ),
                }
                if detail.get("review_confirmation_context")
                else {}
            ),
        }
        atomic_write_json(run_dir / "founder_review.json", approval)

        release_artifacts = self.release_artifact_builder(run_dir, detail, approval)
        if not isinstance(release_artifacts, Mapping):
            raise SnapshotPublicationError("Prospect renderer did not return an artifact mapping")
        release_artifact_manifest = self._artifact_manifest(run_dir, release_artifacts)
        missing_release_artifacts = sorted(REQUIRED_CANDIDATE_ARTIFACTS - set(release_artifact_manifest))
        if missing_release_artifacts:
            raise SnapshotPublicationError(
                "Prospect release bundle is incomplete: " + ", ".join(missing_release_artifacts)
            )
        release = self._promote_release(
            run_dir,
            detail,
            approval,
            release_artifact_manifest,
        )
        state["publication_state"] = "prospect_ready"
        state["release_id"] = release["release_id"]
        state["updated_at"] = now
        state.setdefault("state_history", []).append(
            {
                "at": now,
                "operational_state": state.get("operational_state", "complete"),
                "publication_state": "prospect_ready",
                "reason": "founder_approved_exact_bundle",
            }
        )
        atomic_write_json(run_dir / "snapshot_state.json", state)
        _refresh_telemetry(self.repo_root)
        return self.get_snapshot(report_id)

    def reject(self, report_id: str, *, reason: str, reviewer: str = "founder") -> dict:
        reason = str(reason or "").strip()
        reviewer = str(reviewer or "founder").strip() or "founder"
        if not reason:
            raise SnapshotPublicationError("A rejection reason is required")
        detail = self.get_snapshot(report_id)
        if detail["publication_state"] == "prospect_ready":
            raise SnapshotConflictError("A published immutable release cannot be rejected in place")

        run_dir = self.run_dir(report_id)
        now = utc_now()
        review = {
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "decision": "rejected",
            "reviewer": reviewer,
            "reviewed_at": now,
            "reason": reason,
            **detail["current_hashes"],
        }
        atomic_write_json(run_dir / "founder_review.json", review)
        state = read_json(run_dir / "snapshot_state.json")
        _assert_snapshot_publication_payload(
            state,
            label="existing Snapshot publication state",
        )
        state["purpose"] = FREE_SNAPSHOT_PURPOSE.value
        state["publication_state"] = "rejected"
        state["updated_at"] = now
        state.setdefault("state_history", []).append(
            {
                "at": now,
                "operational_state": state.get("operational_state", "complete"),
                "publication_state": "rejected",
                "reason": reason,
            }
        )
        atomic_write_json(run_dir / "snapshot_state.json", state)
        _refresh_telemetry(self.repo_root)
        return self.get_snapshot(report_id)

    def list_current_releases(self) -> list[dict]:
        clinics_root = self.releases_root / "clinics"
        if not clinics_root.is_dir():
            return []
        rows = []
        for pointer_path in clinics_root.glob("*/current.json"):
            try:
                pointer = read_json(pointer_path)
                release_dir = self._release_dir(pointer["release_id"])
                manifest_path = release_dir / "release_manifest.json"
                raw_manifest = read_json(manifest_path)
                manifest = _publication_release_manifest(raw_manifest)
                validated_pointer = _publication_release_pointer(
                    pointer,
                    manifest=manifest,
                    manifest_path=manifest_path,
                )
            except (KeyError, SnapshotPublicationError):
                continue
            rows.append({**validated_pointer, "manifest": manifest})
        rows.sort(key=lambda row: row.get("published_at") or "", reverse=True)
        return rows

    def current_release(self, clinic_id: str) -> Optional[dict]:
        pointer_path = self.releases_root / "clinics" / safe_slug(clinic_id) / "current.json"
        if not pointer_path.is_file():
            return None
        pointer = read_json(pointer_path)
        release_dir = self._release_dir(str(pointer.get("release_id") or ""))
        manifest_path = release_dir / "release_manifest.json"
        raw_manifest = read_json(manifest_path)
        manifest = _publication_release_manifest(raw_manifest)
        validated_pointer = _publication_release_pointer(
            pointer,
            manifest=manifest,
            manifest_path=manifest_path,
        )
        return {**validated_pointer, "manifest": manifest}

    def read_candidate_artifact(
        self, report_id: str, kind: str, *, expected_review_bundle_sha256: str,
    ) -> tuple[bytes, str]:
        """Read a registered internal candidate bound to the selected review.

        Return verified bytes, never a path that the caller must reopen. Walk
        the registered relative path through directory descriptors so neither
        leaf nor directory symlinks can redirect a download.
        """
        extensions = {"pdf": "pdf", "html": "html", "data": "json", "validation": "txt"}
        if kind not in extensions:
            raise SnapshotNotFoundError("Unknown candidate artifact type")
        detail = self.get_snapshot(report_id)
        current = detail.get("current_hashes") or {}
        if (not expected_review_bundle_sha256
            or expected_review_bundle_sha256 != detail.get("review_bundle_sha256")
            or expected_review_bundle_sha256 != current.get("review_bundle_sha256")):
            raise SnapshotConflictError("Review bundle changed; reload the candidate before downloading")
        artifact = (detail.get("candidate_artifacts") or {}).get(kind)
        if not isinstance(artifact, Mapping):
            raise SnapshotNotFoundError("The candidate has no requested artifact")
        path = Path(str(artifact.get("path") or ""))
        if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
            raise SnapshotPublicationError("Candidate artifact path must stay inside its run directory")
        expected_size = artifact.get("size")
        expected_hash = artifact.get("sha256")
        if (type(expected_size) is not int or expected_size < 0
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise SnapshotConflictError("Candidate artifact metadata is incomplete")
        run_dir = self.run_dir(report_id)
        if (self.runs_root / report_id).is_symlink():
            raise SnapshotPublicationError("Candidate run directory cannot be a symlink")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        directory_fd = None
        file_fd = None
        try:
            directory_fd = os.open(run_dir, flags | os.O_DIRECTORY)
            for part in path.parts[:-1]:
                next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(path.name, flags, dir_fd=directory_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise SnapshotConflictError("Candidate artifact is not a regular file")
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = None
                payload = stream.read()
        except OSError as exc:
            raise SnapshotConflictError("Candidate artifact is missing or is not a regular, direct file") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)
        if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_hash:
            raise SnapshotConflictError("Candidate artifact changed after registration")
        latest = self.get_snapshot(report_id)
        if (latest.get("review_bundle_sha256") != expected_review_bundle_sha256
            or (latest.get("current_hashes") or {}).get("review_bundle_sha256") != expected_review_bundle_sha256
            or (latest.get("candidate_artifacts") or {}).get(kind) != artifact):
            raise SnapshotConflictError("Review bundle changed during download; reload the candidate")
        return payload, f"internal-candidate.{extensions[kind]}"

    def current_release_artifact(self, release_id: str, kind: str) -> Path:
        """Resolve a download only when a clinic's current pointer names it."""
        kind = str(kind or "").strip()
        current = next(
            (row for row in self.list_current_releases() if row.get("release_id") == release_id),
            None,
        )
        if current is None:
            raise SnapshotNotFoundError("Release is not the current approved clinic release")
        artifact = (current.get("manifest") or {}).get("artifacts", {}).get(kind)
        if not isinstance(artifact, Mapping):
            raise SnapshotNotFoundError(f"Approved release has no {kind!r} artifact")
        release_dir = self._release_dir(release_id)
        path = (release_dir / str(artifact.get("path") or "")).resolve()
        try:
            path.relative_to(release_dir)
        except ValueError:
            raise SnapshotPublicationError("Release artifact path escapes its immutable release") from None
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise SnapshotConflictError("Approved release artifact is missing or hash-inconsistent")
        return path

    def mark_retry_requested(self, report_id: str) -> dict:
        detail = self.get_snapshot(report_id)
        if detail["publication_state"] in {"prospect_ready", "rejected"}:
            raise SnapshotConflictError("Create a new Snapshot instead of changing a finalized report")
        state = self.record_operational_state(report_id, "retry_queued", detail="missing_only_retry_requested")
        return {"report_id": report_id, "operational_state": state["operational_state"]}

    def record_competitor_resolution(
        self,
        report_id: str,
        *,
        scope_id: str,
        entity_id: str,
        reviewer: str,
        confirmations: Mapping[str, object],
        authoritative_references: Iterable[str],
    ) -> dict:
        """Store an explicit founder resolution and require deterministic re-analysis."""

        detail = self.get_snapshot(report_id)
        if detail["publication_state"] in {"prospect_ready", "rejected"}:
            raise SnapshotConflictError("A finalized Snapshot cannot accept competitor resolutions")
        scope_id = str(scope_id or "").strip()
        entity_id = str(entity_id or "").strip()
        reviewer = str(reviewer or "").strip()
        if not scope_id or not entity_id or not reviewer:
            raise SnapshotPublicationError("Scope, competitor and reviewer are required")
        references = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in authoritative_references
                if str(item or "").strip()
            )
        )
        if not references:
            raise SnapshotPublicationError("At least one authoritative competitor reference is required")

        review = detail.get("competitor_review")
        scopes = review.get("scopes") if isinstance(review, Mapping) else None
        scope_review = next(
            (
                row
                for row in scopes or []
                if isinstance(row, Mapping) and str(row.get("scope_id") or "") == scope_id
            ),
            None,
        )
        if scope_review is None:
            raise SnapshotPublicationError("The requested competitor scope is not in this Snapshot")
        subject_kind = str(scope_review.get("subject_kind") or "").strip().casefold()
        if not subject_kind:
            analysis = detail.get("analysis")
            analysis_scopes = analysis.get("scopes") if isinstance(analysis, Mapping) else []
            analysis_scope = next(
                (
                    row
                    for row in analysis_scopes or []
                    if isinstance(row, Mapping)
                    and str(row.get("scope_id") or "") == scope_id
                ),
                None,
            )
            subject_kind = str(
                (analysis_scope or {}).get("subject_kind") or "treatment"
            ).strip().casefold()
        if subject_kind not in {"treatment", "clinic_archetype"}:
            raise SnapshotPublicationError(
                "The competitor scope has an unsupported subject contract"
            )

        subject_confirmation = (
            confirmations.get("subject_relevance") is True
            or (
                subject_kind == "treatment"
                and confirmations.get("treatment_relevance") is True
            )
            or (
                subject_kind == "clinic_archetype"
                and confirmations.get("clinic_archetype_relevance") is True
            )
        )
        normalized = {
            "identity": confirmations.get("identity") is True,
            "alias_duplication": confirmations.get("alias_duplication") is True,
            "geography": confirmations.get("geography") is True,
            "subject_relevance": subject_confirmation,
            "active_trading": confirmations.get("active_trading") is True,
        }
        # Retain the old treatment-labelled field only for legacy treatment
        # scopes. Clinic-profile scopes get an honest archetype attestation.
        if subject_kind == "treatment":
            normalized["treatment_relevance"] = subject_confirmation
        else:
            normalized["clinic_archetype_relevance"] = subject_confirmation
        missing = [key for key, confirmed in normalized.items() if not confirmed]
        if missing:
            raise SnapshotPublicationError(
                "All competitor verification checks are required: " + ", ".join(missing)
            )
        unresolved = [
            row
            for row in scope_review.get("unresolved_exceptions") or []
            if isinstance(row, Mapping)
        ]

        def keys(row: Mapping) -> set[str]:
            return {
                str(row.get("entity_id") or "").strip(),
                str(row.get("source_entity_id") or "").strip(),
                str(row.get("canonical_name") or "").strip(),
            } - {""}

        candidate = next((row for row in unresolved if entity_id in keys(row)), None)
        if candidate is None:
            raise SnapshotConflictError(
                "The competitor is no longer an unresolved exception; reload the review"
            )
        source_entity_id = str(
            candidate.get("source_entity_id") or candidate.get("entity_id") or entity_id
        ).strip()
        now = utc_now()
        resolution = {
            "entity_id": source_entity_id,
            "resolved_entity_id": str(candidate.get("entity_id") or source_entity_id),
            "canonical_name": str(candidate.get("canonical_name") or "").strip(),
            "identity_status": "confirmed",
            "alias_duplication_status": "unique",
            "geography_status": "confirmed",
            "subject_kind": subject_kind,
            "subject_relevance_status": "confirmed",
            **(
                {"treatment_status": "confirmed"}
                if subject_kind == "treatment"
                else {"clinic_archetype_status": "confirmed"}
            ),
            "active_trading_status": "active",
            "confirmations": normalized,
            "authoritative_references": references,
            "reviewer": reviewer,
            "reviewed_at": now,
        }
        run_dir = self.run_dir(report_id)
        path = run_dir / "competitor_resolutions.json"
        document = read_json(path) if path.is_file() else {
            "schema_version": "free_snapshot.competitor_resolutions.v1",
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "scope_hash": str((detail.get("scope") or {}).get("scope_hash") or ""),
            "scopes": {},
            "history": [],
        }
        _assert_snapshot_publication_payload(
            document,
            label="existing Snapshot competitor resolutions",
        )
        document["purpose"] = FREE_SNAPSHOT_PURPOSE.value
        document.setdefault(
            "scope_hash", str((detail.get("scope") or {}).get("scope_hash") or "")
        )
        scope_rows = dict((document.get("scopes") or {}).get(scope_id) or {})
        scope_rows[source_entity_id] = resolution
        all_scopes = dict(document.get("scopes") or {})
        all_scopes[scope_id] = scope_rows
        history = list(document.get("history") or [])
        history.append({"scope_id": scope_id, **resolution})
        document.update({"scopes": all_scopes, "history": history, "updated_at": now})
        atomic_write_json(path, document)
        self.mark_partial(
            report_id,
            blockers=["Competitor resolution recorded; re-analysis is required before approval."],
            operational_state="resolution_recorded",
        )
        return resolution

    def _initial_state(self, report_id: str, manifest: Mapping) -> dict:
        now = utc_now()
        return {
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "report_id": report_id,
            "scope_version": _scope_version(manifest),
            "operational_state": "draft",
            "publication_state": "draft",
            "test_mode": bool(manifest.get("test_mode")),
            "blockers": [],
            "quality": {},
            "candidate_artifacts": {},
            "created_at": now,
            "updated_at": now,
            "state_history": [],
        }

    def _summary(self, detail: Mapping) -> dict:
        clinic = detail.get("clinic") if isinstance(detail.get("clinic"), Mapping) else {}
        catchment = detail.get("catchment") if isinstance(detail.get("catchment"), Mapping) else {}
        analysis = detail.get("analysis") if isinstance(detail.get("analysis"), Mapping) else {}
        presentation = (
            detail.get("presentation")
            if isinstance(detail.get("presentation"), Mapping)
            else {}
        )
        nested_presentation = (
            presentation.get("presentation")
            if isinstance(presentation.get("presentation"), Mapping)
            else {}
        )
        preliminary = (
            nested_presentation.get("preliminary_verdict")
            if isinstance(nested_presentation.get("preliminary_verdict"), Mapping)
            else presentation.get("preliminary_competitiveness")
            if isinstance(presentation.get("preliminary_competitiveness"), Mapping)
            else analysis.get("preliminary_competitiveness")
            if isinstance(analysis.get("preliminary_competitiveness"), Mapping)
            else {}
        )
        preliminary_label = str(preliminary.get("label") or "Preliminary analysis").strip()
        presence_report = presentation.get("presence_report")
        presence_report = presence_report if isinstance(presence_report, Mapping) else {}
        presence_metadata = {}
        if presence_report.get("version") == "free-snapshot-presence-report-v3":
            presence_analysis = presentation.get("presence_analysis")
            presence_analysis = presence_analysis if isinstance(presence_analysis, Mapping) else {}
            presence_metadata = {
                "report_kind": "presence_order_frequency",
                "presence_report_version": presence_report["version"],
                "measurement_run_id": analysis.get("run_id") or presence_analysis.get("run_id"),
            }
        areas = detail.get("areas") if isinstance(detail.get("areas"), list) else []
        area_names = [
            str((row.get("catchment") or {}).get("name") or "").strip()
            for row in areas
            if isinstance(row, Mapping)
        ]
        return {
            "report_id": detail["report_id"],
            "clinic_name": clinic.get("name") or "Unknown clinic",
            "clinic_account_id": clinic.get("account_id"),
            "catchment_name": " · ".join(value for value in area_names if value)
            or catchment.get("name"),
            "operational_state": detail.get("operational_state"),
            "publication_state": detail.get("publication_state"),
            "overall_verdict": preliminary_label,
            "preliminary_verdict": preliminary,
            "blockers": detail.get("blockers") or [],
            "coverage": analysis.get("coverage"),
            "test_mode": bool(detail.get("test_mode")),
            "approval_stale": bool(detail.get("approval_stale")),
            "release_id": detail.get("release_id"),
            "created_at": detail.get("created_at"),
            "updated_at": detail.get("updated_at"),
            **presence_metadata,
        }

    def _artifact_manifest(
        self,
        run_dir: Path,
        artifacts: Mapping[str, str | Path],
    ) -> dict[str, dict]:
        manifest: dict[str, dict] = {}
        for kind, raw_path in sorted(artifacts.items()):
            kind = str(kind or "").strip()
            if not kind:
                raise SnapshotPublicationError("Candidate artifact type cannot be blank")
            path = Path(raw_path)
            if not path.is_absolute():
                path = run_dir / path
            path = path.resolve()
            if not path.is_file():
                raise SnapshotPublicationError(f"Candidate artifact is missing: {path}")
            try:
                relative = path.relative_to(run_dir.resolve())
            except ValueError:
                raise SnapshotPublicationError("Candidate artifacts must live inside the immutable run directory") from None
            manifest[kind] = {
                "path": str(relative),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        return manifest

    def _assert_json_candidate_artifacts(
        self,
        run_dir: Path,
        artifacts: Mapping[str, Mapping],
    ) -> None:
        """Reject cross-purpose references inside candidate JSON before writes."""

        for kind, row in artifacts.items():
            if not isinstance(row, Mapping):
                raise SnapshotPublicationError(
                    f"Candidate artifact metadata must be an object: {kind}"
                )
            path = (run_dir / str(row.get("path") or "")).resolve()
            try:
                path.relative_to(run_dir.resolve())
            except ValueError:
                raise SnapshotPublicationError(
                    "Candidate artifacts must live inside the immutable run directory"
                ) from None
            if path.suffix.casefold() != ".json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotPublicationError(
                    f"Candidate JSON artifact {kind!r} cannot be validated: {exc}"
                ) from exc
            _assert_snapshot_publication_payload(
                payload,
                label=f"Snapshot candidate JSON artifact {kind!r}",
            )

    def _current_hashes(
        self,
        run_dir: Path,
        manifest: Mapping,
        analysis: Optional[Mapping],
        presentation: Optional[Mapping],
        state: Mapping,
    ) -> CandidateHashes:
        self._assert_report_target_analysis(run_dir, manifest, analysis)
        scope_hash = str(manifest.get("scope_hash") or state.get("scope_hash") or "")
        results_hash = _verified_results_hash(analysis) if analysis is not None else ""
        copy_hash = _verified_copy_hash(presentation) if presentation is not None else ""
        if presentation is not None and scope_hash and results_hash and copy_hash:
            _verify_artifact_identity(
                presentation,
                scope_hash=scope_hash,
                results_hash=results_hash,
                copy_hash=copy_hash,
            )
            prompt_pack_hash = str(manifest.get("prompt_pack_hash") or "")
            presented_prompt_hash = _declared_hash(
                presentation,
                "prompt_pack_hash",
                "scope_definition.prompt_pack_hash",
                "artifact_identity.prompt_pack_hash",
            )
            if not prompt_pack_hash or presented_prompt_hash != prompt_pack_hash:
                raise SnapshotConflictError(
                    "Presentation prompt_pack_hash disagrees with the frozen scope"
                )
        artifacts = state.get("candidate_artifacts") if isinstance(state.get("candidate_artifacts"), Mapping) else {}
        current_artifacts = {}
        for kind, row in artifacts.items():
            if not isinstance(row, Mapping):
                continue
            path = (run_dir / str(row.get("path") or "")).resolve()
            try:
                path.relative_to(run_dir.resolve())
            except ValueError:
                continue
            current_artifacts[str(kind)] = {
                "path": str(row.get("path") or ""),
                "sha256": sha256_file(path) if path.is_file() else "missing",
                "size": path.stat().st_size if path.is_file() else None,
            }
        review_hash = self._review_bundle_hash(
            str(state.get("report_id") or run_dir.name),
            scope_hash=scope_hash,
            results_hash=results_hash,
            copy_hash=copy_hash,
            artifact_manifest=current_artifacts,
            confirmation_context=_v4_confirmation_context(manifest),
        )
        return CandidateHashes(scope_hash, results_hash, copy_hash, review_hash)

    def _review_bundle_hash(
        self,
        report_id: str,
        *,
        scope_hash: str,
        results_hash: str,
        copy_hash: str,
        artifact_manifest: Mapping,
        confirmation_context: Optional[Mapping] = None,
    ) -> str:
        payload = {
            "report_id": report_id,
            "scope_hash": scope_hash,
            "results_hash": results_hash,
            "copy_hash": copy_hash,
            "candidate_artifacts": artifact_manifest,
        }
        if confirmation_context:
            payload["v4_confirmation_context"] = dict(confirmation_context)
        return sha256_json(payload)

    def _render_prospect_release(
        self,
        run_dir: Path,
        detail: Mapping,
        approval: Mapping,
    ) -> Mapping[str, Path]:
        """Render a fresh unwatermarked bundle after exact founder approval."""
        from clinic_geo.reporting.visibility_snapshot import (
            generate_visibility_snapshot_from_payload,
        )

        presentation = detail.get("presentation")
        if not isinstance(presentation, Mapping):
            raise SnapshotPublicationError("Normalized presentation is missing")
        payload = dict(presentation)
        presentation_meta = dict(payload.get("presentation") or {})
        presentation_meta["internal_candidate"] = False
        payload["presentation"] = presentation_meta
        quality = dict(payload.get("quality") or {})
        quality.update(
            {
                "prospect_ready": True,
                "publication_state": "prospect_ready",
                "founder_review_status": "approved",
                "founder_review": {
                    "status": "approved",
                    "reviewer": approval["reviewer"],
                    "reviewed_at": approval["reviewed_at"],
                    "scope_hash": approval["scope_hash"],
                    "results_hash": approval["results_hash"],
                    "copy_hash": approval["copy_hash"],
                    "review_bundle_sha256": approval["review_bundle_sha256"],
                    "confirmations": dict(approval["confirmations"]),
                },
            }
        )
        payload["quality"] = quality
        payload["publication_state"] = "prospect_ready"
        payload["founder_review"] = dict(approval)
        expected_prompt_hash = str((detail.get("scope") or {}).get("prompt_pack_hash") or "")
        presented_prompt_hash = _declared_hash(
            payload,
            "prompt_pack_hash",
            "scope_definition.prompt_pack_hash",
            "artifact_identity.prompt_pack_hash",
        )
        if not expected_prompt_hash or presented_prompt_hash != expected_prompt_hash:
            raise SnapshotConflictError(
                "Prospect payload prompt_pack_hash differs from the frozen scope"
            )
        _verify_artifact_identity(
            payload,
            scope_hash=str(approval["scope_hash"]),
            results_hash=str(approval["results_hash"]),
            copy_hash=str(approval["copy_hash"]),
        )
        if _verified_copy_hash(payload) != approval["copy_hash"]:
            raise SnapshotConflictError("Prospect payload copy differs from the founder-reviewed copy")
        output_dir = run_dir / "prospect_release" / str(approval["review_bundle_sha256"])
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            artifacts = generate_visibility_snapshot_from_payload(
                payload,
                output_dir=output_dir,
                internal_candidate=False,
            )
        except Exception as exc:
            raise SnapshotPublicationError(f"Prospect release rendering failed: {exc}") from exc
        missing = sorted(REQUIRED_CANDIDATE_ARTIFACTS - set(artifacts))
        if missing:
            raise SnapshotPublicationError("Prospect renderer omitted: " + ", ".join(missing))
        data_path = Path(artifacts["data"])
        release_payload = read_json(data_path)
        if _declared_hash(
            release_payload,
            "prompt_pack_hash",
            "scope_definition.prompt_pack_hash",
            "artifact_identity.prompt_pack_hash",
        ) != expected_prompt_hash:
            raise SnapshotConflictError(
                "Rendered prospect prompt_pack_hash differs from the frozen scope"
            )
        _verify_artifact_identity(
            release_payload,
            scope_hash=str(approval["scope_hash"]),
            results_hash=str(approval["results_hash"]),
            copy_hash=str(approval["copy_hash"]),
        )
        if _verified_copy_hash(release_payload) != approval["copy_hash"]:
            raise SnapshotConflictError("Rendered prospect copy differs from the reviewed copy")
        if _nested(release_payload, "presentation.internal_candidate") is True:
            raise SnapshotPublicationError("Prospect data artifact is still marked as an internal candidate")
        self._assert_no_internal_watermark(Path(artifacts["html"]), Path(artifacts["pdf"]))
        # The release retains the compact verification bundle and its manifest.
        # Raw captured pages and auxiliary model answers remain internal, but
        # they participate in the founder review hash through candidate_artifacts.
        candidate_artifacts = (
            detail.get("candidate_artifacts")
            if isinstance(detail.get("candidate_artifacts"), Mapping)
            else {}
        )
        for kind in ("verification_manifest", "verification_bundle"):
            row = candidate_artifacts.get(kind)
            if not isinstance(row, Mapping) or not row.get("path"):
                continue
            source = (run_dir / str(row["path"])).resolve()
            try:
                source.relative_to(run_dir.resolve())
            except ValueError:
                raise SnapshotPublicationError("Verification artifact escaped its run directory") from None
            if not source.is_file() or sha256_file(source) != row.get("sha256"):
                raise SnapshotConflictError("Verification artifact changed before release")
            artifacts[kind] = source
        return artifacts

    @staticmethod
    def _assert_no_internal_watermark(html_path: Path, pdf_path: Path) -> None:
        marker = "INTERNAL CANDIDATE"
        html = html_path.read_text(encoding="utf-8", errors="replace")
        # The self-contained stylesheet documents the internal-candidate mark,
        # so a source-wide substring search produces a false positive.  A real
        # watermark is emitted as this dedicated visible element.
        if re.search(
            r"class\s*=\s*['\"][^'\"]*\bv2-watermark\b[^'\"]*['\"]",
            html,
            flags=re.IGNORECASE,
        ):
            raise SnapshotPublicationError("Prospect HTML still contains the internal watermark")
        try:
            from pypdf import PdfReader

            pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages).upper()
        except Exception as exc:
            raise SnapshotPublicationError(f"Could not verify prospect PDF watermark removal: {exc}") from exc
        if marker in pdf_text:
            raise SnapshotPublicationError("Prospect PDF still contains the internal watermark")

    def _promote_release(
        self,
        run_dir: Path,
        detail: Mapping,
        approval: Mapping,
        release_artifacts: Mapping[str, Mapping],
    ) -> dict:
        report_id = str(detail["report_id"])
        review_hash = str(approval["review_bundle_sha256"])
        release_id = f"{safe_slug(report_id, fallback='snapshot')}-{review_hash[:12]}"
        release_dir = self._release_dir(release_id)
        if release_dir.exists():
            raise SnapshotConflictError(f"Immutable release already exists: {release_id}")

        self.releases_root.mkdir(parents=True, exist_ok=True)
        temporary = self.releases_root / f".{release_id}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir(parents=False, exist_ok=False)
        try:
            released_artifacts = {}
            for kind, row in sorted(release_artifacts.items()):
                source = (run_dir / str(row["path"])).resolve()
                try:
                    source.relative_to(run_dir.resolve())
                except ValueError:
                    raise SnapshotPublicationError("Candidate artifact escaped its run directory") from None
                if not source.is_file() or sha256_file(source) != row.get("sha256"):
                    raise SnapshotConflictError(f"Prospect release artifact changed before promotion: {kind}")
                target_name = source.name
                target = temporary / target_name
                if target.exists():
                    target_name = f"{safe_slug(kind, fallback='artifact')}-{target_name}"
                    target = temporary / target_name
                shutil.copy2(source, target)
                released_artifacts[kind] = {
                    "path": target_name,
                    "sha256": sha256_file(target),
                    "size": target.stat().st_size,
                }

            for name in ("scope_manifest.json", "report_target.json", "analysis.json", "presentation.json", "founder_review.json"):
                source = run_dir / name
                if source.is_file():
                    shutil.copy2(source, temporary / name)

            clinic = detail.get("clinic") if isinstance(detail.get("clinic"), Mapping) else {}
            clinic_id = str(clinic.get("account_id") or clinic.get("name") or "unknown-clinic")
            release_manifest = {
                "release_version": RELEASE_MANIFEST_VERSION,
                "purpose": FREE_SNAPSHOT_PURPOSE.value,
                "release_id": release_id,
                "report_id": report_id,
                "clinic_id": clinic_id,
                "clinic_name": clinic.get("name"),
                "published_at": approval["reviewed_at"],
                "reviewer": approval["reviewer"],
                "artifacts": released_artifacts,
                "scope_hash": approval["scope_hash"],
                "prompt_pack_hash": detail.get("scope", {}).get("prompt_pack_hash"),
                "results_hash": approval["results_hash"],
                "copy_hash": approval["copy_hash"],
                "review_bundle_sha256": approval["review_bundle_sha256"],
            }
            atomic_write_json(temporary / "release_manifest.json", release_manifest)
            os.replace(temporary, release_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

        clinic_slug = safe_slug(release_manifest["clinic_id"])
        release_manifest_path = release_dir / "release_manifest.json"
        pointer = {
            "pointer_version": RELEASE_POINTER_VERSION,
            "purpose": FREE_SNAPSHOT_PURPOSE.value,
            "release_version": RELEASE_MANIFEST_VERSION,
            "release_manifest_sha256": sha256_file(release_manifest_path),
            "clinic_id": release_manifest["clinic_id"],
            "clinic_name": release_manifest["clinic_name"],
            "report_id": report_id,
            "release_id": release_id,
            "release_manifest": str(release_manifest_path.relative_to(self.repo_root)),
            "published_at": release_manifest["published_at"],
            "scope_hash": release_manifest["scope_hash"],
            "prompt_pack_hash": release_manifest.get("prompt_pack_hash"),
            "results_hash": release_manifest["results_hash"],
            "copy_hash": release_manifest["copy_hash"],
            "review_bundle_sha256": release_manifest["review_bundle_sha256"],
        }
        atomic_write_json(self.releases_root / "clinics" / clinic_slug / "current.json", pointer)
        return pointer

    def _release_dir(self, release_id: str) -> Path:
        clean = str(release_id or "").strip()
        if not clean or Path(clean).name != clean or clean in {".", "..", "clinics"}:
            raise SnapshotNotFoundError("Invalid release ID")
        path = (self.releases_root / clean).resolve()
        if path.parent != self.releases_root.resolve():
            raise SnapshotNotFoundError("Invalid release ID")
        return path


def required_confirmations(
    value: bool = True,
    *,
    scope_version: Optional[str] = None,
) -> dict[str, bool]:
    """Build the confirmation payload for an explicitly selected contract.

    The historical default remains the v2/v3 matrix confirmation set. Callers
    reviewing v4 must opt into its two additional patient-language confirmations.
    """

    keys = (
        V4_FOUNDER_CONFIRMATIONS
        if scope_version == SCOPE_V4_VERSION
        else FOUNDER_CONFIRMATIONS
    )
    return {key: bool(value) for key in keys}


def _refresh_telemetry(repo_root: Path) -> None:
    """Operational telemetry is useful, but can never break publication."""
    try:
        from clinic_geo.workflows.free_audit.telemetry_v2 import write_snapshot_metrics

        write_snapshot_metrics(repo_root)
    except Exception:
        pass
