"""chat_engine.py — the shared ChatGPT-driving core, parameterized by a "regime".

Two regimes share one engine instead of forking the browser code per workflow:

  rotating   the original behavior: a POOL of profiles (config/chrome-profiles/*; login state
             is not guaranteed),
             one tab per profile, VPN rotation + cookie clearing, plain new chats.
             Used by the appearance / finding / enriching workflows.
  logged_in  an ordered set of signed-in profiles, with one or more working tabs per login,
             no VPN and no cookie clearing. Chats can optionally open inside each account's
             configured ChatGPT Project on a chosen model.
             Used by current Project-based research workflows.

The numbered entry scripts (1_run_gpt_prompts.py, …) stay thin: they load prompts/config,
pick a regime, and call run(). The Playwright primitives below are lifted verbatim from the
original 1_run_gpt_prompts.py so the `rotating` regime is behavior-identical.

NOTE: this module keeps a plain name (no number prefix) precisely so the numbered entry
scripts can `import chat_engine` (Python can't import a module whose name starts with a digit).
"""

import os
import re
import time
import json
import math
import random
import asyncio
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from clinic_geo.browser import set_vpn
from clinic_geo.browser.browser_health import (
    cdp_endpoint_health,
    classify_browser_transport,
)
from clinic_geo.browser.human_input import split_typed_lead
from clinic_geo.browser.prompt_timing import PromptTimingRecorder
from clinic_geo.browser.profile_pool import (
    LoggedInProfile,
    ProfileAnchor,
    _browser_context_id,
    fallback_profile_id,
    open_profile_page,
    pair_profile_anchors,
)
from clinic_geo.env import env_int, load_env_file  # noqa: F401  (re-exported for the runners)
from clinic_geo.persistence.outputs import write_answer_file


def log(msg: str):
    print(msg, flush=True)


# Optional clean per-item outcome log (one line per clinic). Enabled by setting OUTCOME_LOG to a
# file path; the workflows point it at e.g. data/clinic-analysis/outcomes.log. Separate from the
# verbose run log so you can see at a glance who analysed cleanly, who hit "too many chats", who erred.
_OUTCOME_LOG_PATH = os.getenv("OUTCOME_LOG") or None


