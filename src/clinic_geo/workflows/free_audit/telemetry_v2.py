"""Deterministic operational metrics for Free Visibility Snapshot v2."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional

from clinic_geo.paths import root_or_default
from clinic_geo.workflows.free_audit.contracts_v2 import (
    SCOPE_V2_VERSION,
    SCOPE_V4_VERSION,
    canonical_sha256,
)


MATERIAL_OVERALL = "Material opportunity demonstrated"
RUNTIME_TELEMETRY_SCHEMA_VERSION = "free_snapshot.runtime_telemetry.v1"


class SnapshotTelemetryError(RuntimeError):
    """Run-local operational telemetry is malformed or cannot be preserved."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, indent=2, ensure_ascii=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _iso(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _elapsed_seconds(started: object, finished: object) -> Optional[float]:
    start = _iso(started)
    finish = _iso(finished)
    if start is None or finish is None:
        return None
    seconds = (finish - start).total_seconds()
    return round(max(0.0, seconds), 3)


class SnapshotRuntimeTelemetry:
    """Append-only stage/provider timing for one resumable Snapshot run."""

    def __init__(
        self,
        run_dir: Path | str,
        report_id: str,
        *,
        execution_manifest: Optional[Mapping] = None,
        resumed: bool = False,
        workflow_started_at: Optional[str] = None,
        workflow_started_monotonic: Optional[float] = None,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.path = self.run_dir / "runtime_telemetry.json"
        self.report_id = str(report_id)
        self._stage_started_monotonic: dict[str, float] = {}
        self._workflow_started_monotonic = (
            float(workflow_started_monotonic)
            if workflow_started_monotonic is not None
            else time.monotonic()
        )
        existing = _json_object(self.path)
        if self.path.exists() and existing is None:
            raise SnapshotTelemetryError(
                f"Existing runtime telemetry is not valid JSON: {self.path}"
            )
        if existing:
            if existing.get("schema_version") != RUNTIME_TELEMETRY_SCHEMA_VERSION:
                raise SnapshotTelemetryError("Runtime telemetry schema is unsupported")
            if str(existing.get("report_id") or "") != self.report_id:
                raise SnapshotTelemetryError("Runtime telemetry report_id does not match")
            self.payload = existing
            interrupted_at = workflow_started_at or _utc_now()
            self._close_orphaned_rows(interrupted_at)
        else:
            manifest = dict(execution_manifest or {})
            self.payload = {
                "schema_version": RUNTIME_TELEMETRY_SCHEMA_VERSION,
                "report_id": self.report_id,
                "execution_engine": manifest.get("execution_engine"),
                "execution_engine_version": manifest.get("execution_engine_version"),
                "classifier_engine": manifest.get("classifier_engine"),
                "classifier_engine_version": manifest.get("classifier_engine_version"),
                "created_at": workflow_started_at or _utc_now(),
                "updated_at": workflow_started_at or _utc_now(),
                "status": "running",
                "duration_seconds": None,
                "duration_seconds_basis": "wall_span_including_resume_idle",
                "wall_span_seconds": None,
                "active_duration_seconds": 0.0,
                "active_duration_basis": "closed_non_interrupted_workflow_attempts",
                "interrupted_duration_upper_bound_seconds": 0.0,
                "workflow_attempts": [],
                "stages": [],
                "preflight_attempts": [],
                "provider_runs": [],
                "interactions": {
                    "collection": 0,
                    "classification": 0,
                    "classification_scheduled": 0,
                    "classification_captured_responses": 0,
                    "classification_submission_confirmation": "not_recorded",
                    "name_reconciliation": 0,
                    "total_browser_or_model": 0,
                    "total_recorded_browser_or_model": 0,
                    "total_interaction_basis": "persisted_transport_evidence",
                },
            }
        attempts = self.payload.setdefault("workflow_attempts", [])
        self.attempt_sequence = len(attempts) + 1
        self._attempt_started_at = workflow_started_at or _utc_now()
        self._attempt_started_monotonic = self._workflow_started_monotonic
        attempts.append(
            {
                "sequence": self.attempt_sequence,
                "resumed": bool(resumed),
                "started_at": self._attempt_started_at,
                "finished_at": None,
                "duration_seconds": None,
                "status": "running",
                "error": None,
            }
        )
        self.payload["status"] = "running"
        self._write()

    def _write(self) -> None:
        self.payload["updated_at"] = _utc_now()
        _atomic_json(self.path, self.payload)

    def _close_orphaned_rows(self, interrupted_at: str) -> None:
        """Close rows left running by a hard stop before appending a resume."""

        interruption = {
            "type": "InterruptedAttempt",
            "detail": "A later workflow attempt resumed this run after an unclosed process.",
        }
        for attempt in self.payload.get("workflow_attempts") or []:
            if isinstance(attempt, dict) and attempt.get("status") == "running":
                attempt.update(
                    {
                        "finished_at": interrupted_at,
                        "duration_seconds": _elapsed_seconds(
                            attempt.get("started_at"), interrupted_at
                        ),
                        "status": "interrupted",
                        "error": dict(interruption),
                    }
                )
        for row in self.payload.get("stages") or []:
            if isinstance(row, dict) and row.get("status") == "running":
                row.update(
                    {
                        "finished_at": interrupted_at,
                        "duration_seconds": _elapsed_seconds(
                            row.get("started_at"), interrupted_at
                        ),
                        "status": "interrupted",
                        "error": dict(interruption),
                    }
                )

    def start_stage(self, name: str, *, reused: bool = False) -> dict:
        """Persist a stage start for spans that cross several workflow calls."""

        clean_name = str(name)
        sequence = 1 + sum(
            1 for item in self.payload.get("stages", []) if item.get("name") == clean_name
        )
        stage_id = f"attempt-{self.attempt_sequence}:{clean_name}:{sequence}"
        row = {
            "stage_id": stage_id,
            "workflow_attempt": self.attempt_sequence,
            "name": clean_name,
            "sequence": sequence,
            "reused": bool(reused),
            "started_at": _utc_now(),
            "finished_at": None,
            "duration_seconds": None,
            "status": "running",
            "error": None,
        }
        self._stage_started_monotonic[stage_id] = time.monotonic()
        self.payload.setdefault("stages", []).append(row)
        self._write()
        return row

    def _finish_stage_row(
        self,
        row: dict,
        *,
        status: str,
        error: Optional[BaseException] = None,
    ) -> None:
        if row.get("status") != "running":
            return
        finished_at = _utc_now()
        stage_id = str(row.get("stage_id") or "")
        started_monotonic = self._stage_started_monotonic.pop(stage_id, None)
        duration = (
            round(max(0.0, time.monotonic() - started_monotonic), 3)
            if started_monotonic is not None
            else _elapsed_seconds(row.get("started_at"), finished_at)
        )
        row.update(
            {
                "finished_at": finished_at,
                "duration_seconds": duration,
                "status": str(status),
                "error": (
                    {"type": type(error).__name__, "detail": str(error)}
                    if error is not None
                    else None
                ),
            }
        )

    def finish_stage(
        self,
        row: Mapping,
        *,
        status: str = "completed",
        error: Optional[BaseException] = None,
    ) -> None:
        """Close a previously persisted stage exactly once."""

        stage_id = str(row.get("stage_id") or "")
        stored = next(
            (
                item
                for item in self.payload.get("stages", [])
                if isinstance(item, dict) and item.get("stage_id") == stage_id
            ),
            None,
        )
        if stored is None:
            raise SnapshotTelemetryError("Runtime telemetry stage is not part of this attempt")
        self._finish_stage_row(stored, status=status, error=error)
        self._write()

    @contextmanager
    def stage(self, name: str, *, reused: bool = False) -> Iterator[dict]:
        """Record one stage attempt, including failures and cancellations."""

        row = self.start_stage(name, reused=reused)
        try:
            yield row
        except BaseException as exc:
            self.finish_stage(
                row,
                status=(
                    "cancelled"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "failed"
                ),
                error=exc,
            )
            raise
        else:
            self.finish_stage(row, status="reused" if reused else "completed")

    def record_provider_runs(self, platform_runs: Mapping[str, Mapping]) -> None:
        """Persist additive provider makespan and charged-interaction totals."""

        collection_total = 0
        preflight_recorded = False
        existing_provider_keys = {
            (
                str(row.get("collection_attempt_id") or row.get("workflow_attempt") or ""),
                str(row.get("platform") or ""),
            )
            for row in self.payload.get("provider_runs") or []
            if isinstance(row, Mapping)
        }
        for platform, raw in platform_runs.items():
            if not isinstance(raw, Mapping):
                continue
            collection_attempt_id = str(raw.get("collection_attempt_id") or "")
            barrier = raw.get("browser_readiness_barrier")
            if not preflight_recorded and isinstance(barrier, Mapping):
                self._record_preflight(
                    barrier,
                    collection_attempt_id=collection_attempt_id,
                )
                preflight_recorded = True
            commands = [
                row for row in raw.get("commands") or [] if isinstance(row, Mapping)
            ]
            interaction_count = raw.get("interaction_count")
            interaction_status = str(
                raw.get("interaction_count_status") or "derived_from_commands"
            )
            interaction_recorded = interaction_status != "unknown_after_worker_failure"
            if not isinstance(interaction_count, int) or isinstance(
                interaction_count, bool
            ):
                interaction_count = sum(
                    len(row.get("observation_ids") or []) for row in commands
                )
            duration = raw.get("duration_seconds")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                duration = _elapsed_seconds(raw.get("started_at"), raw.get("finished_at"))
            provider_key = (
                collection_attempt_id or str(self.attempt_sequence),
                str(platform),
            )
            if provider_key in existing_provider_keys:
                continue
            raw_prompt_references = raw.get("prompt_timings_references")
            if not isinstance(raw_prompt_references, (list, tuple)):
                raw_prompt_references = []
            prompt_references = [
                str(value)
                for value in raw_prompt_references
                if isinstance(value, str) and value.strip()
            ]
            prompt_summary = raw.get("prompt_timing_summary")
            if not isinstance(prompt_summary, Mapping) or prompt_summary.get(
                "schema_version"
            ) != "free_snapshot.prompt_timing_summary.v1":
                prompt_summary = None
            timing_status_counts = raw.get(
                "prompt_timing_ingestion_status_counts"
            )
            if not isinstance(timing_status_counts, Mapping):
                timing_status_counts = {}
            watchdog_attempts = [
                deepcopy(value)
                for value in raw.get("watchdog_attempts") or []
                if isinstance(value, Mapping)
            ]
            collection_flag_state = raw.get("collection_only_flag_state")
            if not isinstance(collection_flag_state, Mapping):
                collection_flag_state = None
            row = {
                "workflow_attempt": self.attempt_sequence,
                "collection_attempt_id": collection_attempt_id or None,
                "platform": str(platform),
                "worker_status": raw.get("worker_status"),
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
                "duration_seconds": duration,
                "planned_observations": raw.get("planned_observations"),
                "command_count": len(commands),
                "interaction_count": (
                    int(interaction_count) if interaction_recorded else None
                ),
                "interaction_count_status": interaction_status,
                "artifact_origin": raw.get("telemetry_artifact_origin")
                or "collection_attempt",
                "retry_amplification": (
                    int(
                        raw.get("retry_amplification")
                        if isinstance(raw.get("retry_amplification"), int)
                        and not isinstance(raw.get("retry_amplification"), bool)
                        else max(
                            0,
                            int(interaction_count)
                            - int(raw.get("planned_observations") or 0),
                        )
                    )
                    if interaction_recorded
                    else None
                ),
                "status_counts": dict(raw.get("status_counts") or {}),
                "prompt_timings_references": prompt_references,
                "prompt_timing_summary": (
                    deepcopy(prompt_summary) if prompt_summary is not None else None
                ),
                "prompt_timing_ingestion_status_counts": dict(
                    timing_status_counts
                ),
                "watchdog_attempts": watchdog_attempts,
                "collection_only_flag_state": (
                    deepcopy(collection_flag_state)
                    if collection_flag_state is not None
                    else None
                ),
            }
            self.payload.setdefault("provider_runs", []).append(row)
            existing_provider_keys.add(provider_key)
            if interaction_recorded:
                collection_total += int(interaction_count)
        interactions = self.payload.setdefault("interactions", {})
        interactions["collection"] = int(interactions.get("collection") or 0) + collection_total
        self._refresh_total_interactions()
        self._write()

    def _record_preflight(
        self,
        barrier: Mapping,
        *,
        collection_attempt_id: str = "",
    ) -> None:
        duration = barrier.get("duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = _elapsed_seconds(
                barrier.get("started_at"), barrier.get("finished_at")
            )
        row = {
            "collection_attempt_id": collection_attempt_id or None,
            "status": barrier.get("status"),
            "started_at": barrier.get("started_at"),
            "finished_at": barrier.get("finished_at"),
            "duration_seconds": duration,
            "error": deepcopy(barrier.get("error"))
            if isinstance(barrier.get("error"), Mapping)
            else None,
        }
        attempts = self.payload.setdefault("preflight_attempts", [])
        key = (
            row["collection_attempt_id"] or "",
            str(row["started_at"] or ""),
        )
        if not any(
            (
                str(item.get("collection_attempt_id") or ""),
                str(item.get("started_at") or ""),
            )
            == key
            for item in attempts
            if isinstance(item, Mapping)
        ):
            attempts.append(row)
        self.payload["preflight"] = row

    def record_provider_artifacts(
        self,
        *,
        reconcile_interrupted: bool = False,
    ) -> None:
        """Ingest durable provider artifacts and optionally close prior orphans.

        ``reconcile_interrupted`` is reserved for the start of a resumed
        workflow.  Exception handlers in the current attempt ingest evidence
        without relabelling that attempt as an interruption by a later one.
        """

        attempt_rows = []
        for attempt_dir in (self.run_dir / "collection_attempts").glob("attempt-*"):
            preflight = _json_object(attempt_dir / "browser_preflight.json")
            interrupted_at = self._attempt_started_at
            interruption = {
                "type": "InterruptedAttempt",
                "detail": "A later workflow attempt resumed this collection.",
            }
            if (
                reconcile_interrupted
                and isinstance(preflight, dict)
                and preflight.get("status") == "running"
            ):
                preflight.update(
                    {
                        "status": "interrupted",
                        "finished_at": interrupted_at,
                        "duration_seconds": _elapsed_seconds(
                            preflight.get("started_at"), interrupted_at
                        ),
                        "error": dict(interruption),
                    }
                )
                _atomic_json(attempt_dir / "browser_preflight.json", preflight)
            attempt = _json_object(attempt_dir / "collection_attempt.json")
            if (
                reconcile_interrupted
                and isinstance(attempt, dict)
                and attempt.get("status") == "running"
            ):
                attempt.update(
                    {
                        "status": "interrupted",
                        "finished_at": interrupted_at,
                        "duration_seconds": _elapsed_seconds(
                            attempt.get("started_at"), interrupted_at
                        ),
                        "error": dict(interruption),
                    }
                )
                _atomic_json(
                    attempt_dir / "collection_attempt.json",
                    attempt,
                )
            attempt_id = str(
                (preflight or {}).get("collection_attempt_id")
                or (attempt or {}).get("collection_attempt_id")
                or attempt_dir.name.removeprefix("attempt-")
            )
            started_at = str((preflight or {}).get("started_at") or "")
            attempt_rows.append((started_at, attempt_dir, attempt_id, preflight))
        for _started_at, attempt_dir, attempt_id, preflight in sorted(attempt_rows):
            if isinstance(preflight, Mapping):
                self._record_preflight(preflight, collection_attempt_id=attempt_id)
            platform_runs = {}
            for path in sorted(
                (attempt_dir / "platforms").glob("*/platform_run.json")
            ):
                value = _json_object(path)
                if value and value.get("platform"):
                    platform_runs[str(value["platform"])] = value
            if platform_runs:
                self.record_provider_runs(platform_runs)
        # Historical pre-WP0 runs only have the convenience artifacts.  Read
        # them before a retry can overwrite those files.  A stable synthetic
        # attempt ID makes repeated resume reconciliation idempotent.
        convenience_runs = {}
        for path in sorted((self.run_dir / "platforms").glob("*/platform_run.json")):
            value = _json_object(path)
            if not value or not value.get("platform"):
                continue
            value = deepcopy(value)
            if not value.get("collection_attempt_id"):
                value["collection_attempt_id"] = (
                    "legacy-convenience-" + canonical_sha256(value)[:24]
                )
                value["telemetry_artifact_origin"] = "pre_wp0_convenience"
            else:
                value["telemetry_artifact_origin"] = "convenience_copy"
            convenience_runs[str(value["platform"])] = value
        if convenience_runs:
            self.record_provider_runs(convenience_runs)
        self._write()

    def record_legacy_classifier_artifacts(
        self,
        *,
        reconcile_interrupted: bool = False,
    ) -> None:
        """Ingest legacy transport evidence and optionally close prior orphans."""

        scheduled = 0
        classified_observations = 0
        captured_responses = 0
        confirmed_submissions = 0
        confirmation_available = True
        ambiguous_submission = False
        for attempt_root in sorted(
            (self.run_dir / "extraction_transport").glob("attempt-*")
        ):
            if not attempt_root.is_dir():
                continue
            path = attempt_root / "extraction_prompts.json"
            value = _json_object(path) or {}
            prompts = [row for row in value.get("prompts") or [] if isinstance(row, Mapping)]
            scheduled += len(prompts)
            classified_observations += sum(
                len(row.get("observation_ids") or []) for row in prompts
            )
            result = _json_object(attempt_root / "transport_attempt.json")
            answer_count = len(list((attempt_root / "answers").glob("*.md")))
            if (
                reconcile_interrupted
                and isinstance(result, dict)
                and result.get("status") == "running"
            ):
                interrupted_at = self._attempt_started_at
                result.update(
                    {
                        "status": "interrupted",
                        "finished_at": interrupted_at,
                        "duration_seconds": _elapsed_seconds(
                            result.get("started_at"), interrupted_at
                        ),
                        "captured_response_count": max(
                            int(result.get("captured_response_count") or 0),
                            answer_count,
                        ),
                        "confirmed_submission_count": None,
                        "submission_confirmation": "ambiguous_after_interruption",
                        "error": {
                            "type": "InterruptedAttempt",
                            "detail": (
                                "A later workflow attempt resumed after this legacy "
                                "classifier transport stopped without acknowledgement."
                            ),
                        },
                    }
                )
                _atomic_json(attempt_root / "transport_attempt.json", result)
            if (result or {}).get("submission_confirmation") == (
                "ambiguous_after_interruption"
            ):
                ambiguous_submission = True
            captured = (result or {}).get("captured_response_count")
            captured_responses += max(
                captured
                if isinstance(captured, int) and not isinstance(captured, bool)
                else 0,
                answer_count,
            )
            confirmed = (result or {}).get("confirmed_submission_count")
            if isinstance(confirmed, int) and not isinstance(confirmed, bool):
                confirmed_submissions += confirmed
            else:
                confirmation_available = False
        interactions = self.payload.setdefault("interactions", {})
        interactions["classification"] = (
            confirmed_submissions if scheduled and confirmation_available else 0
        )
        interactions["classification_scheduled"] = scheduled
        interactions["classification_captured_responses"] = captured_responses
        interactions["classification_submission_confirmation"] = (
            "not_applicable"
            if not scheduled
            else "ambiguous_after_interruption"
            if ambiguous_submission
            else "confirmed"
            if confirmation_available
            else "unavailable_legacy_transport"
        )
        interactions["classification_observation_payloads"] = classified_observations
        self._refresh_total_interactions()
        self._write()

    def record_aggregate_classifier_artifacts(self) -> None:
        """Count the four purpose-bound jobs from their durable send ledger."""

        manifest = _json_object(
            self.run_dir / "classification" / "classification_jobs.json"
        )
        interactions = self.payload.setdefault("interactions", {})
        if not manifest:
            interactions.update(
                {
                    "classification": 0,
                    "classification_scheduled": 0,
                    "classification_captured_responses": 0,
                    "classification_submission_confirmation": "not_applicable",
                    "classification_observation_payloads": 0,
                }
            )
            self._refresh_total_interactions()
            self._write()
            return
        jobs = manifest.get("jobs")
        platforms = manifest.get("platforms")
        if (
            manifest.get("checker_version") != "snapshot-aggregate-classifier-v2"
            or platforms
            != ["chatgpt", "gemini", "google_ai_overview", "copilot"]
            or not isinstance(jobs, Mapping)
            or set(jobs) != set(platforms)
        ):
            raise SnapshotTelemetryError(
                "aggregate classification telemetry found an invalid four-job ledger"
            )
        scheduled = 0
        acknowledged = 0
        captured = 0
        reserved = 0
        ambiguous = False
        observation_payloads = 0
        for platform in platforms:
            job = jobs[platform]
            if not isinstance(job, Mapping):
                raise SnapshotTelemetryError(
                    "aggregate classification telemetry found an invalid job"
                )
            scheduled += int(bool(job.get("input_sha256")))
            acknowledged += int(job.get("submission_count") or 0)
            captured += int(bool(job.get("response_sha256")))
            reserved += int(job.get("send_action_count") or 0)
            ambiguous = ambiguous or job.get("state") == "submission_unknown"
            reference = str(job.get("input_reference") or "")
            input_artifact = _json_object(
                self.run_dir / "classification" / reference
            ) if reference else None
            if input_artifact:
                observation_payloads += int(
                    input_artifact.get("expected_observation_count") or 0
                )
        if ambiguous or reserved > acknowledged:
            confirmation = "ambiguous_after_reservation"
        elif acknowledged == len(platforms):
            confirmation = "confirmed"
        elif scheduled:
            confirmation = "partial_confirmed"
        else:
            confirmation = "not_applicable"
        interactions.update(
            {
                "classification": acknowledged,
                "classification_scheduled": scheduled,
                "classification_captured_responses": captured,
                "classification_submission_confirmation": confirmation,
                "classification_observation_payloads": observation_payloads,
            }
        )
        self._refresh_total_interactions()
        interactions["total_interaction_basis"] = (
            "persisted provider-command evidence plus the durable exactly-four "
            "aggregate classification send ledger"
        )
        self._write()

    def record_legacy_name_reconciliation_artifacts(self) -> None:
        """Count legacy reconciliation chats with a persisted transport attempt."""

        count = 0
        for path in sorted(
            (self.run_dir / "platforms").glob(
                "*/name_reconciliation/transport_result.json"
            )
        ):
            row = _json_object(path)
            if row and row.get("started_at"):
                count += 1
        interactions = self.payload.setdefault("interactions", {})
        interactions["name_reconciliation"] = count
        self._refresh_total_interactions()
        self._write()

    def _refresh_total_interactions(self) -> None:
        interactions = self.payload.setdefault("interactions", {})
        confirmed_total = sum(
            int(interactions.get(key) or 0)
            for key in ("collection", "classification", "name_reconciliation")
        )
        interactions["total_browser_or_model"] = confirmed_total
        interactions["total_recorded_browser_or_model"] = confirmed_total
        interactions["total_interaction_basis"] = (
            "persisted provider-command/transport evidence; legacy classification "
            "schedules are reported separately until acknowledgement is available"
        )

    def finish(self, status: str, *, error: Optional[BaseException] = None) -> None:
        """Finish the current workflow attempt without discarding prior attempts."""

        dangling_status = (
            "cancelled"
            if status == "cancelled"
            else "failed" if status in {"failed", "completed"} else str(status)
        )
        for row in self.payload.get("stages") or []:
            if (
                isinstance(row, dict)
                and row.get("workflow_attempt") == self.attempt_sequence
                and row.get("status") == "running"
            ):
                dangling_error = error or SnapshotTelemetryError(
                    "Workflow attempt ended before this stage reached its persistence boundary"
                )
                self._finish_stage_row(
                    row,
                    status=dangling_status,
                    error=dangling_error,
                )
        attempts = self.payload.get("workflow_attempts") or []
        current = attempts[-1] if attempts else None
        if isinstance(current, dict) and current.get("status") == "running":
            current.update(
                {
                    "finished_at": _utc_now(),
                    "duration_seconds": round(
                        max(0.0, time.monotonic() - self._attempt_started_monotonic), 3
                    ),
                    "status": str(status),
                    "error": (
                        {"type": type(error).__name__, "detail": str(error)}
                        if error is not None
                        else None
                    ),
                }
            )
        self.payload["status"] = str(status)
        self.payload["finished_at"] = _utc_now()
        wall_span = _elapsed_seconds(
            self.payload.get("created_at"), self.payload.get("finished_at")
        )
        active_duration = round(
            sum(
                float(row.get("duration_seconds") or 0.0)
                for row in self.payload.get("workflow_attempts") or []
                if isinstance(row, Mapping)
                and row.get("status") != "interrupted"
            ),
            3,
        )
        interrupted_upper_bound = round(
            sum(
                float(row.get("duration_seconds") or 0.0)
                for row in self.payload.get("workflow_attempts") or []
                if isinstance(row, Mapping)
                and row.get("status") == "interrupted"
            ),
            3,
        )
        # Preserve the pre-WP0 additive field for consumers, but state its
        # semantics explicitly and provide the performance-safe active total.
        self.payload["duration_seconds"] = wall_span
        self.payload["duration_seconds_basis"] = "wall_span_including_resume_idle"
        self.payload["wall_span_seconds"] = wall_span
        self.payload["active_duration_seconds"] = active_duration
        self.payload["active_duration_basis"] = (
            "closed_non_interrupted_workflow_attempts"
        )
        self.payload["interrupted_duration_upper_bound_seconds"] = (
            interrupted_upper_bound
        )
        self._write()


def _overall_verdict(analysis: Mapping) -> str:
    value = analysis.get("overall_verdict")
    if isinstance(value, Mapping):
        value = value.get("label") or value.get("status")
    return str(value or "unknown").strip() or "unknown"


def summarize_snapshot_metrics(repo_root: Path | str | None = None) -> dict:
    """Recompute commercial/quality telemetry from immutable run state."""

    root = root_or_default(repo_root).resolve()
    runs_root = root / "data" / "free-audit" / "runs"
    publication_states: Counter[str] = Counter()
    operational_states: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    review_seconds: list[float] = []
    scope_versions: Counter[str] = Counter()
    prepared = approved = rejected = audit_recommended = 0

    if runs_root.is_dir():
        for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            manifest = _json_object(run_dir / "scope_manifest.json")
            scope_version = str((manifest or {}).get("scope_version") or "")
            if not manifest or scope_version not in {SCOPE_V2_VERSION, SCOPE_V4_VERSION}:
                continue
            state = _json_object(run_dir / "snapshot_state.json")
            analysis = _json_object(run_dir / "analysis.json")
            if not state or not analysis:
                continue
            prepared += 1
            scope_versions[scope_version] += 1
            publication_state = str(state.get("publication_state") or "draft")
            operational_state = str(state.get("operational_state") or "unknown")
            publication_states[publication_state] += 1
            operational_states[operational_state] += 1
            verdict = _overall_verdict(analysis)
            verdicts[verdict] += 1
            if verdict == MATERIAL_OVERALL:
                audit_recommended += 1

            history = state.get("state_history")
            if isinstance(history, list):
                previous = None
                for item in history:
                    if not isinstance(item, Mapping):
                        continue
                    current = str(item.get("publication_state") or "")
                    if previous and current and current != previous:
                        transitions[f"{previous}->{current}"] += 1
                    if current:
                        previous = current

            founder_review = _json_object(run_dir / "founder_review.json")
            if not founder_review:
                continue
            decision = str(founder_review.get("decision") or "")
            if decision == "approved":
                approved += 1
            elif decision == "rejected":
                rejected += 1
                reason = str(founder_review.get("reason") or "unspecified").strip()
                rejection_reasons[reason or "unspecified"] += 1

            reviewed_at = _iso(founder_review.get("reviewed_at"))
            review_started_at = None
            if isinstance(history, list):
                for item in history:
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("publication_state") in {
                        "awaiting_founder_review",
                        "inconclusive",
                    }:
                        review_started_at = _iso(item.get("at"))
                        break
            if reviewed_at and review_started_at:
                seconds = (reviewed_at - review_started_at).total_seconds()
                if seconds >= 0:
                    review_seconds.append(seconds)

    recommendation_rate = audit_recommended / prepared if prepared else 0.0
    warnings = []
    if prepared and audit_recommended == prepared:
        warnings.append(
            "Credibility warning: 100% of prepared Snapshots recommend the Diagnostic Audit."
        )
    return {
        "schema_version": "free_snapshot.metrics.v1",
        "snapshots_prepared": prepared,
        "scope_version_distribution": dict(sorted(scope_versions.items())),
        "publication_state_distribution": dict(sorted(publication_states.items())),
        "operational_state_distribution": dict(sorted(operational_states.items())),
        "verdict_distribution": dict(sorted(verdicts.items())),
        "state_transitions": dict(sorted(transitions.items())),
        "snapshots_approved": approved,
        "snapshots_rejected": rejected,
        "rejection_reason_distribution": dict(sorted(rejection_reasons.items())),
        "review_duration_seconds": {
            "count": len(review_seconds),
            "mean": round(statistics.fmean(review_seconds), 3) if review_seconds else None,
            "median": round(statistics.median(review_seconds), 3) if review_seconds else None,
        },
        "audit_recommended": audit_recommended,
        "audit_recommendation_rate": recommendation_rate,
        "credibility_warnings": warnings,
    }


def write_snapshot_metrics(repo_root: Path | str | None = None) -> Path:
    """Atomically update the internal Snapshot metrics roll-up."""

    root = root_or_default(repo_root).resolve()
    path = root / "data" / "free-audit" / "snapshot_metrics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        summarize_snapshot_metrics(root), indent=2, ensure_ascii=False
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
