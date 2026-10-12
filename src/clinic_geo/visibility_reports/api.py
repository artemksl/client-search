"""Free Visibility Snapshot measurement and local report handoff facade.

This module deliberately contains no browser website, prompt name listing,
exact name matching, send attempt, retry, recovery, Google parser, measurement group scheduler,
classification, or publication implementation.  Those measurement concerns
belong to the external ``visibility_runner`` package.

The facade adds only application ownership:

* one import surface for a Free Snapshot worker;
* a result view whose properties delegate to the unchanged runner result;
* explicit lifecycle names that make normal execution and operator recovery
  impossible to confuse.

The explicit report handoff delegates to the report package and registers a
candidate with the existing publication store. That store retains its approval
rules; registration does not change the runner policy or approve publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from .client_research_evidence import (
    ClientResearchEvidenceError, adapt_client_research_result,
)


def _integration_module():
    """Load the optional runner integration only when measurement is requested."""

    from clinic_geo.integrations import visibility_runner as integration

    return integration


@dataclass(frozen=True, init=False)
class FreeSnapshotResult:
    """Lossless application view of one runner-owned result.

    No tuple, prompt measurement, ordered-name occurrence, issue, evidence reference,
    budget, or transport milestone is reconstructed.  Every property returns
    the exact object exposed by ``ApplicationResult``.
    """

    _application_result: Any

    def __init__(self, runner_result: object):
        application_result = _integration_module().adapt_result(runner_result)
        object.__setattr__(self, "_application_result", application_result)

    @property
    def runner_result(self):
        """Return the exact immutable ``visibility_runner.RunResult``."""

        return self._application_result.module_result

    @property
    def run_id(self) -> str:
        return self._application_result.run_id

    @property
    def status(self):
        """Return the runner status unchanged."""

        return self._application_result.status

    @property
    def observations(self):
        """Return runner prompt measurements in exact result order."""

        return self._application_result.observations

    @property
    def packets(self):
        return self._application_result.packets

    @property
    def issues(self):
        return self._application_result.issues

    @property
    def artifacts(self):
        return self._application_result.artifacts

    @property
    def submission_budget(self):
        """Return the authorization limit, not an inferred usage count."""

        return self._application_result.submission_budget

    @property
    def transport_counts(self):
        """Return actual runner milestones without collapsing their meaning."""

        return self._application_result.transport_counts

    @property
    def policy_version(self) -> str:
        return self._application_result.policy_version

    @property
    def succeeded(self) -> bool:
        """True only for the runner's exact ``SUCCEEDED`` status."""

        return self._application_result.succeeded


def adapt_runner_result(runner_result: object) -> FreeSnapshotResult:
    """Adapt a typed runner result without transforming its evidence."""

    return FreeSnapshotResult(runner_result)


def prepare_snapshot_report(job_id: str, *, repo_root, run_root=None, **options) -> dict:
    """Prepare immutable local report files and register them for review.

    Existing publication policy and explicit founder approval remain in force.
    No measurement execution or publication is authorized by this operation.
    """
    from .report_handoff import prepare_snapshot_report as prepare

    return prepare(job_id, repo_root=repo_root, run_root=run_root, **options)


def normalize_report_target(value) -> dict:
    """Validate an identity already resolved from the verified clinic directory."""
    from .report_target import normalize_report_target as normalize

    return normalize(value)


def verify_report_target_binding(binding, scope, report_id) -> dict:
    """Verify the separate clinic report identity against unchanged scope."""
    from .report_target import verify_report_target_binding as verify

    return verify(binding, scope, report_id)


class FreeSnapshotMeasurementAPI:
    """Synchronous application lifecycle for runner-owned measurement.

    ``run_new`` is the normal authorized path: pure planning followed by one
    direct ``Runner.run(plan)`` delegation in the underlying integration.
    ``resume`` continues existing authorization without granting a new pass.
    ``refetch`` consumes a distinct, explicit finite-pass request.
    """

    def __init__(self, runtime: object):
        integration = _integration_module()
        self._component = integration.VisibilityMeasurementComponent(runtime)

    def plan(
        self,
        measurement: object,
        *,
        run_id: str,
    ):
        """Purely plan one unique run through ``Runner.plan()``."""

        return self._component.plan(measurement, run_id=run_id)

    def run(self, plan: object) -> FreeSnapshotResult:
        """Execute one already-authorized plan exactly once."""

        return adapt_runner_result(self._component.run(plan))

    def run_new(
        self,
        measurement: object,
        *,
        run_id: str,
    ) -> FreeSnapshotResult:
        """Plan, then directly execute, one unique new run."""

        plan = self.plan(measurement, run_id=run_id)
        return self.run(plan)

    def inspect(self, run_id: str) -> FreeSnapshotResult:
        """Read current durable runner state without sending."""

        return adapt_runner_result(self._component.inspect(run_id))

    def read_artifact(self, run_id: str, reference: object) -> bytes:
        """Read hash-bound evidence through the runner's verified reader."""

        return self._component.read_artifact(run_id, reference)

    def resume(self, run_id: str) -> FreeSnapshotResult:
        """Request explicit runner-owned safe recovery."""

        return adapt_runner_result(self._component.resume(run_id))

    @property
    def recovery_available(self) -> bool:
        """Expose the complete installed engine recovery capability."""

        return self._component.recovery_available

    def execution(self, run_id: str, *, execution_id: str | None = None):
        """Hold engine ownership across execution and committed host output."""

        return self._component.execution(run_id, execution_id=execution_id)

    def plan_refetch(self, run_id: str, *, request_id: str | None = None):
        """Read a revision-bound preview; this never authorizes execution."""

        return self._component.plan_refetch(run_id, request_id=request_id)

    def refetch(self, request: object) -> FreeSnapshotResult:
        """Execute one explicitly authorized finite pass in the guarded worker."""

        return adapt_runner_result(self._component.refetch(request))

    def failure_readout(self, run_id: str, *, worker_observation: object = None):
        """Project the engine's shared diagnostic schema without recovery."""

        return self._component.failure_readout(run_id, worker_observation=worker_observation)

    def failure_readout_for_plan(self, plan: object, *, worker_observation: object = None):
        """Project a frozen, never-started plan without creating execution state."""

        return self._component.failure_readout_for_plan(plan, worker_observation=worker_observation)

    def resolve_recovery_link(self, run_id: str, reference: str):
        """Resolve a typed evidence reference through the engine boundary."""

        return self._component.resolve_recovery_link(run_id, reference)

    def ownership(self, run_id: str):
        """Sample public ownership information without claiming the run."""

        return self._component.ownership(run_id)

    def cancel(self, run_id: str, *, execution_id: str | None = None) -> FreeSnapshotResult:
        """Cancel runner-owned work, fencing callbacks to their execution."""

        options = {"execution_id": execution_id} if execution_id is not None else {}
        return adapt_runner_result(self._component.cancel(run_id, **options))


__all__ = [
    "ClientResearchEvidenceError",
    "adapt_client_research_result",
    "FreeSnapshotMeasurementAPI",
    "FreeSnapshotResult",
    "adapt_runner_result",
    "prepare_snapshot_report",
    "normalize_report_target",
    "verify_report_target_binding",
]
