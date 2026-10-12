"""set_vpn.py — shared VPN rotation for every script that talks to ChatGPT.

PrivadoVPN isn't scriptable, so the UI automation lives in PrivadoRandom.app — a
tiny AppleScript bundle that holds the macOS Accessibility grant (keeping the
privilege in the app means it works no matter which terminal runs the pipeline).

On call, ``set_vpn()`` launches the app with ``open -W`` (blocking until it
finishes connecting) to switch to a random FREE country that is NOT the one
recorded in its shared state file, then returns the new country. The app reads
that file to know which country to EXCLUDE and writes the chosen one back, so
repeated calls keep rotating.

This function is SYNC by design. Call it from async code via
``await asyncio.to_thread(set_vpn)``. Because the VPN is system-wide, callers
should rotate BETWEEN passes, never while requests are in flight on other
profiles.
"""

import asyncio
import os
import platform
import re
import subprocess
import time
from pathlib import Path

from clinic_geo.paths import repo_root

PROJECT_ROOT = repo_root()

VPN_TIMEOUT = 90          # seconds to wait for the app to connect
VPN_LAUNCH_TRIES = 4      # retries if the app fails to launch / doesn't switch
VPN_LAUNCH_BACKOFF = 6    # seconds between launch retries (lets the app settle)


def _log(msg: str):
    print(msg, flush=True)