def outcome_log(item: str, status: str, detail: str = ""):
    """Append one tidy outcome line. No-op unless OUTCOME_LOG is set."""
    if not _OUTCOME_LOG_PATH:
        return
    try:
        line = f"{item}\t{status}"
        if detail:
            line += f"\t{detail}"
        with open(_OUTCOME_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


# === regimes ===
# A regime is the "how do we run" axis only (profile strategy + VPN + cookies). What chat to
# open (plain new chat vs a Project on a chosen model) is a SEPARATE concern — EngineConfig.target —
# so e.g. extraction can run logged_in with plain chats while the analysis stage runs logged_in
# inside a Project on "Pro Extended".
@dataclass(frozen=True)
class Regime:
    name: str
    fanout: str          # "profile_per_tab" (pool) | "tabs_in_one_profile" (signed-in set) | "ephemeral_guest"
    vpn: bool            # rotate the VPN + coordinate switches across profiles
    clear_cookies: bool  # purge non-essential cookies between prompts (look fresh)


REGIMES = {
    "rotating": Regime("rotating", fanout="profile_per_tab", vpn=True, clear_cookies=True),
    "logged_in": Regime("logged_in", fanout="tabs_in_one_profile", vpn=False, clear_cookies=False),
}


def resolve_regime(name: Optional[str] = None) -> Regime:
    key = (name or os.getenv("REGIME") or "rotating").strip()
    if key not in REGIMES:
        raise SystemExit(f"✗ Unknown regime '{key}'. Known regimes: {', '.join(REGIMES)}")
    return REGIMES[key]


def resolve_human_like() -> bool:
    """Single source of truth for whether to drive ChatGPT like a person (typing with cadence,
    mouse moves, reading/think pauses). CONSISTENT across the whole codebase: on by default for
    every regime and target. Set HUMAN_LIKE=0 to disable globally (e.g. a fast bulk pass).
    Controls PACING only — it does not affect how many tabs are opened."""
    env = os.getenv("HUMAN_LIKE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return True


def load_logged_in_profiles(config_path: Path):
    """Read ordered logged-in rows from ``logged-in-profiles.json``.

    Canonical rows expose ``id``, ``display_name`` and ``chrome_directory``.
    Legacy ``profile``/``label`` rows remain readable and receive the same
    deterministic fallback id as :mod:`clinic_geo.browser.profile_pool`.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            chrome_directory = str(
                d.get("chrome_directory") or d.get("profile") or ""
            ).strip()
            if not chrome_directory:
                continue
            display_name = str(
                d.get("display_name")
                or d.get("label")
                or d.get("name")
                or chrome_directory
            ).strip()
            profile_id = str(d.get("id") or "").strip() or fallback_profile_id(
                display_name,
                chrome_directory,
            )
            out.append({
                "id": profile_id,
                "display_name": display_name or chrome_directory,
                "chrome_directory": chrome_directory,
                "project_url": (str(d.get("project_url") or "").strip() or None),
            })
    return out


@dataclass
class EngineConfig:
    output_dir: Path
    profiles_dir: Path
    cdp_url: str
    chatgpt_url: str = "https://chatgpt.com/"
    target: str = "new_chat"                  # "new_chat" | "project" (open a Project + select a model)
    project_url: Optional[str] = None         # target="project"
    model_label: Optional[str] = None         # target="project"
    new_chat_model: Optional[str] = None       # target="new_chat": opt-in model/effort to pick (e.g. "Instant")
    num_profiles: int = 1                     # rotating
    max_per_profile: Optional[int] = None     # rotating cap (None = run every given entry, e.g. --fill)
    tabs: int = 3                             # logged_in: cap on TOTAL concurrent tabs (ANALYSIS_TABS)
    tabs_per_profile: int = 1                 # logged_in: concurrent tabs PER logged-in profile
    logged_in_profiles: Optional[list] = None # [{id, display_name, chrome_directory, project_url}]
    response_timeout: int = 240
    use_vpn: bool = True                      # honored only if the regime rotates
    rotate_vpn_on_start: bool = True          # main run rotates up front; --fill rotates between rounds itself
    human_like: bool = False                  # type/click/pace like a person (see human-behavior block)
    # Page-audit lane (ADR-002) opt-in ONLY. Default False keeps every existing
    # workflow byte-identical. When True, a ChatGPT rate-limit notice surfaces as
    # a terminal item failure instead of triggering the engine's internal
    # escalating wait/retry — the assist wave scheduler owns the long delays and
    # retries the item in a later wave (never a long sleep inside this engine).
    assist_no_rate_limit_retry: bool = False
    # Page-audit harvest-later opt-in ONLY. Default False keeps every existing
    # workflow byte-identical. When True, the engine stops right after the send
    # click: it records the new conversation's permanent /c/<id> URL into a
    # `*.sent.json` sidecar in output_dir and returns WITHOUT waiting for the
    # response or writing an answer file. The caller harvests the answer later
    # by revisiting that URL (long "ultra" generations finish server-side).
    send_only: bool = False


# === coordination state (single process, as before) ===
_new_tab_lock = None
_cookie_lock = None
_vpn_gate = None

ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
USER_SELECTOR = '[data-message-author-role="user"]'
STOP_SELECTOR = (
    'button[data-testid="stop-button"], '
    'button[aria-label="Stop streaming"], '
    'button[aria-label="Stop generating"]'
)
SEND_SELECTORS = (
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send message"]',
)

_CONVERSATION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PROJECT_ID_RE = re.compile(r"^g-p-[0-9a-f]{32}$")
_PROJECT_SUFFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROJECT_PATH_PART_RE = re.compile(r"^[a-z0-9_-]+$")


def _configured_project_id(project_url: str) -> str:
    """Return the exact Project id encoded by a configured Project URL."""

    if not isinstance(project_url, str) or project_url != project_url.strip():
        raise ValueError("configured Project URL must be a canonical string")
    try:
        parsed = urlsplit(project_url)
    except ValueError as exc:
        raise ValueError("configured Project URL is malformed") from exc
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "chatgpt.com"
        or parsed.query
        or parsed.fragment
        or len(parts) < 2
        or parts[0] != "g"
        or not _PROJECT_ID_RE.fullmatch(parts[1])
    ):
        raise ValueError("configured Project URL has no strict chatgpt.com Project id")
    return parts[1]


def validate_sent_conversation_reference(
    conversation_url: object,
    *,
    project_url: Optional[str] = None,
    prompt_id: object = None,
    expected_prompt_id: Optional[str] = None,
    sent_at: object = None,
    require_sidecar_metadata: bool = False,
) -> dict:
    """Validate and canonicalize the durable send/harvest handoff.

    Legacy sends are bound to a plain ``/c/<uuid>`` path. Project sends are
    bound to the exact ``g-p-<id>`` from the configured Project URL; ChatGPT's
    observed conversation slug may append a lower-case suffix such as
    ``-audit-run`` to that exact id. Query strings and fragments are discarded
    only after the origin, path, Project binding, and lowercase UUID verify.

    When ``require_sidecar_metadata`` is true, the sidecar's prompt id must be
    byte-for-byte equal to the expected id and its timestamp must be finite.
    Booleans are never timestamps even though Python treats them as integers.
    """

    if (
        not isinstance(conversation_url, str)
        or not conversation_url
        or conversation_url != conversation_url.strip()
    ):
        raise ValueError("conversation URL must be a canonical string")
    try:
        parsed = urlsplit(conversation_url)
    except ValueError as exc:
        raise ValueError("conversation URL is malformed") from exc
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        raise ValueError("conversation URL must use https://chatgpt.com")

    parts = [part for part in parsed.path.split("/") if part]
    if project_url:
        configured_id = _configured_project_id(project_url)
        if len(parts) < 4 or parts[0] != "g" or parts[-2] != "c":
            raise ValueError("conversation URL is not a Project conversation")
        landed_project = parts[1]
        if landed_project == configured_id:
            pass
        elif landed_project.startswith(configured_id + "-"):
            suffix = landed_project[len(configured_id) + 1 :]
            if not _PROJECT_SUFFIX_RE.fullmatch(suffix):
                raise ValueError("conversation URL has an invalid Project suffix")
        else:
            raise ValueError("conversation URL is bound to a different Project")
        if any(not _PROJECT_PATH_PART_RE.fullmatch(part) for part in parts[2:-2]):
            raise ValueError("conversation URL has an invalid Project path")
    else:
        if len(parts) != 2 or parts[0] != "c":
            raise ValueError("legacy conversation URL must be plain /c/<uuid>")

    conversation_id = parts[-1] if parts else ""
    if not _CONVERSATION_UUID_RE.fullmatch(conversation_id):
        raise ValueError("conversation URL has no lowercase UUID")
    canonical_path = "/" + "/".join(parts)
    if parsed.path not in {canonical_path, canonical_path + "/"}:
        raise ValueError("conversation URL path is not canonical")
    canonical_url = urlunsplit(
        ("https", "chatgpt.com", canonical_path, "", "")
    )

    validated = {"conversation_url": canonical_url}
    if require_sidecar_metadata:
        if not isinstance(expected_prompt_id, str) or prompt_id != expected_prompt_id:
            raise ValueError("sidecar prompt id does not exactly match")
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
            raise ValueError("sidecar timestamp must be finite")
        try:
            sent_timestamp = float(sent_at)
        except (ValueError, OverflowError) as exc:
            raise ValueError("sidecar timestamp must be finite") from exc
        if not math.isfinite(sent_timestamp):
            raise ValueError("sidecar timestamp must be finite")
        validated.update(
            {
                "prompt_id": prompt_id,
                "sent_at": sent_timestamp,
            }
        )
    return validated


class PromptVerificationError(RuntimeError):
    """The complete frozen prompt could not be proven in the composer."""


class ChatGPTSubmissionError(RuntimeError):
    """ChatGPT could not safely perform or acknowledge one submission action."""

# === rate-limit handling ("too many requests" → wait, then resume) ===
# When ChatGPT shows a rate-limit notice instead of answering, the worker WAITS and retries the
# SAME item — with an ESCALATING backoff: 5 min, then 10, then 30, then 60. After the schedule is
# exhausted it gives up on that item (so --fill backfills it later).
#
# Three ways to configure it, in precedence order:
#   RATE_LIMIT_BACKOFF   the exact schedule, comma-separated seconds ("300,600,1800,3600")
#   RATE_LIMIT_WAIT_SEC + RATE_LIMIT_RETRIES   a first wait and a retry count; the schedule doubles
#                        from the first wait, capped at one hour
#   neither              the default schedule below
DEFAULT_RATE_LIMIT_BACKOFF = [300, 600, 1800, 3600]
MAX_RATE_LIMIT_WAIT = 3600


def _parse_backoff(raw):
    try:
        return [int(x.strip()) for x in (raw or "").split(",") if x.strip()]
    except ValueError:
        return []


def _resolve_backoff():
    explicit = _parse_backoff(os.getenv("RATE_LIMIT_BACKOFF"))
    if explicit:
        return explicit
    if not (os.getenv("RATE_LIMIT_WAIT_SEC") or os.getenv("RATE_LIMIT_RETRIES")):
        return list(DEFAULT_RATE_LIMIT_BACKOFF)
    wait = max(1, env_int("RATE_LIMIT_WAIT_SEC", DEFAULT_RATE_LIMIT_BACKOFF[0]))
    schedule = []
    for _ in range(max(1, env_int("RATE_LIMIT_RETRIES", len(DEFAULT_RATE_LIMIT_BACKOFF)))):
        schedule.append(min(wait, MAX_RATE_LIMIT_WAIT))
        wait *= 2
    return schedule


RATE_LIMIT_BACKOFF = _resolve_backoff()

# Phrases ChatGPT uses when it throttles a send. Matched case-insensitively against the page text.
# Kept specific to real rate-limit language so generic "something went wrong" overlays don't
# trigger a needless 5-minute wait (those still fall through to the normal skip/retry path).
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "you've sent too many messages",
    "you’ve sent too many messages",
    "you've reached your limit",
    "you’ve reached your limit",
    "you've reached our limit",
    "you’ve reached our limit",
    "you've hit the free plan limit",
    "you’ve hit the free plan limit",
    "you've hit your limit",
    "you’ve hit your limit",
    "please slow down",
    "slow down",
    "rate limit",
    "rate-limit",
    "too quickly",
    "making requests too quickly",
    "you're sending messages too quickly",
    "you’re sending messages too quickly",
)


class RateLimitError(Exception):
    """ChatGPT showed a 'too many requests' / rate-limit notice. The caller should wait
    and retry the same item rather than skip it. The message is the on-page text."""


async def rate_limit_message(page):
    """If the page is showing a rate-limit notice, return the exact line of text (for logging);
    otherwise None. Reads visible body text only — cheap and side-effect-free."""
    if page is None:
        return None
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 3000) : ''")
    except Exception:
        return None
    text = body or ""
    low = text.lower()
    for marker in _RATE_LIMIT_MARKERS:
        if marker in low:
            for line in text.splitlines():
                stripped = line.strip()
                if marker in stripped.lower() and stripped:
                    return stripped
            return marker
    return None


# A ChatGPT Project caps how many chats it can hold. "Too many chats" is NOT a rate limit (waiting
# won't help) — it means the Project is full and someone must delete chats / use another project.
_CHATS_LIMIT_MARKERS = (
    "too many chats",
    "maximum number of chats",
    "reached the limit of chats",
    "reached the maximum number of conversations",
    "too many conversations",
    "delete some chats",
    "delete a chat to",
    "chat limit",
)


async def chats_limit_message(page):
    """If the page shows a Project 'too many chats' / chat-limit notice, return the matched line."""
    if page is None:
        return None
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 3000) : ''")
    except Exception:
        return None
    text = body or ""
    low = text.lower()
    for marker in _CHATS_LIMIT_MARKERS:
        if marker in low:
            for line in text.splitlines():
                stripped = line.strip()
                if marker in stripped.lower() and stripped:
                    return stripped
            return marker
    return None


# === human-like behavior (enabled per EngineConfig.human_like) ===
# The goal is to make a logged-in session look like a person at the keyboard rather than a
# script firing instant fills + sends in lockstep: type with a varying cadence (Shift+Enter for
# newlines so the message isn't sent early), move the mouse to things before clicking, pause to
# "read" and "think", randomise the send method and the order of work, and stagger the tabs.
# All timings are tunable via env so the pace can be dialed up or down without code changes.
HUMAN_INPUT_MODE = (os.getenv("HUMAN_INPUT_MODE", "type").strip().lower() or "type")  # "type" | "lead_append" | "paste"
HUMAN_TYPE_CPS = float(os.getenv("HUMAN_TYPE_CPS", "24") or 24)            # avg chars/sec while typing
HUMAN_TYPE_MAX_SECONDS = float(os.getenv("HUMAN_TYPE_MAX_SECONDS", "75") or 75)  # cap typing time/prompt
HUMAN_TYPED_LEAD_MAX_CHARS = int(os.getenv("HUMAN_TYPED_LEAD_MAX_CHARS", "42") or 42)
HUMAN_THINK_MIN = float(os.getenv("HUMAN_THINK_MIN", "2.5") or 2.5)        # gap between prompts (low)
HUMAN_THINK_MAX = float(os.getenv("HUMAN_THINK_MAX", "8.0") or 8.0)        # gap between prompts (high)
HUMAN_READ_MAX = float(os.getenv("HUMAN_READ_MAX", "9.0") or 9.0)          # cap "reading the answer" time
HUMAN_START_STAGGER = float(os.getenv("HUMAN_START_STAGGER", "9.0") or 9.0)  # max stagger before a tab's 1st item
# Open each logged_in tab this many seconds AFTER the previous one (tab N waits N×this before
# opening), so the tabs don't all appear/connect at once. Default 60s = "at least a minute apart".
TAB_OPEN_STAGGER_SEC = float(os.getenv("TAB_OPEN_STAGGER_SEC", "60") or 60)
TAB_OPEN_PROFILE_OFFSET_SEC = float(
    os.getenv("TAB_OPEN_PROFILE_OFFSET_SEC", "0.5") or 0.5
)
# Recycle (close + reopen) a logged_in tab after this many chats, to flush the memory ChatGPT's
# single-page app slowly accumulates over hundreds of conversations (the long-run OOM cause).
# 0 disables. Keeps FULL prompt content — it's a memory flush, not a content cut.
TAB_RECYCLE_EVERY = int(os.getenv("TAB_RECYCLE_EVERY", "15") or 15)
HUMAN_BREAK_CHANCE = float(os.getenv("HUMAN_BREAK_CHANCE", "0.06") or 0.06)  # chance of a longer pause between items
COMPOSER_VERIFY_TIMEOUT_SECONDS = 5.0
SUBMISSION_ACK_TIMEOUT_SECONDS = 5.0


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().casefold() in {"1", "true", "yes", "on"}


def _collection_flag(name: str) -> bool:
    return (
        str(os.getenv("SNAPSHOT_STAGE") or "").strip() == "collection"
        and _env_flag(name)
    )


def _typed_lead_max_chars() -> int:
    """Resolve the one-value fast-input canary without changing typing speed."""

    if _collection_flag("SNAPSHOT_CHATGPT_FAST_INPUT_V1"):
        return max(
            12,
            env_int("SNAPSHOT_CHATGPT_FAST_TYPED_LEAD_MAX_CHARS", 24),
        )
    return max(12, HUMAN_TYPED_LEAD_MAX_CHARS)


def _tab_open_delay(worker_index: int, profile_index: int, lane_index: int) -> tuple[float, str]:
    """Return the default global or collection-canary tab-open delay."""

    if _collection_flag("SNAPSHOT_CHATGPT_PROFILE_LANE_STAGGER_V1"):
        return (
            max(0.0, lane_index * TAB_OPEN_STAGGER_SEC)
            + max(0.0, profile_index * TAB_OPEN_PROFILE_OFFSET_SEC),
            "profile_lane_v1",
        )
    return max(0.0, worker_index * TAB_OPEN_STAGGER_SEC), "global_worker_index"


async def human_pause(lo, hi):
    await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))


async def human_move_to(page, locator):
    """Glide the mouse toward a locator over several steps (a human reaches, not teleports)."""
    try:
        box = await locator.bounding_box()
        if not box:
            return
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        await page.mouse.move(tx, ty, steps=random.randint(8, 22))
    except Exception:
        pass


async def human_click(page, locator):
    await human_move_to(page, locator)
    await human_pause(0.05, 0.25)
    await locator.click()


async def _type_chars(page, text, cps=None):
    """Press `text` into the focused element key-by-key with a human cadence: jittered per-key
    delay, small pauses after punctuation, the odd hesitation. Newlines use Shift+Enter so the
    message isn't sent early."""
    base = 1.0 / max(cps or HUMAN_TYPE_CPS, 5.0)
    for ch in text:
        if ch == "\n":
            await page.keyboard.press("Shift+Enter")
        else:
            await page.keyboard.type(ch)
        d = base * random.uniform(0.55, 1.7)
        if ch in ".!?":
            d += random.uniform(0.12, 0.5)
        elif ch in ",;:":
            d += random.uniform(0.04, 0.18)
        elif ch == " " and random.random() < 0.06:
            d += random.uniform(0.08, 0.3)
        if random.random() < 0.008:                          # rare "thinking" hesitation
            d += random.uniform(0.4, 1.4)
        await asyncio.sleep(d)


async def human_type(page, locator, text):
    """Type `text` into `locator` with a person's cadence. Falls back to fill() on any error so a
    single hiccup never drops the prompt."""
    cps = HUMAN_TYPE_CPS
    if len(text) / max(cps, 1.0) > HUMAN_TYPE_MAX_SECONDS:   # keep very long prompts within budget
        cps = len(text) / HUMAN_TYPE_MAX_SECONDS
    try:
        await _type_chars(page, text, cps)
    except Exception:
        try:
            await locator.fill(text)
        except Exception:
            pass


async def human_type_lead_and_append(page, locator, prompt):
    """Type the opening naturally, then append the untouched remainder once."""

    lead, remainder = split_typed_lead(
        prompt,
        single_sentence_max_chars=_typed_lead_max_chars(),
    )
    cps = HUMAN_TYPE_CPS
    if len(lead) / max(cps, 1.0) > HUMAN_TYPE_MAX_SECONDS:
        cps = len(lead) / HUMAN_TYPE_MAX_SECONDS
    try:
        await _type_chars(page, lead, cps)
        if remainder:
            await human_pause(0.18, 0.55)
            await page.keyboard.insert_text(remainder)
    except Exception:
        await locator.fill(prompt, timeout=120000)


async def human_chunked_fill(page, locator, prompt):
    """Enter a LARGE prompt the way a person assembles a long message: a RANDOM mix of short
    key-by-key typed bursts and pasted chunks, with human pauses between. The amount typed, the
    number/placement of typed bursts, and whether it even starts by typing all vary PER PROMPT —
    so each chat's input takes a different length of time and the parallel tabs naturally desync
    and send at different moments (no synchronised burst that reads as 'requests too quickly').
    Only a few hundred chars are ever typed (the bulk is pasted) so it stays fast at 50k+ chars."""
    n = len(prompt)
    pos = 0
    typed_budget = random.randint(120, 700)                  # total chars typed THIS prompt (varies)
    typed_so_far = 0
    start_typing = random.random() < 0.7                     # most, not all, start by typing
    try:
        first = True
        while pos < n:
            remaining = n - pos
            want_type = (typed_so_far < typed_budget and remaining > 40 and
                         ((first and start_typing) or (not first and random.random() < 0.45)))
            first = False
            if want_type:
                seg = min(remaining, typed_budget - typed_so_far, random.randint(40, 200))
                await _type_chars(page, prompt[pos:pos + seg]); pos += seg; typed_so_far += seg
                await human_pause(0.4, 1.6)
            else:
                chunk = max(1, min(remaining, int(remaining * random.uniform(0.4, 0.85))))
                await page.keyboard.insert_text(prompt[pos:pos + chunk]); pos += chunk
                await human_pause(0.3, 1.2)
    except Exception:
        try:                                                 # ensure the prompt ends up complete
            await page.keyboard.insert_text(prompt[pos:])
        except Exception:
            try:
                await locator.fill(prompt)
            except Exception:
                pass


async def human_fill_prompt(
    page,
    locator,
    prompt,
    *,
    reject_cookie_consent=False,
):
    """Put the prompt into the composer the way a person would: click in, then type an opening
    and append the rest (default), or use an explicitly selected legacy input mode.

    Paste uses keyboard.insert_text (a single CDP insertText at the focused composer) rather than
    locator.fill(): fill() clears-then-types and is capped by the page's default timeout, which a
    very large prompt (100k+ chars in ChatGPT's rich-text editor) blows past. insert_text drops the
    whole block in one event with no per-op timeout. Falls back to fill() if insert_text errors."""
    # The conversation-history notice may be injected a moment after the page
    # and composer become ready, so dismiss it at the final interaction point
    # as well as during page setup.
    await _dismiss_onboarding(
        page,
        reject_cookie_consent=reject_cookie_consent,
    )
    await human_click(page, locator)
    await human_pause(0.25, 0.9)
    if HUMAN_INPUT_MODE == "chunked":
        await locator.click()                          # ensure the composer is focused
        await human_chunked_fill(page, locator, prompt)
        await human_pause(0.4, 1.2)
    elif HUMAN_INPUT_MODE == "paste":
        try:
            await _dismiss_onboarding(
                page,
                reject_cookie_consent=reject_cookie_consent,
            )
            await locator.click()                      # ensure the composer is focused
            await page.keyboard.insert_text(prompt)
        except Exception:
            await locator.fill(prompt, timeout=120000)
        await human_pause(0.4, 1.2)
    elif HUMAN_INPUT_MODE in {"type", "lead_append"}:
        await human_type_lead_and_append(page, locator, prompt)
    else:
        await human_type(page, locator, prompt)


def _timing_mark(timing, event: str) -> None:
    try:
        if timing is not None:
            timing.mark(event)
    except Exception:
        pass


def _timing_field(timing, name: str, value) -> None:
    try:
        if timing is not None:
            timing.field(name, value)
    except Exception:
        pass


def _timing_finish(timing, status: str, error: BaseException | type | None = None) -> None:
    try:
        if timing is not None:
            timing.finish(status, error)
    except Exception:
        pass


def _new_prompt_timing(
    prompt_id: str,
    *,
    profile_id: str = "unknown",
    lane_index: int = 0,
    configured_x: int = 1,
    worker_index: int = 0,
):
    try:
        return PromptTimingRecorder.from_env(
            platform="chatgpt",
            prompt_id=prompt_id,
            profile_id=profile_id,
            lane_index=lane_index,
            configured_x=configured_x,
            worker_index=worker_index,
        )
    except Exception:
        return None


def _normalise_composer_text(value: object) -> str:
    """Normalise only browser-DOM representation differences."""

    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .strip()
    )


async def _locator_value(locator, method_name: str):
    method = getattr(locator, method_name, None)
    if method is None:
        raise AttributeError(method_name)
    try:
        return await method(timeout=2000)
    except TypeError:
        return await method()


async def read_composer_text(composer) -> Optional[str]:
    """Read textarea/input values or rich contenteditable rendered text."""

    kind = None
    evaluate = getattr(composer, "evaluate", None)
    if evaluate is not None:
        try:
            kind = await evaluate(
                """el => {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag === 'textarea' || tag === 'input') return 'value';
                    if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') return 'text';
                    return 'unknown';
                }"""
            )
        except Exception:
            kind = None

    methods = (
        ("input_value",) if kind == "value"
        else ("inner_text", "text_content") if kind == "text"
        else ("input_value", "inner_text", "text_content")
    )
    for method_name in methods:
        if getattr(composer, method_name, None) is None:
            continue
        try:
            value = await _locator_value(composer, method_name)
        except Exception:
            continue
        if value is not None:
            return str(value)
    return None


async def _wait_for_exact_composer(
    composer,
    prompt: str,
    *,
    timeout_seconds: float = COMPOSER_VERIFY_TIMEOUT_SECONDS,
) -> bool:
    expected = _normalise_composer_text(prompt)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        actual = await read_composer_text(composer)
        if actual is not None and _normalise_composer_text(actual) == expected:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.1, remaining))


