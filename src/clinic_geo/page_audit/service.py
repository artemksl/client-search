"""Stage 4 — thin orchestration used by the web API (ADR-002 §6).

* ``start`` launches the page-audit worker through the EXISTING background-job
  seam (injected ``launcher``; in production this is the same ``jobs.py``
  workflow the paid-audit launch uses). It is IDEMPOTENT: a repeated start while
  a run is active or complete relaunches nothing.
* ``status`` is a READ-ONLY view over the ledger + persisted results. It never
  enqueues work and never calls a model.

Both take injected paths/launcher so the API and tests stay decoupled from the
browser/engine stack.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from clinic_geo.browser.chat_engine import validate_sent_conversation_reference
from clinic_geo.page_audit.ledger import (
    FAILED_STATE,
    MAX_PAGE_RETRY_ACTIONS,
    RETRY_REOPEN,
    RETRY_RESEND,
    Ledger,
    LedgerError,
    default_ledger_path,
)
from clinic_geo.page_audit.results import RESULTS_SCHEMA_VERSION

ENGINE_DATA_RELATIVE = Path("data") / "paid-audit"
RESULTS_RELATIVE = ENGINE_DATA_RELATIVE / "page-audit" / "results"
_RESULTS_DOCUMENT_FIELDS = frozenset(
    {
        "results_schema_version",
        "run",
        "provenance",
        "advisory_notice",
        "pages",
        "c05_aggregate",
        "review_records",
        "excluded_pages",
        "results_sha256",
    }
)
_RESULTS_RUN_FIELDS = frozenset({"client_id", "engagement_id", "run_id"})
_MARKER_INITIALIZATION_GRACE_SECONDS = 30.0


class PageAuditResultsError(ValueError):
    """A persisted page-audit results document cannot be consumed safely."""

    code = "page_audit_results_invalid"


class PageAuditResultsNotReadyError(PageAuditResultsError):
    """A valid-looking results artifact is still awaiting server collection."""

    code = "page_audit_results_not_ready"


def _load_validated_results_document(
    path: Path | str,
    client: str,
    engagement: str,
    run: str,
    *,
    require_existing: bool = False,
) -> dict | None:
    """Load one ResultsStore document through the canonical trust boundary."""

    results_path = Path(path)
    try:
        raw = results_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if require_existing:
            raise PageAuditResultsError(
                "Page-audit results are required for collection resume"
            ) from exc
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PageAuditResultsError("Page-audit results are unreadable") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PageAuditResultsError("Page-audit results are invalid JSON") from exc
    if not isinstance(document, dict):
        raise PageAuditResultsError("Page-audit results must be a JSON object")
    if set(document) != _RESULTS_DOCUMENT_FIELDS:
        raise PageAuditResultsError(
            "Page-audit results do not match the ResultsStore document contract"
        )

    run_identity = document.get("run")
    if not isinstance(run_identity, dict) or set(run_identity) != _RESULTS_RUN_FIELDS:
        raise PageAuditResultsError(
            "Page-audit results have an invalid run identity contract"
        )
    expected_identity = (str(client), str(engagement), str(run))
    actual_identity = (
        str(run_identity.get("client_id") or ""),
        str(run_identity.get("engagement_id") or ""),
        str(run_identity.get("run_id") or ""),
    )
    if actual_identity != expected_identity:
        raise PageAuditResultsError(
            "Page-audit results identity does not match the requested run"
        )
    if not isinstance(document.get("pages"), dict):
        raise PageAuditResultsError("Page-audit results have no pages object")
    if document.get("results_schema_version") != RESULTS_SCHEMA_VERSION:
        raise PageAuditResultsError(
            "Page-audit results schema version is unsupported"
        )

    expected_sha = document.get("results_sha256")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise PageAuditResultsError("Page-audit results have no valid SHA-256")
    hash_body = dict(document)
    hash_body.pop("results_sha256", None)
    canonical = json.dumps(
        hash_body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    actual_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual_sha != expected_sha:
        raise PageAuditResultsError("Page-audit results SHA-256 does not match")
    return dict(document)


def _canonical_conversation_url(
    conversation_ref: object, project_url: object
) -> str | None:
    """The canonical chat URL, or ``None`` when the stored reference is not one.

    Runs the same strict check the transport applies to a fresh send, so a
    transcript file path (synchronous-mode runs) or any other stored string is
    never offered to the founder as a chat link.
    """

    if not conversation_ref:
        return None
    project = str(project_url).strip() if project_url else ""
    try:
        validated = validate_sent_conversation_reference(
            conversation_ref, project_url=project or None
        )
    except ValueError:
        return None
    return validated["conversation_url"]


def _results_path(repo_root: Path, client: str, engagement: str, run: str) -> Path:
    return repo_root / RESULTS_RELATIVE / client / engagement / run / "results.json"


def _marker_path(repo_root: Path, client: str, engagement: str, run: str) -> Path:
    return repo_root / RESULTS_RELATIVE / client / engagement / run / ".worker-launched"


def _read_marker_owner(marker: Path) -> dict | None:
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    pid = document.get("pid")
    started_at = document.get("started_at")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 1
        or isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
    ):
        return None
    try:
        started_at_value = float(started_at)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(started_at_value):
        return None
    return {"pid": pid, "started_at": started_at_value}


def _process_is_alive(pid: int) -> bool:
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except (OSError, OverflowError):
        # The worker may belong to a previous web-server process. In that case
        # it is not waitable here, so fall back to the portable signal probe.
        pass
    else:
        if waited_pid == pid:
            return False
    try:
        os.kill(pid, 0)
    except OverflowError:
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _marker_is_live(marker: Path) -> bool:
    owner = _read_marker_owner(marker)
    if owner is not None:
        return _process_is_alive(owner["pid"])
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    # An O_EXCL creator necessarily has a very small open-to-write window. A
    # competing start must not unlink that fresh, not-yet-parseable claim.
    return age <= _MARKER_INITIALIZATION_GRACE_SECONDS


def _write_marker_fd(fd: int, owner: dict) -> None:
    payload = (
        json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    offset = 0
    while offset < len(payload):
        offset += os.write(fd, payload[offset:])
    os.fsync(fd)


def _replace_marker_owner(marker: Path, expected: dict, replacement: dict) -> bool:
    """Publish a child owner atomically while a valid reservation stays visible."""

    if _read_marker_owner(marker) != expected:
        return False
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.", dir=marker.parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            _write_marker_fd(fd, replacement)
        finally:
            os.close(fd)
        if _read_marker_owner(marker) != expected:
            return False
        os.replace(temporary, marker)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_marker_if_owned(
    marker: Path, *, pid: int, started_at: float | None = None
) -> bool:
    owner = _read_marker_owner(marker)
    if owner is None or owner["pid"] != pid:
        return False
    if started_at is not None and owner["started_at"] != float(started_at):
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def release_worker_marker(
    repo_root: Path | str,
    client: str,
    engagement: str,
    run: str,
    *,
    pid: int | None = None,
) -> bool:
    """Remove only the marker owned by this exiting worker process."""

    return _remove_marker_if_owned(
        _marker_path(Path(repo_root).resolve(), client, engagement, run),
        pid=os.getpid() if pid is None else pid,
    )


class PageAuditService:
    def __init__(self, repo_root: Path | str, *, ledger_path: Path | str | None = None,
                 launcher: Optional[Callable[..., object]] = None,
                 clock: Callable[[], float] = time.time):
        """Build the service around an optional worker launcher.

        Launchers accept ``harvest_only`` as a keyword and return a child
        PID or process object.
        """
        self.repo_root = Path(repo_root).resolve()
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        self.launcher = launcher
        self.clock = clock

    def _ledger_path_for(self, client: str, engagement: str, run: str) -> Path:
        if self.ledger_path is not None:
            return self.ledger_path
        return default_ledger_path(self.repo_root, client, engagement, run)

    def read_results(self, client: str, engagement: str, run: str) -> dict | None:
        """Return one validated structured-results document, or ``None`` if absent.

        Invalid paths and malformed, misidentified, or hash-inconsistent bytes are
        errors rather than another spelling of "the advisory lane did not run".

        A valid partial file is refused with ``PageAuditResultsNotReadyError``
        while the run still owes work: rows awaiting collection, rows queued for
        a later wave, or rows dispatched without a terminal state.
        """

        root = (self.repo_root / RESULTS_RELATIVE).resolve()
        path = _results_path(
            self.repo_root, str(client), str(engagement), str(run)
        ).resolve()
        if root not in path.parents:
            raise PageAuditResultsError(
                "Page-audit results path escapes its storage directory"
            )
        ledger_view = self._ledger_view(
            str(client), str(engagement), str(run)
        )
        if ledger_view["ledger_present"] and not ledger_view["ledger_readable"]:
            raise PageAuditResultsError(
                "Page-audit results readiness cannot be established because "
                "the scheduler ledger is unreadable"
            )
        if ledger_view["awaiting_harvest"] > 0:
            raise PageAuditResultsNotReadyError(
                "Page-audit results are not ready while collection is pending"
            )
        if (
            ledger_view["queued_for_run"] > 0
            or ledger_view["non_terminal_for_run"] > 0
        ):
            raise PageAuditResultsNotReadyError(
                "Page-audit results are not ready while run work is queued "
                "or in flight"
            )
        document = _load_validated_results_document(
            path, str(client), str(engagement), str(run)
        )
        if document is None:
            return None
        if any(
            isinstance(page, dict)
            and page.get("terminal_state") == "sent_awaiting_response"
            for page in document["pages"].values()
        ):
            raise PageAuditResultsNotReadyError(
                "Page-audit results are not ready while collection is pending"
            )
        return dict(document)

    # -- read-only status --------------------------------------------------
    def status(self, client: str, engagement: str, run: str) -> dict:
        results_path = _results_path(self.repo_root, client, engagement, run)
        marker = _marker_path(self.repo_root, client, engagement, run)
        results = None
        results_error = None
        try:
            results = _load_validated_results_document(
                results_path, str(client), str(engagement), str(run)
            )
        except PageAuditResultsError as exc:
            results_error = exc.code
        results_valid = results is not None

        pages = results.get("pages", {}) if results_valid else {}
        page_ids = list(pages)
        awaiting_result_pages = [
            pid
            for pid in page_ids
            if isinstance(pages[pid], dict)
            and pages[pid].get("terminal_state") == "sent_awaiting_response"
        ]
        completed_pages = [
            pid
            for pid in page_ids
            if isinstance(pages[pid], dict)
            and (
                (
                    pages[pid].get("terminal_state")
                    and pages[pid].get("terminal_state")
                    != "sent_awaiting_response"
                )
                or pages[pid].get("routing")
                == "ai_unavailable_page_too_large"
            )
        ]
        validated_counts: dict = {}
        review_records = results.get("review_records") if results_valid else None
        review_count = len(review_records) if isinstance(review_records, list) else 0
        for pid in page_ids:
            page = pages[pid] if isinstance(pages[pid], dict) else {}
            check_results = page.get("results", {})
            if not isinstance(check_results, dict):
                continue
            for res in check_results.values():
                if not isinstance(res, dict):
                    continue
                state = res.get("state")
                validated_counts[state] = validated_counts.get(state, 0) + 1

        ledger_view = self._ledger_view(client, engagement, run)
        failed_pages = self._failed_pages(ledger_view, pages)
        results_awaiting_harvest = len(awaiting_result_pages)
        ledger_awaiting_harvest = ledger_view["awaiting_harvest"]
        awaiting_harvest = max(
            results_awaiting_harvest, ledger_awaiting_harvest
        )
        ledger_trustworthy = (
            not ledger_view["ledger_present"] or ledger_view["ledger_readable"]
        )
        live_worker = _marker_is_live(marker)
        # A run counts as finished only when the finalizer's own receipt names
        # the digest of the file on disk. A run whose results were persisted but
        # never finalized reports not-complete until one finalize pass records
        # its receipt.
        persisted_sha = results.get("results_sha256") if results_valid else None
        finalized = (
            isinstance(persisted_sha, str)
            and ledger_view["finalization_receipt_sha256"] == persisted_sha
        )
        complete = (
            results_valid
            and bool(page_ids)
            and ledger_trustworthy
            and finalized
            and len(completed_pages) == len(page_ids)
            and ledger_view["queued_for_run"] == 0
            and ledger_view["non_terminal_for_run"] == 0
            and awaiting_harvest == 0
        )
        if complete:
            state = "complete"
        elif awaiting_harvest > 0:
            state = "awaiting_harvest"
        elif live_worker or ledger_view["items_for_run"] > 0 or ledger_view["queued_for_run"] > 0:
            state = "running"
        else:
            state = "idle"

        return {
            "state": state,
            "pages_completed": len(completed_pages),
            "pages_total": len(page_ids),
            "pages_remaining": max(0, len(page_ids) - len(completed_pages)),
            "awaiting_harvest": awaiting_harvest,
            "results_awaiting_harvest": results_awaiting_harvest,
            "ledger_awaiting_harvest": ledger_awaiting_harvest,
            "ledger_present": ledger_view["ledger_present"],
            "ledger_readable": ledger_view["ledger_readable"],
            "worker_live": live_worker,
            "in_flight": ledger_view["non_terminal_for_run"],
            "active_wave": ledger_view["active_wave"],
            "next_wave_not_before": ledger_view["next_wave_not_before"],
            "queued": ledger_view["queued_for_run"],
            "validated_counts": validated_counts,
            "review_record_count": review_count,
            "c05_aggregate": results.get("c05_aggregate") if results_valid else None,
            "results_available": (
                results_valid
                and ledger_trustworthy
                and awaiting_harvest == 0
                and ledger_view["queued_for_run"] == 0
                and ledger_view["non_terminal_for_run"] == 0
            ),
            "results_valid": results_valid,
            "results_error": results_error,
            "failed_pages": failed_pages,
            "finalized": finalized,
            "finalization_receipt_sha256": ledger_view[
                "finalization_receipt_sha256"
            ],
            "wave_settings": ledger_view["wave_settings"],
        }

    @staticmethod
    def _failed_pages(ledger_view: dict, pages: dict) -> list:
        """Read-only list of pages whose newest send ended with no usable answer.

        Nothing here changes state. A chat link is offered only when the stored
        reference passes the strict validation; the raw reference is never
        exposed, so a transcript file path yields no link.
        """

        failed = []
        for page_id, item in ledger_view["failed_items"].items():
            page = pages.get(page_id)
            page = page if isinstance(page, dict) else {}
            counts = ledger_view["retry_counts"].get(page_id, {})
            reopen_count = int(counts.get(RETRY_REOPEN, 0))
            resend_count = int(counts.get(RETRY_RESEND, 0))
            conversation_url = _canonical_conversation_url(
                item["conversation_ref"], ledger_view["project_url"]
            )
            routing = page.get("routing")
            failed.append(
                {
                    "page_id": page_id,
                    "primary_url": page.get("primary_url"),
                    "terminal_state": (
                        page.get("terminal_state") or item["terminal_state"]
                    ),
                    "detail": page.get("detail"),
                    "routing": routing,
                    "conversation_url": conversation_url,
                    "reopen_count": reopen_count,
                    "resend_count": resend_count,
                    "max_retries": MAX_PAGE_RETRY_ACTIONS,
                    "can_reopen": (
                        conversation_url is not None
                        and reopen_count < MAX_PAGE_RETRY_ACTIONS
                    ),
                    "can_resend": (
                        resend_count < MAX_PAGE_RETRY_ACTIONS
                        and routing != "ai_unavailable_page_too_large"
                    ),
                }
            )
        failed.sort(key=lambda entry: entry["page_id"])
        return failed

    def _ledger_view(self, client: str, engagement: str, run: str) -> dict:
        view = {
            "ledger_present": False,
            "ledger_readable": False,
            "active_wave": None,
            "next_wave_not_before": None,
            "queued_for_run": 0,
            "items_for_run": 0,
            "awaiting_harvest": 0,
            "non_terminal_for_run": 0,
            "finalization_receipt_sha256": None,
            "project_url": None,
            "failed_items": {},
            "retry_counts": {},
            "wave_settings": None,
        }
        ledger_path = self._ledger_path_for(client, engagement, run)
        if not ledger_path.is_file():
            return view
        view["ledger_present"] = True
        unreadable_view = dict(view)
        conn = None
        try:
            uri = f"{ledger_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute("SELECT id FROM waves WHERE state='active' ORDER BY id ASC LIMIT 1").fetchone()
            view["active_wave"] = int(row["id"]) if row else None
            deadline = conn.execute(
                "SELECT MAX(next_wave_not_before) AS d FROM waves WHERE next_wave_not_before IS NOT NULL"
            ).fetchone()["d"]
            view["next_wave_not_before"] = None if deadline is None else float(deadline)
            view["queued_for_run"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE audit_run_id=? AND status='queued'", (run,)
            ).fetchone()["n"])
            view["items_for_run"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM wave_items WHERE audit_run_id=?", (run,)
            ).fetchone()["n"])
            view["awaiting_harvest"] = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM wave_items "
                    "WHERE audit_run_id=? AND terminal_state='sent_awaiting_response'",
                    (run,),
                ).fetchone()["n"]
            )
            view["non_terminal_for_run"] = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM wave_items "
                    "WHERE audit_run_id=? AND terminal_state IS NULL",
                    (run,),
                ).fetchone()["n"]
            )
            # Effective pacing of the active/last wave, read from the pinned ledger
            # snapshot (never live env). Stays None until the first wave exists.
            settings_row = conn.execute(
                "SELECT parallel_chats, delay_min_seconds, delay_max_seconds "
                "FROM waves ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if settings_row is not None:
                view["wave_settings"] = {
                    "parallel_chats": int(settings_row["parallel_chats"]),
                    "delay_min_seconds": int(settings_row["delay_min_seconds"]),
                    "delay_max_seconds": int(settings_row["delay_max_seconds"]),
                }
            # The finalizer's receipt. A scheduler database written before
            # receipts existed has no such table, which is not an unreadable
            # ledger: it simply has no receipt yet.
            # The Project URL pinned at startup, used to check chat links.
            startup_row = conn.execute(
                "SELECT project_url FROM startup_records ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if startup_row is not None:
                view["project_url"] = startup_row["project_url"]
            # Latest row per page; a page is failed only if its newest row is.
            latest_by_page = {}
            for row in conn.execute(
                "SELECT id, page_id, terminal_state, conversation_ref, "
                "packet_hash, check_set_id FROM wave_items "
                "WHERE audit_run_id=? ORDER BY id ASC",
                (run,),
            ):
                latest_by_page[row["page_id"]] = {
                    "wave_item_id": int(row["id"]),
                    "terminal_state": row["terminal_state"],
                    "conversation_ref": row["conversation_ref"],
                    "packet_hash": row["packet_hash"],
                    "check_set_id": row["check_set_id"],
                }
            view["failed_items"] = {
                page_id: item
                for page_id, item in latest_by_page.items()
                if item["terminal_state"] == FAILED_STATE
            }
            # Founder-triggered retries so far. A database written before these
            # existed has no such table, which means no retry has been made.
            retry_table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='page_retry_actions'"
            ).fetchone()
            if retry_table is not None:
                retry_counts: dict = {}
                for row in conn.execute(
                    "SELECT page_id, action, COUNT(*) AS n FROM page_retry_actions "
                    "WHERE audit_run_id=? GROUP BY page_id, action",
                    (run,),
                ):
                    retry_counts.setdefault(row["page_id"], {})[row["action"]] = int(
                        row["n"]
                    )
                view["retry_counts"] = retry_counts
            receipt_table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='finalization_receipts'"
            ).fetchone()
            if receipt_table is not None:
                receipt_row = conn.execute(
                    "SELECT results_sha256 FROM finalization_receipts "
                    "WHERE audit_run_id=? ORDER BY id DESC LIMIT 1",
                    (run,),
                ).fetchone()
                if receipt_row is not None:
                    view["finalization_receipt_sha256"] = receipt_row[
                        "results_sha256"
                    ]
            view["ledger_readable"] = True
        except sqlite3.Error:
            view = unreadable_view
        finally:
            if conn is not None:
                conn.close()
        return view

    # -- founder-triggered retries of one failed page ----------------------
    def _retry_precondition(
        self, client: str, engagement: str, run: str, page_id: str, action: str
    ) -> tuple:
        """Locate one failed page for a retry, or the reason it cannot be taken.

        Returns ``(ledger_view, item, ledger_path, reason)``. ``reason`` is
        ``None`` only when the retry may proceed.
        """

        ledger_view = self._ledger_view(client, engagement, run)
        ledger_path = self._ledger_path_for(client, engagement, run)
        if not ledger_view["ledger_present"]:
            return ledger_view, None, ledger_path, "page_audit_ledger_absent"
        if not ledger_view["ledger_readable"]:
            return ledger_view, None, ledger_path, "page_audit_ledger_unavailable"
        item = ledger_view["failed_items"].get(str(page_id))
        if item is None:
            return ledger_view, None, ledger_path, "page_is_not_failed"
        counts = ledger_view["retry_counts"].get(str(page_id), {})
        if int(counts.get(action, 0)) >= MAX_PAGE_RETRY_ACTIONS:
            return ledger_view, item, ledger_path, f"{action}_limit_reached"
        return ledger_view, item, ledger_path, None

    def reopen_failed_page(
        self, client: str, engagement: str, run: str, page_id: str
    ) -> dict:
        """Founder-triggered: read one failed page's existing answer again.

        Nothing is sent and no worker is launched here. The reopened row makes
        the run owe an answer again, so ``status`` stops reporting complete,
        ``read_results`` refuses, and the existing start path resumes a
        collection-only worker that reads the conversation already there.
        """

        page_id = str(page_id)
        ledger_view, item, ledger_path, reason = self._retry_precondition(
            client, engagement, run, page_id, RETRY_REOPEN
        )
        if reason is None and _canonical_conversation_url(
            item["conversation_ref"], ledger_view["project_url"]
        ) is None:
            reason = "no_valid_conversation_url"
        if reason is None:
            ledger = Ledger(ledger_path)
            try:
                ledger.reopen_failed_item(
                    item["wave_item_id"],
                    conversation_ref=item["conversation_ref"],
                )
            except LedgerError as exc:
                reason = str(exc)
            finally:
                ledger.close()
        return {
            "reopened": reason is None,
            "reason": "awaiting_collection" if reason is None else reason,
            "status": self.status(client, engagement, run),
        }

    def _page_routing(
        self, client: str, engagement: str, run: str, page_id: str
    ) -> str | None:
        """The routing recorded for one page, or ``None`` when unavailable."""

        try:
            document = _load_validated_results_document(
                _results_path(
                    self.repo_root, str(client), str(engagement), str(run)
                ),
                str(client),
                str(engagement),
                str(run),
            )
        except PageAuditResultsError:
            return None
        if document is None:
            return None
        page = document["pages"].get(str(page_id))
        return page.get("routing") if isinstance(page, dict) else None

    def resend_failed_page(
        self, client: str, engagement: str, run: str, page_id: str
    ) -> dict:
        """Founder-triggered: queue ONE new conversation for a failed page.

        Nothing is sent here. The job joins the run's own queue with the page's
        existing reuse key and check-set id, so the next start relaunches the
        full worker and the wave scheduler sends it as a NEW conversation under
        the untouched 600-900 second pacing.
        """

        page_id = str(page_id)
        _ledger_view, item, ledger_path, reason = self._retry_precondition(
            client, engagement, run, page_id, RETRY_RESEND
        )
        if reason is None and (
            self._page_routing(client, engagement, run, page_id)
            == "ai_unavailable_page_too_large"
        ):
            # The page was never sendable: it was too large for the model.
            reason = "page_was_never_sendable"
        if reason is None:
            ledger = Ledger(ledger_path)
            try:
                ledger.enqueue_page_resend(
                    run,
                    page_id,
                    packet_hash=item["packet_hash"],
                    check_set_id=item["check_set_id"],
                )
            except LedgerError as exc:
                reason = str(exc)
            finally:
                ledger.close()
        return {
            "resent": reason is None,
            "reason": (
                "queued_for_a_new_conversation" if reason is None else reason
            ),
            "status": self.status(client, engagement, run),
        }

    # -- idempotent start --------------------------------------------------
    def start(self, client: str, engagement: str, run: str) -> dict:
        status = self.status(client, engagement, run)
        marker = _marker_path(self.repo_root, client, engagement, run)
        if _marker_is_live(marker):
            return {"started": False, "reason": "running", "status": status}
        if status["state"] == "complete":
            return {"started": False, "reason": "complete", "status": status}

        if status["results_error"] is not None:
            return {
                "started": False,
                "reason": status["results_error"],
                "status": status,
            }

        ledger_awaiting_harvest = status["ledger_awaiting_harvest"] > 0
        results_awaiting_harvest = status["results_awaiting_harvest"] > 0
        if ledger_awaiting_harvest and not status["results_valid"]:
            return {
                "started": False,
                "reason": "harvest_results_unavailable",
                "status": status,
            }
        if (results_awaiting_harvest and not ledger_awaiting_harvest
                and not status["ledger_readable"]):
            return {"started": False, "reason": "harvest_ledger_unavailable", "status": status}
        if status["ledger_present"] and not status["ledger_readable"]:
            return {
                "started": False,
                "reason": "page_audit_ledger_unavailable",
                "status": status,
            }

        harvest_only = (
            ledger_awaiting_harvest
            and status["queued"] == 0
            and status["in_flight"] == 0
        )
        if self.launcher is None:
            return {
                "started": False,
                "reason": "launcher_unavailable",
                "status": status,
            }

        started_at = float(self.clock())
        marker.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(
                    str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                break
            except FileExistsError:
                if _marker_is_live(marker):
                    return {
                        "started": False,
                        "reason": "running",
                        "status": self.status(client, engagement, run),
                    }
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass

        reservation = {
            "pid": os.getpid(),
            "started_at": started_at,
        }
        try:
            _write_marker_fd(fd, reservation)
        except BaseException:
            os.close(fd)
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)

        try:
            launched = self.launcher(
                client, engagement, run, harvest_only=harvest_only
            )
            launched_pid = getattr(launched, "pid", launched)
            if (
                isinstance(launched_pid, bool)
                or not isinstance(launched_pid, int)
                or launched_pid < 1
            ):
                _remove_marker_if_owned(
                    marker, pid=os.getpid(), started_at=started_at
                )
                return {
                    "started": False,
                    "reason": "worker_pid_unavailable",
                    "status": self.status(client, engagement, run),
                }
            child_owner = {
                "pid": launched_pid,
                "started_at": started_at,
            }
            if not _replace_marker_owner(marker, reservation, child_owner):
                raise RuntimeError("Page-audit worker marker ownership changed")
        except BaseException:
            _remove_marker_if_owned(
                marker, pid=os.getpid(), started_at=started_at
            )
            raise
        return {
            "started": True,
            "reason": "resumed_harvest" if harvest_only else "launched",
            "status": self.status(client, engagement, run),
        }
