"""Bind a later clinic choice to unchanged area measurement evidence."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy

from .calculate_visibility import compute_results_hash, verify_results_hash
from .host_job_artifacts import SnapshotReportPreparationError, _validated_job_id


MATCHING_POLICY = "exact-recorded-name-v1"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def normalize_report_target(value: Mapping) -> dict:
    """Validate a trusted directory identity, retaining exact name spelling.

    The caller must resolve identity and aliases from its verified directory.
    This layer cannot turn a caller-supplied alias into verified evidence.
    """
    allowed = {"account_id", "name", "verified_aliases", "research_release_id",
               "research_manifest_sha256"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise SnapshotReportPreparationError("report target must contain a verified clinic identity")
    for key in ("account_id", "name"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise SnapshotReportPreparationError(f"report target requires {key}")
    aliases = value.get("verified_aliases", [])
    if (not isinstance(aliases, (list, tuple))
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)):
        raise SnapshotReportPreparationError("report target verified_aliases must be exact names")
    result = {"account_id": value["account_id"], "name": value["name"],
              "verified_aliases": sorted(set(aliases) - {value["name"]})}
    if any(key in value for key in ("research_release_id", "research_manifest_sha256")):
        if (not isinstance(value.get("research_release_id"), str)
                or not value["research_release_id"].strip()
                or not isinstance(value.get("research_manifest_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["research_manifest_sha256"])):
            raise SnapshotReportPreparationError("report target directory provenance is incomplete")
        result.update({key: value[key] for key in ("research_release_id", "research_manifest_sha256")})
    return result


def make_report_target_binding(scope: Mapping, target: Mapping, *, measurement_run_id: str) -> dict:
    target = normalize_report_target(target)
    if not isinstance(scope, Mapping):
        raise SnapshotReportPreparationError("report target requires a frozen measurement scope")
    if scope.get("scope_kind") == "diagnostic":
        raise SnapshotReportPreparationError("Diagnostic runs cannot produce clinic or area reports")
    snapshot_id = _validated_job_id(scope.get("snapshot_id"))
    scope_hash = scope.get("scope_hash")
    if not isinstance(scope_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", scope_hash):
        raise SnapshotReportPreparationError("report target requires a frozen measurement scope hash")
    identity = {"schema_version": "visibility-report-target-v1",
                "measurement_run_id": _validated_job_id(measurement_run_id),
                "measurement_snapshot_id": snapshot_id, "scope_hash": scope_hash,
                "matching_policy": MATCHING_POLICY, "report_target": target}
    return {**identity, "report_id": f"{snapshot_id[:120]}--clinic-{_digest(identity)[:32]}"}


def verify_report_target_binding(binding: Mapping, scope: Mapping, report_id: str) -> dict:
    if not isinstance(binding, Mapping):
        raise SnapshotReportPreparationError("report target binding must be an object")
    expected = make_report_target_binding(scope, binding.get("report_target"),
                                         measurement_run_id=binding.get("measurement_run_id"))
    if dict(binding) != expected or expected["report_id"] != report_id:
        raise SnapshotReportPreparationError("report target binding differs from its immutable identity")
    return expected


def bind_report_target(analysis: Mapping, binding: Mapping) -> dict:
    """Add report identity without altering the frozen measurement or metrics."""
    if not verify_results_hash(analysis):
        raise SnapshotReportPreparationError("measurement analysis results hash does not verify")
    scope = analysis.get("frozen_scope")
    verify_report_target_binding(binding, scope, binding["report_id"])
    if analysis.get("run_id") != binding["measurement_run_id"]:
        raise SnapshotReportPreparationError("report target belongs to another measurement")
    result = deepcopy(dict(analysis))
    result.update({"report_id": binding["report_id"],
                   "report_target": deepcopy(binding["report_target"]),
                   "report_target_binding": deepcopy(dict(binding)),
                   "measurement_results_hash": analysis["results_hash"]})
    result["results_hash"] = compute_results_hash(result)
    return result


def report_target_names(analysis: Mapping) -> frozenset[str]:
    target = analysis.get("report_target")
    if isinstance(target, Mapping):
        checked = normalize_report_target(target)
        return frozenset([checked["name"], *checked["verified_aliases"]])
    clinic = (analysis.get("frozen_scope") or {}).get("clinic") or {}
    return frozenset([clinic["name"]]) if clinic.get("name") else frozenset()


def target_name_aggregate(analysis: Mapping, observations: Sequence[Mapping]) -> dict:
    """Union exact matches per observation; retain the runner's name order."""
    names = report_target_names(analysis)
    positions = []
    frequency = 0
    for observation in observations:
        matches = [row for row in observation.get("grounded_occurrences") or []
                   if isinstance(row, Mapping) and row.get("surface_form") in names]
        frequency += len(matches)
        if matches:
            positions.append(matches[0].get("position"))
    numeric = [position for position in positions if isinstance(position, (int, float))
               and not isinstance(position, bool)]
    denominator = len(observations)
    return {"metrics": {
        "presence": {"numerator": len(positions), "denominator": denominator,
                     "rate": len(positions) / denominator if denominator else None},
        "first_mention_rank": {"recorded_positions": positions,
                               "mean": sum(numeric) / len(numeric) if numeric else None},
        "frequency": {"occurrences": frequency},
    }}