async def verify_composer_prompt(
    composer,
    prompt: str,
    *,
    timeout_seconds: float = COMPOSER_VERIFY_TIMEOUT_SECONDS,
) -> None:
    """Verify exact input, repairing the entire value at most once before send."""

    if await _wait_for_exact_composer(
        composer,
        prompt,
        timeout_seconds=timeout_seconds,
    ):
        return
    await composer.fill(prompt)
    if await _wait_for_exact_composer(
        composer,
        prompt,
        timeout_seconds=timeout_seconds,
    ):
        return
    raise PromptVerificationError(
        "ChatGPT composer did not retain the complete frozen prompt after one exact repair"
    )


async def _locator_count(page, selector: str) -> int:
    return int(await page.locator(selector).count())


async def _locator_visible(locator) -> bool:
    visible = getattr(locator, "is_visible", None)
    if visible is None:
        return False
    return bool(await visible())


async def _stop_visible(page) -> bool:
    locator = page.locator(STOP_SELECTOR)
    if await locator.count() <= 0:
        return False
    try:
        return await _locator_visible(locator.last)
    except Exception:
        return False


async def _visible_enabled_send_control(page):
    """Return an enabled Send button, or fail closed if a Send control exists disabled."""

    saw_control = False
    for selector in SEND_SELECTORS:
        locator = page.locator(selector).last
        visible = False
        try:
            await locator.wait_for(state="visible", timeout=3000)
            visible = True
            saw_control = True
        except PlaywrightTimeoutError:
            try:
                saw_control = saw_control or await locator.count() > 0
            except Exception:
                pass
            continue
        except (AttributeError, TypeError):
            try:
                exists = await locator.count() > 0
                saw_control = saw_control or exists
                visible = exists and await _locator_visible(locator)
            except Exception:
                continue
        if not visible:
            continue
        enabled = getattr(locator, "is_enabled", None)
        try:
            if enabled is not None and not await enabled():
                continue
        except Exception as exc:
            raise ChatGPTSubmissionError(
                "Could not establish that the visible ChatGPT Send control is enabled"
            ) from exc
        return locator
    if saw_control:
        raise ChatGPTSubmissionError(
            "ChatGPT Send control exists but is not both visible and enabled"
        )
    return None


