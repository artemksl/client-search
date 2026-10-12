"""Offline scheduling with injected ports; never a real thread, job or browser."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from clinic_geo.outreach.api import ConflictError
from clinic_geo.web.client_research_scheduler import ClientResearchScheduler, latest_due_slot

ANCHOR = datetime(2026, 3, 28, 10, tzinfo=timezone.utc)


class Service:
    def __init__(self, schedules, now):
        self.schedules = schedules
        self.now = now
        self.advances = []

    def _now(self):
        return self.now

    def enabled_schedules(self):
        return deepcopy([schedule for schedule in self.schedules if schedule["enabled"]])

    def advance_schedule(self, workspace_id, revision, next_due_at):
        self.advances.append((workspace_id, revision, next_due_at))
        for schedule in self.schedules:
            if schedule["workspace_id"] == workspace_id and schedule["revision"] == revision and schedule["enabled"]:
                schedule["next_due_at"] = next_due_at


class Coordinator:
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.slots = {}
        self.blocked = set()
        self.invalid = set()
        self.reconcile_error = None

    def reconcile(self):
        self.calls.append(("reconcile",))
        if self.reconcile_error:
            raise self.reconcile_error

    def dispatch_due(self, workspace_id, schedule, slot):
        self.calls.append(("dispatch", workspace_id, schedule["revision"], slot))
        if workspace_id in self.blocked:
            raise ConflictError("An exact owned conversation requires attention")
        if workspace_id in self.invalid:
            raise ValueError("Frozen grant scope changed")
        key = (schedule["revision"], slot)
        self.slots.setdefault(key, {"id": f"round-{workspace_id}-{slot}", "state": "queued"})
        return self.slots[key]

    def pause_schedule(self, workspace_id, reason):
        self.calls.append(("pause", workspace_id, reason))
        for schedule in self.service.schedules:
            if schedule["workspace_id"] == workspace_id:
                schedule["enabled"] = False
                schedule["pause_reason"] = reason


def schedule(workspace="city", revision="grant", **changes):
    return {"workspace_id": workspace, "revision": revision, "enabled": True, "anchor": ANCHOR.isoformat(),
            "next_due_at": (ANCHOR + timedelta(hours=72)).isoformat(), "interval_hours": 72,
            "execution_fingerprint": "frozen", "grant": {"kind": "recommendations"}, **changes}


def harness(now, schedules=None):
    service = Service(schedules if schedules is not None else [schedule()], now)
    coordinator = Coordinator(service)
    return ClientResearchScheduler(service, coordinator), service, coordinator


def test_constructor_and_module_import_do_not_reconcile_or_start():
    scheduler, service, coordinator = harness(ANCHOR)
    assert scheduler._thread is None
    assert coordinator.calls == []
    assert service.advances == []


def test_first_due_is_72_utc_hours_across_daylight_saving_change():
    before, service, coordinator = harness(ANCHOR + timedelta(hours=71, minutes=59))
    assert before.tick()["schedules"][0]["status"] == "not_due"
    assert not coordinator.slots
    service.now = ANCHOR + timedelta(hours=72)
    report = before.tick()
    assert report["schedules"][0]["slot"] == 1
    assert len(coordinator.slots) == 1
    assert service.schedules[0]["next_due_at"] == (ANCHOR + timedelta(hours=144)).isoformat()


def test_long_outage_reserves_latest_slot_once_with_no_catchup_burst():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72 * 10 + 1))
    report = scheduler.tick()
    assert report["schedules"][0]["slot"] == 10
    assert list(coordinator.slots) == [("grant", 10)]
    assert service.schedules[0]["next_due_at"] == (ANCHOR + timedelta(hours=72 * 11)).isoformat()
    assert scheduler.tick()["schedules"][0]["status"] == "not_due"
    assert len(coordinator.slots) == 1


def test_restart_after_reserve_before_advance_recovers_same_slot_identity():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    original_advance = service.advance_schedule
    failures = [True]
    def interrupted_advance(*args):
        if failures.pop() if failures else False:
            raise RuntimeError("Crash after reservation")
        original_advance(*args)
    service.advance_schedule = interrupted_advance
    assert scheduler.tick()["schedules"][0]["status"] == "error"
    restarted = ClientResearchScheduler(service, coordinator)
    assert restarted.tick()["schedules"][0]["status"] == "reserved"
    assert len(coordinator.slots) == 1


def test_blocked_owner_retains_due_target_and_later_coalesces_latest_slot():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    coordinator.blocked.add("city")
    assert scheduler.tick()["schedules"][0]["status"] == "waiting"
    assert service.advances == []
    service.now += timedelta(hours=72 * 4)
    coordinator.blocked.clear()
    assert scheduler.tick()["schedules"][0]["slot"] == 5
    assert list(coordinator.slots) == [("grant", 5)]


def test_unknown_reconciliation_state_prevents_every_new_admission():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    coordinator.reconcile_error = RuntimeError("Ownership cannot be proved")
    report = scheduler.tick()
    assert report["reconciled"] is False
    assert report["errors"][0]["operation"] == "reconcile"
    assert not coordinator.slots
    assert not service.advances


def test_changed_scope_pauses_grant_without_reserving_or_advancing():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    coordinator.invalid.add("city")
    assert scheduler.tick()["schedules"][0]["status"] == "paused"
    assert service.schedules[0]["enabled"] is False
    assert not coordinator.slots
    assert not service.advances


def test_attention_result_is_retained_and_not_replaced_by_another_slot():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    coordinator.slots[("grant", 1)] = {"id": "held", "state": "waiting_attention"}
    report = scheduler.tick()
    assert report["schedules"][0]["status"] == "waiting"
    assert report["schedules"][0]["round_id"] == "held"
    assert not service.advances


def test_due_cities_share_coordinator_and_a_busy_one_does_not_burst():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72),
        [schedule("one", "grant1"), schedule("two", "grant2")])
    coordinator.blocked.add("two")
    report = scheduler.tick()
    assert [entry["status"] for entry in report["schedules"]] == ["reserved", "waiting"]
    assert coordinator.calls[0] == ("reconcile",)
    assert list(coordinator.slots) == [("grant1", 1)]


def test_concurrent_tick_does_not_duplicate_outbox_reservation():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72))
    scheduler._tick_lock.acquire()
    try:
        assert scheduler.tick()["busy"] is True
        assert coordinator.calls == []
    finally:
        scheduler._tick_lock.release()


@pytest.mark.parametrize("interval", [1, 29, True, "30"])
def test_polling_never_faster_than_30_seconds(interval):
    _, service, coordinator = harness(ANCHOR)
    with pytest.raises(ValueError, match="30"):
        ClientResearchScheduler(service, coordinator, interval_seconds=interval)


def test_invalid_persisted_interval_pauses_instead_of_expanding_permission():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72), [schedule(interval_hours=24)])
    assert scheduler.tick()["schedules"][0]["status"] == "paused"
    assert not coordinator.slots


def test_pause_before_tick_blocks_future_slots_and_constructor_close_is_inert():
    scheduler, service, coordinator = harness(ANCHOR + timedelta(hours=72), [schedule(enabled=False)])
    scheduler.close()
    assert scheduler.tick()["schedules"] == []
    assert not coordinator.slots


def test_manual_check_cannot_move_anchor_or_anchored_next_due():
    latest, next_due = latest_due_slot(ANCHOR, ANCHOR + timedelta(hours=73))
    assert latest == 1
    assert next_due == ANCHOR + timedelta(hours=144)