def vpn_app_path() -> Path:
    """Resolve the VPN helper without depending on this project's parent path.

    ``PRIVADO_APP`` may point at any copy. Otherwise a helper bundled in
    ``tools/`` is preferred, followed by conventional per-user locations.
    """
    configured = os.getenv("PRIVADO_APP", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    candidates = (
        PROJECT_ROOT / "tools" / "PrivadoRandom.app",
        Path.home() / "Applications" / "PrivadoRandom.app",
        Path.home() / "Desktop" / "PrivadoRandom.app",
        Path("/Applications/PrivadoRandom.app"),
    )
    return next((path for path in candidates if path.exists()), candidates[0])


def _embedded_country_file(app_path: Path) -> Path | None:
    """Read the state-file path compiled into an existing AppleScript app.

    Older copies of PrivadoRandom.app contain an absolute ``vpnCountryPath``.
    Discovering that value keeps those already-authorised helpers usable after
    the Clinic GEO directory is moved. ``VPN_COUNTRY_FILE`` can override the
    matching path for a custom helper.
    """
    if platform.system() != "Darwin" or not app_path.exists():
        return None
    try:
        result = subprocess.run(
            ["osadecompile", str(app_path)],
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'property\s+vpnCountryPath\s*:\s*"([^"]+)"', result.stdout)
    return Path(match.group(1)).expanduser() if match else None


def vpn_country_file(app_path: Path | None = None) -> Path:
    """Return the shared VPN state file used by Python and the helper app."""
    configured = os.getenv("VPN_COUNTRY_FILE", "").strip()
    if configured:
        value = Path(configured).expanduser()
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()
    return _embedded_country_file(app_path or vpn_app_path()) or (PROJECT_ROOT / "vpn_country")


def read_vpn_country(country_file: Path | None = None) -> str:
    """Return the country currently recorded by the VPN helper (or '')."""
    try:
        return (country_file or vpn_country_file()).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def vpn_available() -> bool:
    """True if we can drive the VPN (macOS + the granted app is present)."""
    if platform.system() != "Darwin":
        _log("⚠ VPN: not macOS — skipping VPN control.")
        return False
    app_path = vpn_app_path()
    if not app_path.exists():
        _log(f"⚠ VPN: helper not found at {app_path} — skipping VPN control. "
             "Set PRIVADO_APP to its location.")
        return False
    return True


def set_vpn() -> str:
    """Switch to a random free country other than the recorded one, then return it.

    The exclusion is enforced by PrivadoRandom.app using its shared state file;
    we detect success here by the recorded country actually CHANGING. Retries on the
    LaunchServices -600 race / an unchanged country (e.g. the app raced
    PrivadoVPN's post-connect window transition). With no state file the app is
    free to pick any country (nothing to exclude).
    """
    app_path = vpn_app_path()
    country_file = vpn_country_file(app_path)
    prev = read_vpn_country(country_file)

    for attempt in range(1, VPN_LAUNCH_TRIES + 1):
        try:
            subprocess.run(["open", "-W", str(app_path)], timeout=VPN_TIMEOUT, check=True)
        except subprocess.TimeoutExpired:
            _log(f"⚠ VPN: timed out after {VPN_TIMEOUT}s (attempt {attempt}).")
        except (subprocess.CalledProcessError, OSError) as e:
            _log(f"⚠ VPN: launch attempt {attempt} failed ({e}).")

        country = read_vpn_country(country_file)
        if country and country != prev:
            _log(f"🔒 VPN set to {country}.")
            return country

        if attempt < VPN_LAUNCH_TRIES:
            _log(f"⚠ VPN: attempt {attempt} did not switch country "
                 f"(country='{country or '∅'}') — retrying...")
            time.sleep(VPN_LAUNCH_BACKOFF)

    country = read_vpn_country(country_file)
    _log(f"⚠ VPN: could not switch to a new country after {VPN_LAUNCH_TRIES} "
         f"attempts (country='{country or '∅'}').")
    return country


class VpnGate:
    """Coordinate system-wide VPN switches across concurrently-running profiles.

    The VPN is machine-wide, so switching it while another profile has a request
    in flight would break that profile. This turns a switch into a BARRIER: the
    profile that needs a new country parks; every other still-active profile parks
    at its next safe checkpoint (between items); and only once ALL active profiles
    are parked is the VPN switched — ONCE — after which everyone resumes together.

    Per-profile usage (drive each profile task like this):
        await gate.join()
        try:
            for item in items:
                await gate.checkpoint(label)   # park here if a switch is pending
                ...                            # work; may call: await gate.switch(label)
        finally:
            await gate.leave()
    """

    def __init__(self, do_switch=None):
        # do_switch: async callable performing the real switch; defaults to
        # rotating via set_vpn() in a worker thread (it's blocking).
        self._do_switch = do_switch or (lambda: asyncio.to_thread(set_vpn))
        self._active = 0     # profiles still working
        self._parked = 0     # profiles currently waiting at the barrier
        self._want = False   # a switch has been requested
        self._gen = 0        # bumped each time a switch completes (releases a cohort)
        self._cond = asyncio.Condition()

    async def join(self):
        async with self._cond:
            self._active += 1

    async def leave(self):
        async with self._cond:
            self._active -= 1
            # A profile finishing may complete the barrier for the rest.
            await self._maybe_switch("(leaving)")
            self._cond.notify_all()

    async def checkpoint(self, label=""):
        """Safe point between items: park here if a coordinated switch is pending."""
        async with self._cond:
            if self._want:
                await self._park(label)

    async def switch(self, label=""):
        """Request a coordinated VPN switch and return once it has happened."""
        async with self._cond:
            if not self._want:
                _log(f"[{label}] ⚠ requesting a coordinated VPN switch — "
                     f"pausing all profiles at their next safe point…")
            self._want = True
            await self._park(label)

    async def _park(self, label):
        # caller holds the lock
        self._parked += 1
        if await self._maybe_switch(label):
            return
        gen = self._gen
        while self._gen == gen and self._want:
            await self._cond.wait()

    async def _maybe_switch(self, label):
        # caller holds the lock; returns True if THIS caller performed the switch
        if self._want and self._active > 0 and self._parked >= self._active:
            _log(f"[{label}] all {self._active} active profile(s) parked — switching VPN now.")
            try:
                await self._do_switch()
            finally:
                self._want = False
                self._parked = 0
                self._gen += 1
                self._cond.notify_all()
            return True
        return False
