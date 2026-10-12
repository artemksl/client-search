"""Barrier-synchronised wave scheduler for the one-page AI audit lane (ADR-002 §5).

This layer owns the long inter-wave delays. The shared browser engine must never
sleep a cooldown; the scheduler does, via an INJECTED sleeper so tests never wait.

Wave semantics (exact):

* When the persisted deadline permits, dequeue exactly one job in deterministic
  FIFO order and open one NEW conversation. Serial sends are a hard ban-risk
  control, not an operator-tunable throughput setting.
* The wave ends only when its dispatched item is terminal.
* ``wave_completed_at`` is the LAST terminal time. One random integer delay is
  drawn in the pinned range, persisted together with ``next_wave_not_before``
  BEFORE any waiting, and never redrawn (including across restarts).
* Retries are new jobs enqueued for later waves and open new conversations; the
  scheduler never resends an item within or after its wave.

Clock, sleeper and random-integer source are all injected.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from clinic_geo.page_audit.ledger import Ledger, WaveItemRow


DEFAULT_DELAY_MIN_SECONDS = 600
DEFAULT_DELAY_MAX_SECONDS = 900


class SchedulerSettingsError(ValueError):
    """A wave setting is out of its permitted range."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_setting_values(
    delay_min_seconds: object,
    delay_max_seconds: object,
) -> None:
    """Enforce the non-configurable send-pacing safety floor."""

    if (
        not _is_int(delay_min_seconds)
        or delay_min_seconds < DEFAULT_DELAY_MIN_SECONDS
    ):
        raise SchedulerSettingsError(
            "delay_min_seconds must be an integer >= "
            f"{DEFAULT_DELAY_MIN_SECONDS} to preserve send pacing, "
            f"got {delay_min_seconds!r}"
        )
    if not _is_int(delay_max_seconds) or delay_max_seconds < 0:
        raise SchedulerSettingsError(
            "delay_max_seconds must be a non-negative integer, got "
            f"{delay_max_seconds!r}"
        )
    if delay_min_seconds > delay_max_seconds:
        raise SchedulerSettingsError(
            f"delay_min_seconds ({delay_min_seconds}) must not exceed "
            f"delay_max_seconds ({delay_max_seconds})"
        )


@dataclass(frozen=True)
class WaveSettings:
    """Validated, pinned-at-wave-start pacing settings."""

    delay_min_seconds: int = DEFAULT_DELAY_MIN_SECONDS
    delay_max_seconds: int = DEFAULT_DELAY_MAX_SECONDS

    def __post_init__(self) -> None:
        # Intrinsic validation prevents callers from bypassing ``validated``
        # with direct dataclass construction before scheduler injection.
        _validate_setting_values(
            self.delay_min_seconds,
            self.delay_max_seconds,
        )

    @classmethod
    def validated(
        cls,
        *,
        delay_min_seconds: int = DEFAULT_DELAY_MIN_SECONDS,
        delay_max_seconds: int = DEFAULT_DELAY_MAX_SECONDS,
    ) -> "WaveSettings":
        settings = cls(
            delay_min_seconds=delay_min_seconds,
            delay_max_seconds=delay_max_seconds,
        )
        return settings


@dataclass(frozen=True)
class DispatchOutcome:
    """The terminal result of dispatching one wave item.

    ``terminal_time`` is when the chat reached its terminal state; the wave
    cooldown is measured from the LAST such time across the wave. When omitted,
    the scheduler stamps the current clock at recording.
    """

    terminal_state: str
    conversation_ref: Optional[str] = None
    transcript_ref: Optional[str] = None
    terminal_time: Optional[float] = None


@dataclass(frozen=True)
class WaveHandle:
    wave_id: int
    items: tuple[WaveItemRow, ...]