async def _submission_acknowledged(
    page,
    composer,
    *,
    before_user_count: int,
    before_assistant_count: int,
    before_stop_visible: bool,
    timeout_seconds: float = SUBMISSION_ACK_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        value = await read_composer_text(composer)
        if value is not None and not _normalise_composer_text(value):
            return True
        if await _locator_count(page, USER_SELECTOR) > before_user_count:
            return True
        if await _locator_count(page, ASSISTANT_SELECTOR) > before_assistant_count:
            return True
        if not before_stop_visible and await _stop_visible(page):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.1, remaining))


async def submit_verified_prompt_once(
    page,
    composer,
    prompt: str,
    *,
    human_like: bool,
    timing=None,
    acknowledgement_timeout_seconds: float = SUBMISSION_ACK_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    """Execute exactly one submission action and require visible turn acknowledgement."""

    before_assistant_count = await _locator_count(page, ASSISTANT_SELECTOR)
    before_user_count = await _locator_count(page, USER_SELECTOR)
    before_composer = await read_composer_text(composer)
    before_stop_visible = await _stop_visible(page)
    if (
        before_composer is None
        or _normalise_composer_text(before_composer)
        != _normalise_composer_text(prompt)
    ):
        raise PromptVerificationError(
            "ChatGPT composer changed after verification and before submission"
        )

    send = await _visible_enabled_send_control(page)
    if send is not None:
        _timing_mark(timing, "send_control_ready")
        if human_like:
            await human_move_to(page, send)
            await human_pause(0.05, 0.22)
        method = "button"
        action_composer = await read_composer_text(composer)
        if (
            action_composer is None
            or _normalise_composer_text(action_composer)
            != _normalise_composer_text(prompt)
        ):
            raise PromptVerificationError(
                "ChatGPT composer changed while waiting for the Send control"
            )
        _timing_mark(timing, "send_action")
        try:
            # This invocation is the action boundary. If it raises after dispatch,
            # deliberately do not attempt another click or an Enter fallback.
            await send.click()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChatGPTSubmissionError(
                f"ChatGPT button submission failed after its single action: {type(exc).__name__}"
            ) from exc
    else:
        method = "enter"
        action_composer = await read_composer_text(composer)
        if (
            action_composer is None
            or _normalise_composer_text(action_composer)
            != _normalise_composer_text(prompt)
        ):
            raise PromptVerificationError(
                "ChatGPT composer changed before the Enter submission action"
            )
        _timing_mark(timing, "send_action")
        try:
            press = getattr(composer, "press", None)
            if press is not None:
                await press("Enter")
            else:
                await page.keyboard.press("Enter")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChatGPTSubmissionError(
                f"ChatGPT Enter submission failed after its single action: {type(exc).__name__}"
            ) from exc

    if not await _submission_acknowledged(
        page,
        composer,
        before_user_count=before_user_count,
        before_assistant_count=before_assistant_count,
        before_stop_visible=before_stop_visible,
        timeout_seconds=acknowledgement_timeout_seconds,
    ):
        raise ChatGPTSubmissionError(
            f"ChatGPT did not acknowledge its single {method} submission; it was not repeated"
        )
    _timing_mark(timing, "submission_acknowledged")
    _timing_field(timing, "submission_action_method", method)
    return method, before_assistant_count


async def human_send(page):
    """Send the message the way people do — usually clicking the Send button (after moving the
    mouse to it), sometimes just pressing Enter."""
    if random.random() < 0.7:
        for selector in ('button[data-testid="send-button"]', 'button[aria-label="Send prompt"]',
                         'button[aria-label="Send message"]'):
            loc = page.locator(selector).last
            try:
                await loc.wait_for(state="visible", timeout=3000)
                if await loc.is_enabled():
                    await human_move_to(page, loc)
                    await human_pause(0.05, 0.22)
                    await loc.click()
                    return
            except Exception:
                pass
    await page.keyboard.press("Enter")


async def human_read(page, answer):
    """Pause as if reading the reply — loosely scaled to its length, capped — with a little scroll."""
    t = random.uniform(1.2, 3.0) + min(len(answer or "") / 700.0, HUMAN_READ_MAX)
    try:
        await page.mouse.wheel(0, random.randint(200, 700))
    except Exception:
        pass
    await human_pause(t * 0.5, t)


def load_profile_names(profiles_dir: Path):
    """Read Chrome directories from descriptive or legacy metadata snapshots.

    Current files use ordered, identity-bearing names such as
    ``01_ortemksl_personal__profile_7.txt``. Older job snapshots containing
    contiguous ``profileN.txt`` files remain supported.
    """

    names = []
    files = sorted(profiles_dir.glob("[0-9][0-9]_*.txt"))
    if not files:
        files = []
        i = 1
        while True:
            path = profiles_dir / f"profile{i}.txt"
            if not path.exists():
                break
            files.append(path)
            i += 1
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"Profile Path.*?/Chrome/(.+)", text)
        if m:
            names.append(m.group(1).strip())
    return names


# === browser primitives (lifted from the original engine) ===
async def find_composer(page):
    selectors = ['[data-testid="prompt-textarea"]', 'div[contenteditable="true"]', 'textarea']
    deadline = time.time() + 12
    while time.time() < deadline:
        for selector in selectors:
            loc = page.locator(selector).last
            try:
                await loc.wait_for(state="visible", timeout=1500)
                return loc
            except PlaywrightTimeoutError:
                continue
    raise RuntimeError("Could not find ChatGPT input box.")


async def click_send(page):
    for selector in ('button[data-testid="send-button"]', 'button[aria-label="Send prompt"]',
                     'button[aria-label="Send message"]'):
        loc = page.locator(selector).last
        try:
            await loc.wait_for(state="visible", timeout=3000)
            if await loc.is_enabled():
                await loc.click()
                return
        except Exception:
            pass
    await page.keyboard.press("Enter")


_KEEP_COOKIE_DOMAINS = (
    "chatgpt.com",
    "openai.com",
    "auth0.com",
    "oaiusercontent.com",
    "oaistatic.com",
    # The canonical Chrome pool is authenticated on every Snapshot platform.
    # A different rotating workflow must not silently destroy those sessions.
    "google.com",
    "google.co.uk",
    "googleusercontent.com",
    "microsoft.com",
    "microsoftonline.com",
    "live.com",
    "bing.com",
    "copilot.com",
)
_KEEP_COOKIE_NAME_PREFIXES = ("__Secure-next-auth", "__Host-", "cf_clearance", "__cf_bm")


async def clear_cookies(page):
    """Delete non-provider cookies while preserving configured AI sessions."""
    try:
        ctx = page.context
        async with _cookie_lock:
            cookies = await ctx.cookies()
            keep = []
            for c in cookies:
                domain = (c.get("domain") or "").lstrip(".").lower()
                name = c.get("name", "")
                if any(domain == d or domain.endswith("." + d) for d in _KEEP_COOKIE_DOMAINS):
                    keep.append(c)
                elif any(name.startswith(p) for p in _KEEP_COOKIE_NAME_PREFIXES):
                    keep.append(c)
            await ctx.clear_cookies()
            if keep:
                await ctx.add_cookies(keep)
    except Exception:
        pass


# The GPT-5 picker button has no stable testid/aria — it's a `button[aria-haspopup="menu"]`
# near the composer whose label is the current mode/effort (e.g. "Instant"). The options are
# role=menuitemradio ("Instant" / "Medium" / "High"), with model variants under a submenu.
_MODE_RE = r"^(instant|auto|thinking|medium|high|pro|gpt|fast|standard|extended|szybk|automat|myślen)"


