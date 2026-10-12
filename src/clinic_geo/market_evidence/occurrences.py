"""Pure, versioned literal-occurrence extraction for Market Evidence.

Candidate discovery is part of the extractor contract.  Callers may assert the
exact ordered candidate-name set they expect, but they cannot manufacture it.
Offsets use Unicode code-point indexes and UTF-8 byte indexes simultaneously;
the accepted raw bytes are never normalized or rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from clinic_geo.evidence.purpose import EvidencePurpose, validate_evidence_purpose
from clinic_geo.integrity import canonical_sha256, sha256_bytes


OCCURRENCE_RUN_SCHEMA = "market_evidence.occurrence_run.v3"
OCCURRENCE_OUTPUT_SCHEMA = "market_evidence.occurrence_output.v3"
OCCURRENCE_OFFSET_CONTRACT = "unicode-code-points-and-utf8-bytes.v1"
EXTRACTOR_INPUT_SCHEMA = "market_evidence.extractor_input_manifest.v2"
CANDIDATE_MANIFEST_SCHEMA = "market_evidence.candidate_manifest.v2"
OCCURRENCE_COVERAGE_SCHEMA = "market_evidence.occurrence_coverage_manifest.v1"
ARTIFACT_COVERAGE_PROOF_SCHEMA = "market_evidence.artifact_coverage_proof.v1"
DEFAULT_EXTRACTOR_CONTRACT = "market-literal-extractor.v3"
DEFAULT_CANDIDATE_CONTRACT = "market-delimited-token-span-candidates.v2"
DEFAULT_COVERAGE_CONTRACT = "market-occurrence-coverage.v1"
STRUCTURED_OCCURRENCE_HEADER = "MARKET_EVIDENCE_OCCURRENCES_V1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’:.-][^\W_]+)*", re.UNICODE)
_HARD_SEPARATOR_RE = re.compile(r"[,;/|\r\n]|\.{2,}|(?:^|\s)&(?:\s|$)")
_MAX_UNSTRUCTURED_SPAN_TOKENS = 8
_MAX_UNSTRUCTURED_CANDIDATES = 4096


class OccurrenceExtractionError(ValueError):
    """Raw bytes or deterministic candidate coverage cannot be proven exact."""


class OccurrenceCoverageState(str, Enum):
    """Whether exact raw bytes prove their complete clinic-occurrence universe."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise OccurrenceExtractionError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OccurrenceExtractionError(f"{name} must be a lowercase SHA-256")
    return value


def _market_purpose(value: EvidencePurpose | str) -> EvidencePurpose:
    purpose = validate_evidence_purpose(value)
    if purpose is not EvidencePurpose.MARKET_DISCOVERY:
        raise OccurrenceExtractionError("occurrence extraction requires market_discovery")
    return purpose


