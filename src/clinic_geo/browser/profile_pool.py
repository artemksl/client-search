"""Ordered access to real Chrome profile windows exposed through CDP.

Chrome exposes each on-disk profile as a separate CDP browser-context id, but
Playwright can merge all of those profiles into one ``BrowserContext`` when it
attaches to a running Chrome instance.  Consequently, ``context.new_page()``
does not identify which on-disk profile owns the new tab.

The logged-in launcher opens one tagged ChatGPT anchor page in every configured
profile. This module deduplicates those anchors by their raw CDP
browser-context id, matches their tags to stable profile directory keys, and
opens new tabs with ``window.open`` from the corresponding anchor. A tab opened
that way stays in the anchor's real Chrome profile even when Playwright
presents every profile as one context.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

from clinic_geo.env import env_float, env_static_dir
from clinic_geo.paths import resolve_repo_relative

try:  # Unix/macOS production path; tests and Windows keep the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows
    fcntl = None


# Chrome's primary/default CDP browser context is valid but has no
# ``browserContextId`` in ``Target.getTargetInfo``. Give that one context a
# stable internal key so it participates in the same exact-profile matching as
# incognito/profile contexts that do have an explicit CDP id.
DEFAULT_BROWSER_CONTEXT_ID = "__chrome_default_browser_context__"
DEFAULT_POPUP_LOCK_PATH = Path(tempfile.gettempdir()) / "clinic-geo-profile-popup.lock"
DEFAULT_PROFILE_LEASE_ROOT = Path(tempfile.gettempdir()) / "clinic-geo-profile-leases"


class ProfileLeaseError(RuntimeError):
    """Base error for an exclusive persistent-profile lease."""


class ProfileLeaseTimeout(ProfileLeaseError):
    """Raised when another job owns a requested Chrome profile too long."""


class ProfileLeaseCancelled(ProfileLeaseError):
    """Raised when collection is cancelled while waiting for a profile."""


class ProfileLeaseQuarantined(ProfileLeaseError):
    """Raised when a prior owner could not confirm its browser worker exited."""


_PROFILE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_THREAD_LOCKS_GUARD = threading.Lock()


class _ProfileUsageGate:
    """Process-local readers/writer gate mirroring the advisory profile lock."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    def acquire(
        self,
        *,
        shared: bool,
        deadline: float,
        check_cancelled: Callable[[], None],
    ) -> bool:
        with self.condition:
            if shared:
                while self.writer or self.waiting_writers:
                    check_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self.condition.wait(timeout=min(0.1, remaining))
                self.readers += 1
                return True

            self.waiting_writers += 1
            try:
                while self.writer or self.readers:
                    check_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self.condition.wait(timeout=min(0.1, remaining))
                self.writer = True
                return True
            finally:
                self.waiting_writers -= 1

    def release(self, *, shared: bool) -> None:
        with self.condition:
            if shared:
                self.readers -= 1
            else:
                self.writer = False
            self.condition.notify_all()


_PROFILE_USAGE_GATES: dict[str, _ProfileUsageGate] = {}
_PROFILE_USAGE_GATES_GUARD = threading.Lock()


def _profile_usage_gate(key: str) -> _ProfileUsageGate:
    with _PROFILE_USAGE_GATES_GUARD:
        gate = _PROFILE_USAGE_GATES.get(key)
        if gate is None:
            gate = _ProfileUsageGate()
            _PROFILE_USAGE_GATES[key] = gate
        return gate


def _profile_thread_lock(key: str) -> threading.Lock:
    """Return the process-local half of a cross-process profile lock."""

    with _PROFILE_THREAD_LOCKS_GUARD:
        lock = _PROFILE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROFILE_THREAD_LOCKS[key] = lock
        return lock


def _lease_key(chrome_directory: str, namespace: str) -> str:
    material = f"{namespace}\0{chrome_directory}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _scoped_lease_namespace(namespace: str, lease_scope: str) -> str:
    return f"{namespace}\0profile-lane:{str(lease_scope).strip().casefold()}"


def _profile_quarantine_path(
    root: Path,
    chrome_directory: str,
    namespace: str,
    lease_scope: Optional[str] = None,
) -> Path:
    effective_namespace = (
        _scoped_lease_namespace(namespace, lease_scope)
        if lease_scope
        else namespace
    )
    return root / f"{_lease_key(chrome_directory, effective_namespace)}.quarantine.json"