async def _open_model_menu(page):
    """Open the model/effort menu. POLLS for up to ~9s, because under heavy concurrency the
    picker button can load late (a fixed 1s settle isn't enough). Returns True if it opened."""
    deadline = time.time() + 9
    while time.time() < deadline:
        for sel in ('button[data-testid="model-switcher-dropdown-button"]',
                    'button[aria-label*="model" i]', '[data-testid="model-switcher"] button'):
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=700)
                await btn.click()
                return True
            except Exception:
                continue
        # generic: a menu button near the composer whose text is a known mode/effort name
        try:
            handle = await page.evaluate_handle(
                "() => { const re=new RegExp(%r,'i');"
                " for (const b of document.querySelectorAll('button[aria-haspopup=\"menu\"]')) {"
                "  const t=(b.textContent||'').trim(); const r=b.getBoundingClientRect();"
                "  if (re.test(t) && r.left>500 && r.top>120) return b; } return null; }" % _MODE_RE)
            el = handle.as_element()
            if el:
                await el.click()
                return True
        except Exception:
            pass
        await asyncio.sleep(0.6)
    return False


async def _dismiss_onboarding(page, *, reject_cookie_consent=False):
    """Best-effort: close the intermittent 'Enable memory' onboarding modal (and similar dialogs),
    whose full-screen overlay otherwise intercepts the model-picker click. Only clicks a dismissive
    button ('Not now'/'Skip'/…), never a confirm — safe to call before opening the menu."""
    try:
        # Fresh logged-out ChatGPT tabs can receive the cookie dialog after the
        # initial page audit. Only the ephemeral evidence collector may handle
        # it here, and only through an explicitly dismissive choice.
        if reject_cookie_consent:
            for label in ("Reject non-essential", "Reject all"):
                try:
                    button = page.get_by_role(
                        "button",
                        name=re.compile(f"^{re.escape(label)}$", re.I),
                    ).first
                    await button.click(timeout=800)
                    break
                except Exception:
                    continue
        # ChatGPT can show a conversation-history rate-limit notice over an
        # otherwise ready composer. "Got it" only acknowledges that notice;
        # leaving it open makes every composer click time out.
        if await page.query_selector('[data-testid="modal-conversation-history-rate-limit"]'):
            try:
                await page.get_by_role("button", name=re.compile(r"^Got it$", re.I)).first.click(timeout=1200)
                return
            except Exception:
                pass
        if not await page.query_selector('[data-testid="modal-memory-onboarding"], [role="dialog"]'):
            return
        labels = ["Not now", "Skip", "Maybe later", "No thanks", "Close"]
        if reject_cookie_consent:
            labels[:0] = ["Reject non-essential", "Reject all"]
        for label in labels:
            try:
                btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                await btn.click(timeout=800)
                return
            except Exception:
                continue
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def select_model(page, label):
    """Open the model/effort menu and pick the option matching `label` (e.g. "Medium"/"High"
    reasoning effort). Returns True if the option was selected, False if it could not be
    (e.g. the page is throttled and the picker never loaded) — the caller skips & retries so
    no answer is ever saved on the wrong model."""
    if not label:
        return True
    await _dismiss_onboarding(page)
    if not await _open_model_menu(page):
        log(f"   ⚠ model switcher not found (page throttled?) — wanted '{label}'.")
        return False
    await asyncio.sleep(0.5)
    # the effort levels are menuitemradio; models are menuitem/option — try all roles.
    for role in ("menuitemradio", "menuitem", "option"):
        try:
            opt = page.get_by_role(role, name=re.compile(re.escape(label), re.I)).first
            await opt.wait_for(state="visible", timeout=1500)
            await opt.click()
            return True
        except Exception:
            continue
    # maybe nested under a model submenu (e.g. a GPT-x entry -> effort) — expand then retry
    for more in ("Thinking", "GPT", "More models", "More", "Effort", "Model", "Advanced"):
        try:
            sub = page.get_by_role("menuitem", name=re.compile(more, re.I)).first
            await sub.wait_for(state="visible", timeout=1000)
            await sub.hover()
            await asyncio.sleep(0.5)
            opt = page.get_by_role("menuitemradio", name=re.compile(re.escape(label), re.I)).first
            await opt.wait_for(state="visible", timeout=1000)
            await opt.click()
            return True
        except Exception:
            continue
    log(f"   ⚠ could not select '{label}'.")
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def extract_answer(page):
    """Return the final answer text, excluding any reasoning/thinking block.

    When ChatGPT web-searches, it linkifies URLs in the answer into inline source/citation
    "cards" whose VISIBLE text is the page title (e.g. "Top-Rated.Online Travel"), not the URL.
    A plain inner_text then splices those titles into the JSON and corrupts it. So we read the
    rendered text from a CLONE in which every <a> is replaced by its href (the real URL) and
    pure-UI bits (buttons/svg/footnote <sup>) are dropped. Pure DOM — works even when the window
    is hidden. Falls back to the old inner_text path if the JS read yields nothing."""
    assistant = page.locator(ASSISTANT_SELECTOR)
    count = await assistant.count()
    if count == 0:
        return ""
    try:
        text = await page.evaluate(
            """() => {
                const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
                if (!msgs.length) return '';
                const last = msgs[msgs.length - 1];
                const node = last.querySelector('.markdown') || last;
                const clone = node.cloneNode(true);
                clone.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || a.textContent || '';
                    a.replaceWith(document.createTextNode(href));
                });
                clone.querySelectorAll('button, svg, sup, [role="button"]').forEach(e => e.remove());
                return (clone.innerText || clone.textContent || '');
            }"""
        )
        if text and text.strip():
            return text.strip()
    except Exception:
        pass
    last = assistant.nth(count - 1)
    md = last.locator(".markdown")
    if await md.count() > 0:
        return (await md.last.inner_text()).strip()
    return (await last.inner_text()).strip()


async def wait_for_response_finished(
    page,
    before_count: int,
    timeout_seconds=240,
    start_timeout=90,
    *,
    timing=None,
):
    start = time.time()
    assistant = page.locator(ASSISTANT_SELECTOR)
    # Phase 1: generation must START within start_timeout. A rate-limited/blocked send produces
    # no assistant message at all, so this fails fast instead of waiting the full timeout.
    gen_deadline = min(timeout_seconds, start_timeout)
    while time.time() - start < gen_deadline:
        if await assistant.count() > before_count:
            _timing_mark(timing, "first_response_detected")
            break
        await asyncio.sleep(0.2)
    else:
        notice = await rate_limit_message(page)
        if notice:
            raise RateLimitError(notice)
        raise TimeoutError(f"No response started within {gen_deadline}s (blocked / rate-limited?).")
    stop_btn = page.locator(
        'button[data-testid="stop-button"], button[aria-label="Stop streaming"], button[aria-label="Stop generating"]')
    copy_btn = page.locator('button[data-testid="copy-turn-action-button"], button[aria-label="Copy"]')
    while time.time() - start < timeout_seconds:
        if await stop_btn.count() == 0:
            await asyncio.sleep(0.3)
            if await stop_btn.count() == 0 and await copy_btn.count() > 0:
                answer = await extract_answer(page)
                if answer:
                    _timing_mark(timing, "response_completed")
                    return answer
            if await stop_btn.count() == 0:
                await asyncio.sleep(0.4)
                if await stop_btn.count() == 0:
                    answer = await extract_answer(page)
                    if answer:
                        _timing_mark(timing, "response_completed")
                        return answer
        await asyncio.sleep(0.2)
    notice = await rate_limit_message(page)
    if notice:
        raise RateLimitError(notice)
    raise TimeoutError("Timed out waiting for ChatGPT response to finish.")


def write_output(output_dir: Path, global_index, name, prompt, metadata, answer):
    """The NNN_<name>.md answer-file contract, owned by clinic_geo.persistence.outputs."""
    return write_answer_file(output_dir, global_index, name, prompt, metadata, answer)


async def open_chat_tab(anchor, url):
    """Open an exact, uniquely tagged tab in the anchor's real Chrome profile.

    Separate provider subprocesses can open pages in the same merged Playwright
    context. Waiting for the next generic ``page`` event can therefore capture
    another provider's popup. ``open_profile_page`` resolves a unique marker and
    verifies the raw CDP browser-context id before this function navigates it.
    """

    if isinstance(anchor, ProfileAnchor):
        profile_anchor = anchor
    else:
        context_id = await _browser_context_id(anchor)
        if not context_id:
            raise RuntimeError("Could not identify the Chrome profile context for a new ChatGPT tab")
        profile_anchor = ProfileAnchor(
            identity=LoggedInProfile(
                id=fallback_profile_id("Chrome profile", context_id),
                display_name="Chrome profile",
                chrome_directory=context_id,
            ),
            page=anchor,
            browser_context_id=context_id,
        )
    lock = _new_tab_lock or asyncio.Lock()
    page = await open_profile_page(profile_anchor, lock=lock, timeout_seconds=20.0)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except BaseException:
        try:
            await asyncio.shield(page.close())
        except BaseException:
            pass
        raise
    return page


_ERROR_MARKERS = (
    "this page isn't working", "this page isn’t working", "http error",
    "is currently unable to handle", "bad gateway", "gateway time-out",
    "service unavailable", "site can’t be reached", "site can't be reached",
    "no internet", "err_",
)


async def page_is_broken(page):
    if page is None:
        return True
    try:
        url = (page.url or "").lower()
    except Exception:
        return True
    if not url or url.startswith("chrome-error://") or url.startswith("about:blank"):
        return True
    if "chatgpt.com" not in url:
        return True
    try:
        title = (await page.title()) or ""
    except Exception:
        return True
    try:
        body = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 600) : ''")
    except Exception:
        body = ""
    blob = (title + " " + (body or "")).lower()
    return any(m in blob for m in _ERROR_MARKERS)


async def reopen_profile(anchor, url, bad_page=None):
    if bad_page is not None:
        try:
            await bad_page.close()
        except Exception:
            pass
    try:
        await anchor.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        return await open_chat_tab(anchor, url)
    except Exception:
        return None