@dataclass(frozen=True, slots=True, kw_only=True)
class RawOccurrenceArtifact:
    logical_observation_id: str
    artifact_sha256: str
    content: bytes = field(repr=False)
    coverage_evidence_id: str | None = None
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        _text(self.logical_observation_id, "logical_observation_id")
        _sha(self.artifact_sha256, "artifact_sha256")
        if not isinstance(self.content, bytes) or not self.content:
            raise OccurrenceExtractionError("raw artifact content must be non-empty bytes")
        if sha256_bytes(self.content) != self.artifact_sha256:
            raise OccurrenceExtractionError("raw artifact SHA-256 does not match its bytes")
        evidence_id = self.coverage_evidence_id or self.logical_observation_id
        object.__setattr__(
            self,
            "coverage_evidence_id",
            _text(evidence_id, "coverage_evidence_id"),
        )
        object.__setattr__(self, "purpose", _market_purpose(self.purpose))

    @property
    def raw_answer_sha256(self) -> str:
        return sha256_bytes(self.content)

    def manifest_dict(self) -> dict[str, object]:
        return {
            "logical_observation_id": self.logical_observation_id,
            "coverage_evidence_id": self.coverage_evidence_id,
            "artifact_sha256": self.artifact_sha256,
            "raw_answer_sha256": self.raw_answer_sha256,
            "byte_length": len(self.content),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactCoverageProof:
    """Byte-bound proof (or explicit lack of proof) for one accepted artifact."""

    logical_observation_id: str
    coverage_evidence_id: str
    artifact_sha256: str
    raw_answer_sha256: str
    byte_length: int
    coverage_contract_version: str
    state: OccurrenceCoverageState
    proof_kind: str
    candidate_ids: tuple[str, ...]
    failure_code: str | None = None
    proof_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "logical_observation_id",
            "coverage_evidence_id",
            "coverage_contract_version",
            "proof_kind",
        ):
            _text(getattr(self, name), name)
        for name in ("artifact_sha256", "raw_answer_sha256"):
            _sha(getattr(self, name), name)
        if self.artifact_sha256 != self.raw_answer_sha256:
            raise OccurrenceExtractionError("coverage proof must bind the exact raw bytes")
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int):
            raise OccurrenceExtractionError("coverage proof byte_length must be an integer")
        if self.byte_length <= 0:
            raise OccurrenceExtractionError("coverage proof byte_length must be positive")
        if not isinstance(self.state, OccurrenceCoverageState):
            raise TypeError("coverage proof state must be an OccurrenceCoverageState")
        if not isinstance(self.candidate_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.candidate_ids
        ):
            raise TypeError("coverage proof candidate_ids must contain identifiers")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise OccurrenceExtractionError("coverage proof candidate IDs cannot repeat")
        if self.state is OccurrenceCoverageState.COMPLETE:
            if self.proof_kind != "strict-structured-occurrence-envelope":
                raise OccurrenceExtractionError("complete coverage requires the strict envelope")
            if self.failure_code is not None:
                raise OccurrenceExtractionError("complete coverage cannot have a failure code")
        elif not isinstance(self.failure_code, str) or not self.failure_code.strip():
            raise OccurrenceExtractionError("incomplete coverage requires a failure code")
        object.__setattr__(self, "proof_sha256", canonical_sha256(self.identity_dict()))

    def identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": ARTIFACT_COVERAGE_PROOF_SCHEMA,
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "logical_observation_id": self.logical_observation_id,
            "coverage_evidence_id": self.coverage_evidence_id,
            "artifact_sha256": self.artifact_sha256,
            "raw_answer_sha256": self.raw_answer_sha256,
            "byte_length": self.byte_length,
            "coverage_contract_version": self.coverage_contract_version,
            "state": self.state.value,
            "proof_kind": self.proof_kind,
            "candidate_ids": list(self.candidate_ids),
            "failure_code": self.failure_code,
        }

    def manifest_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "proof_sha256": self.proof_sha256}


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicCandidate:
    candidate_id: str
    candidate_sha256: str
    logical_observation_id: str
    coverage_evidence_id: str
    artifact_sha256: str
    raw_answer_sha256: str
    literal_name: str
    exact_excerpt: str
    character_start: int
    character_end: int
    byte_start: int
    byte_end: int
    candidate_contract_version: str
    derivation_rule: str = "unstructured-delimited-token-span"

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "logical_observation_id",
            "coverage_evidence_id",
            "literal_name",
            "exact_excerpt",
            "candidate_contract_version",
            "derivation_rule",
        ):
            _text(getattr(self, name), name)
        for name in ("candidate_sha256", "artifact_sha256", "raw_answer_sha256"):
            _sha(getattr(self, name), name)
        for start_name, end_name in (
            ("character_start", "character_end"),
            ("byte_start", "byte_end"),
        ):
            start = getattr(self, start_name)
            end = getattr(self, end_name)
            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise OccurrenceExtractionError(f"{start_name} must be non-negative")
            if isinstance(end, bool) or not isinstance(end, int) or end <= start:
                raise OccurrenceExtractionError(f"{end_name} must be greater than {start_name}")
        if self.literal_name != self.exact_excerpt:
            raise OccurrenceExtractionError("candidate must equal the exact raw excerpt")
        expected = canonical_sha256(self.identity_dict())
        if self.candidate_sha256 != expected:
            raise OccurrenceExtractionError("candidate SHA-256 does not match provenance")
        if self.candidate_id != f"market-candidate-{expected}":
            raise OccurrenceExtractionError("candidate ID does not match provenance")

    def identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": "market_evidence.deterministic_candidate.v2",
            "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
            "logical_observation_id": self.logical_observation_id,
            "coverage_evidence_id": self.coverage_evidence_id,
            "artifact_sha256": self.artifact_sha256,
            "raw_answer_sha256": self.raw_answer_sha256,
            "literal_name": self.literal_name,
            "exact_excerpt": self.exact_excerpt,
            "candidate_contract_version": self.candidate_contract_version,
            "derivation_rule": self.derivation_rule,
            "character_start": self.character_start,
            "character_end": self.character_end,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }

    def manifest_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_sha256": self.candidate_sha256,
            **self.identity_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LiteralOccurrence:
    occurrence_id: str
    occurrence_sha256: str
    candidate_id: str
    logical_observation_id: str
    coverage_evidence_id: str
    artifact_sha256: str
    raw_answer_sha256: str
    literal_name: str
    exact_excerpt: str
    character_start: int
    character_end: int
    byte_start: int
    byte_end: int
    extractor_contract_version: str
    offset_contract_version: str = OCCURRENCE_OFFSET_CONTRACT
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY

    def __post_init__(self) -> None:
        for name in (
            "occurrence_id",
            "candidate_id",
            "logical_observation_id",
            "coverage_evidence_id",
            "literal_name",
            "exact_excerpt",
            "extractor_contract_version",
            "offset_contract_version",
        ):
            _text(getattr(self, name), name)
        for name in ("occurrence_sha256", "artifact_sha256", "raw_answer_sha256"):
            _sha(getattr(self, name), name)
        for start_name, end_name in (
            ("character_start", "character_end"),
            ("byte_start", "byte_end"),
        ):
            start = getattr(self, start_name)
            end = getattr(self, end_name)
            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise OccurrenceExtractionError(f"{start_name} must be non-negative")
            if isinstance(end, bool) or not isinstance(end, int) or end <= start:
                raise OccurrenceExtractionError(f"{end_name} must be greater than {start_name}")
        if self.literal_name != self.exact_excerpt:
            raise OccurrenceExtractionError("literal name must equal the exact raw excerpt")
        object.__setattr__(self, "purpose", _market_purpose(self.purpose))
        expected = canonical_sha256(self.identity_dict())
        if self.occurrence_sha256 != expected:
            raise OccurrenceExtractionError("occurrence SHA-256 does not match exact offsets")
        if self.occurrence_id != f"market-occurrence-{expected}":
            raise OccurrenceExtractionError("occurrence ID does not match its immutable identity")

    def identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": "market_evidence.literal_occurrence.v3",
            "purpose": self.purpose.value,
            "candidate_id": self.candidate_id,
            "logical_observation_id": self.logical_observation_id,
            "coverage_evidence_id": self.coverage_evidence_id,
            "artifact_sha256": self.artifact_sha256,
            "raw_answer_sha256": self.raw_answer_sha256,
            "literal_name": self.literal_name,
            "exact_excerpt": self.exact_excerpt,
            "extractor_contract_version": self.extractor_contract_version,
            "offset_contract_version": self.offset_contract_version,
            "character_start": self.character_start,
            "character_end": self.character_end,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }

    def manifest_dict(self) -> dict[str, object]:
        return {
            "occurrence_id": self.occurrence_id,
            "occurrence_sha256": self.occurrence_sha256,
            **self.identity_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OccurrenceExtractionRun:
    window_id: str
    measurement_spec_sha256: str
    input_manifest_sha256: str
    artifacts: tuple[RawOccurrenceArtifact, ...] = field(repr=False)
    candidates: tuple[DeterministicCandidate, ...]
    occurrences: tuple[LiteralOccurrence, ...]
    coverage_proofs: tuple[ArtifactCoverageProof, ...]
    extractor_contract_version: str = DEFAULT_EXTRACTOR_CONTRACT
    candidate_contract_version: str = DEFAULT_CANDIDATE_CONTRACT
    coverage_contract_version: str = DEFAULT_COVERAGE_CONTRACT
    offset_contract_version: str = OCCURRENCE_OFFSET_CONTRACT
    schema_version: str = OCCURRENCE_RUN_SCHEMA
    purpose: EvidencePurpose = EvidencePurpose.MARKET_DISCOVERY
    raw_artifact_manifest_sha256: str = field(init=False)
    extractor_input_manifest_sha256: str = field(init=False)
    candidate_manifest_sha256: str = field(init=False)
    coverage_manifest_sha256: str = field(init=False)
    output_manifest_sha256: str = field(init=False)
    extraction_run_sha256: str = field(init=False)
    occurrence_run_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.window_id, "window_id")
        _sha(self.measurement_spec_sha256, "measurement_spec_sha256")
        _sha(self.input_manifest_sha256, "input_manifest_sha256")
        _text(self.extractor_contract_version, "extractor_contract_version")
        _text(self.candidate_contract_version, "candidate_contract_version")
        _text(self.coverage_contract_version, "coverage_contract_version")
        if self.offset_contract_version != OCCURRENCE_OFFSET_CONTRACT:
            raise OccurrenceExtractionError("unsupported occurrence offset contract")
        if self.schema_version != OCCURRENCE_RUN_SCHEMA:
            raise OccurrenceExtractionError("unsupported occurrence run schema")
        object.__setattr__(self, "purpose", _market_purpose(self.purpose))
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(item, RawOccurrenceArtifact) for item in self.artifacts
        ):
            raise TypeError("artifacts must be a tuple of RawOccurrenceArtifact values")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, DeterministicCandidate) for item in self.candidates
        ):
            raise TypeError("candidates must be DeterministicCandidate values")
        if not isinstance(self.occurrences, tuple) or any(
            not isinstance(item, LiteralOccurrence) for item in self.occurrences
        ):
            raise TypeError("occurrences must be LiteralOccurrence values")
        if not isinstance(self.coverage_proofs, tuple) or any(
            not isinstance(item, ArtifactCoverageProof) for item in self.coverage_proofs
        ):
            raise TypeError("coverage_proofs must be ArtifactCoverageProof values")
        ordered_artifacts = tuple(sorted(self.artifacts, key=lambda item: item.logical_observation_id))
        if len({item.logical_observation_id for item in ordered_artifacts}) != len(ordered_artifacts):
            raise OccurrenceExtractionError("raw artifact observations cannot repeat")
        if len({item.coverage_evidence_id for item in ordered_artifacts}) != len(ordered_artifacts):
            raise OccurrenceExtractionError("raw artifact coverage identities cannot repeat")
        candidate_key = lambda item: (
            item.logical_observation_id,
            item.character_start,
            item.character_end,
            item.literal_name,
            item.candidate_id,
        )
        ordered_candidates = tuple(sorted(self.candidates, key=candidate_key))
        ordered_occurrences = tuple(
            sorted(
                self.occurrences,
                key=lambda item: (
                    item.logical_observation_id,
                    item.character_start,
                    item.character_end,
                    item.literal_name,
                    item.occurrence_id,
                ),
            )
        )
        expected_candidates, expected_proofs = _derive_candidates_and_coverage(
            ordered_artifacts,
            candidate_contract_version=self.candidate_contract_version,
            coverage_contract_version=self.coverage_contract_version,
        )
        expected_occurrences = tuple(
            _literal_occurrence(item, self.extractor_contract_version)
            for item in expected_candidates
        )
        if ordered_candidates != expected_candidates:
            raise OccurrenceExtractionError(
                "candidates must be exactly re-derived from the accepted raw bytes"
            )
        if ordered_occurrences != expected_occurrences:
            raise OccurrenceExtractionError(
                "occurrences must exactly match their raw-byte candidates"
            )
        artifact_ids = {item.logical_observation_id for item in ordered_artifacts}
        if any(item.logical_observation_id not in artifact_ids for item in ordered_candidates):
            raise OccurrenceExtractionError("candidate references an artifact outside the run")
        if {item.candidate_id for item in ordered_candidates} != {
            item.candidate_id for item in ordered_occurrences
        } or len(ordered_candidates) != len(ordered_occurrences):
            raise OccurrenceExtractionError(
                "occurrences must exactly cover the deterministic candidate manifest"
            )
        ordered_proofs = tuple(
            sorted(self.coverage_proofs, key=lambda item: item.logical_observation_id)
        )
        if ordered_proofs != expected_proofs:
            raise OccurrenceExtractionError(
                "coverage proofs must be exactly re-derived from the accepted raw bytes"
            )
        if {item.logical_observation_id for item in ordered_proofs} != artifact_ids:
            raise OccurrenceExtractionError(
                "coverage proofs must exactly cover the accepted raw artifacts"
            )
        candidates_by_artifact = {
            artifact_id: tuple(
                item.candidate_id
                for item in ordered_candidates
                if item.logical_observation_id == artifact_id
            )
            for artifact_id in artifact_ids
        }
        artifacts_by_id = {
            item.logical_observation_id: item for item in ordered_artifacts
        }
        for proof in ordered_proofs:
            artifact = artifacts_by_id[proof.logical_observation_id]
            if (
                proof.coverage_evidence_id != artifact.coverage_evidence_id
                or proof.artifact_sha256 != artifact.artifact_sha256
                or proof.raw_answer_sha256 != artifact.raw_answer_sha256
                or proof.byte_length != len(artifact.content)
                or proof.coverage_contract_version != self.coverage_contract_version
                or proof.candidate_ids
                != candidates_by_artifact[proof.logical_observation_id]
            ):
                raise OccurrenceExtractionError(
                    "coverage proof does not match its exact artifact and candidates"
                )
        object.__setattr__(self, "artifacts", ordered_artifacts)
        object.__setattr__(self, "candidates", ordered_candidates)
        object.__setattr__(self, "occurrences", ordered_occurrences)
        object.__setattr__(self, "coverage_proofs", ordered_proofs)
        raw_hash = canonical_sha256(self.raw_artifact_manifest())
        extractor_input_hash = canonical_sha256(self.extractor_input_manifest())
        candidate_hash = canonical_sha256(self.candidate_manifest())
        coverage_hash = canonical_sha256(self.coverage_manifest())
        output_hash = canonical_sha256(self.output_manifest())
        run_hash = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "purpose": self.purpose.value,
                "window_id": self.window_id,
                "measurement_spec_sha256": self.measurement_spec_sha256,
                "input_manifest_sha256": self.input_manifest_sha256,
                "raw_artifact_manifest_sha256": raw_hash,
                "extractor_input_manifest_sha256": extractor_input_hash,
                "candidate_manifest_sha256": candidate_hash,
                "coverage_manifest_sha256": coverage_hash,
                "extractor_contract_version": self.extractor_contract_version,
                "candidate_contract_version": self.candidate_contract_version,
                "coverage_contract_version": self.coverage_contract_version,
                "offset_contract_version": self.offset_contract_version,
                "output_manifest_sha256": output_hash,
            }
        )
        object.__setattr__(self, "raw_artifact_manifest_sha256", raw_hash)
        object.__setattr__(self, "extractor_input_manifest_sha256", extractor_input_hash)
        object.__setattr__(self, "candidate_manifest_sha256", candidate_hash)
        object.__setattr__(self, "coverage_manifest_sha256", coverage_hash)
        object.__setattr__(self, "output_manifest_sha256", output_hash)
        object.__setattr__(self, "extraction_run_sha256", run_hash)
        object.__setattr__(self, "occurrence_run_id", f"market-occurrence-run-{run_hash}")

    @property
    def source_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.coverage_evidence_id for item in self.artifacts)

    @property
    def ordered_candidate_names(self) -> tuple[str, ...]:
        return _ordered_unique(item.literal_name for item in self.candidates)

    @property
    def coverage_complete(self) -> bool:
        return bool(self.coverage_proofs) and all(
            item.state is OccurrenceCoverageState.COMPLETE
            for item in self.coverage_proofs
        )

    @property
    def source_occurrence_coverage(self) -> tuple[tuple[str, bool, str], ...]:
        return tuple(
            (
                item.coverage_evidence_id,
                item.state is OccurrenceCoverageState.COMPLETE,
                item.proof_sha256,
            )
            for item in self.coverage_proofs
        )

    def raw_artifact_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "market_evidence.raw_artifact_manifest.v2",
            "artifacts": [item.manifest_dict() for item in self.artifacts],
        }

    def extractor_input_manifest(self) -> dict[str, object]:
        return {
            "schema_version": EXTRACTOR_INPUT_SCHEMA,
            "purpose": self.purpose.value,
            "window_id": self.window_id,
            "measurement_spec_sha256": self.measurement_spec_sha256,
            "accepted_run_manifest_sha256": self.input_manifest_sha256,
            "extractor_contract_version": self.extractor_contract_version,
            "candidate_contract_version": self.candidate_contract_version,
            "coverage_contract_version": self.coverage_contract_version,
            "occurrence_coverage_manifest_sha256": canonical_sha256(
                self.coverage_manifest()
            ),
            "occurrence_coverage_complete": self.coverage_complete,
            "offset_contract_version": self.offset_contract_version,
            "raw_artifacts": [item.manifest_dict() for item in self.artifacts],
        }

    def candidate_manifest(self) -> dict[str, object]:
        return {
            "schema_version": CANDIDATE_MANIFEST_SCHEMA,
            "candidate_contract_version": self.candidate_contract_version,
            "candidates": [item.manifest_dict() for item in self.candidates],
        }

    def coverage_manifest(self) -> dict[str, object]:
        return {
            "schema_version": OCCURRENCE_COVERAGE_SCHEMA,
            "coverage_contract_version": self.coverage_contract_version,
            "complete": self.coverage_complete,
            "proofs": [item.manifest_dict() for item in self.coverage_proofs],
        }

    def output_manifest(self) -> dict[str, object]:
        return {
            "schema_version": OCCURRENCE_OUTPUT_SCHEMA,
            "candidate_manifest_sha256": canonical_sha256(self.candidate_manifest()),
            "coverage_manifest_sha256": canonical_sha256(self.coverage_manifest()),
            "coverage_complete": self.coverage_complete,
            "occurrences": [item.manifest_dict() for item in self.occurrences],
        }


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _candidate_from_span(
    artifact: RawOccurrenceArtifact,
    text: str,
    character_start: int,
    character_end: int,
    candidate_contract_version: str,
    derivation_rule: str,
) -> DeterministicCandidate:
    literal_name = text[character_start:character_end]
    prefix = text[:character_start].encode("utf-8")
    excerpt_bytes = literal_name.encode("utf-8")
    identity = {
        "schema_version": "market_evidence.deterministic_candidate.v2",
        "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
        "logical_observation_id": artifact.logical_observation_id,
        "coverage_evidence_id": artifact.coverage_evidence_id,
        "artifact_sha256": artifact.artifact_sha256,
        "raw_answer_sha256": artifact.raw_answer_sha256,
        "literal_name": literal_name,
        "exact_excerpt": literal_name,
        "candidate_contract_version": candidate_contract_version,
        "derivation_rule": derivation_rule,
        "character_start": character_start,
        "character_end": character_end,
        "byte_start": len(prefix),
        "byte_end": len(prefix) + len(excerpt_bytes),
    }
    digest = canonical_sha256(identity)
    return DeterministicCandidate(
        candidate_id=f"market-candidate-{digest}",
        candidate_sha256=digest,
        logical_observation_id=artifact.logical_observation_id,
        coverage_evidence_id=artifact.coverage_evidence_id or artifact.logical_observation_id,
        artifact_sha256=artifact.artifact_sha256,
        raw_answer_sha256=artifact.raw_answer_sha256,
        literal_name=literal_name,
        exact_excerpt=literal_name,
        character_start=character_start,
        character_end=character_end,
        byte_start=len(prefix),
        byte_end=len(prefix) + len(excerpt_bytes),
        candidate_contract_version=candidate_contract_version,
        derivation_rule=derivation_rule,
    )


