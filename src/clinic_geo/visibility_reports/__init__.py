"""Public application boundary for Free Visibility Snapshot measurement.

Importing this package is safe when the optional external ``visibility_runner``
distribution is not installed.  The dependency is resolved only when a caller
constructs the measurement API or adapts a runner result.
"""

from .api import (
    FreeSnapshotMeasurementAPI,
    FreeSnapshotResult,
    adapt_runner_result,
    prepare_snapshot_report,
    normalize_report_target,
    verify_report_target_binding,
)
from .contracts_v3 import (
    ArtifactReferenceV3,
    EvidenceReferencesV3,
    GroundedOccurrenceV3,
    IssueV3,
    SNAPSHOT_OBSERVATIONS_V3,
    SnapshotObservationV3,
    SnapshotObservationsV3,
    SnapshotObservationsV3Error,
    SnapshotPacketV3,
)
from .convert_measurement_results import (
    ADAPTER_VERSION,
    ArtifactPayloadMismatchError,
    NonTerminalRunStatusError,
    PlanIdentityError,
    PromptIdentityError,
    PublicationAdapterError,
    RawAnswerDecodeError,
    RunPlanRequiredError,
    adapt_run_result,
)
from .prepare_report_data import (
    SnapshotReportDataError,
    build_report_data,
)

__all__ = [
    "ADAPTER_VERSION",
    "ArtifactPayloadMismatchError",
    "ArtifactReferenceV3",
    "EvidenceReferencesV3",
    "FreeSnapshotMeasurementAPI",
    "FreeSnapshotResult",
    "GroundedOccurrenceV3",
    "IssueV3",
    "NonTerminalRunStatusError",
    "PlanIdentityError",
    "PromptIdentityError",
    "PublicationAdapterError",
    "RawAnswerDecodeError",
    "RunPlanRequiredError",
    "SNAPSHOT_OBSERVATIONS_V3",
    "SnapshotObservationV3",
    "SnapshotObservationsV3",
    "SnapshotObservationsV3Error",
    "SnapshotPacketV3",
    "SnapshotReportDataError",
    "adapt_run_result",
    "adapt_runner_result",
    "build_report_data",
    "prepare_snapshot_report",
    "normalize_report_target",
    "verify_report_target_binding",
]