async def open_working_chat(anchor, url, label):
    """Open a chat tab and make sure it's the real ChatGPT page (rotating recovery:
    reopen the profile, then a COORDINATED VPN switch if it's still broken)."""
    try:
        page = await open_chat_tab(anchor, url)
    except Exception:
        page = None
    if not await page_is_broken(page):
        return page
    log(f"[{label}] ⚠ non-ChatGPT/error page — closing & reopening the profile.")
    page = await reopen_profile(anchor, url, page)
    if not await page_is_broken(page):
        return page
    if _vpn_gate is not None:
        log(f"[{label}] ⚠ still broken after reopen.")
        await _vpn_gate.switch(label)
        page = await reopen_profile(anchor, url, page)
    return page


async def collect_profile_pages(browser, want):
    """Return one ChatGPT page per profile window (rotating fan-out)."""
    pages = []
    for _ in range(20):
        pages = []
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "chatgpt.com" in (pg.url or ""):
                    pages.append(pg)
        if len(pages) >= want:
            return pages[:want]
        await asyncio.sleep(0.5)
    return pages


async def collect_chatgpt_pages(browser):
    """Return every open chatgpt.com page (logged_in fan-out uses pages[0] as the anchor)."""
    for _ in range(20):
        pages = [pg for ctx in browser.contexts for pg in ctx.pages if "chatgpt.com" in (pg.url or "")]
        if pages:
            return pages
        await asyncio.sleep(0.5)
    return []


# === per-prompt send ===
async def _record_sent_conversation(page, cfg, global_index, name):
    """send_only seam: persist the conversation's permanent URL, never wait.

    ChatGPT rewrites the address to /c/<uuid> shortly after the first message is
    sent; that URL survives tab closes and Chrome restarts, so a later harvest
    pass can revisit the finished answer. Failure to observe the rewrite is an
    error (the caller fails the item closed) — a sidecar is never guessed.
    """
    # ChatGPT first shows a client-side placeholder (/c/WEB:<uuid>) whose uuid is
    # NOT the server conversation id — a later visit to it 404s to home. Wait for
    # the server-acknowledged rewrite: a plain /c/<uuid> address. Fail closed if
    # it never appears; a placeholder must never be recorded as harvestable.
    real_id = re.compile(r"/c/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=$|[/?#])")
    url = ""
    validated = None
    for _ in range(60):  # <= ~30s
        url = page.url or ""
        if real_id.search(url):
            sent_at = time.time()
            project_mode = getattr(cfg, "target", "new_chat") == "project"
            if project_mode and not str(
                getattr(cfg, "project_url", "") or ""
            ).strip():
                raise ValueError(
                    "send_only Project mode requires a configured Project URL"
                )
            validated = validate_sent_conversation_reference(
                url,
                project_url=cfg.project_url if project_mode else None,
                prompt_id=str(name),
                expected_prompt_id=str(name),
                sent_at=sent_at,
                require_sidecar_metadata=True,
            )
            break
        await asyncio.sleep(0.5)
    if validated is None:
        raise RuntimeError(
            "send_only: server conversation URL never materialised after send "
            f"(last seen: {url!r})"
        )
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(name))[:80] or "prompt"
    sidecar = Path(cfg.output_dir) / f"{int(global_index):03d}_{safe}.sent.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(validated, indent=2),
        encoding="utf-8",
    )
    return sidecar


