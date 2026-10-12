"""Local, explicitly started scheduler for frozen three-day research grants.

The scheduler reserves work through the guarded coordinator. It never starts a
browser, grants execution permission, refreshes identification or reconstructs
missed measurements. UTC slots remain anchored across restarts and DST.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
import threading
from typing import Mapping

from clinic_geo.outreach.api import ConflictError


_LOG = logging.getLogger(__name__)
_INTERVAL = timedelta(hours=72)


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Schedule timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def latest_due_slot(anchor: datetime | str, now: datetime | str) -> tuple[int, datetime]:
    """Return the latest anchored slot and the next future due time.

    Slot zero is the enable time and is never dispatched. Missing ten intervals
    selects slot ten once, rather than constructing ten pending jobs.
    """
    anchor, now = _utc(anchor), _utc(now)
    slot = max(0, int((now - anchor) // _INTERVAL))
    return slot, anchor + (slot + 1) * _INTERVAL


class ClientResearchScheduler:
    def __init__(self, service, coordinator, *, clock=None, interval_seconds=30):
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or interval_seconds < 30:
            raise ValueError("Scheduler polling interval must be at least 30 seconds")
        self.service = service
        self.coordinator = coordinator
        self.clock = clock or service._now
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_report: dict | None = None

    def tick(self) -> dict:
        """Reconcile durable ownership, then reserve at most one slot per city.

        A retained execution/blocker conflict keeps its due target for the next
        poll. Each retry recomputes the newest due slot, so a busy city never
        builds a backlog. Only the coordinator can coalesce unused reservations
        or safely admit a slot under the current grant.
        """
        now = _utc(self.clock())
        report = {"checked_at": now.isoformat(), "reconciled": False, "schedules": [], "errors": []}
        if not self._tick_lock.acquire(blocking=False):
            return {**report, "busy": True}
        try:
            try:
                self.coordinator.reconcile()
                report["reconciled"] = True
                schedules = self.service.enabled_schedules()
            except Exception as exc:
                # Unknown durable state must be reconciled before any new
                # admission, even when another city's settings are valid.
                report["errors"].append({"operation": "reconcile", "error": str(exc)})
                self.last_report = report
                return report
            for schedule in schedules:
                entry = {"workspace_id": schedule["workspace_id"], "schedule_revision": schedule["revision"]}
                try:
                    anchor = _utc(schedule["anchor"])
                    due = _utc(schedule["next_due_at"])
                    if schedule.get("interval_hours", 72) != 72:
                        raise ValueError("The grant requires a 72-hour interval")
                    slot, next_due = latest_due_slot(anchor, now)
                    if anchor > now:
                        raise ValueError("The schedule anchor is in the future")
                    entry.update({"slot": slot, "next_due_at": next_due.isoformat()})
                    if slot < 1 or now < due:
                        entry["status"] = "not_due"
                    else:
                        # dispatch_due revalidates enabled/revision/fingerprint
                        # transactionally and reconciles an existing slot.
                        result = self.coordinator.dispatch_due(schedule["workspace_id"], schedule, slot)
                        if isinstance(result, Mapping):
                            entry["round_id"] = result.get("id", result.get("round_id"))
                            if result.get("state") == "waiting_attention" or result.get("status") in {"waiting", "blocked"}:
                                entry["status"] = "waiting"
                                report["schedules"].append(entry)
                                continue
                            entry["status"] = "skipped" if result.get("state", result.get("status")) == "skipped" else "reserved"
                        else:
                            entry["status"] = "reserved"
                        self.service.advance_schedule(schedule["workspace_id"], schedule["revision"], next_due.isoformat())
                except ConflictError as exc:
                    # Scope/ownership controls belong to the coordinator. Do
                    # not invent a fresh request, cancel a hold, or consume an
                    # unsafely admitted slot after a conflict.
                    entry.update({"status": "waiting", "error": str(exc)})
                except ValueError as exc:
                    try:
                        self.coordinator.pause_schedule(schedule["workspace_id"], str(exc))
                        entry.update({"status": "paused", "error": str(exc)})
                    except Exception as pause_error:
                        entry.update({"status": "error", "error": str(pause_error)})
                except Exception as exc:
                    entry.update({"status": "error", "error": str(exc)})
                report["schedules"].append(entry)
            self.last_report = report
            return report
        finally:
            self._tick_lock.release()

    def start(self):
        """Explicit server composition only; constructor/import never starts it."""
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="client-research-scheduler", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                _LOG.exception("Client research scheduler tick failed")
            self._stop.wait(self.interval_seconds)

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
