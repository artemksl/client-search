"""Durable, transactional ledger for the one-page AI audit wave scheduler.

Owns the explicitly supplied SQLite file for one audit queue. Audit workers
resolve that file beneath their client/engagement/run results directory. It
persists:

* ``startup_records`` — the resolved profile, separately-pinned model, and
  assumed context window recorded at startup (ADR-002 §5);
* ``jobs`` — the durable FIFO queue for that audit;
* ``waves`` — one row per barrier wave with its pinned settings snapshot, the
  once-drawn cooldown delay and the computed ``next_wave_not_before`` deadline;
* ``wave_items`` — each dispatched job with its terminal state, timestamps and
  conversation/transcript references (Stage 4 consumes this table).

Durability invariants enforced here (not in the scheduler):

* the chosen delay is drawn once per wave and, once persisted, is NEVER
  overwritten — including across process restarts;
* an item whose prior dispatch cannot be proven terminal is reconciled to
  ``unknown_in_flight`` and is never automatically resent.

This module never sleeps and never touches a browser.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from clinic_geo.page_audit.profile import ResolvedProfile, StartupRecord


_LEDGER_STORAGE_RELATIVE_PATH = Path("data/paid-audit/page-audit/results")

# The full closed set of terminal states an item may reach.
# ``sent_awaiting_response`` is terminal FOR THE WAVE (the send completed and
# the barrier/cooldown machinery treats it like any other closed item) but the
# page still owes an answer: the harvest pass later resolves it to
# ``succeeded``/``failed`` via ``record_harvest_outcome``.
TERMINAL_STATES = frozenset(
    {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "submission_integrity_unverified",
        "unknown_in_flight",
        "sent_awaiting_response",
    }
)
AWAITING_HARVEST_STATE = "sent_awaiting_response"
HARVEST_FINAL_STATES = frozenset({"succeeded", "failed"})
FAILED_STATE = "failed"

# Founder-triggered retries of a page that ended with no usable answer.
RETRY_REOPEN = "reopen"
RETRY_RESEND = "resend"
RETRY_ACTIONS = frozenset({RETRY_REOPEN, RETRY_RESEND})
MAX_PAGE_RETRY_ACTIONS = 2

WAVE_ACTIVE = "active"
WAVE_COMPLETED = "completed"

JOB_QUEUED = "queued"
JOB_DISPATCHED = "dispatched"


class LedgerError(RuntimeError):
    """A durable-state invariant was violated."""


@dataclass(frozen=True)
class JobRow:
    id: int
    audit_run_id: str
    page_id: str
    packet_hash: str
    check_set_id: str
    status: str
    enqueued_at: float


@dataclass(frozen=True)
class WaveRow:
    id: int
    state: str
    parallel_chats: int
    delay_min_seconds: int
    delay_max_seconds: int
    chosen_delay_seconds: Optional[int]
    wave_completed_at: Optional[float]
    next_wave_not_before: Optional[float]
    startup_record_id: int
    created_at: float


@dataclass(frozen=True)
class WaveItemRow:
    id: int
    wave_id: int
    job_id: int
    audit_run_id: str
    page_id: str
    packet_hash: str
    check_set_id: str
    dispatch_time: float
    terminal_state: Optional[str]
    terminal_time: Optional[float]
    conversation_ref: Optional[str]
    transcript_ref: Optional[str]
    created_at: float
    updated_at: float

    @property
    def is_terminal(self) -> bool:
        return self.terminal_state is not None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS startup_records (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id           TEXT    NOT NULL,
    profile_display_name TEXT    NOT NULL,
    chrome_directory     TEXT    NOT NULL,
    project_url          TEXT,
    model_id             TEXT    NOT NULL,
    model_version        TEXT    NOT NULL,
    context_window       INTEGER NOT NULL,
    created_at           REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- FIFO order
    audit_run_id TEXT    NOT NULL,
    page_id      TEXT    NOT NULL,
    packet_hash  TEXT    NOT NULL,
    check_set_id TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'queued',
    enqueued_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS waves (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    state                TEXT    NOT NULL,
    parallel_chats       INTEGER NOT NULL,
    delay_min_seconds    INTEGER NOT NULL,
    delay_max_seconds    INTEGER NOT NULL,
    chosen_delay_seconds INTEGER,
    wave_completed_at    REAL,
    next_wave_not_before REAL,
    startup_record_id    INTEGER NOT NULL REFERENCES startup_records(id),
    created_at           REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS wave_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id          INTEGER NOT NULL REFERENCES waves(id),
    job_id           INTEGER NOT NULL REFERENCES jobs(id),
    audit_run_id     TEXT    NOT NULL,
    page_id          TEXT    NOT NULL,
    packet_hash      TEXT    NOT NULL,
    check_set_id     TEXT    NOT NULL,
    dispatch_time    REAL    NOT NULL,
    terminal_state   TEXT,
    terminal_time    REAL,
    conversation_ref TEXT,
    transcript_ref   TEXT,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS finalization_receipts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id   TEXT    NOT NULL,
    results_sha256 TEXT    NOT NULL,
    created_at     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS page_retry_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id TEXT    NOT NULL,
    page_id      TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    wave_item_id INTEGER,
    job_id       INTEGER,
    created_at   REAL    NOT NULL
);
"""


