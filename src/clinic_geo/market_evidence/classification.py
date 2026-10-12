"""Closed, fake-client-only Market Evidence classification contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from clinic_geo.evidence.purpose import EvidencePurpose
from clinic_geo.integrity import canonical_json_bytes, canonical_sha256, sha256_bytes

from .occurrences import (
    OCCURRENCE_COVERAGE_SCHEMA,
    ArtifactCoverageProof,
    OccurrenceCoverageState,
    OccurrenceExtractionRun,
)


MARKET_CLASSIFICATION_REQUEST_SCHEMA = "market_evidence.classification_request.v3"
MARKET_CLASSIFICATION_RESPONSE_SCHEMA = "market_evidence.classification_response.v1"
MARKET_CLASSIFICATION_OUTPUT_SCHEMA = "market_evidence.classification_output.v1"
DEFAULT_BATCHING_CONTRACT = "market-classification-single-batch.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MarketClassificationError(RuntimeError):
    """Base error for fail-closed market classification."""


class MarketClassificationValidationError(MarketClassificationError, ValueError):
    """The response cannot attest the exact requested occurrence set."""


class MarketClassification(str, Enum):
    CLINIC_NAME = "clinic_name"
    NON_CLINIC = "non_clinic"
    AMBIGUOUS = "ambiguous"


class MarketClassificationClient(Protocol):
    """Injected test/shadow boundary; no live implementation exists in P2."""

    def classify(self, request_bytes: bytes, /) -> bytes: ...


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MarketClassificationValidationError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MarketClassificationValidationError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketClassificationRequest:
    occurrence_run: OccurrenceExtractionRun = field(repr=False)
    occurrence_run_id: str
    occurrence_manifest_sha256: str
    raw_artifact_manifest_sha256: str
    occurrence_coverage_manifest_sha256: str
    occurrence_coverage_complete: bool
    source_evidence_ids: tuple[str, ...]
    source_occurrence_coverage: tuple[tuple[str, bool, str], ...]
    occurrence_coverage_proofs: tuple[ArtifactCoverageProof, ...] = field(repr=False)
    occurrences: tuple[dict[str, str], ...]
    batching_contract_version: str
    prompt_version: str
    prompt_sha256: str
    model_contract: str
    observed_model_epoch: str
    request_bytes: bytes = field(repr=False)
    request_sha256: str
    classification_run_id: str
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    schema_version: str = MARKET_CLASSIFICATION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence_run, OccurrenceExtractionRun):
            raise MarketClassificationValidationError(
                "classification requires a verified occurrence extraction run"
            )
        for name in (
            "occurrence_run_id",
            "batching_contract_version",
            "prompt_version",
            "model_contract",
            "observed_model_epoch",
            "classification_run_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "occurrence_manifest_sha256",
            "raw_artifact_manifest_sha256",
            "occurrence_coverage_manifest_sha256",
            "prompt_sha256",
            "request_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.purpose is not EvidencePurpose.MARKET_DISCOVERY:
            raise MarketClassificationValidationError("classification requires market_discovery")
        if self.schema_version != MARKET_CLASSIFICATION_REQUEST_SCHEMA:
            raise MarketClassificationValidationError("unsupported classification request schema")
        if type(self.occurrence_coverage_complete) is not bool:
            raise MarketClassificationValidationError(
                "occurrence_coverage_complete must be a bool"
            )
        if not isinstance(self.request_bytes, bytes) or not self.request_bytes:
            raise MarketClassificationValidationError("request_bytes must be non-empty bytes")
        if sha256_bytes(self.request_bytes) != self.request_sha256:
            raise MarketClassificationValidationError("request SHA-256 does not match bytes")
        run_hash = canonical_sha256(
            {
                "schema_version": "market_evidence.classification_run_identity.v1",
                "purpose": self.purpose.value,
                "occurrence_run_id": self.occurrence_run_id,
                "request_sha256": self.request_sha256,
            }
        )
        if self.classification_run_id != f"market-classification-run-{run_hash}":
            raise MarketClassificationValidationError(
                "classification run ID does not match exact request identity"
            )
        ids = tuple(item.get("occurrence_id") for item in self.occurrences)
        if any(not isinstance(value, str) or not value for value in ids):
            raise MarketClassificationValidationError("request occurrence IDs are invalid")
        if len(set(ids)) != len(ids):
            raise MarketClassificationValidationError("request occurrence IDs cannot repeat")
        if not isinstance(self.source_evidence_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.source_evidence_ids
        ):
            raise MarketClassificationValidationError(
                "source evidence IDs must be a tuple of non-empty values"
            )
        if not self.source_evidence_ids or len(set(self.source_evidence_ids)) != len(
            self.source_evidence_ids
        ):
            raise MarketClassificationValidationError(
                "source evidence IDs must exactly identify the accepted artifacts"
            )
        if any(
            item.get("coverage_evidence_id") not in self.source_evidence_ids
            for item in self.occurrences
        ):
            raise MarketClassificationValidationError(
                "classification occurrences must belong to accepted source evidence"
            )
        if not isinstance(self.source_occurrence_coverage, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 3
            or not isinstance(item[0], str)
            or type(item[1]) is not bool
            or not isinstance(item[2], str)
            or _SHA256_RE.fullmatch(item[2]) is None
            for item in self.source_occurrence_coverage
        ):
            raise MarketClassificationValidationError(
                "source occurrence coverage must contain exact proof tuples"
            )
        if not isinstance(self.occurrence_coverage_proofs, tuple) or any(
            not isinstance(item, ArtifactCoverageProof)
            for item in self.occurrence_coverage_proofs
        ):
            raise MarketClassificationValidationError(
                "occurrence coverage proofs must contain exact proof manifests"
            )
        proofs = tuple(
            sorted(
                self.occurrence_coverage_proofs,
                key=lambda item: item.logical_observation_id,
            )
        )
        if not proofs:
            raise MarketClassificationValidationError(
                "classification requires occurrence coverage proofs"
            )
        contract_versions = {item.coverage_contract_version for item in proofs}
        if len(contract_versions) != 1:
            raise MarketClassificationValidationError(
                "occurrence coverage proofs must use one contract"
            )
        coverage_manifest = {
            "schema_version": OCCURRENCE_COVERAGE_SCHEMA,
            "coverage_contract_version": next(iter(contract_versions)),
            "complete": all(
                item.state is OccurrenceCoverageState.COMPLETE for item in proofs
            ),
            "proofs": [item.manifest_dict() for item in proofs],
        }
        if canonical_sha256(coverage_manifest) != self.occurrence_coverage_manifest_sha256:
            raise MarketClassificationValidationError(
                "occurrence coverage manifest hash is not exact"
            )
        coverage = tuple(
            (
                item.coverage_evidence_id,
                item.state is OccurrenceCoverageState.COMPLETE,
                item.proof_sha256,
            )
            for item in proofs
        )
        if (
            self.source_occurrence_coverage != coverage
            or tuple(item[0] for item in coverage) != self.source_evidence_ids
            or self.occurrence_coverage_complete
            != bool(coverage_manifest["complete"])
        ):
            raise MarketClassificationValidationError(
                "source occurrence coverage must exactly cover accepted evidence"
            )
        expected_occurrences = tuple(
            {
                "occurrence_id": item.occurrence_id,
                "literal_name": item.literal_name,
                "logical_observation_id": item.logical_observation_id,
                "coverage_evidence_id": item.coverage_evidence_id,
                "raw_answer_sha256": item.raw_answer_sha256,
            }
            for item in self.occurrence_run.occurrences
        )
        if (
            self.occurrence_run_id != self.occurrence_run.occurrence_run_id
            or self.occurrence_manifest_sha256
            != self.occurrence_run.output_manifest_sha256
            or self.raw_artifact_manifest_sha256
            != self.occurrence_run.raw_artifact_manifest_sha256
            or self.occurrence_coverage_manifest_sha256
            != self.occurrence_run.coverage_manifest_sha256
            or self.occurrence_coverage_complete
            is not self.occurrence_run.coverage_complete
            or self.source_evidence_ids != self.occurrence_run.source_evidence_ids
            or coverage != self.occurrence_run.source_occurrence_coverage
            or proofs != self.occurrence_run.coverage_proofs
            or self.occurrences != expected_occurrences
        ):
            raise MarketClassificationValidationError(
                "classification request must exactly match its verified occurrence run"
            )
        object.__setattr__(self, "occurrence_coverage_proofs", proofs)

    def source_occurrence_coverage_manifest(self) -> list[dict[str, object]]:
        return [
            {
                "coverage_evidence_id": evidence_id,
                "complete": complete,
                "coverage_proof_sha256": proof_sha256,
            }
            for evidence_id, complete, proof_sha256 in self.source_occurrence_coverage
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketClassificationResult:
    classification_result_id: str
    result_sha256: str
    occurrence_id: str
    literal_name: str
    classification: MarketClassification
    canonical_name: str | None

    def __post_init__(self) -> None:
        _text(self.classification_result_id, "classification_result_id")
        _sha(self.result_sha256, "result_sha256")
        _text(self.occurrence_id, "occurrence_id")
        _text(self.literal_name, "literal_name")
        if not isinstance(self.classification, MarketClassification):
            raise TypeError("classification must be a MarketClassification")
        if self.classification is MarketClassification.CLINIC_NAME:
            if self.canonical_name != self.literal_name:
                raise MarketClassificationValidationError(
                    "clinic classification cannot invent or rewrite a literal name"
                )
        elif self.canonical_name is not None:
            raise MarketClassificationValidationError(
                "non-clinic or ambiguous results cannot invent a canonical name"
            )

    def manifest_dict(self) -> dict[str, object]:
        return {
            "classification_result_id": self.classification_result_id,
            "result_sha256": self.result_sha256,
            "occurrence_id": self.occurrence_id,
            "literal_name": self.literal_name,
            "classification": self.classification.value,
            "canonical_name": self.canonical_name,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedMarketClassification:
    request: MarketClassificationRequest
    raw_response_bytes: bytes = field(repr=False)
    results: tuple[MarketClassificationResult, ...]
    raw_response_sha256: str = field(init=False)
    output_manifest_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, MarketClassificationRequest):
            raise TypeError("request must be a MarketClassificationRequest")
        if not isinstance(self.raw_response_bytes, bytes) or not self.raw_response_bytes:
            raise MarketClassificationValidationError("raw response must be non-empty bytes")
        if not isinstance(self.results, tuple) or any(
            not isinstance(item, MarketClassificationResult) for item in self.results
        ):
            raise TypeError("results must be MarketClassificationResult values")
        # P2-A007/A009: per-occurrence results are not a caller assertion.  They
        # are re-derived here from the exact raw response bytes bound to this
        # request; a caller cannot supply, relabel, or invent a classification
        # that does not follow byte-for-byte from the persisted classifier
        # response.  Any divergence fails closed.
        derived = _derive_classification_results(self.request, self.raw_response_bytes)
        if tuple(sorted(self.results, key=lambda item: item.occurrence_id)) != derived:
            raise MarketClassificationValidationError(
                "classification results must derive exactly from the raw response bytes"
            )
        object.__setattr__(self, "results", derived)
        object.__setattr__(self, "raw_response_sha256", sha256_bytes(self.raw_response_bytes))
        object.__setattr__(self, "output_manifest_sha256", canonical_sha256(self.output_manifest()))

    def output_manifest(self) -> dict[str, object]:
        return {
            "schema_version": MARKET_CLASSIFICATION_OUTPUT_SCHEMA,
            "request_sha256": self.request.request_sha256,
            "occurrence_manifest_sha256": self.request.occurrence_manifest_sha256,
            "results": [item.manifest_dict() for item in self.results],
        }


def build_classification_request(
    occurrence_run: OccurrenceExtractionRun,
    *,
    prompt_version: str,
    prompt_text: str,
    model_contract: str,
    observed_model_epoch: str,
    batching_contract_version: str = DEFAULT_BATCHING_CONTRACT,
) -> MarketClassificationRequest:
    if not isinstance(occurrence_run, OccurrenceExtractionRun):
        raise TypeError("occurrence_run must be an OccurrenceExtractionRun")
    _text(prompt_version, "prompt_version")
    _text(prompt_text, "prompt_text")
    _text(model_contract, "model_contract")
    _text(observed_model_epoch, "observed_model_epoch")
    _text(batching_contract_version, "batching_contract_version")
    occurrence_rows = tuple(
        {
            "occurrence_id": item.occurrence_id,
            "literal_name": item.literal_name,
            "logical_observation_id": item.logical_observation_id,
            "coverage_evidence_id": item.coverage_evidence_id,
            "raw_answer_sha256": item.raw_answer_sha256,
        }
        for item in occurrence_run.occurrences
    )
    prompt_sha256 = sha256_bytes(prompt_text.encode("utf-8"))
    payload = {
        "schema_version": MARKET_CLASSIFICATION_REQUEST_SCHEMA,
        "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
        "occurrence_run_id": occurrence_run.occurrence_run_id,
        "occurrence_manifest_sha256": occurrence_run.output_manifest_sha256,
        "raw_artifact_manifest_sha256": occurrence_run.raw_artifact_manifest_sha256,
        "occurrence_coverage_manifest_sha256": occurrence_run.coverage_manifest_sha256,
        "occurrence_coverage_complete": occurrence_run.coverage_complete,
        "source_evidence_ids": list(occurrence_run.source_evidence_ids),
        "source_occurrence_coverage": [
            {
                "coverage_evidence_id": evidence_id,
                "complete": complete,
                "coverage_proof_sha256": proof_sha256,
            }
            for evidence_id, complete, proof_sha256
            in occurrence_run.source_occurrence_coverage
        ],
        "batching_contract_version": batching_contract_version,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "model_contract": model_contract,
        "observed_model_epoch": observed_model_epoch,
        "occurrences": list(occurrence_rows),
    }
    request_bytes = canonical_json_bytes(payload)
    request_sha256 = sha256_bytes(request_bytes)
    run_hash = canonical_sha256(
        {
            "schema_version": "market_evidence.classification_run_identity.v1",
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "occurrence_run_id": occurrence_run.occurrence_run_id,
            "request_sha256": request_sha256,
        }
    )
    return MarketClassificationRequest(
        occurrence_run=occurrence_run,
        occurrence_run_id=occurrence_run.occurrence_run_id,
        occurrence_manifest_sha256=occurrence_run.output_manifest_sha256,
        raw_artifact_manifest_sha256=occurrence_run.raw_artifact_manifest_sha256,
        occurrence_coverage_manifest_sha256=occurrence_run.coverage_manifest_sha256,
        occurrence_coverage_complete=occurrence_run.coverage_complete,
        source_evidence_ids=occurrence_run.source_evidence_ids,
        source_occurrence_coverage=occurrence_run.source_occurrence_coverage,
        occurrence_coverage_proofs=occurrence_run.coverage_proofs,
        occurrences=occurrence_rows,
        batching_contract_version=batching_contract_version,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        model_contract=model_contract,
        observed_model_epoch=observed_model_epoch,
        request_bytes=request_bytes,
        request_sha256=request_sha256,
        classification_run_id=f"market-classification-run-{run_hash}",
    )


def _derive_classification_results(
    request: MarketClassificationRequest,
    raw_response_bytes: bytes,
) -> tuple[MarketClassificationResult, ...]:
    """Parse and validate the exact closed response into ordered results.

    This is the single authority that turns raw classifier response bytes into
    per-occurrence results.  It is used both by
    :func:`validate_classification_response` and by
    :class:`ValidatedMarketClassification`'s constructor so that a validated
    classification can never carry results that the raw bytes do not produce.
    """

    if not isinstance(request, MarketClassificationRequest):
        raise TypeError("request must be a MarketClassificationRequest")
    if not isinstance(raw_response_bytes, bytes) or not raw_response_bytes:
        raise MarketClassificationValidationError("classification response must be bytes")
    try:
        payload = json.loads(raw_response_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketClassificationValidationError("classification response is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "request_sha256",
        "occurrence_manifest_sha256",
        "results",
    }:
        raise MarketClassificationValidationError("classification response keys are not exact")
    if payload["schema_version"] != MARKET_CLASSIFICATION_RESPONSE_SCHEMA:
        raise MarketClassificationValidationError("classification response schema is wrong")
    if payload["request_sha256"] != request.request_sha256:
        raise MarketClassificationValidationError("classification response request hash is wrong")
    if payload["occurrence_manifest_sha256"] != request.occurrence_manifest_sha256:
        raise MarketClassificationValidationError("classification response occurrence hash is wrong")
    if not isinstance(payload["results"], list):
        raise MarketClassificationValidationError("classification results must be a list")
    requested = {item["occurrence_id"]: item for item in request.occurrences}
    seen: set[str] = set()
    results: list[MarketClassificationResult] = []
    for raw in payload["results"]:
        if not isinstance(raw, dict) or set(raw) != {
            "occurrence_id",
            "classification",
            "canonical_name",
        }:
            raise MarketClassificationValidationError("classification result keys are not exact")
        occurrence_id = raw["occurrence_id"]
        if occurrence_id not in requested or occurrence_id in seen:
            raise MarketClassificationValidationError(
                "classification result has an extra, invented, or duplicate occurrence ID"
            )
        seen.add(occurrence_id)
        try:
            classification = MarketClassification(raw["classification"])
        except (TypeError, ValueError) as exc:
            raise MarketClassificationValidationError("classification enum is invalid") from exc
        canonical_name = raw["canonical_name"]
        if canonical_name is not None and not isinstance(canonical_name, str):
            raise MarketClassificationValidationError("canonical_name must be text or null")
        identity = {
            "classification_run_id": request.classification_run_id,
            "occurrence_id": occurrence_id,
            "classification": classification.value,
            "canonical_name": canonical_name,
        }
        digest = canonical_sha256(identity)
        results.append(
            MarketClassificationResult(
                classification_result_id=f"market-classification-result-{digest}",
                result_sha256=digest,
                occurrence_id=occurrence_id,
                literal_name=requested[occurrence_id]["literal_name"],
                classification=classification,
                canonical_name=canonical_name,
            )
        )
    if seen != set(requested):
        raise MarketClassificationValidationError("classification response omitted occurrences")
    return tuple(sorted(results, key=lambda item: item.occurrence_id))


def validate_classification_response(
    request: MarketClassificationRequest,
    raw_response_bytes: bytes,
) -> ValidatedMarketClassification:
    results = _derive_classification_results(request, raw_response_bytes)
    return ValidatedMarketClassification(
        request=request,
        raw_response_bytes=raw_response_bytes,
        results=results,
    )


def classify_with_client(
    request: MarketClassificationRequest,
    client: MarketClassificationClient,
) -> ValidatedMarketClassification:
    """Perform one injected call and validate the exact closed response."""

    if not hasattr(client, "classify"):
        raise TypeError("client must implement classify(request_bytes)")
    response = client.classify(request.request_bytes)
    if not isinstance(response, bytes):
        raise MarketClassificationValidationError("classification client must return bytes")
    return validate_classification_response(request, response)


__all__ = [
    "DEFAULT_BATCHING_CONTRACT",
    "MARKET_CLASSIFICATION_OUTPUT_SCHEMA",
    "MARKET_CLASSIFICATION_REQUEST_SCHEMA",
    "MARKET_CLASSIFICATION_RESPONSE_SCHEMA",
    "MarketClassification",
    "MarketClassificationClient",
    "MarketClassificationError",
    "MarketClassificationRequest",
    "MarketClassificationResult",
    "MarketClassificationValidationError",
    "ValidatedMarketClassification",
    "build_classification_request",
    "classify_with_client",
    "validate_classification_response",
]