def _structured_occurrence_spans(text: str) -> tuple[tuple[int, int], ...] | None:
    """Return exact clinic fields only for the closed byte-level envelope."""

    lines = text.splitlines(keepends=True)
    if (
        not lines
        or lines[0] != f"{STRUCTURED_OCCURRENCE_HEADER}\n"
        or lines[-1] != "END\n"
        or not text.endswith("\n")
    ):
        return None
    spans: list[tuple[int, int]] = []
    cursor = len(lines[0])
    for line in lines[1:-1]:
        if not line.startswith("CLINIC\t") or not line.endswith("\n"):
            return None
        literal = line[len("CLINIC\t") : -1]
        if not literal or literal != literal.strip() or "\t" in literal:
            return None
        start = cursor + len("CLINIC\t")
        spans.append((start, start + len(literal)))
        cursor += len(line)
    return tuple(spans)


def _unstructured_occurrence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Enumerate bounded token-aligned spans without claiming semantic coverage."""

    tokens = tuple(_TOKEN_RE.finditer(text))
    groups: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    previous_end = 0
    for token in tokens:
        gap = text[previous_end : token.start()]
        is_conjunction = token.group(0).casefold() == "and"
        if current and _HARD_SEPARATOR_RE.search(gap):
            groups.append(current)
            current = []
        if is_conjunction:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(token)
        previous_end = token.end()
    if current:
        groups.append(current)

    spans: list[tuple[int, int]] = []
    for group in groups:
        for start_index, first in enumerate(group):
            for last in group[
                start_index : start_index + _MAX_UNSTRUCTURED_SPAN_TOKENS
            ]:
                spans.append((first.start(), last.end()))
                if len(spans) > _MAX_UNSTRUCTURED_CANDIDATES:
                    raise OccurrenceExtractionError(
                        "unstructured candidate universe exceeds the safe bound"
                    )
    return tuple(spans)


def _derive_candidates_and_coverage(
    artifacts: tuple[RawOccurrenceArtifact, ...],
    *,
    candidate_contract_version: str,
    coverage_contract_version: str,
) -> tuple[tuple[DeterministicCandidate, ...], tuple[ArtifactCoverageProof, ...]]:
    if candidate_contract_version != DEFAULT_CANDIDATE_CONTRACT:
        raise OccurrenceExtractionError("unsupported deterministic candidate contract")
    _text(coverage_contract_version, "coverage_contract_version")
    candidates: list[DeterministicCandidate] = []
    proofs: list[ArtifactCoverageProof] = []
    for artifact in sorted(artifacts, key=lambda item: item.logical_observation_id):
        try:
            text = artifact.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise OccurrenceExtractionError("raw answer is not valid UTF-8") from exc
        structured_spans = _structured_occurrence_spans(text)
        if structured_spans is None:
            spans = _unstructured_occurrence_spans(text)
            state = OccurrenceCoverageState.INCOMPLETE
            proof_kind = "unstructured-candidate-discovery"
            failure_code = "unstructured_candidate_universe_unproven"
            derivation_rule = "unstructured-delimited-token-span"
        else:
            spans = structured_spans
            state = OccurrenceCoverageState.COMPLETE
            proof_kind = "strict-structured-occurrence-envelope"
            failure_code = None
            derivation_rule = "strict-structured-occurrence-envelope"
        artifact_candidates = tuple(
            _candidate_from_span(
                artifact,
                text,
                start,
                end,
                candidate_contract_version,
                derivation_rule,
            )
            for start, end in spans
        )
        candidates.extend(artifact_candidates)
        proofs.append(
            ArtifactCoverageProof(
                logical_observation_id=artifact.logical_observation_id,
                coverage_evidence_id=(
                    artifact.coverage_evidence_id or artifact.logical_observation_id
                ),
                artifact_sha256=artifact.artifact_sha256,
                raw_answer_sha256=artifact.raw_answer_sha256,
                byte_length=len(artifact.content),
                coverage_contract_version=coverage_contract_version,
                state=state,
                proof_kind=proof_kind,
                candidate_ids=tuple(item.candidate_id for item in artifact_candidates),
                failure_code=failure_code,
            )
        )
    return tuple(candidates), tuple(proofs)


def derive_deterministic_candidates(
    artifacts: Iterable[RawOccurrenceArtifact],
    *,
    candidate_contract_version: str = DEFAULT_CANDIDATE_CONTRACT,
) -> tuple[DeterministicCandidate, ...]:
    """Discover exact spans; unstructured results never imply complete coverage."""

    rows = tuple(sorted(tuple(artifacts), key=lambda item: item.logical_observation_id))
    candidates, _ = _derive_candidates_and_coverage(
        rows,
        candidate_contract_version=candidate_contract_version,
        coverage_contract_version=DEFAULT_COVERAGE_CONTRACT,
    )
    return candidates


def _literal_occurrence(
    candidate: DeterministicCandidate,
    extractor_contract_version: str,
) -> LiteralOccurrence:
    identity = {
        "schema_version": "market_evidence.literal_occurrence.v3",
        "purpose": EvidencePurpose.MARKET_DISCOVERY.value,
        "candidate_id": candidate.candidate_id,
        "logical_observation_id": candidate.logical_observation_id,
        "coverage_evidence_id": candidate.coverage_evidence_id,
        "artifact_sha256": candidate.artifact_sha256,
        "raw_answer_sha256": candidate.raw_answer_sha256,
        "literal_name": candidate.literal_name,
        "exact_excerpt": candidate.exact_excerpt,
        "extractor_contract_version": extractor_contract_version,
        "offset_contract_version": OCCURRENCE_OFFSET_CONTRACT,
        "character_start": candidate.character_start,
        "character_end": candidate.character_end,
        "byte_start": candidate.byte_start,
        "byte_end": candidate.byte_end,
    }
    digest = canonical_sha256(identity)
    return LiteralOccurrence(
        occurrence_id=f"market-occurrence-{digest}",
        occurrence_sha256=digest,
        candidate_id=candidate.candidate_id,
        logical_observation_id=candidate.logical_observation_id,
        coverage_evidence_id=candidate.coverage_evidence_id,
        artifact_sha256=candidate.artifact_sha256,
        raw_answer_sha256=candidate.raw_answer_sha256,
        literal_name=candidate.literal_name,
        exact_excerpt=candidate.exact_excerpt,
        character_start=candidate.character_start,
        character_end=candidate.character_end,
        byte_start=candidate.byte_start,
        byte_end=candidate.byte_end,
        extractor_contract_version=extractor_contract_version,
    )


def extract_occurrence_run(
    *,
    window_id: str,
    measurement_spec_sha256: str,
    input_manifest_sha256: str,
    artifacts: Iterable[RawOccurrenceArtifact],
    literal_names: Iterable[str] | None = None,
    extractor_contract_version: str = DEFAULT_EXTRACTOR_CONTRACT,
    candidate_contract_version: str = DEFAULT_CANDIDATE_CONTRACT,
    coverage_contract_version: str = DEFAULT_COVERAGE_CONTRACT,
) -> OccurrenceExtractionRun:
    """Extract the exact deterministic candidate manifest from immutable bytes.

    ``literal_names`` is a fail-closed assertion retained for compatibility;
    it is never the source of candidates.  An omitted, extra, or reordered
    assertion conflicts with the names independently derived from the bytes.
    """

    artifact_rows = tuple(artifacts)
    if not artifact_rows:
        raise OccurrenceExtractionError("occurrence extraction requires raw artifacts")
    _text(extractor_contract_version, "extractor_contract_version")
    candidates, coverage_proofs = _derive_candidates_and_coverage(
        artifact_rows,
        candidate_contract_version=candidate_contract_version,
        coverage_contract_version=coverage_contract_version,
    )
    derived_names = _ordered_unique(item.literal_name for item in candidates)
    if literal_names is not None:
        asserted_names = tuple(_text(value, "literal_name") for value in literal_names)
        if len(set(asserted_names)) != len(asserted_names):
            raise OccurrenceExtractionError("literal names cannot contain duplicates")
        if asserted_names != derived_names:
            raise OccurrenceExtractionError(
                "caller candidate assertion does not exactly match deterministic candidates"
            )
    occurrences = tuple(
        _literal_occurrence(candidate, extractor_contract_version)
        for candidate in candidates
    )
    return OccurrenceExtractionRun(
        window_id=window_id,
        measurement_spec_sha256=measurement_spec_sha256,
        input_manifest_sha256=input_manifest_sha256,
        artifacts=artifact_rows,
        candidates=candidates,
        occurrences=occurrences,
        coverage_proofs=coverage_proofs,
        extractor_contract_version=extractor_contract_version,
        candidate_contract_version=candidate_contract_version,
        coverage_contract_version=coverage_contract_version,
    )


__all__ = [
    "CANDIDATE_MANIFEST_SCHEMA",
    "ARTIFACT_COVERAGE_PROOF_SCHEMA",
    "DEFAULT_CANDIDATE_CONTRACT",
    "DEFAULT_COVERAGE_CONTRACT",
    "DEFAULT_EXTRACTOR_CONTRACT",
    "EXTRACTOR_INPUT_SCHEMA",
    "OCCURRENCE_OFFSET_CONTRACT",
    "OCCURRENCE_COVERAGE_SCHEMA",
    "OCCURRENCE_OUTPUT_SCHEMA",
    "OCCURRENCE_RUN_SCHEMA",
    "ArtifactCoverageProof",
    "DeterministicCandidate",
    "LiteralOccurrence",
    "OccurrenceCoverageState",
    "OccurrenceExtractionError",
    "OccurrenceExtractionRun",
    "RawOccurrenceArtifact",
    "STRUCTURED_OCCURRENCE_HEADER",
    "derive_deterministic_candidates",
    "extract_occurrence_run",
]