def default_ledger_path(
    repo_root: Path | str | None,
    client: str,
    engagement: str,
    run: str,
) -> Path:
    """Return the contained scheduler path for one full audit identity."""

    from clinic_geo.paths import repo_root as _repo_root

    root = Path(repo_root).expanduser().resolve() if repo_root else _repo_root()
    storage_root = (root / _LEDGER_STORAGE_RELATIVE_PATH).resolve()
    path = (
        storage_root
        / str(client)
        / str(engagement)
        / str(run)
        / "scheduler.sqlite3"
    ).resolve()
    if storage_root not in path.parents:
        raise LedgerError("Page-audit scheduler path escapes its storage directory")
    return path


class Ledger:
    """Transactional durable state for the wave scheduler."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        repo_root: Path | str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        if db_path is None:
            raise LedgerError(
                "Ledger requires an explicit per-audit or dedicated database path"
            )
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- startup record ----------------------------------------------------
    def record_startup(self, record: StartupRecord) -> int:
        now = self.clock()
        p = record.profile
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO startup_records (
                    profile_id, profile_display_name, chrome_directory,
                    project_url, model_id, model_version, context_window,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.id,
                    p.display_name,
                    p.chrome_directory,
                    p.project_url,
                    record.model_id,
                    record.model_version,
                    int(record.context_window),
                    now,
                ),
            )
        return int(cur.lastrowid)

    def get_startup_record(self, startup_record_id: int) -> StartupRecord:
        row = self._conn.execute(
            "SELECT * FROM startup_records WHERE id = ?", (startup_record_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"no startup record {startup_record_id}")
        return StartupRecord(
            profile=ResolvedProfile(
                id=row["profile_id"],
                display_name=row["profile_display_name"],
                chrome_directory=row["chrome_directory"],
                project_url=row["project_url"],
            ),
            model_id=row["model_id"],
            model_version=row["model_version"],
            context_window=int(row["context_window"]),
        )

    # -- queue -------------------------------------------------------------
    def enqueue_job(
        self,
        audit_run_id: str,
        page_id: str,
        packet_hash: str,
        check_set_id: str,
    ) -> int:
        """Append one job to the durable FIFO queue. Returns its job id."""

        now = self.clock()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO jobs (
                    audit_run_id, page_id, packet_hash, check_set_id,
                    status, enqueued_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (audit_run_id, page_id, packet_hash, check_set_id, JOB_QUEUED, now),
            )
        return int(cur.lastrowid)

    def count_queued(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ?", (JOB_QUEUED,)
        ).fetchone()
        return int(row["n"])

    def queued_jobs(self, limit: Optional[int] = None) -> list[JobRow]:
        sql = "SELECT * FROM jobs WHERE status = ? ORDER BY id ASC"
        params: tuple = (JOB_QUEUED,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (JOB_QUEUED, int(limit))
        return [self._job(row) for row in self._conn.execute(sql, params)]

    # -- waves -------------------------------------------------------------
    def create_wave(
        self,
        *,
        parallel_chats: int,
        delay_min_seconds: int,
        delay_max_seconds: int,
        startup_record_id: int,
    ) -> int:
        """Open a new active wave, pinning its settings snapshot."""

        now = self.clock()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO waves (
                    state, parallel_chats, delay_min_seconds, delay_max_seconds,
                    chosen_delay_seconds, wave_completed_at, next_wave_not_before,
                    startup_record_id, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    WAVE_ACTIVE,
                    int(parallel_chats),
                    int(delay_min_seconds),
                    int(delay_max_seconds),
                    int(startup_record_id),
                    now,
                ),
            )
        return int(cur.lastrowid)

    def dispatch_job_into_wave(
        self,
        *,
        wave_id: int,
        job: JobRow,
        dispatch_time: Optional[float] = None,
    ) -> int:
        """Move a queued job into a wave as a dispatched item (atomic)."""

        now = dispatch_time if dispatch_time is not None else self.clock()
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"cannot dispatch unknown job {job.id}")
            if row["status"] != JOB_QUEUED:
                raise LedgerError(
                    f"job {job.id} is {row['status']!r}, not queued; refusing re-dispatch"
                )
            self._conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (JOB_DISPATCHED, job.id)
            )
            cur = self._conn.execute(
                """
                INSERT INTO wave_items (
                    wave_id, job_id, audit_run_id, page_id, packet_hash,
                    check_set_id, dispatch_time, terminal_state, terminal_time,
                    conversation_ref, transcript_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    wave_id,
                    job.id,
                    job.audit_run_id,
                    job.page_id,
                    job.packet_hash,
                    job.check_set_id,
                    now,
                    now,
                    now,
                ),
            )
        return int(cur.lastrowid)

    def set_item_terminal(
        self,
        item_id: int,
        terminal_state: str,
        *,
        terminal_time: Optional[float] = None,
        conversation_ref: Optional[str] = None,
        transcript_ref: Optional[str] = None,
    ) -> None:
        if terminal_state not in TERMINAL_STATES:
            raise LedgerError(f"unknown terminal state {terminal_state!r}")
        when = terminal_time if terminal_time is not None else self.clock()
        with self._conn:
            row = self._conn.execute(
                "SELECT terminal_state FROM wave_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"no wave item {item_id}")
            if row["terminal_state"] is not None:
                raise LedgerError(
                    f"wave item {item_id} is already terminal "
                    f"({row['terminal_state']!r}); refusing to overwrite"
                )
            self._conn.execute(
                """
                UPDATE wave_items
                   SET terminal_state = ?, terminal_time = ?,
                       conversation_ref = ?, transcript_ref = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    terminal_state,
                    when,
                    conversation_ref,
                    transcript_ref,
                    self.clock(),
                    item_id,
                ),
            )

    def complete_wave(
        self,
        wave_id: int,
        *,
        wave_completed_at: float,
        chosen_delay_seconds: int,
        next_wave_not_before: float,
    ) -> None:
        """Persist the cooldown atomically. Never redraws a persisted delay.

        The delay + deadline are written in one transaction, BEFORE any waiting.
        If the wave already has a persisted delay (e.g. a prior restart already
        closed it), the existing values are kept unchanged.
        """

        with self._conn:
            row = self._conn.execute(
                "SELECT state, chosen_delay_seconds FROM waves WHERE id = ?",
                (wave_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(f"no wave {wave_id}")
            if row["chosen_delay_seconds"] is not None:
                # Already drawn and persisted — invariant: never redraw.
                return
            self._conn.execute(
                """
                UPDATE waves
                   SET state = ?, wave_completed_at = ?, chosen_delay_seconds = ?,
                       next_wave_not_before = ?
                 WHERE id = ?
                """,
                (
                    WAVE_COMPLETED,
                    float(wave_completed_at),
                    int(chosen_delay_seconds),
                    float(next_wave_not_before),
                    wave_id,
                ),
            )

    # -- reads -------------------------------------------------------------
    def get_wave(self, wave_id: int) -> WaveRow:
        row = self._conn.execute(
            "SELECT * FROM waves WHERE id = ?", (wave_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"no wave {wave_id}")
        return self._wave(row)

    def open_wave(self) -> Optional[WaveRow]:
        """Return the single active (not-yet-completed) wave, if any."""

        row = self._conn.execute(
            "SELECT * FROM waves WHERE state = ? ORDER BY id ASC LIMIT 1",
            (WAVE_ACTIVE,),
        ).fetchone()
        return self._wave(row) if row is not None else None

    def wave_items(self, wave_id: int) -> list[WaveItemRow]:
        return [
            self._item(row)
            for row in self._conn.execute(
                "SELECT * FROM wave_items WHERE wave_id = ? ORDER BY id ASC",
                (wave_id,),
            )
        ]

    def non_terminal_items(self, wave_id: int) -> list[WaveItemRow]:
        return [
            self._item(row)
            for row in self._conn.execute(
                """
                SELECT * FROM wave_items
                 WHERE wave_id = ? AND terminal_state IS NULL
                 ORDER BY id ASC
                """,
                (wave_id,),
            )
        ]

    def awaiting_harvest_items(self, audit_run_id: str) -> list[WaveItemRow]:
        """Every item sent in send-only mode whose answer is still owed."""

        return [
            self._item(row)
            for row in self._conn.execute(
                """
                SELECT * FROM wave_items
                 WHERE audit_run_id = ? AND terminal_state = ?
                 ORDER BY id ASC
                """,
                (str(audit_run_id), AWAITING_HARVEST_STATE),
            )
        ]

    def record_harvest_outcome(
        self,
        item_id: int,
        final_state: str,
        *,
        transcript_ref: Optional[str] = None,
        harvest_time: Optional[float] = None,
    ) -> None:
        """Resolve one awaiting item to its harvested outcome.

        Refuses to touch any row that is not currently awaiting harvest, so a
        finished item can never be silently rewritten.
        """

        if final_state not in HARVEST_FINAL_STATES:
            raise LedgerError(
                f"harvest outcome must be one of {sorted(HARVEST_FINAL_STATES)}, "
                f"got {final_state!r}"
            )
        when = harvest_time if harvest_time is not None else self.clock()
        with self._conn:
            row = self._conn.execute(
                "SELECT terminal_state FROM wave_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"no wave item {item_id}")
            if row["terminal_state"] != AWAITING_HARVEST_STATE:
                raise LedgerError(
                    f"wave item {item_id} is {row['terminal_state']!r}, not "
                    f"{AWAITING_HARVEST_STATE!r}; refusing to record a harvest"
                )
            self._conn.execute(
                """
                UPDATE wave_items
                   SET terminal_state = ?, terminal_time = ?,
                       transcript_ref = ?, updated_at = ?
                 WHERE id = ?
                """,
                (final_state, when, transcript_ref, self.clock(), item_id),
            )

    # -- founder-triggered page retries ------------------------------------
    def count_page_retry_actions(
        self, audit_run_id: str, page_id: str, action: str
    ) -> int:
        """How many times the founder has already taken ``action`` on a page."""

        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM page_retry_actions
             WHERE audit_run_id = ? AND page_id = ? AND action = ?
            """,
            (str(audit_run_id), str(page_id), str(action)),
        ).fetchone()
        return int(row["n"])

    def reopen_failed_item(
        self,
        item_id: int,
        *,
        conversation_ref: str,
        max_reopens: int = MAX_PAGE_RETRY_ACTIONS,
    ) -> int:
        """Return one failed item to ``sent_awaiting_response`` for collection.

        This is the ONLY sanctioned way a finished row changes state, and it is
        always an explicit founder-triggered transition. It refuses unless the
        row is ``failed``, its stored conversation reference is exactly the one
        the caller validated, and the page has been reopened fewer than
        ``max_reopens`` times. ``record_harvest_outcome`` is untouched: it still
        refuses every row that is not awaiting.

        ``terminal_time`` is set back to the dispatch time because the answer
        has been owed since the send, so the collection pass treats it as due
        rather than waiting a further collection delay.
        """

        with self._conn:
            row = self._conn.execute(
                """
                SELECT audit_run_id, page_id, terminal_state, conversation_ref,
                       dispatch_time
                  FROM wave_items WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(f"no wave item {item_id}")
            if row["terminal_state"] != FAILED_STATE:
                raise LedgerError(
                    f"wave item {item_id} is {row['terminal_state']!r}, not "
                    f"{FAILED_STATE!r}; refusing to reopen it"
                )
            if (
                not conversation_ref
                or row["conversation_ref"] != conversation_ref
            ):
                raise LedgerError(
                    f"wave item {item_id} does not store the conversation "
                    "reference that was validated; refusing to reopen it"
                )
            already = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM page_retry_actions
                     WHERE audit_run_id = ? AND page_id = ? AND action = ?
                    """,
                    (row["audit_run_id"], row["page_id"], RETRY_REOPEN),
                ).fetchone()["n"]
            )
            if already >= int(max_reopens):
                raise LedgerError(
                    f"page {row['page_id']!r} has been reopened {already} "
                    f"time(s); the limit is {max_reopens}"
                )
            now = self.clock()
            self._conn.execute(
                """
                UPDATE wave_items
                   SET terminal_state = ?, terminal_time = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    AWAITING_HARVEST_STATE,
                    float(row["dispatch_time"]),
                    now,
                    item_id,
                ),
            )
            cur = self._conn.execute(
                """
                INSERT INTO page_retry_actions (
                    audit_run_id, page_id, action, wave_item_id, job_id,
                    created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (row["audit_run_id"], row["page_id"], RETRY_REOPEN, item_id, now),
            )
        return int(cur.lastrowid)

    def enqueue_page_resend(
        self,
        audit_run_id: str,
        page_id: str,
        *,
        packet_hash: str,
        check_set_id: str,
        max_resends: int = MAX_PAGE_RETRY_ACTIONS,
    ) -> int:
        """Queue exactly ONE new job for a page and record that it was queued.

        The job carries the page's existing reuse key, so ``plan_run``'s own
        dedupe sees it on the next relaunch and does not enqueue a second one.
        Both writes happen in one transaction, so a recorded re-send always has
        its job and a job always has its record. Refuses while the page already
        has a queued job, and once the page has been sent again
        ``max_resends`` times.
        """

        with self._conn:
            already = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM page_retry_actions
                     WHERE audit_run_id = ? AND page_id = ? AND action = ?
                    """,
                    (str(audit_run_id), str(page_id), RETRY_RESEND),
                ).fetchone()["n"]
            )
            if already >= int(max_resends):
                raise LedgerError(
                    f"page {page_id!r} has been sent again {already} time(s); "
                    f"the limit is {max_resends}"
                )
            queued = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM jobs
                     WHERE audit_run_id = ? AND page_id = ? AND status = ?
                    """,
                    (str(audit_run_id), str(page_id), JOB_QUEUED),
                ).fetchone()["n"]
            )
            if queued:
                raise LedgerError(
                    f"page {page_id!r} already has a queued job; refusing to "
                    "queue a second one"
                )
            now = self.clock()
            cur = self._conn.execute(
                """
                INSERT INTO jobs (
                    audit_run_id, page_id, packet_hash, check_set_id,
                    status, enqueued_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(audit_run_id),
                    str(page_id),
                    str(packet_hash),
                    str(check_set_id),
                    JOB_QUEUED,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
            self._conn.execute(
                """
                INSERT INTO page_retry_actions (
                    audit_run_id, page_id, action, wave_item_id, job_id,
                    created_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (str(audit_run_id), str(page_id), RETRY_RESEND, job_id, now),
            )
        return job_id

    def latest_cooldown_deadline(self) -> Optional[float]:
        """The furthest persisted ``next_wave_not_before`` across completed waves."""

        row = self._conn.execute(
            "SELECT MAX(next_wave_not_before) AS deadline FROM waves "
            "WHERE next_wave_not_before IS NOT NULL"
        ).fetchone()
        return None if row["deadline"] is None else float(row["deadline"])

    # -- finalization receipt ----------------------------------------------
    def record_finalization_receipt(
        self, audit_run_id: str, results_sha256: str
    ) -> int:
        """Record which results bytes the finalizer persisted, and when.

        Append-only: each finalize pass adds a row, so an earlier receipt is
        never rewritten and the newest row names the current file.
        """

        digest = str(results_sha256)
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise LedgerError(
                "finalization receipt needs a lowercase SHA-256 hex digest, "
                f"got {results_sha256!r}"
            )
        now = self.clock()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO finalization_receipts (
                    audit_run_id, results_sha256, created_at
                ) VALUES (?, ?, ?)
                """,
                (str(audit_run_id), digest, now),
            )
        return int(cur.lastrowid)

    def latest_finalization_receipt(self, audit_run_id: str) -> Optional[dict]:
        """The most recent receipt for one run, or ``None`` if never finalized."""

        row = self._conn.execute(
            """
            SELECT results_sha256, created_at FROM finalization_receipts
             WHERE audit_run_id = ? ORDER BY id DESC LIMIT 1
            """,
            (str(audit_run_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "results_sha256": row["results_sha256"],
            "created_at": float(row["created_at"]),
        }

    # -- row mappers -------------------------------------------------------
    @staticmethod
    def _job(row: sqlite3.Row) -> JobRow:
        return JobRow(
            id=int(row["id"]),
            audit_run_id=row["audit_run_id"],
            page_id=row["page_id"],
            packet_hash=row["packet_hash"],
            check_set_id=row["check_set_id"],
            status=row["status"],
            enqueued_at=float(row["enqueued_at"]),
        )

    @staticmethod
    def _wave(row: sqlite3.Row) -> WaveRow:
        return WaveRow(
            id=int(row["id"]),
            state=row["state"],
            parallel_chats=int(row["parallel_chats"]),
            delay_min_seconds=int(row["delay_min_seconds"]),
            delay_max_seconds=int(row["delay_max_seconds"]),
            chosen_delay_seconds=(
                None
                if row["chosen_delay_seconds"] is None
                else int(row["chosen_delay_seconds"])
            ),
            wave_completed_at=(
                None
                if row["wave_completed_at"] is None
                else float(row["wave_completed_at"])
            ),
            next_wave_not_before=(
                None
                if row["next_wave_not_before"] is None
                else float(row["next_wave_not_before"])
            ),
            startup_record_id=int(row["startup_record_id"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> WaveItemRow:
        return WaveItemRow(
            id=int(row["id"]),
            wave_id=int(row["wave_id"]),
            job_id=int(row["job_id"]),
            audit_run_id=row["audit_run_id"],
            page_id=row["page_id"],
            packet_hash=row["packet_hash"],
            check_set_id=row["check_set_id"],
            dispatch_time=float(row["dispatch_time"]),
            terminal_state=row["terminal_state"],
            terminal_time=(
                None if row["terminal_time"] is None else float(row["terminal_time"])
            ),
            conversation_ref=row["conversation_ref"],
            transcript_ref=row["transcript_ref"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


__all__ = [
    "TERMINAL_STATES",
    "AWAITING_HARVEST_STATE",
    "HARVEST_FINAL_STATES",
    "WAVE_ACTIVE",
    "WAVE_COMPLETED",
    "JOB_QUEUED",
    "JOB_DISPATCHED",
    "LedgerError",
    "JobRow",
    "WaveRow",
    "WaveItemRow",
    "Ledger",
    "default_ledger_path",
]
