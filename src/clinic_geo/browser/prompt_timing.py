"""Fail-open per-attempt timing records for Snapshot browser measurements.

The recorder deliberately keeps prompts, answers, cookies, account data, and
free-form exception text out of the JSONL artifact.  Callers collect events in
memory and write once, after the measured interaction has reached a terminal
state.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "free_snapshot.prompt_timing.v1"
TIMING_FILENAME = "prompt_timings.jsonl"
ENABLE_ENV = "SNAPSHOT_PROMPT_TIMING_ENABLED"
DISABLE_ENV = "SNAPSHOT_PROMPT_TIMING_DISABLE"

_APPEND_LOCK = threading.Lock()
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Only operational, non-content fields belong in ``details``.  URL values are
# retained for navigation diagnostics, but prompt-bearing ``q`` parameters are
# redacted before persistence.
_SAFE_DETAIL_FIELDS = {
    "tab_open_delay_seconds",
    "tab_open_delay_formula_mode",
    "submission_action_method",
    "input_mode",
    "gemini_modal_signature",
    "gemini_modal_recognised",
    "google_requested_url",
    "google_redirected_url",
    "google_final_url",
    "google_parameter_validation",
    "google_shadow_absence_threshold_seconds",
    "google_overview_after_shadow_threshold",
    "outer_watchdog_seconds",
    "candidate_watchdog_seconds",
    "watchdog_components",
}
_URL_DETAIL_FIELDS = {
    "google_requested_url",
    "google_redirected_url",
    "google_final_url",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_bool(name: str) -> Optional[bool]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def timing_enabled(stage: Optional[str] = None) -> bool:
    """Return whether prompt timing is enabled for ``stage``.

    Collection is enabled by default. ``SNAPSHOT_PROMPT_TIMING_ENABLED`` can
    explicitly opt another stage in or any stage out; the dedicated disable
    switch always wins.
    """

    if _env_bool(DISABLE_ENV) is True:
        return False
    explicit = _env_bool(ENABLE_ENV)
    if explicit is not None:
        return explicit
    return str(stage or os.getenv("SNAPSHOT_STAGE") or "").strip() == "collection"


def _redacted_url(value: object) -> str:
    """Keep only the frozen Google contract structure, never echoed query content."""

    raw = str(value or "")
    try:
        parts = urlsplit(raw)
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            normalised_key = key.casefold()
            if normalised_key not in {"q", "hl", "gl", "pws"}:
                continue
            query.append((key, "<redacted>" if normalised_key == "q" else item))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
        )
    except Exception:
        return ""


def _safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:256]


def _safe_detail(name: str, value: object) -> object:
    if name in _URL_DETAIL_FIELDS:
        return _redacted_url(value)
    if isinstance(value, Mapping):
        return {
            str(key)[:64]: _safe_scalar(item)
            for key, item in value.items()
        }
    return _safe_scalar(value)


class PromptTimingRecorder:
    """Collect one prompt-attempt record in memory and append it fail-open."""

    def __init__(
        self,
        *,
        data_dir: Path | str | None,
        platform: str,
        stage: str,
        prompt_id: str,
        outer_attempt: int = 1,
        profile_id: Optional[str] = None,
        lane_index: int = 0,
        configured_x: int = 1,
        worker_index: int = 0,
        enabled: Optional[bool] = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = _utc_now,
    ) -> None:
        self.enabled = timing_enabled(stage) if enabled is None else bool(enabled)
        self.path = Path(data_dir) / TIMING_FILENAME if data_dir else None
        self.platform = str(platform or "unknown")
        self.stage = str(stage or "unknown")
        self.prompt_id = str(prompt_id or "unknown")
        self.outer_attempt = max(1, int(outer_attempt or 1))
        self.profile_id = str(profile_id or "unknown")
        self.lane_index = max(0, int(lane_index or 0))
        self.configured_x = max(1, int(configured_x or 1))
        self.worker_index = max(0, int(worker_index or 0))
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._started_monotonic = self._safe_monotonic()
        self._started_at = self._safe_utc_now()
        self._events: dict[str, float] = {}
        self._details: dict[str, object] = {}
        self._finished = False

    @classmethod
    def from_env(
        cls,
        *,
        platform: str,
        prompt_id: str,
        profile_id: Optional[str] = None,
        lane_index: int = 0,
        configured_x: int = 1,
        worker_index: int = 0,
        data_dir: Path | str | None = None,
        stage: Optional[str] = None,
        outer_attempt: Optional[int] = None,
        enabled: Optional[bool] = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = _utc_now,
    ) -> "PromptTimingRecorder":
        try:
            resolved_attempt = int(
                outer_attempt
                if outer_attempt is not None
                else (os.getenv("SNAPSHOT_OUTER_ATTEMPT") or 1)
            )
        except (TypeError, ValueError):
            resolved_attempt = 1
        recorder = cls(
            data_dir=data_dir or os.getenv("DATA_DIR"),
            platform=platform,
            stage=str(stage or os.getenv("SNAPSHOT_STAGE") or "unknown"),
            prompt_id=prompt_id,
            outer_attempt=resolved_attempt,
            profile_id=profile_id,
            lane_index=lane_index,
            configured_x=configured_x,
            worker_index=worker_index,
            enabled=enabled,
            monotonic=monotonic,
            utc_now=utc_now,
        )
        try:
            current_watchdog = os.getenv("SNAPSHOT_OUTER_WATCHDOG_SECONDS")
            if current_watchdog not in (None, ""):
                recorder.field("outer_watchdog_seconds", float(current_watchdog))
        except (TypeError, ValueError):
            pass
        try:
            shadow_raw = os.getenv("SNAPSHOT_WATCHDOG_CAPACITY_SHADOW_JSON")
            shadow = json.loads(shadow_raw) if shadow_raw else None
            if isinstance(shadow, Mapping):
                candidate = shadow.get("candidate_watchdog_seconds")
                if candidate is None:
                    candidate = shadow.get("total_seconds")
                if candidate is None:
                    candidate = shadow.get("candidate_timeout_seconds")
                if candidate is not None:
                    recorder.field("candidate_watchdog_seconds", candidate)
                components = shadow.get("watchdog_components")
                if components is None:
                    components = shadow.get("components")
                if components is None:
                    components = shadow.get("components_seconds")
                if isinstance(components, Mapping):
                    recorder.field("watchdog_components", components)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return recorder

    def _safe_monotonic(self) -> float:
        try:
            return float(self._monotonic())
        except Exception:
            return 0.0

    def _safe_utc_now(self) -> str:
        try:
            return str(self._utc_now())
        except Exception:
            return ""

    def mark(self, event: str) -> None:
        """Record the first monotonic offset for a named event."""

        if not self.enabled or not _EVENT_NAME.fullmatch(str(event or "")):
            return
        try:
            self._events.setdefault(
                str(event),
                round(max(0.0, self._safe_monotonic() - self._started_monotonic), 6),
            )
        except Exception:
            pass

    def field(self, name: str, value: object) -> None:
        """Attach one allowlisted, non-content diagnostic field."""

        if not self.enabled or name not in _SAFE_DETAIL_FIELDS:
            return
        try:
            self._details[name] = _safe_detail(name, value)
        except Exception:
            pass

    def finish(self, final_provider_status: str, error: BaseException | type | None = None) -> None:
        """Append the terminal record once. All failures are intentionally ignored."""

        if not self.enabled or self._finished:
            return
        self._finished = True
        try:
            finished_monotonic = self._safe_monotonic()
            if isinstance(error, type):
                error_type = error.__name__
            elif error is not None:
                error_type = type(error).__name__
            else:
                error_type = None
            record = {
                "schema_version": SCHEMA_VERSION,
                "platform": self.platform,
                "stage": self.stage,
                "prompt_id": self.prompt_id,
                "outer_attempt": self.outer_attempt,
                "profile_id": self.profile_id,
                "lane_index": self.lane_index,
                "configured_x": self.configured_x,
                "worker_index": self.worker_index,
                "final_provider_status": str(final_provider_status or "unknown"),
                "terminal_error_type": error_type,
                "started_at": self._started_at,
                "finished_at": self._safe_utc_now(),
                "duration_seconds": round(
                    max(0.0, finished_monotonic - self._started_monotonic), 6
                ),
                "events": dict(self._events),
                "details": dict(self._details),
            }
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            with _APPEND_LOCK:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(payload)
        except Exception:
            pass


__all__ = [
    "DISABLE_ENV",
    "ENABLE_ENV",
    "PromptTimingRecorder",
    "SCHEMA_VERSION",
    "TIMING_FILENAME",
    "timing_enabled",
]
