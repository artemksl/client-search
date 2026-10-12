"""Prepare report data from a completed visibility measurement.

Convert verified results, calculate visibility and assemble the report's input
data. This module does not write, render, approve or release report files.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .calculate_visibility import analyze_snapshot_observations
from .api import FreeSnapshotResult
from .convert_measurement_results import adapt_run_result
from .create_visibility_report import build_renderer_payload


ReadArtifact = Callable[[str, object], bytes]


class SnapshotReportDataError(ValueError):
    """A runner result cannot produce truthful report-ready data."""


def build_report_data(
    result: FreeSnapshotResult,
    *,
    read_artifact: ReadArtifact,
) -> dict[str, object]:
    """Return report-ready JSON facts for one successful runner result.

    The frozen scope passed to the publication adapter is the exact
    ``RunResult.plan.scope_manifest`` object.  ``read_artifact`` must be the
    runner's verified artifact reader (normally
    :meth:`FreeSnapshotMeasurementAPI.read_artifact`).
    """

    if not isinstance(result, FreeSnapshotResult):
        raise SnapshotReportDataError("result must be a FreeSnapshotResult")
    if not callable(read_artifact):
        raise SnapshotReportDataError("read_artifact must be callable")
    if not result.succeeded:
        raise SnapshotReportDataError(
            "report data requires a runner result with status succeeded"
        )

    runner_result = result.runner_result
    plan = getattr(runner_result, "plan", None)
    if plan is None:
        raise SnapshotReportDataError(
            "report data requires the runner result's immutable plan"
        )
    scope_manifest = getattr(plan, "scope_manifest", None)
    if not isinstance(scope_manifest, Mapping):
        raise SnapshotReportDataError(
            "report data requires plan.scope_manifest"
        )

    observations = adapt_run_result(
        result,
        frozen_scope=scope_manifest,
        read_artifact=read_artifact,
    )
    analysis = analyze_snapshot_observations(observations)
    payload = build_renderer_payload(analysis)
    if payload is None:
        raise SnapshotReportDataError(
            "successful runner result did not produce eligible report data"
        )
    return payload


__all__ = [
    "SnapshotReportDataError",
    "build_report_data",
]