def _timing_error_status(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    return "transient_error"


async def _send_and_save(
    page,
    regime,
    cfg,
    global_index,
    name,
    prompt,
    metadata,
    *,
    navigate,
    nav_url=None,
    timing=None,
):
    # navigate=True when the tab is REUSED (logged_in) and must be reset to a fresh chat per item;
    # rotating opens a fresh tab per item, so its page is already a clean new chat (navigate=False).
    # nav_url is the per-profile URL to open (a Project URL for --project, else plain chatgpt.com).
    owns_timing = timing is None
    timing = timing or _new_prompt_timing(str(name))
    try:
        if navigate:
            _timing_mark(timing, "navigation_started")
            await page.goto(nav_url or cfg.chatgpt_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.0 if cfg.target == "project" else 0.5)
            _timing_mark(timing, "navigation_completed")
            if cfg.target == "project" and cfg.model_label:
                ok = await select_model(page, cfg.model_label)
                if not ok:
                    # The model picker failing to load is almost always an account/page throttle.
                    # Treat it like a rate limit so the worker WAITS and retries the same item rather
                    # than skipping (logged_in does only one fill round, so a skip here is permanent).
                    notice = await rate_limit_message(page)
                    raise RateLimitError(notice or f"model picker for '{cfg.model_label}' unavailable (page throttled)")
        # Plain new chats (opt-in via NEW_CHAT_MODEL): pick a model/effort here rather than in the
        # navigate block above, because the rotating regime opens a fresh tab (navigate=False) and would
        # otherwise never reach that block. No label set (the default for every other stream) => untouched
        # profile default. This is BEST-EFFORT: a free-tier account exposes only its single (instant-class)
        # model with no effort submenu, so the label legitimately won't be there — we warn and proceed on
        # the default rather than hanging. The real "Instant" effort exists only on logged-in Plus profiles
        # (--regime logged_in). Set NEW_CHAT_MODEL_STRICT=1 to hard-fail-and-retry instead (Plus + must-have).
        if cfg.target != "project" and cfg.new_chat_model:
            ok = await select_model(page, cfg.new_chat_model)
            if not ok:
                if env_int("NEW_CHAT_MODEL_STRICT", 0):
                    notice = await rate_limit_message(page)
                    raise RateLimitError(notice or f"model picker for '{cfg.new_chat_model}' unavailable (page throttled)")
                log(f"   ℹ '{cfg.new_chat_model}' not selectable here (free tier has one model) — using the default.")
        reject_cookie_consent = getattr(regime, "name", "") == "ephemeral_guest"
        await _dismiss_onboarding(
            page,
            reject_cookie_consent=reject_cookie_consent,
        )
        composer = await find_composer(page)
        _timing_mark(timing, "composer_ready")
        if cfg.human_like:
            await human_pause(0.6, 1.8)                  # glance at the freshly-loaded chat
            _timing_mark(timing, "input_started")
            await human_fill_prompt(
                page,
                composer,
                prompt,
                reject_cookie_consent=reject_cookie_consent,
            )
            _timing_mark(timing, "input_completed")
            _timing_field(timing, "input_mode", HUMAN_INPUT_MODE)
            await verify_composer_prompt(composer, prompt)
            _timing_mark(timing, "input_verified")
            if regime.clear_cookies:
                await clear_cookies(page)
            await human_pause(0.4, 1.6)                  # re-read the prompt before sending
            _, before = await submit_verified_prompt_once(
                page,
                composer,
                prompt,
                human_like=True,
                timing=timing,
            )
            if getattr(cfg, "send_only", False):
                sidecar = await _record_sent_conversation(page, cfg, global_index, name)
                _timing_mark(timing, "sent_detached")
                if owns_timing:
                    _timing_finish(timing, "sent_detached")
                return sidecar
            answer = await wait_for_response_finished(
                page,
                before_count=before,
                timeout_seconds=cfg.response_timeout,
                timing=timing,
            )
            await human_read(page, answer)               # "read" the reply before moving on
        else:
            _timing_mark(timing, "input_started")
            await composer.click()
            await composer.fill(prompt)
            _timing_mark(timing, "input_completed")
            _timing_field(timing, "input_mode", "atomic_fill")
            await verify_composer_prompt(composer, prompt)
            _timing_mark(timing, "input_verified")
            if regime.clear_cookies:
                await clear_cookies(page)
            _, before = await submit_verified_prompt_once(
                page,
                composer,
                prompt,
                human_like=False,
                timing=timing,
            )
            if getattr(cfg, "send_only", False):
                sidecar = await _record_sent_conversation(page, cfg, global_index, name)
                _timing_mark(timing, "sent_detached")
                if owns_timing:
                    _timing_finish(timing, "sent_detached")
                return sidecar
            answer = await wait_for_response_finished(
                page,
                before_count=before,
                timeout_seconds=cfg.response_timeout,
                timing=timing,
            )
        # One chat contains exactly one user prompt. Any later reconciliation is
        # deliberately launched as a separate fresh-chat job.
        output_path = write_output(cfg.output_dir, global_index, name, prompt, metadata, answer)
        _timing_mark(timing, "answer_persisted")
        if owns_timing:
            _timing_finish(timing, "collected")
        return output_path
    except BaseException as exc:
        if owns_timing:
            _timing_finish(timing, _timing_error_status(exc), exc)
        raise


# === workers ===
def _reraise_assist_submission_error(cfg, results):
    """Assist page-audit lane only: surface a worker's PromptVerificationError.

    ``asyncio.gather(return_exceptions=True)`` turns a worker exception into a
    result value; for the assist lane we re-raise the first submission-verification
    failure so ``run`` propagates ``submission_integrity_unverified`` to the
    transport. Non-assist runs (flag off) are unaffected and keep ignoring the
    gather results exactly as before.
    """
    if not getattr(cfg, "assist_no_rate_limit_retry", False):
        return
    for result in results or []:
        if isinstance(result, PromptVerificationError):
            raise result


async def _attempt_with_rate_limit_retry(label, global_index, attempt, stats=None, *, no_retry=False):
    """Run attempt() (one full open+send+save). If ChatGPT shows a 'too many requests' notice, log
    it and retry the SAME item with an ESCALATING wait (RATE_LIMIT_BACKOFF: 5m → 10m → 30m → 60m).
    After the schedule is exhausted it gives up (so --fill backfills it). Other errors propagate.
    `stats` (optional dict) gets stats['waits'] = how many rate-limit waits this item incurred.

    `no_retry` (page-audit lane opt-in): surface a RateLimitError immediately as a terminal failure
    for the item without waiting — the assist wave scheduler retries it in a later wave. Every other
    caller passes the default False, so behavior is unchanged."""
    sched = "→".join(f"{w // 60}m" for w in RATE_LIMIT_BACKOFF)
    tries = 0
    while True:
        try:
            return await attempt()
        except RateLimitError as e:
            if no_retry:
                raise
            if stats is not None:
                stats["waits"] = stats.get("waits", 0) + 1
            if tries >= len(RATE_LIMIT_BACKOFF):
                raise RuntimeError(f"still rate-limited after escalating waits ({sched}) — \"{e}\"")
            wait_s = RATE_LIMIT_BACKOFF[tries]
            tries += 1
            mins = max(1, wait_s // 60)
            log(f"[{label}] ({global_index}) ⏳ ChatGPT rate limit: \"{e}\" — waiting {mins} min then "
                f"resuming (backoff {tries}/{len(RATE_LIMIT_BACKOFF)}: {sched})…")
            await asyncio.sleep(wait_s)
            log(f"[{label}] ({global_index}) ▶ resuming after {mins}-min wait.")


async def _run_profile(anchor, label, assignments, regime, cfg):
    """rotating: one profile runs its assigned prompts sequentially, each in a fresh tab."""
    profile_id = (
        anchor.identity.id
        if isinstance(anchor, ProfileAnchor)
        else str(label or "rotating-profile")
    )
    if _vpn_gate is not None:
        await _vpn_gate.join()
    try:
        for (global_index, name, prompt, metadata) in assignments:
            if _vpn_gate is not None:
                await _vpn_gate.checkpoint(label)

            async def attempt(global_index=global_index, name=name, prompt=prompt, metadata=metadata):
                # one full attempt: fresh tab per try, closed on the way out (incl. before a retry wait).
                timing = _new_prompt_timing(
                    str(name),
                    profile_id=profile_id,
                    configured_x=1,
                )
                _timing_mark(timing, "worker_scheduled")
                page = None
                terminal_status = "transient_error"
                terminal_error = None
                try:
                    _timing_mark(timing, "tab_open_started")
                    _timing_mark(timing, "navigation_started")
                    page = await open_working_chat(anchor, cfg.chatgpt_url, label)
                    _timing_mark(timing, "tab_ready")
                    _timing_mark(timing, "navigation_completed")
                    output = await _send_and_save(
                        page,
                        regime,
                        cfg,
                        global_index,
                        name,
                        prompt,
                        metadata,
                        navigate=False,
                        timing=timing,
                    )
                    terminal_status = "collected"
                    return output
                except BaseException as exc:
                    terminal_status = _timing_error_status(exc)
                    terminal_error = exc
                    raise
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        _timing_mark(timing, "tab_closed")
                    _timing_finish(timing, terminal_status, terminal_error)

            try:
                log(f"[{label}] ({global_index}) Running: {name}")
                out = await _attempt_with_rate_limit_retry(
                    label, global_index, attempt,
                    no_retry=getattr(cfg, "assist_no_rate_limit_retry", False),
                )
                log(f"[{label}] ({global_index}) ✓ Saved: {out}")
            except Exception as e:
                log(f"[{label}] ({global_index}) ✗ Error: {e} — skipping")
    finally:
        if _vpn_gate is not None:
            await _vpn_gate.leave()


async def _run_tab(
    anchor,
    label,
    assignments,
    regime,
    cfg,
    nav_url,
    worker_index=0,
    profile_index=0,
    lane_index=0,
):
    """Run one worker-owned logged-in page, resetting to a fresh chat per item."""

    assignments = list(assignments)
    if not assignments:
        return
    if cfg.human_like:
        random.shuffle(assignments)  # don't march through adjacent indices in lockstep

    anchor_page = anchor.page if isinstance(anchor, ProfileAnchor) else anchor
    profile_id = (
        anchor.identity.id
        if isinstance(anchor, ProfileAnchor)
        else str(label or f"profile-{profile_index + 1}")
    )
    browser_health_before = await asyncio.to_thread(cdp_endpoint_health, cfg.cdp_url)
    delay, delay_mode = _tab_open_delay(worker_index, profile_index, lane_index)

    def make_timing(prompt_id: str):
        row = _new_prompt_timing(
            str(prompt_id),
            profile_id=profile_id,
            lane_index=lane_index,
            configured_x=max(1, cfg.tabs_per_profile),
            worker_index=worker_index,
        )
        _timing_mark(row, "worker_scheduled")
        _timing_field(row, "tab_open_delay_seconds", delay)
        _timing_field(row, "tab_open_delay_formula_mode", delay_mode)
        return row

    first_timing = make_timing(str(assignments[0][1]))
    first_timing_available = True
    page = None
    current_timing = first_timing
    current_status = "transient_error"
    current_error = None
    try:
        if delay > 0:  # don't open all tabs at once
            log(f"[{label}] holding {delay:g}s before opening its tab (staggered open).")
            await asyncio.sleep(delay)
        _timing_mark(first_timing, "tab_open_started")
        page = await open_chat_tab(anchor, cfg.chatgpt_url)
        _timing_mark(first_timing, "tab_ready")
        # Fail FAST on a blocked/rate-limited page (a click on a rate-limit overlay otherwise hangs
        # the Playwright default) so the worker skips instead of stalling.
        page.set_default_timeout(15000)
        if cfg.human_like:
            await human_pause(0.5, HUMAN_START_STAGGER)

        processed = 0
        for assignment_index, (global_index, name, prompt, metadata) in enumerate(assignments):
            has_more_assignments = assignment_index + 1 < len(assignments)
            abort_worker = False

            async def attempt(
                global_index=global_index,
                name=name,
                prompt=prompt,
                metadata=metadata,
            ):
                nonlocal first_timing_available
                nonlocal current_timing, current_status, current_error
                if first_timing_available:
                    timing = first_timing
                    first_timing_available = False
                else:
                    timing = make_timing(str(name))
                    # This lane already owns a ready page; the attempt still records
                    # the state from which its fresh-chat navigation begins.
                    _timing_mark(timing, "tab_ready")
                current_timing = timing
                current_status = "transient_error"
                current_error = None
                try:
                    # navigate=True re-opens a fresh chat each try, so a retry after
                    # a rate-limit wait never reuses the prior conversation.
                    return await _send_and_save(
                        page,
                        regime,
                        cfg,
                        global_index,
                        name,
                        prompt,
                        metadata,
                        navigate=True,
                        nav_url=nav_url,
                        timing=timing,
                    )
                except RateLimitError as exc:
                    current_status = "rate_limited"
                    current_error = exc
                    _timing_finish(timing, current_status, current_error)
                    current_timing = None
                    raise

            who = f"{name} ({(metadata or {}).get('clinic', '')})".strip()
            stats = {"waits": 0}
            try:
                log(f"[{label}] ({global_index}) Running: {name}")
                out = await _attempt_with_rate_limit_retry(
                    label, global_index, attempt, stats,
                    no_retry=getattr(cfg, "assist_no_rate_limit_retry", False),
                )
                current_status = "collected"
                current_error = None
                log(f"[{label}] ({global_index}) ✓ Saved: {out}")
                if stats["waits"] == 0:
                    outcome_log(who, "analyzed successfully in one go")
                else:
                    outcome_log(who, f"analyzed (after {stats['waits']} rate-limit wait(s))")
            except Exception as e:
                # Assist page-audit lane (ADR-002): an unverifiable in-composer
                # submission must NEVER be silently swallowed. Propagate it so run()
                # can surface submission_integrity_unverified to the transport. Every
                # other error keeps the existing skip-and-continue behavior, and
                # non-assist workflows (flag off) are byte-identical.
                if getattr(cfg, "assist_no_rate_limit_retry", False) and isinstance(
                    e, PromptVerificationError
                ):
                    raise
                if current_timing is not None:
                    current_status = _timing_error_status(e)
                    current_error = e
                log(f"[{label}] ({global_index}) ✗ Error: {e} — skipping")
                # Surface a Project "too many chats" limit prominently — it's not a rate limit and
                # won't clear on its own; the Project must be emptied / swapped.
                try:
                    chats_notice = await chats_limit_message(page)
                except Exception:
                    chats_notice = None
                if chats_notice:
                    log(f"[{label}] ⚠⚠ TOO MANY CHATS — Project chat limit hit: \"{chats_notice}\" — "
                        f"clear chats in the Project or switch project_url; new chats can't be created.")
                    outcome_log(who, "ERROR: too many chats", chats_notice)
                else:
                    outcome_log(who, "ERROR", str(e))
                # If the whole BROWSER died (not just this page), abort this tab immediately —
                # otherwise every remaining clinic fails identically and floods the logs.
                try:
                    browser_alive = anchor_page.context.browser.is_connected()
                except Exception:
                    browser_alive = False
                if not browser_alive:
                    browser_health_after = await asyncio.to_thread(
                        cdp_endpoint_health,
                        cfg.cdp_url,
                    )
                    scope = classify_browser_transport(
                        client_connected=False,
                        page_closed=True,
                        before=browser_health_before,
                        after=browser_health_after,
                    )
                    session_id = browser_health_after.get("browser_session_id") or "unknown"
                    log(
                        f"[{label}] ⛔ Playwright connection lost ({scope}; "
                        f"CDP reachable={browser_health_after.get('reachable')}; "
                        f"browser_session={session_id}) — aborting this worker; "
                        "saved observations remain resumable."
                    )
                    outcome_log(who, "ERROR: browser transport", scope)
                    abort_worker = True
                else:
                    # Otherwise it's a page-level crash/close — reopen the tab and carry on.
                    try:
                        dead = page is None or page.is_closed() or "crash" in str(e).lower()
                    except Exception:
                        dead = True
                    if dead:
                        log(f"[{label}] page crashed/closed — reopening the tab.")
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
                            _timing_mark(current_timing, "tab_closed")
                            page = None
                        try:
                            _timing_mark(current_timing, "tab_open_started")
                            page = await open_chat_tab(anchor, cfg.chatgpt_url)
                            page.set_default_timeout(15000)
                            _timing_mark(current_timing, "tab_ready")
                        except Exception as e2:
                            log(f"[{label}] ⛔ couldn't reopen tab ({e2}) — browser likely gone, aborting.")
                            abort_worker = True

            # Preserve the existing post-attempt placement, but never wait after
            # the final assignment in this lane.
            if cfg.human_like and has_more_assignments and not abort_worker:
                _timing_mark(current_timing, "between_prompt_pause_started")
                if random.random() < HUMAN_BREAK_CHANCE:
                    await human_pause(15.0, 45.0)
                else:
                    await human_pause(HUMAN_THINK_MIN, HUMAN_THINK_MAX)
                _timing_mark(current_timing, "between_prompt_pause_completed")

            # Periodically recycle only when this lane has another assignment.
            processed += 1
            if (
                TAB_RECYCLE_EVERY
                and processed % TAB_RECYCLE_EVERY == 0
                and has_more_assignments
                and not abort_worker
            ):
                log(f"[{label}] ♻ recycling tab after {processed} chats (memory flush).")
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    _timing_mark(current_timing, "tab_closed")
                    page = None
                try:
                    _timing_mark(current_timing, "tab_open_started")
                    page = await open_chat_tab(anchor, cfg.chatgpt_url)
                    page.set_default_timeout(15000)
                    _timing_mark(current_timing, "tab_ready")
                except Exception as e3:
                    try:
                        gone = not anchor_page.context.browser.is_connected()
                    except Exception:
                        gone = True
                    log(f"[{label}] couldn't reopen tab after recycle ({e3})"
                        + (" — browser gone, aborting." if gone else "."))
                    abort_worker = True

            if abort_worker:
                break
            if has_more_assignments and current_timing is not None:
                _timing_finish(current_timing, current_status, current_error)
                current_timing = None
    except BaseException as exc:
        if current_timing is not None and current_status != "collected":
            current_status = _timing_error_status(exc)
            current_error = exc
        raise
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
            _timing_mark(current_timing, "tab_closed")
        if current_timing is not None:
            _timing_finish(current_timing, current_status, current_error)


def _cdp_unreachable(cdp_url):
    log(f"\n✗ Can't reach Chrome on {cdp_url} (remote debugging not available).")
    log('  rotating regime:  bash "config/launch_chrome.sh"')
    log('  logged_in regime: bash "config/launch_logged_in.sh"')
    raise SystemExit(1)


def init_coordination(vpn_gate=None):
    """Arm the process-wide coordination primitives the browser helpers close over.

    The helpers in this module (`open_chat_tab`, `clear_cookies`, `open_working_chat`) read these
    module globals, so any module that reuses them — `extract_names` runs its own worker loop over
    the same primitives — MUST call this first. It also has to pass the SAME `VpnGate` instance it
    waits on itself: a switch announced on one gate is invisible to workers parked on another.
    """
    global _new_tab_lock, _cookie_lock, _vpn_gate
    _new_tab_lock = asyncio.Lock()
    _cookie_lock = asyncio.Lock()
    _vpn_gate = vpn_gate
    return _vpn_gate


async def connect_cdp(p, cdp_url):
    """Attach to the already-running Chrome, or exit with an actionable message."""
    try:
        return await p.chromium.connect_over_cdp(cdp_url, timeout=15000)
    except Exception:
        _cdp_unreachable(cdp_url)


async def run(regime: Regime, entries, cfg: EngineConfig):
    """Drive `entries` (list of (global_index, name, prompt, metadata)) through ChatGPT
    according to `regime`. Connects to the already-running Chrome over CDP; never closes it."""
    vpn_on = regime.vpn and cfg.use_vpn and set_vpn.vpn_available()
    init_coordination(set_vpn.VpnGate() if vpn_on else None)
    if vpn_on and cfg.rotate_vpn_on_start:
        await asyncio.to_thread(set_vpn.set_vpn)

    log(f"Regime: {regime.name}  |  connecting to Chrome at {cfg.cdp_url}...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cfg.cdp_url, timeout=15000)
        except Exception:
            _cdp_unreachable(cfg.cdp_url)

        if regime.fanout == "profile_per_tab":
            profile_names = load_profile_names(cfg.profiles_dir)
            if not profile_names:
                raise RuntimeError("No Chrome profile metadata found in config/chrome-profiles/")
            want = min(cfg.num_profiles, len(profile_names))
            pages = await collect_profile_pages(browser, want)
            if not pages:
                raise RuntimeError("No ChatGPT tabs found. Did launch_chrome.sh open the profiles?")
            usable = min(want, len(pages))
            if usable < want:
                log(f"⚠ Only {len(pages)} ChatGPT profile tab(s) detected; using {usable}.")
            total = len(entries) if cfg.max_per_profile is None else min(len(entries), usable * cfg.max_per_profile)
            selected = entries[:total]
            buckets = [[] for _ in range(usable)]
            for idx, entry in enumerate(selected):
                buckets[idx % usable].append(entry)
            log(f"Running {total} prompt(s) across {usable} profile(s).\n")
            tasks = []
            for i in range(usable):
                if not buckets[i]:
                    continue
                label = profile_names[i] if i < len(profile_names) else f"profile{i + 1}"
                tasks.append(_run_profile(pages[i], label, buckets[i], regime, cfg))
            await asyncio.gather(*tasks, return_exceptions=True)
        elif regime.fanout == "ephemeral_guest":
            # Outreach evidence collection owns a freshly-created guest Chrome and
            # has already audited it as logged out before reaching this runner.  It
            # deliberately has no logged-in profile metadata to pair.  Use its sole
            # ChatGPT page only as an anchor, then open one fresh chat per prompt.
            # The surrounding live runner stops and deletes this browser before it
            # creates the second independent run.
            if len(browser.contexts) != 1:
                raise RuntimeError(
                    "Ephemeral guest collection requires exactly one browser context."
                )
            pages = await collect_chatgpt_pages(browser)
            if not pages:
                raise RuntimeError("No ChatGPT page found in the ephemeral guest browser.")
            log(f"Running {len(entries)} prompt(s) in one ephemeral guest profile.\n")
            await _run_profile(pages[0], "ephemeral-guest", entries, regime, cfg)
        else:  # tabs_in_one_profile: cfg.tabs = TOTAL concurrent tabs, spread round-robin over the profiles
            profiles = list(cfg.logged_in_profiles or [])
            if not profiles or any(
                not str(row.get("chrome_directory") or row.get("profile") or "").strip()
                for row in profiles
            ):
                raise RuntimeError(
                    "The logged_in regime requires configured raw Chrome profile directories in "
                    "logged-in-profiles.json."
                )
            configured_pool = [
                LoggedInProfile(
                    id=str(row.get("id") or "").strip() or fallback_profile_id(
                        row.get("display_name") or row.get("label"),
                        row.get("chrome_directory") or row.get("profile"),
                    ),
                    display_name=str(
                        row.get("display_name")
                        or row.get("label")
                        or row.get("chrome_directory")
                        or row.get("profile")
                    ).strip(),
                    chrome_directory=str(
                        row.get("chrome_directory") or row.get("profile")
                    ).strip(),
                    project_url=str(row.get("project_url") or cfg.project_url or "").strip(),
                )
                for row in profiles
            ]
            profile_anchors = await pair_profile_anchors(browser, configured_pool)
            n = len(profile_anchors)
            if n != len(configured_pool):
                raise RuntimeError(
                    f"Matched {n} ChatGPT profile anchor(s) for "
                    f"{len(configured_pool)} configured profile(s); refusing a partial profile run."
                )
            # cfg.tabs_per_profile concurrent tabs PER logged-in profile (round-robin assigns
            # tab1..tabK to each account), capped by cfg.tabs = TOTAL concurrent tabs.
            # Tab count is independent of cfg.human_like: that flag controls PACING (typing cadence
            # and think-pauses) only. It used to also switch this line's semantics, which made
            # cfg.tabs unreachable — HUMAN_LIKE is never set, so the total-tabs knob was dead config.
            n_tabs = max(1, min(n * max(1, cfg.tabs_per_profile), max(1, cfg.tabs)))
            workers = []   # (anchor, label, nav_url, profile_index, lane_index)
            for w in range(n_tabs):
                pi = w % n
                lane = w // n
                anchor = profile_anchors[pi]
                pname = anchor.identity.display_name or anchor.identity.id or f"p{pi + 1}"
                purl = anchor.identity.project_url or cfg.project_url
                nav_url = purl if cfg.target == "project" else cfg.chatgpt_url
                workers.append((anchor, f"{pname}:tab{lane + 1}", nav_url, pi, lane))
            buckets = [[] for _ in workers]
            for idx, entry in enumerate(entries):
                buckets[idx % len(workers)].append(entry)
            log(f"Running {len(entries)} item(s) across {len(workers)} concurrent tab(s) "
                f"over {n} profile(s).\n")
            _tab_results = await asyncio.gather(*(
                _run_tab(
                    anchor,
                    label,
                    buckets[w],
                    regime,
                    cfg,
                    nav_url,
                    worker_index=w,
                    profile_index=profile_index,
                    lane_index=lane_index,
                )
                for w, (anchor, label, nav_url, profile_index, lane_index) in enumerate(workers)
                if buckets[w]
            ),
                                 return_exceptions=True)
            _reraise_assist_submission_error(cfg, _tab_results)

    log("\nDone.")