class WaveScheduler:
    """Drives the durable ledger through barrier waves with injected time."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        startup_record_id: int,
        settings: Optional[WaveSettings] = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Callable[[int, int], int] = random.randint,
    ) -> None:
        self.ledger = ledger
        self.startup_record_id = startup_record_id
        self._settings = settings or WaveSettings.validated()
        self.clock = clock
        self.sleeper = sleeper
        self.rng = rng

    # -- settings ----------------------------------------------------------
    @property
    def settings(self) -> WaveSettings:
        return self._settings

    # -- queue -------------------------------------------------------------
    def enqueue(
        self,
        audit_run_id: str,
        page_id: str,
        packet_hash: str,
        check_set_id: str,
    ) -> int:
        return self.ledger.enqueue_job(
            audit_run_id, page_id, packet_hash, check_set_id
        )

    def pending_count(self) -> int:
        return self.ledger.count_queued()

    # -- deadline ----------------------------------------------------------
    def seconds_until_ready(self, now: Optional[float] = None) -> float:
        """Seconds to wait before the next wave may start (0 if ready now).

        The first wave may begin immediately when no cooldown has been persisted,
        or when the persisted deadline has already passed.
        """

        deadline = self.ledger.latest_cooldown_deadline()
        if deadline is None:
            return 0.0
        current = self.clock() if now is None else now
        return max(0.0, deadline - current)

    # -- wave lifecycle ----------------------------------------------------
    def start_wave(self) -> Optional[WaveHandle]:
        """Pin settings, open a wave, dequeue jobs (FIFO).

        Returns ``None`` when the queue is empty. Each dequeued job is dispatched
        into a NEW conversation slot; a completed chat never frees a slot, so the
        batch size for this wave is fixed here and never refilled.
        """

        pinned = self._settings
        jobs = self.ledger.queued_jobs(limit=1)
        if not jobs:
            return None
        wave_id = self.ledger.create_wave(
            parallel_chats=1,
            delay_min_seconds=pinned.delay_min_seconds,
            delay_max_seconds=pinned.delay_max_seconds,
            startup_record_id=self.startup_record_id,
        )
        for job in jobs:
            self.ledger.dispatch_job_into_wave(
                wave_id=wave_id, job=job, dispatch_time=self.clock()
            )
        # Items are returned in id order == dispatch order == FIFO enqueue order.
        items = tuple(self.ledger.wave_items(wave_id))
        return WaveHandle(wave_id=wave_id, items=items)

    def record_terminal(self, item_id: int, outcome: DispatchOutcome) -> None:
        self.ledger.set_item_terminal(
            item_id,
            outcome.terminal_state,
            terminal_time=(
                outcome.terminal_time
                if outcome.terminal_time is not None
                else self.clock()
            ),
            conversation_ref=outcome.conversation_ref,
            transcript_ref=outcome.transcript_ref,
        )

    def finalize_wave(self, wave_id: int) -> None:
        """Close a fully-terminal wave: draw+persist the cooldown once, atomically.

        Raises if any item is still non-terminal (the barrier is not satisfied).
        """

        non_terminal = self.ledger.non_terminal_items(wave_id)
        if non_terminal:
            raise RuntimeError(
                f"wave {wave_id} has {len(non_terminal)} non-terminal item(s); "
                "the barrier is not satisfied"
            )
        wave = self.ledger.get_wave(wave_id)
        if wave.chosen_delay_seconds is not None:
            # Delay already drawn/persisted (e.g. by restart reconcile) — never redraw.
            return
        items = self.ledger.wave_items(wave_id)
        terminal_times = [
            it.terminal_time for it in items if it.terminal_time is not None
        ]
        completed_at = max(terminal_times) if terminal_times else self.clock()
        delay = int(self.rng(wave.delay_min_seconds, wave.delay_max_seconds))
        self.ledger.complete_wave(
            wave_id,
            wave_completed_at=completed_at,
            chosen_delay_seconds=delay,
            next_wave_not_before=completed_at + delay,
        )

    # -- restart -----------------------------------------------------------
    def reconcile_on_restart(self) -> None:
        """Resume a killed run without resending.

        An item whose dispatch cannot be proven terminal becomes
        ``unknown_in_flight`` and is never resent. The interrupted wave is then
        closed so its cooldown is honoured. A wave already in cooldown (its delay
        persisted) is left untouched — the deadline stands and the delay is never
        redrawn.
        """

        wave = self.ledger.open_wave()
        if wave is None:
            return
        for item in self.ledger.non_terminal_items(wave.id):
            self.ledger.set_item_terminal(
                item.id,
                "unknown_in_flight",
                terminal_time=self.clock(),
            )
        self.finalize_wave(wave.id)

    # -- full run ----------------------------------------------------------
    def run_until_drained(
        self,
        dispatch_all: Callable[[Sequence[WaveItemRow]], Sequence[DispatchOutcome]],
    ) -> int:
        """Reconcile, then run barrier waves until the queue is empty.

        ``dispatch_all`` receives one wave's items, opens a new conversation per
        item, and returns their terminal outcomes (one per item, same order). It
        is the transport boundary; the scheduler owns only pacing and the barrier.
        Returns the number of waves run in this call.
        """

        self.reconcile_on_restart()
        waves_run = 0
        while self.pending_count() > 0:
            wait = self.seconds_until_ready()
            if wait > 0:
                self.sleeper(wait)
            handle = self.start_wave()
            if handle is None:
                break
            outcomes = list(dispatch_all(handle.items))
            if len(outcomes) != len(handle.items):
                raise RuntimeError(
                    f"dispatch returned {len(outcomes)} outcome(s) for "
                    f"{len(handle.items)} item(s)"
                )
            for item, outcome in zip(handle.items, outcomes):
                self.record_terminal(item.id, outcome)
            self.finalize_wave(handle.wave_id)
            waves_run += 1
        return waves_run


__all__ = [
    "DEFAULT_DELAY_MIN_SECONDS",
    "DEFAULT_DELAY_MAX_SECONDS",
    "SchedulerSettingsError",
    "WaveSettings",
    "DispatchOutcome",
    "WaveHandle",
    "WaveScheduler",
]