def quarantine_profile_leases(
    profiles: Iterable["LoggedInProfile"],
    *,
    owner: str,
    namespace: str = "default-chrome-user-data",
    lease_scope: Optional[str] = None,
    detail: str = "",
    lock_root: Path | str | None = None,
) -> tuple[Path, ...]:
    """Persistently block profile lanes whose previous worker may still be alive.

    Callers normally invoke this while holding ``exclusive_profile_leases``.
    The marker survives advisory-lock release and process restarts, so a later
    job using the same namespace and scope cannot reuse that Chrome profile
    until an operator has established that the orphaned provider is gone and
    removes the marker deliberately. An unscoped marker blocks every lane;
    a scoped marker blocks only that provider lane.
    """

    root = Path(lock_root or DEFAULT_PROFILE_LEASE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    quarantined_at = time.time()
    for profile in profiles:
        path = _profile_quarantine_path(
            root,
            profile.chrome_directory,
            namespace,
            lease_scope,
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "owner": str(owner),
                        "profile_id": profile.id,
                        "profile_name": profile.display_name,
                        "chrome_directory": profile.chrome_directory,
                        "namespace": str(namespace),
                        "lease_scope": str(lease_scope or ""),
                        "quarantined_at_epoch": quarantined_at,
                        "detail": str(detail or ""),
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            written.append(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return tuple(written)


@contextmanager
def exclusive_profile_leases(
    profiles: Iterable["LoggedInProfile"],
    *,
    owner: str,
    namespace: str = "default-chrome-user-data",
    lease_scope: Optional[str] = None,
    timeout_seconds: float = 300.0,
    cancelled: Optional[Callable[[], bool]] = None,
    lock_root: Path | str | None = None,
):
    """Exclusively lease namespaced lanes through real Chrome profiles.

    The process-local lock serializes threads in one Snapshot process; the
    advisory file lock serializes separate Snapshot/job processes. Locks are
    acquired in stable directory order to prevent deadlocks. The browser
    subprocess does not inherit these descriptors, so cancellation or process
    exit releases only this worker's leases and never touches Chrome itself.
    With ``lease_scope`` set, the worker takes a shared physical-profile gate
    plus an exclusive provider-lane lock. Different providers can therefore
    own one tab each in the same profile, while duplicate work by one provider
    still serializes. Unscoped legacy/maintenance callers take the physical
    gate exclusively and remain mutually exclusive with every scoped lane.
    """

    requested = sorted(
        list(profiles),
        key=lambda row: (row.chrome_directory.casefold(), row.id),
    )
    if not requested:
        yield
        return
    root = Path(lock_root or DEFAULT_PROFILE_LEASE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    acquired: list[
        tuple[
            object,
            Optional[threading.Lock],
            object,
            _ProfileUsageGate,
            bool,
        ]
    ] = []

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise ProfileLeaseCancelled(
                f"Profile lease acquisition cancelled for {owner}"
            )

    def timeout(profile: "LoggedInProfile") -> ProfileLeaseTimeout:
        return ProfileLeaseTimeout(
            f"Timed out waiting for Chrome profile "
            f"{profile.display_name} ({profile.chrome_directory})"
        )

    def acquire_advisory(handle: object, operation: int, profile: "LoggedInProfile") -> None:
        if fcntl is None:
            return
        while True:
            check_cancelled()
            try:
                fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise timeout(profile)
                time.sleep(0.1)

    def assert_not_quarantined(
        profile: "LoggedInProfile",
        *,
        marker_scope: Optional[str],
        include_scoped: bool = False,
    ) -> None:
        quarantine_path = _profile_quarantine_path(
            root,
            profile.chrome_directory,
            namespace,
            marker_scope,
        )
        candidates = [quarantine_path] if quarantine_path.is_file() else []
        if include_scoped:
            for candidate in root.glob("*.quarantine.json"):
                if candidate == quarantine_path:
                    continue
                try:
                    marker = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(marker, dict):
                    continue
                if (
                    str(marker.get("namespace") or "") == str(namespace)
                    and str(marker.get("chrome_directory") or "")
                    == profile.chrome_directory
                    and str(marker.get("lease_scope") or "")
                ):
                    candidates.append(candidate)
        if not candidates:
            return
        quarantine_path = candidates[0]
        detail = ""
        try:
            marker = json.loads(quarantine_path.read_text(encoding="utf-8"))
            if isinstance(marker, dict):
                detail = str(marker.get("detail") or "").strip()
        except (OSError, json.JSONDecodeError):
            # The marker itself is authoritative. A malformed marker must
            # fail closed instead of restoring access.
            pass
        suffix = f": {detail}" if detail else ""
        raise ProfileLeaseQuarantined(
            "Chrome profile "
            f"{profile.display_name} ({profile.chrome_directory}) "
            f"is quarantined by {quarantine_path}{suffix}"
        )

    try:
        for profile in requested:
            physical_key = _lease_key(profile.chrome_directory, namespace)
            shared_physical = bool(lease_scope)
            usage_gate = _profile_usage_gate(physical_key)
            if not usage_gate.acquire(
                shared=shared_physical,
                deadline=deadline,
                check_cancelled=check_cancelled,
            ):
                raise timeout(profile)

            physical_handle = None
            lane_handle = None
            lane_thread_lock = None
            lane_thread_acquired = False
            try:
                physical_handle = (root / f"{physical_key}.lock").open(
                    "a+", encoding="utf-8"
                )
                acquire_advisory(
                    physical_handle,
                    (
                        fcntl.LOCK_SH if shared_physical else fcntl.LOCK_EX
                    )
                    if fcntl is not None
                    else 0,
                    profile,
                )
                assert_not_quarantined(
                    profile,
                    marker_scope=None,
                    include_scoped=not shared_physical,
                )

                if lease_scope:
                    lane_namespace = _scoped_lease_namespace(namespace, lease_scope)
                    lane_key = _lease_key(profile.chrome_directory, lane_namespace)
                    lane_thread_lock = _profile_thread_lock(lane_key)
                    while True:
                        check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise timeout(profile)
                        if lane_thread_lock.acquire(timeout=min(0.1, remaining)):
                            lane_thread_acquired = True
                            break
                    lane_handle = (root / f"{lane_key}.lock").open(
                        "a+", encoding="utf-8"
                    )
                    acquire_advisory(
                        lane_handle,
                        fcntl.LOCK_EX if fcntl is not None else 0,
                        profile,
                    )
                    assert_not_quarantined(profile, marker_scope=lease_scope)
                else:
                    lane_handle = physical_handle

                lane_handle.seek(0)
                lane_handle.truncate()
                json.dump(
                    {
                        "owner": str(owner),
                        "profile_id": profile.id,
                        "profile_name": profile.display_name,
                        "chrome_directory": profile.chrome_directory,
                        "namespace": str(namespace),
                        "lease_scope": str(lease_scope or ""),
                    },
                    lane_handle,
                    ensure_ascii=False,
                )
                lane_handle.write("\n")
                lane_handle.flush()
                acquired.append(
                    (
                        lane_handle,
                        lane_thread_lock,
                        physical_handle,
                        usage_gate,
                        shared_physical,
                    )
                )
            except BaseException:
                if lane_handle is not None and lane_handle is not physical_handle:
                    try:
                        if fcntl is not None:
                            fcntl.flock(lane_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        lane_handle.close()
                if lane_thread_lock is not None and lane_thread_acquired:
                    lane_thread_lock.release()
                if physical_handle is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(physical_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        physical_handle.close()
                usage_gate.release(shared=shared_physical)
                raise
        yield
    finally:
        for (
            lane_handle,
            lane_thread_lock,
            physical_handle,
            usage_gate,
            shared_physical,
        ) in reversed(acquired):
            try:
                if lane_handle is not physical_handle:
                    try:
                        if fcntl is not None:
                            fcntl.flock(lane_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        lane_handle.close()
                if lane_thread_lock is not None:
                    lane_thread_lock.release()
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(physical_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    physical_handle.close()
                    usage_gate.release(shared=shared_physical)


PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def fallback_profile_id(display_name: object, chrome_directory: object) -> str:
    """Derive a stable code id for a legacy profile row.

    Canonical rows should always provide ``id``.  During migration, an exact
    ``loggedin(account.name)`` label becomes ``account_name`` and an exact
    ``loggedinN`` label becomes ``loggedin_N``.  Everything else is casefolded
    and reduced to an underscore-separated ASCII identifier.
    """

    value = str(display_name or "").strip()
    wrapped = re.fullmatch(r"loggedin\((.+)\)", value, flags=re.IGNORECASE)
    if wrapped:
        value = wrapped.group(1)
    elif re.fullmatch(r"loggedin\d+", value, flags=re.IGNORECASE):
        value = re.sub(r"(?i)^loggedin", "loggedin_", value)
    value = value or str(chrome_directory or "").strip()
    identifier = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return identifier or "chrome_profile"


@dataclass(frozen=True, init=False)
class LoggedInProfile:
    """A configured, persistent Chrome profile.

    ``id`` is CiteUp's stable code identifier. ``display_name`` is the friendly
    name shown in logs and evidence. ``chrome_directory`` is Chrome's immutable
    on-disk directory key (for example ``Profile 7`` or ``Default``) and is the
    only value used to launch or match a real browser context.

    The custom constructor and read-only ``profile``/``label`` properties keep
    older callers working while they migrate to the explicit attribute names.
    """

    id: str
    display_name: str
    chrome_directory: str
    project_url: str = ""

    def __init__(
        self,
        chrome_directory: str | None = None,
        display_name: str | None = None,
        project_url: str = "",
        *,
        id: str | None = None,
        profile: str | None = None,
        label: str | None = None,
    ) -> None:
        """Build a canonical identity while accepting the legacy names.

        Positional ``(profile, label, project_url)`` construction intentionally
        remains valid for compatibility with external integrations.
        """

        directory = str(chrome_directory or profile or "").strip()
        name = str(display_name or label or directory).strip() or directory
        identifier = str(id or "").strip() or fallback_profile_id(name, directory)
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "display_name", name)
        object.__setattr__(self, "chrome_directory", directory)
        object.__setattr__(self, "project_url", str(project_url or "").strip())

    @property
    def profile(self) -> str:
        """Legacy alias for ``chrome_directory``."""

        return self.chrome_directory

    @property
    def label(self) -> str:
        """Legacy alias for ``display_name``."""

        return self.display_name


@dataclass(frozen=True, init=False)
class ProfileAnchor:
    """A configured profile paired with a page known to belong to it."""

    identity: LoggedInProfile
    page: object
    browser_context_id: str

    def __init__(
        self,
        identity: LoggedInProfile | None = None,
        page: object = None,
        browser_context_id: str = "",
        *,
        profile: LoggedInProfile | None = None,
    ) -> None:
        selected = identity or profile
        if selected is None:
            raise TypeError("ProfileAnchor requires a logged-in profile identity")
        object.__setattr__(self, "identity", selected)
        object.__setattr__(self, "page", page)
        object.__setattr__(self, "browser_context_id", str(browser_context_id or ""))

    @property
    def profile(self) -> LoggedInProfile:
        """Legacy alias for ``identity``."""

        return self.identity


def logged_in_profiles_path(value: Path | str | None = None) -> Path:
    """Resolve the ordered logged-in profile configuration used by a job."""

    configured = value or os.getenv("LOGGEDIN_PROFILES_PATH")
    if configured:
        return resolve_repo_relative(configured)
    return env_static_dir() / "logged-in-profiles.json"


def load_logged_in_profile_pool(value: Path | str | None = None) -> list[LoggedInProfile]:
    """Load and validate the ordered persistent-profile pool.

    Canonical rows use ``id``, ``display_name`` and ``chrome_directory``.
    Older ``profile``/``label`` rows remain readable and receive a deterministic
    id from :func:`fallback_profile_id`.
    """

    path = logged_in_profiles_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Logged-in Chrome profile config is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Logged-in Chrome profile config is invalid: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"Logged-in Chrome profile config must be a JSON list: {path}")

    rows: list[LoggedInProfile] = []
    seen_directories: set[str] = set()
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        chrome_directory = str(
            item.get("chrome_directory") or item.get("profile") or ""
        ).strip()
        if not chrome_directory:
            continue
        if chrome_directory in seen_directories:
            raise RuntimeError(
                f"Duplicate Chrome profile directory '{chrome_directory}' in {path}"
            )
        display_name = str(
            item.get("display_name")
            or item.get("label")
            or item.get("name")
            or chrome_directory
        ).strip()
        profile_id = str(item.get("id") or "").strip() or fallback_profile_id(
            display_name,
            chrome_directory,
        )
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise RuntimeError(
                f"Logged-in profile id '{profile_id}' in {path} must use only "
                "lowercase letters, numbers and underscores"
            )
        if profile_id in seen_ids:
            raise RuntimeError(f"Duplicate logged-in profile id '{profile_id}' in {path}")
        seen_directories.add(chrome_directory)
        seen_ids.add(profile_id)
        rows.append(
            LoggedInProfile(
                id=profile_id,
                display_name=display_name or chrome_directory,
                chrome_directory=chrome_directory,
                project_url=str(item.get("project_url") or "").strip(),
            )
        )
    if not rows:
        raise RuntimeError(f"No logged-in Chrome profiles are configured in {path}")
    return rows


def _is_profile_anchor_candidate(page: object) -> bool:
    """Return whether a page can identify a persistent Chrome profile.

    Current launchers use a network-independent tagged ``about:blank`` page.
    Tagged ChatGPT pages and untagged ChatGPT pages from older launchers remain
    candidates for backward compatibility.
    """

    try:
        url = str(getattr(page, "url", "") or "")
    except Exception:
        return False
    return profile_marker_from_url(url) is not None or "chatgpt.com" in url.casefold()


def profile_marker_from_url(url: object) -> Optional[str]:
    """Return the raw Chrome directory encoded by the logged-in launcher URL."""

    try:
        parsed = urlsplit(str(url or ""))
    except Exception:
        return None
    for component in (parsed.fragment, parsed.query):
        values = parse_qs(component).get("citeup-profile") or []
        value = str(values[0] if values else "").strip()
        if value:
            return value
    return None


def _profile_marker(page: object) -> Optional[str]:
    """Return the raw profile directory encoded by a launcher's anchor page."""

    try:
        return profile_marker_from_url(getattr(page, "url", ""))
    except Exception:
        return None


async def _browser_context_id(page: object) -> Optional[str]:
    """Read a stable CDP context key, even under merged Playwright contexts.

    Chrome omits ``browserContextId`` for its valid default context. A complete
    ``TargetInfo`` without that optional field therefore maps to
    :data:`DEFAULT_BROWSER_CONTEXT_ID`; malformed payloads and CDP failures
    still return ``None`` and fail closed.
    """

    # Public-looking test doubles can provide either hint without having to
    # implement a CDP session.  Production pages fall through to CDP.
    for attribute in ("browser_context_id", "profile_context_id"):
        value = getattr(page, attribute, None)
        if value:
            return str(value)

    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "new_cdp_session"):
        return None
    session = None
    try:
        session = await context.new_cdp_session(page)
        payload = await session.send("Target.getTargetInfo")
        target = payload.get("targetInfo") if isinstance(payload, dict) else None
        if not isinstance(target, dict) or not target.get("targetId"):
            return None
        value = target.get("browserContextId")
        return str(value) if value else DEFAULT_BROWSER_CONTEXT_ID
    except Exception:
        return None
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass


def _all_pages(browser: object) -> list[object]:
    pages: list[object] = []
    for context in list(getattr(browser, "contexts", ()) or ()):
        pages.extend(list(getattr(context, "pages", ()) or ()))
    return pages


def _acquire_process_popup_lock():
    """Acquire the cross-process lock protecting a shared anchor handshake."""

    configured = str(os.getenv("PROFILE_POPUP_LOCK_PATH") or "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_POPUP_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_process_popup_lock(handle) -> None:
    """Release a lock returned by :func:`_acquire_process_popup_lock`."""

    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def pair_profile_anchors(
    browser: object,
    profiles: Iterable[LoggedInProfile],
    *,
    wait_seconds: float | None = None,
) -> list[ProfileAnchor]:
    """Pair configured rows with distinct persistent ChatGPT anchors.

    Tagged launch anchors are matched by raw Chrome directory key. Legacy
    untagged anchors retain order-based pairing. We deliberately fail when
    fewer distinct persistent contexts are available; falling back to an
    incognito context would lose the provider logins and silently change the
    requested measurement setup.
    """

    configured = list(profiles)
    if not configured:
        raise RuntimeError("No logged-in Chrome profiles were supplied")
    timeout = max(
        0.0,
        float(
            wait_seconds
            if wait_seconds is not None
            else env_float("AI_PROVIDER_PROFILE_WAIT_SECONDS", 10.0)
        ),
    )
    deadline = asyncio.get_running_loop().time() + timeout
    discovered: list[tuple[object, str, Optional[str]]] = []
    matches: dict[str, tuple[object, str]] = {}

    while True:
        discovered = []
        by_context_id: dict[str, tuple[object, str, Optional[str]]] = {}
        for page in _all_pages(browser):
            if not _is_profile_anchor_candidate(page):
                continue
            context_id = await _browser_context_id(page)
            if not context_id:
                continue
            marker = _profile_marker(page)
            existing = by_context_id.get(context_id)
            # Prefer the launcher's tagged anchor over a restored ChatGPT tab
            # from the same profile. Dict insertion order still preserves the
            # legacy launch order for profiles without markers.
            if existing is None or (marker and not existing[2]):
                by_context_id[context_id] = (page, context_id, marker)
        discovered = list(by_context_id.values())

        matches = {}
        used_context_ids: set[str] = set()
        tagged = {
            marker: (page, context_id)
            for page, context_id, marker in discovered
            if marker
        }
        for row in configured:
            candidate = tagged.get(row.chrome_directory)
            if candidate and candidate[1] not in used_context_ids:
                matches[row.chrome_directory] = candidate
                used_context_ids.add(candidate[1])

        # Older launchers did not tag anchors. Preserve their ordered behavior
        # only for still-unmatched rows; never attribute an anchor explicitly
        # tagged for a different profile by position.
        legacy = [
            (page, context_id)
            for page, context_id, marker in discovered
            if not marker and context_id not in used_context_ids
        ]
        unmatched = [row for row in configured if row.chrome_directory not in matches]
        for row, candidate in zip(unmatched, legacy):
            matches[row.chrome_directory] = candidate
            used_context_ids.add(candidate[1])

        if len(matches) == len(configured):
            break
        if asyncio.get_running_loop().time() >= deadline:
            requested = ", ".join(row.display_name for row in configured)
            raise RuntimeError(
                "Persistent Chrome profile pool is incomplete: "
                f"matched {len(matches)} of {len(configured)} configured profile "
                f"anchor(s) from {len(discovered)} distinct Chrome contexts "
                f"({requested}). Relaunch Chrome with "
                "config/launch_logged_in.sh using this job's LOGGEDIN_PROFILES_PATH."
            )
        await asyncio.sleep(0.2)

    return [
        ProfileAnchor(
            identity=row,
            page=matches[row.chrome_directory][0],
            browser_context_id=matches[row.chrome_directory][1],
        )
        for row in configured
    ]


async def open_profile_page(
    anchor: ProfileAnchor,
    *,
    lock: asyncio.Lock,
    timeout_seconds: float = 20.0,
) -> object:
    """Open a uniquely identifiable tab inside ``anchor``'s real profile."""

    marker = f"about:blank#clinic-geo-provider-{uuid.uuid4().hex}"
    # Take the process-local lock before submitting the blocking file-lock
    # acquisition. Otherwise a large same-process fan-out can fill the default
    # executor with file-lock waiters and leave no thread available to release
    # the lock held by the first popup handshake.
    async with lock:
        process_lock = await asyncio.to_thread(_acquire_process_popup_lock)
        handed_off = False
        try:
            # Every provider is a separate process attached to the same anchor
            # targets. The asyncio lock serializes workers in this process; the
            # file lock serializes the short window.open/discovery handshake
            # across provider processes. Once this function returns, the
            # provider pages run concurrently as before.
            await anchor.page.evaluate("url => window.open(url, '_blank')", marker)

            # A unique marker identifies exactly this popup even though each
            # CDP client can observe pages opened by every other provider.
            deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
            while asyncio.get_running_loop().time() < deadline:
                for page in list(getattr(anchor.page.context, "pages", ()) or ()):
                    try:
                        page_url = str(getattr(page, "url", "") or "")
                    except Exception:
                        continue
                    if page_url != marker:
                        continue
                    context_id = await _browser_context_id(page)
                    if context_id != anchor.browser_context_id:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        raise RuntimeError(
                            "Provider popup opened in the wrong Chrome profile: "
                            f"expected context {anchor.browser_context_id}, got "
                            f"{context_id or 'unknown'}"
                        )
                    handed_off = True
                    return page
                await asyncio.sleep(0.05)
            raise RuntimeError(
                f"Could not open a provider tab for {anchor.identity.display_name} "
                f"({anchor.identity.id}; Chrome directory {anchor.identity.chrome_directory})"
            )
        finally:
            # Cancellation can arrive after window.open but before discovery
            # has handed the page to the provider. Remove only this call's
            # uniquely tagged popup; never close the anchor, profile window,
            # browser, or a page owned by another platform session.
            if not handed_off:
                for page in list(getattr(anchor.page.context, "pages", ()) or ()):
                    try:
                        if str(getattr(page, "url", "") or "") == marker:
                            await page.close()
                    except Exception:
                        pass
            await asyncio.to_thread(_release_process_popup_lock, process_lock)
