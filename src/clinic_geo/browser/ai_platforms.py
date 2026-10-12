"""Browser providers for non-ChatGPT audit prompt collection.

The free-audit pipeline stores answers as Markdown files in ``raw_gpt_answers``.
This module keeps that contract intact while driving browser surfaces whose DOMs
are different from ChatGPT: Gemini, Google Search AI Overview, and Copilot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import parse_qs, parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from clinic_geo.env import env_float, env_int
from clinic_geo.browser.browser_health import (
    cdp_endpoint_health,
    cdp_session_changed,
    classify_browser_transport,
)
from clinic_geo.browser.human_input import split_typed_lead
from clinic_geo.browser.profile_pool import (
    ProfileAnchor,
    load_logged_in_profile_pool,
    open_profile_page,
    pair_profile_anchors,
)
from clinic_geo.browser.prompt_timing import PromptTimingRecorder, TIMING_FILENAME
from clinic_geo.parsing.prompt_sets import load_prompts
from clinic_geo.persistence.outputs import write_answer_file
from clinic_geo.platforms import (
    CHATGPT,
    COPILOT,
    DEFAULT_AUDIT_PLATFORMS,
    FULL_AUDIT_PLATFORMS,
    GEMINI,
    GOOGLE_AI_OVERVIEW,
    PLATFORM_ALIASES,
    RETRYABLE_STATUSES,
    normalize_platform,
    parse_platforms,
    platform_data_dir,
)

@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    start_url: str
    interface_notes: list[str]
    composer_selectors: tuple[str, ...] = ()
    send_selectors: tuple[str, ...] = ()
    stop_selectors: tuple[str, ...] = ()
    answer_selectors: tuple[str, ...] = ()
    auth_wall_markers: tuple[str, ...] = ()
    unavailable_markers: tuple[str, ...] = ()
    status: str = "browser"


@dataclass(frozen=True)
class ProviderInteractionPolicy:
    """Bounded, human-paced input without changing provider concurrency.

    The policy is deliberately local to one provider tab.  Different leased
    profiles and different platforms still overlap; only the input events for a
    single prompt are paced. The first sentence (or a short opening phrase for
    a one-sentence prompt) is typed, then the exact remainder is appended once.
    """

    human_like: bool = True
    key_delay_min_ms: float = 18.0
    key_delay_max_ms: float = 48.0
    chunk_min_chars: int = 4
    chunk_max_chars: int = 14
    chunk_pause_min_seconds: float = 0.03
    chunk_pause_max_seconds: float = 0.16
    pre_input_min_seconds: float = 0.18
    pre_input_max_seconds: float = 0.65
    pre_submit_min_seconds: float = 0.25
    pre_submit_max_seconds: float = 0.85
    page_settle_min_seconds: float = 0.8
    page_settle_max_seconds: float = 2.2
    long_prompt_threshold: int = 360
    typed_lead_max_chars: int = 42
    submission_ack_timeout_ms: int = 5000


class ProviderSubmissionUnavailable(RuntimeError):
    """The prompt was entered, but the surface did not start a new turn."""


class ProviderSendControlMissing(RuntimeError):
    """No send control was found before any submission action was attempted."""


class ProviderPromptVerificationError(RuntimeError):
    """The provider composer could not be read back as the exact frozen prompt."""


class GoogleContractMismatch(RuntimeError):
    """Google did not retain the frozen search URL contract."""


class GoogleBlocked(RuntimeError):
    """Google presented a CAPTCHA or unusual-traffic surface."""


PROVIDER_SPECS = {
    CHATGPT: ProviderSpec(
        key=CHATGPT,
        label="ChatGPT",
        start_url="https://chatgpt.com/",
        status="existing_chatgpt_runner",
        interface_notes=[
            "Handled by clinic_geo.browser.run_gpt_prompts.",
        ],
    ),
    GEMINI: ProviderSpec(
        key=GEMINI,
        label="Gemini",
        start_url="https://gemini.google.com/app",
        composer_selectors=(
            'div[contenteditable="true"][role="textbox"][aria-label="Enter a prompt for Gemini"]',
            "rich-textarea .ql-editor[contenteditable='true']",
            "rich-textarea [role='textbox']",
        ),
        send_selectors=(
            "gem-icon-button.send-button button",
            "gem-icon-button.submit button",
            'button[aria-label="Send message"]',
            'button[aria-label="Enviar mensaje"]',
            'button[data-testid*="send"]',
            'button:has-text("Submit")',
        ),
        stop_selectors=(
            'button[aria-label="Stop response"]',
            'button:has-text("Stop response")',
        ),
        answer_selectors=(
            "model-response .markdown",
            "model-response message-content",
            "model-response structured-content-container.model-response-text",
            "model-response .response-container",
            ".markdown",
            "message-content",
            "structured-content-container",
            ".model-response-text",
            ".response-container",
            "[data-test-id='model-response']",
        ),
        interface_notes=[
            "Gemini exposes a rich-textarea composer in the configured persistent profile.",
            "The contenteditable child has role=textbox and aria-label='Enter a prompt for Gemini'.",
            "The enabled submit control is a button with aria-label='Send message'.",
            "While generating, Gemini shows a button with aria-label='Stop response'.",
            "Completed answers live in model-response; the deepest stable text nodes observed were .markdown, message-content, and structured-content-container.model-response-text.",
        ],
    ),
    GOOGLE_AI_OVERVIEW: ProviderSpec(
        key=GOOGLE_AI_OVERVIEW,
        label="Google AI Overview",
        start_url="https://www.google.com/search",
        interface_notes=[
            "Prompts are submitted as Google Search queries in the browser.",
            "A successful response is detected by a visible 'AI Overview' heading.",
            "The observed AI Overview block includes Show more / aria-label='Show more AI Overview' and source cards/chips.",
            "Unusual-traffic/CAPTCHA text is treated as blocked, not as an absent overview.",
        ],
    ),
    COPILOT: ProviderSpec(
        key=COPILOT,
        label="Copilot",
        start_url="https://copilot.com/",
        composer_selectors=(
            "textarea[placeholder*='Ask']",
            "textarea[aria-label*='Ask']",
            "textarea[aria-label*='Message']",
            "[contenteditable='true'][role='textbox']",
            "[data-testid='composer-input']",
            "cib-text-input textarea",
        ),
        send_selectors=(
            "button[data-testid='submit-button']",
            "button[aria-label*='Submit']",
            "button[title*='Submit']",
            "button[aria-label*='Send']",
            "button[data-testid*='send']",
            "button:has-text('Send')",
        ),
        stop_selectors=(
            "button[data-testid='stop-button']",
            "button[aria-label*='Stop']",
            "button:has-text('Stop')",
        ),
        answer_selectors=(
            "[data-content='ai-message']",
            "[data-testid='ai-message-body']",
            "[data-testid='ai-message']",
            "[data-testid*='bot-message']",
            "[class*='bot-message']",
            "cib-message-group",
            "[class*='response']",
        ),
        auth_wall_markers=(
            "Sign in to Copilot",
            "Sign in with Microsoft",
            "Sign in with Apple",
            "Sign in with Google",
        ),
        interface_notes=[
            "The tested logged-out Copilot page is a hard sign-in wall.",
            "Observed heading: h1[data-testid='anonymous-block-page-title'] with text 'Sign in to Copilot'.",
            "Observed sign-in buttons have titles/text 'Sign in with Microsoft', 'Sign in with Apple', and 'Sign in with Google'.",
            "Authenticated-chat selectors are included as fallbacks, but the logged-out interface has no usable composer.",
        ],
    ),
}


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().casefold() in {"1", "true", "yes", "on"}


def _collection_flag_enabled(name: str) -> bool:
    """Return whether an experimental switch is explicitly collection-scoped."""

    return (
        str(os.getenv("SNAPSHOT_STAGE") or "").strip().casefold() == "collection"
        and _truthy_env(name)
    )


@dataclass
class PromptRunResult:
    platform: str
    prompt_id: str
    index: int
    status: str
    output_file: Optional[str] = None
    detail: str = ""
    citations: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    link_followup_status: Optional[str] = None
    link_followup_detail: str = ""
    link_followup_output_file: Optional[str] = None
    link_followup_attempts: int = 0
    link_followup_max_attempts: int = 0
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    chrome_directory: Optional[str] = None
    profile: Optional[str] = None
    profile_label: Optional[str] = None
    transport_diagnostic: Optional[dict] = None

    def __post_init__(self) -> None:
        """Populate canonical identity fields and compatibility aliases."""

        directory = str(self.chrome_directory or self.profile or "").strip() or None
        name = str(self.profile_name or self.profile_label or directory or "").strip() or None
        profile_id = str(self.profile_id or directory or "").strip() or None
        self.profile_id = profile_id
        self.profile_name = name
        self.chrome_directory = directory
        self.profile = directory
        self.profile_label = name


def interface_report(platforms: Iterable[str] = FULL_AUDIT_PLATFORMS) -> dict:
    return {key: asdict(PROVIDER_SPECS[key]) for key in platforms}


def _profile_artifact_fields(
    *,
    profile_id: Optional[str] = None,
    profile_name: Optional[str] = None,
    chrome_directory: Optional[str] = None,
    profile: Optional[str] = None,
    profile_label: Optional[str] = None,
) -> dict[str, str]:
    """Return canonical profile identity fields plus legacy aliases."""

    directory = str(chrome_directory or profile or "").strip()
    if not directory:
        return {}
    name = str(profile_name or profile_label or directory).strip() or directory
    stable_id = str(profile_id or directory).strip() or directory
    return {
        "profile_id": stable_id,
        "profile_name": name,
        "chrome_directory": directory,
        # Compatibility aliases used by older provider/Snapshot artifacts.
        "profile": directory,
        "profile_label": name,
    }


def _metadata(
    metadata: dict,
    platform: str,
    status: str,
    detail: str = "",
    citations: Optional[list] = None,
    *,
    profile_id: Optional[str] = None,
    profile_name: Optional[str] = None,
    chrome_directory: Optional[str] = None,
    profile: Optional[str] = None,
    profile_label: Optional[str] = None,
) -> dict:
    merged = dict(metadata or {})
    merged["source"] = {
        "platform": platform,
        "platform_label": PROVIDER_SPECS[platform].label,
        "status": status,
        "detail": detail,
        "citations": citations or [],
    }
    identity = _profile_artifact_fields(
        profile_id=profile_id,
        profile_name=profile_name,
        chrome_directory=chrome_directory,
        profile=profile,
        profile_label=profile_label,
    )
    merged["source"].update(identity)
    if identity:
        # Retain the original raw-answer metadata keys too.
        merged["source"]["chrome_profile"] = identity["chrome_directory"]
        merged["source"]["chrome_profile_label"] = identity["profile_name"]
    return merged


def _status_answer(label: str, status: str, detail: str) -> str:
    return f"[{label} {status}]\n\n{detail}".strip()


def resolve_provider_interaction_policy() -> ProviderInteractionPolicy:
    """Resolve explicit provider pacing, defaulting to the global human policy."""

    raw = str(
        os.getenv("AI_PROVIDER_HUMAN_LIKE")
        if os.getenv("AI_PROVIDER_HUMAN_LIKE") is not None
        else os.getenv("HUMAN_LIKE", "")
    ).strip().casefold()
    human_like = raw not in {"0", "false", "no", "off"}

    key_min = max(0.0, env_float("AI_PROVIDER_KEY_DELAY_MIN_MS", 18.0))
    key_max = max(key_min, env_float("AI_PROVIDER_KEY_DELAY_MAX_MS", 48.0))
    chunk_min = max(1, env_int("AI_PROVIDER_INPUT_CHUNK_MIN_CHARS", 4))
    chunk_max = max(chunk_min, env_int("AI_PROVIDER_INPUT_CHUNK_MAX_CHARS", 14))
    return ProviderInteractionPolicy(
        human_like=human_like,
        key_delay_min_ms=key_min,
        key_delay_max_ms=key_max,
        chunk_min_chars=chunk_min,
        chunk_max_chars=chunk_max,
        chunk_pause_min_seconds=max(
            0.0, env_float("AI_PROVIDER_CHUNK_PAUSE_MIN_SECONDS", 0.03)
        ),
        chunk_pause_max_seconds=max(
            0.0, env_float("AI_PROVIDER_CHUNK_PAUSE_MAX_SECONDS", 0.16)
        ),
        pre_input_min_seconds=max(
            0.0, env_float("AI_PROVIDER_PRE_INPUT_MIN_SECONDS", 0.18)
        ),
        pre_input_max_seconds=max(
            0.0, env_float("AI_PROVIDER_PRE_INPUT_MAX_SECONDS", 0.65)
        ),
        pre_submit_min_seconds=max(
            0.0, env_float("AI_PROVIDER_PRE_SUBMIT_MIN_SECONDS", 0.25)
        ),
        pre_submit_max_seconds=max(
            0.0, env_float("AI_PROVIDER_PRE_SUBMIT_MAX_SECONDS", 0.85)
        ),
        page_settle_min_seconds=max(
            0.0, env_float("AI_PROVIDER_PAGE_SETTLE_MIN_SECONDS", 0.8)
        ),
        page_settle_max_seconds=max(
            0.0, env_float("AI_PROVIDER_PAGE_SETTLE_MAX_SECONDS", 2.2)
        ),
        long_prompt_threshold=max(
            80, env_int("AI_PROVIDER_LONG_PROMPT_THRESHOLD", 360)
        ),
        typed_lead_max_chars=max(
            12, env_int("AI_PROVIDER_TYPED_LEAD_MAX_CHARS", 42)
        ),
        submission_ack_timeout_ms=max(
            500, env_int("AI_PROVIDER_SUBMISSION_ACK_TIMEOUT_MS", 5000)
        ),
    )


async def provider_pause(
    minimum: float,
    maximum: float,
    *,
    policy: Optional[ProviderInteractionPolicy] = None,
) -> float:
    """Sleep for a bounded jittered interval and return its duration."""

    policy = policy or resolve_provider_interaction_policy()
    if not policy.human_like:
        return 0.0
    low = max(0.0, min(float(minimum), float(maximum)))
    high = max(low, max(float(minimum), float(maximum)))
    seconds = random.uniform(low, high)
    if seconds:
        await asyncio.sleep(seconds)
    return seconds


def is_browser_transport_error(exc: BaseException) -> bool:
    """Identify a lost page/context/CDP connection without masking it as UI drift."""

    name = type(exc).__name__.casefold()
    detail = str(exc or "").casefold()
    return "targetclosed" in name or any(
        marker in detail
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "page has been closed",
            "connection closed",
            "connection is closed",
            "browser has disconnected",
            "browser is not connected",
            "target closed",
        )
    )


def page_is_closed(page) -> bool:
    state = getattr(page, "is_closed", False)
    try:
        return bool(state()) if callable(state) else bool(state)
    except Exception:
        return True


async def browser_transport_diagnostic(
    page,
    before: object = None,
    *,
    cdp_url: Optional[str] = None,
) -> dict:
    """Distinguish a dead target/client from an unavailable/replaced shared CDP."""

    client_connected = False
    try:
        browser = page.context.browser
        client_connected = bool(browser and browser.is_connected())
    except Exception:
        pass
    after = await asyncio.to_thread(
        cdp_endpoint_health,
        cdp_url or os.getenv("CDP_URL", "http://127.0.0.1:9222"),
    )
    return {
        "scope": classify_browser_transport(
            client_connected=client_connected,
            page_closed=page_is_closed(page),
            before=before,
            after=after,
        ),
        "playwright_client_connected": client_connected,
        "page_closed": page_is_closed(page),
        "endpoint_after": after,
        "browser_session_changed": cdp_session_changed(before, after),
    }


async def first_visible_locator(page, selectors: Iterable[str], *, timeout_ms: int = 30000):
    deadline = time.time() + timeout_ms / 1000
    last_error = None
    while time.time() < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                count = await loc.count()
                for index in range(count - 1, -1, -1):
                    item = loc.nth(index)
                    if await item.is_visible():
                        return item
            except Exception as exc:  # selector dialects can vary across providers
                if is_browser_transport_error(exc) or page_is_closed(page):
                    raise
                last_error = exc
        await asyncio.sleep(0.25)
    if last_error:
        raise TimeoutError(f"No visible locator found; last selector error: {last_error}")
    raise TimeoutError("No visible locator found")


async def locator_has_visible_item(locator) -> bool:
    """Return whether a possibly stale multi-locator has a visible element."""

    try:
        count = await locator.count()
    except Exception:
        return False
    for index in range(count - 1, -1, -1):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


def gemini_answer_is_complete(
    text: str,
    *,
    stable_for_seconds: float,
    stop_visible: bool,
    aria_busy: bool,
) -> bool:
    """Recognise completion even when Gemini leaves stale controls in the DOM."""

    if not str(text or "").strip():
        return False
    if not stop_visible and not aria_busy and stable_for_seconds >= 1.0:
        return True
    # Gemini sometimes leaves both the stop control and ``aria-busy=true``
    # behind after generation. Stable answer text is the reliable fallback.
    return stable_for_seconds >= 8.0


COMMON_CONSENT_LABELS = (
    "Reject all",
    "Reject non-essential",
    "Accept all",
    "I agree",
    "Got it",
    "Continue",
    "No thanks",
)
REJECT_ONLY_CONSENT_LABELS = (
    "Reject all",
    "Reject non-essential",
    "No thanks",
)

# Canary infrastructure intentionally ships without a guessed Gemini dialog.
# A signature may be added only after live telemetry confirms its stable DOM
# identity and the exact safe action. Values are exact accessible button names.
GEMINI_SAFE_MODAL_ALLOWLIST: dict[str, tuple[str, ...]] = {}
GEMINI_MODAL_SELECTORS = (
    '[role="dialog"]',
    'mat-dialog-container[role="dialog"]',
    '.cdk-overlay-pane [role="dialog"]',
    # Never click this generic surface. Its presence is only an unknown-modal
    # fail-closed signal when the Gemini canary is active.
    '.cdk-overlay-backdrop',
)
GOOGLE_SAFE_CONSENT_LABELS = (
    "Reject all",
    "Reject non-essential",
)


async def dismiss_common_consent(page, *, allowed_labels: Iterable[str] | None = None):
    labels = tuple(allowed_labels) if allowed_labels is not None else COMMON_CONSENT_LABELS
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=1500)
                await asyncio.sleep(0.7)
                return label
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
            pass
    return None


async def gemini_modal_signature(page) -> Optional[str]:
    """Return a bounded DOM-only signature for a visible Gemini dialog.

    The signature intentionally hashes stable element attributes and never
    stores dialog text, account information, or arbitrary modal content.
    """

    for selector in GEMINI_MODAL_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count - 1, -1, -1):
                item = locator.nth(index)
                visible = getattr(item, "is_visible", None)
                if visible is not None and not await visible():
                    continue
                surface = (
                    "overlay_backdrop"
                    if selector == ".cdk-overlay-backdrop"
                    else "role_dialog"
                )
                parts = [f"surface={surface}", f"selector={selector}"]
                for attribute in (
                    "id",
                    "data-testid",
                    "aria-labelledby",
                    "class",
                ):
                    getter = getattr(item, "get_attribute", None)
                    if getter is None:
                        continue
                    value = str(await getter(attribute) or "").strip()
                    if value:
                        parts.append(f"{attribute}={value[:160]}")
                digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
                return f"gemini_dialog_dom_v1:{digest}"
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
    return None


async def dismiss_allowlisted_gemini_modal(page, signature: str) -> str:
    """Dismiss one telemetry-confirmed Gemini dialog using one exact safe action."""

    labels = GEMINI_SAFE_MODAL_ALLOWLIST.get(str(signature or ""))
    if not labels:
        raise ProviderSubmissionUnavailable(
            "Gemini displayed an unknown modal; no submission action was attempted"
        )

    for label in labels:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(f"^{re.escape(label)}$", re.I),
            )
            if not await button.count():
                continue
            button = button.first
            visible = getattr(button, "is_visible", None)
            if visible is not None and not await visible():
                continue
            enabled = getattr(button, "is_enabled", None)
            if enabled is not None and not await enabled():
                continue
            await button.click(timeout=5000)
            break
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
            raise ProviderSubmissionUnavailable(
                "Gemini safe modal action failed; no submission action was attempted"
            ) from exc
    else:
        raise ProviderSubmissionUnavailable(
            "Gemini allowlisted modal exposed no enabled safe action"
        )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if await gemini_modal_signature(page) is None:
            return label
        await asyncio.sleep(0.1)
    raise ProviderSubmissionUnavailable(
        "Gemini modal remained after its allowlisted action; no submission action was attempted"
    )


async def fill_textbox(locator, text: str):
    try:
        await locator.fill(text, timeout=15000)
        return
    except Exception as exc:
        if is_browser_transport_error(exc):
            raise
        pass
    handle = await locator.element_handle()
    if handle is None:
        raise RuntimeError("composer element disappeared")
    await handle.evaluate(
        """(el, value) => {
            el.focus();
            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return;
            }
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, value);
            el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
        }""",
        text,
    )


def _normalise_composer_text(value: object) -> str:
    return (
        str(value or "")
        .replace("\u00a0", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


async def textbox_value(locator) -> Optional[str]:
    """Read either a textarea value or rich-contenteditable text."""

    attempted = False
    succeeded = False
    for method_name in ("input_value", "inner_text", "text_content"):
        method = getattr(locator, method_name, None)
        if method is None:
            continue
        attempted = True
        try:
            try:
                value = await method(timeout=2000)
            except TypeError:
                value = await method()
        except Exception as exc:
            if is_browser_transport_error(exc):
                raise
            continue
        succeeded = True
        # ``inner_text`` is empty for a textarea, so keep looking for a useful
        # value. An actually empty composer remains a valid final result.
        if value not in (None, "") or method_name == "text_content":
            return str(value or "")
    return "" if attempted and succeeded else None


async def verify_textbox_exact(locator, prompt: str) -> None:
    """Fail closed unless the complete frozen prompt can be read back exactly."""

    actual = await textbox_value(locator)
    if actual is None:
        raise ProviderPromptVerificationError("provider composer could not be read back")
    if _normalise_composer_text(actual) != _normalise_composer_text(prompt):
        raise ProviderPromptVerificationError(
            "provider composer did not retain the complete frozen prompt"
        )


async def _move_pointer_to(page, locator) -> None:
    try:
        box = await locator.bounding_box()
        if not box:
            return
        x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        await page.mouse.move(x, y, steps=random.randint(5, 14))
    except Exception:
        pass


async def _paced_keyboard_input(
    page,
    text: str,
    policy: ProviderInteractionPolicy,
) -> None:
    """Type a human lead, then append the exact remainder in one operation."""

    async def type_segment(segment: str) -> None:
        offset = 0
        while offset < len(segment):
            size = random.randint(policy.chunk_min_chars, policy.chunk_max_chars)
            chunk = segment[offset : offset + size]
            offset += len(chunk)
            delay_ms = random.uniform(policy.key_delay_min_ms, policy.key_delay_max_ms)
            await page.keyboard.type(chunk, delay=delay_ms)
            pause_low = min(
                policy.chunk_pause_min_seconds,
                policy.chunk_pause_max_seconds,
            )
            pause_high = max(
                policy.chunk_pause_min_seconds,
                policy.chunk_pause_max_seconds,
            )
            if chunk.rstrip().endswith((".", "?", "!", ",", ";", ":")):
                pause_high += 0.12
            if offset < len(segment) and pause_high > 0:
                await asyncio.sleep(random.uniform(pause_low, pause_high))

    lead, remainder = split_typed_lead(
        text,
        single_sentence_max_chars=policy.typed_lead_max_chars,
    )
    await type_segment(lead)
    if remainder:
        await asyncio.sleep(random.uniform(0.18, 0.55))
        await page.keyboard.insert_text(remainder)


async def enter_textbox(
    page,
    locator,
    text: str,
    *,
    policy: Optional[ProviderInteractionPolicy] = None,
    timing: Optional[PromptTimingRecorder] = None,
) -> str:
    """Focus, enter, and verify a complete provider prompt.

    The fallback replaces the whole composer value, so a partial paced-input
    failure can never duplicate the beginning of a prompt.
    """

    policy = policy or resolve_provider_interaction_policy()
    if timing:
        timing.mark("input_started")
    if not policy.human_like:
        await fill_textbox(locator, text)
        if timing:
            timing.mark("input_completed")
        await verify_textbox_exact(locator, text)
        if timing:
            timing.mark("input_verified")
            timing.field("input_mode", "atomic_fill")
        return "atomic_fill"

    try:
        await _move_pointer_to(page, locator)
        try:
            await locator.click(timeout=5000)
        except TypeError:
            await locator.click()
        await provider_pause(
            policy.pre_input_min_seconds,
            policy.pre_input_max_seconds,
            policy=policy,
        )
        try:
            await locator.press("ControlOrMeta+A")
            await locator.press("Backspace")
        except Exception:
            await locator.fill("")
        await _paced_keyboard_input(page, text, policy)
        if timing:
            timing.mark("input_completed")
        await verify_textbox_exact(locator, text)
        if timing:
            timing.mark("input_verified")
            timing.field("input_mode", "paced_keyboard")
        return "paced_keyboard"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise
        await fill_textbox(locator, text)
        if timing:
            timing.mark("input_completed")
        await verify_textbox_exact(locator, text)
        if timing:
            timing.mark("input_verified")
            timing.field("input_mode", "verified_fill_fallback")
        return "verified_fill_fallback"


async def _submission_acknowledged(
    page,
    locator,
    spec: ProviderSpec,
    *,
    before_answer_count: int,
    before_stop_visible: bool,
    timeout_ms: int,
) -> bool:
    """Require a clear turn-start signal; a changed draft/newline is not enough."""

    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        value = await textbox_value(locator)
        if value is not None and not _normalise_composer_text(value):
            return True
        try:
            if spec.answer_selectors:
                count = await page.locator(", ".join(spec.answer_selectors)).count()
                if count > before_answer_count:
                    return True
            if spec.stop_selectors and not before_stop_visible:
                stop_visible = await locator_has_visible_item(
                    page.locator(", ".join(spec.stop_selectors))
                )
                if stop_visible:
                    return True
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
        await asyncio.sleep(0.1)
    return False


async def _click_send_control(
    page,
    selectors: Iterable[str],
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> None:
    try:
        send = await first_visible_locator(page, selectors, timeout_ms=4000)
    except TimeoutError as exc:
        # This is the only state in which Enter is allowed: discovery finished
        # without finding a send control, so no click could have been invoked.
        raise ProviderSendControlMissing("no visible provider send control") from exc
    enabled = getattr(send, "is_enabled", None)
    if enabled is not None:
        try:
            is_enabled = await enabled()
        except TypeError:
            is_enabled = True
        if not is_enabled:
            raise RuntimeError("visible provider send control is disabled")
    await _move_pointer_to(page, send)
    if timing:
        timing.mark("send_control_ready")
        timing.field("submission_action_method", "button")
        # Mark immediately before invocation. Any timeout or exception after
        # this point is an attempted action and may never fall back to Enter.
        timing.mark("send_action")
    await send.click(timeout=5000)


async def submit_textbox(
    page,
    composer,
    prompt: str,
    spec: ProviderSpec,
    *,
    policy: Optional[ProviderInteractionPolicy] = None,
    timing: Optional[PromptTimingRecorder] = None,
) -> str:
    """Submit using a human action and prove that the composer acknowledged it."""

    policy = policy or resolve_provider_interaction_policy()
    await provider_pause(
        policy.pre_submit_min_seconds,
        policy.pre_submit_max_seconds,
        policy=policy,
    )
    # The composer may be replaced or truncated during the pre-submit pause
    # (including by modal transitions). Re-read immediately before any action.
    await verify_textbox_exact(composer, prompt)
    if timing:
        timing.mark("input_verified")
    try:
        before_answer_count = (
            await page.locator(", ".join(spec.answer_selectors)).count()
            if spec.answer_selectors
            else 0
        )
        before_stop_visible = (
            await locator_has_visible_item(page.locator(", ".join(spec.stop_selectors)))
            if spec.stop_selectors
            else False
        )
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise
        before_answer_count = 0
        before_stop_visible = False

    # Prefer the provider's current enabled button. Only fall back to Enter when
    # no button was found, before any submission action could have happened.
    # Once an action executes, never issue a second one: delayed acknowledgement
    # must not duplicate a successful prompt.
    method = "button"
    try:
        await _click_send_control(page, spec.send_selectors, timing=timing)
    except asyncio.CancelledError:
        raise
    except ProviderSendControlMissing:
        method = "enter"
        press = getattr(composer, "press", None)
        if timing:
            timing.field("submission_action_method", method)
            timing.mark("send_action")
        if press is not None:
            await press("Enter")
        else:
            await page.keyboard.press("Enter")
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise
        raise ProviderSubmissionUnavailable(
            f"{spec.label} submit action failed after verified input: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if await _submission_acknowledged(
        page,
        composer,
        spec,
        before_answer_count=before_answer_count,
        before_stop_visible=before_stop_visible,
        timeout_ms=policy.submission_ack_timeout_ms,
    ):
        if timing:
            timing.mark("submission_acknowledged")
        return method
    raise ProviderSubmissionUnavailable(
        f"{spec.label} did not acknowledge the {method} submission after verified input; "
        "the prompt was not submitted again to avoid a duplicate turn."
    )


def _clean_answer(text: str, *, remove_prefixes: Iterable[str] = ()) -> str:
    out = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\u00a0", " ")).strip()
    for prefix in remove_prefixes:
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix):].strip()
    return out


async def extract_last_text(
    page,
    selectors: Iterable[str],
    *,
    root_selector: Optional[str] = None,
    fallback_to_root: bool = True,
) -> str:
    selector_list = list(selectors)
    return await page.evaluate(
        """({selectors, rootSelector, fallbackToRoot}) => {
            const roots = rootSelector ? [...document.querySelectorAll(rootSelector)] : [document];
            const root = roots.length ? roots[roots.length - 1] : document;
            const ignored = new Set(['BUTTON', 'SVG', 'STYLE', 'SCRIPT', 'NOSCRIPT', 'TEMPLATE']);
            const deepText = (node) => {
                if (!node) return '';
                if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || '';
                if (node.nodeType === Node.ELEMENT_NODE) {
                    if (ignored.has(node.tagName)) return '';
                    if (node.getAttribute('aria-hidden') === 'true') return '';
                }
                const parts = [];
                if (node.shadowRoot) parts.push(deepText(node.shadowRoot));
                for (const child of node.childNodes || []) parts.push(deepText(child));
                return parts.join(' ');
            };
            const deepQueryAll = (node, selector, found = []) => {
                if (!node || !node.querySelectorAll) return found;
                try { found.push(...node.querySelectorAll(selector)); } catch (_) {}
                for (const element of node.querySelectorAll('*')) {
                    if (element.shadowRoot) deepQueryAll(element.shadowRoot, selector, found);
                }
                return found;
            };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            for (const selector of selectors) {
                const nodes = deepQueryAll(root, selector);
                for (let i = nodes.length - 1; i >= 0; i--) {
                    const node = nodes[i];
                    const text = clean(deepText(node));
                    if (text) return text;
                }
            }
            return fallbackToRoot ? clean(deepText(root)) : '';
        }""",
        {
            "selectors": selector_list,
            "rootSelector": root_selector,
            "fallbackToRoot": fallback_to_root,
        },
    )


async def _run_gemini_turn(
    page,
    prompt: str,
    timeout_seconds: int,
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> tuple[str, str, list[dict]]:
    spec = PROVIDER_SPECS[GEMINI]
    policy = resolve_provider_interaction_policy()
    composer = await first_visible_locator(page, spec.composer_selectors, timeout_ms=60000)
    if timing:
        timing.mark("composer_ready")
    before_count = await page.locator("model-response").count()
    try:
        before_text = _clean_answer(
            await extract_last_text(
                page,
                spec.answer_selectors,
                root_selector="model-response",
            ),
            remove_prefixes=("Gemini said",),
        )
    except Exception:
        before_text = ""
    await enter_textbox(page, composer, prompt, policy=policy, timing=timing)
    modal_signature = await gemini_modal_signature(page)
    if timing:
        timing.field("gemini_modal_signature", modal_signature or "none")
        timing.field(
            "gemini_modal_recognised",
            bool(modal_signature and modal_signature in GEMINI_SAFE_MODAL_ALLOWLIST),
        )
    if _collection_flag_enabled("SNAPSHOT_GEMINI_SAFE_MODAL_DISMISS_V1") and modal_signature:
        try:
            await dismiss_allowlisted_gemini_modal(page, modal_signature)
            try:
                await verify_textbox_exact(composer, prompt)
            except ProviderPromptVerificationError:
                # Dialog transitions can replace or truncate rich-textarea
                # contents. One whole-value repair is permitted before Send.
                await fill_textbox(composer, prompt)
                await verify_textbox_exact(composer, prompt)
                if timing:
                    timing.mark("input_verified")
        except (ProviderSubmissionUnavailable, ProviderPromptVerificationError) as exc:
            return "send_missing", _status_answer(spec.label, "send_missing", str(exc)), []
    try:
        await submit_textbox(
            page,
            composer,
            prompt,
            spec,
            policy=policy,
            timing=timing,
        )
    except ProviderSubmissionUnavailable as exc:
        return "send_missing", _status_answer(spec.label, "send_missing", str(exc)), []

    stop = page.locator(", ".join(spec.stop_selectors))
    busy = page.locator("model-response [aria-busy='true']")
    started = time.time()
    saw_response = False
    last_text = ""
    last_change_at = started
    while time.time() - started < timeout_seconds:
        try:
            response_count = await page.locator("model-response").count()
            text = await extract_last_text(
                page,
                spec.answer_selectors,
                root_selector="model-response",
            )
            text = _clean_answer(text, remove_prefixes=("Gemini said",))
            if response_count > before_count or (text and text != before_text):
                if not saw_response and timing:
                    timing.mark("first_response_detected")
                saw_response = True
            if saw_response:
                now = time.time()
                if text != last_text:
                    last_text = text
                    last_change_at = now
                if gemini_answer_is_complete(
                    last_text,
                    stable_for_seconds=now - last_change_at,
                    stop_visible=await locator_has_visible_item(stop),
                    aria_busy=await locator_has_visible_item(busy),
                ):
                    if timing:
                        timing.mark("response_completed")
                    return "ok", last_text, []
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
            pass
        await asyncio.sleep(0.5)
    if not saw_response:
        return "timeout", _status_answer(spec.label, "timeout", "No Gemini response started before the timeout."), []
    text = last_text or await extract_last_text(
        page,
        spec.answer_selectors,
        root_selector="model-response",
    )
    text = _clean_answer(text, remove_prefixes=("Gemini said",))
    return "partial", text or _status_answer(spec.label, "partial", "Gemini started responding but no extractable answer text was found."), []


async def run_gemini_prompt(
    page,
    prompt: str,
    timeout_seconds: int,
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> tuple[str, str, list[dict]]:
    spec = PROVIDER_SPECS[GEMINI]
    policy = resolve_provider_interaction_policy()
    if timing:
        timing.mark("navigation_started")
    await page.goto(spec.start_url, wait_until="domcontentloaded", timeout=60000)
    if timing:
        timing.mark("navigation_completed")
    if policy.human_like:
        await provider_pause(
            policy.page_settle_min_seconds,
            policy.page_settle_max_seconds,
            policy=policy,
        )
    else:
        await asyncio.sleep(1.0)
    if _collection_flag_enabled("SNAPSHOT_GEMINI_SAFE_MODAL_DISMISS_V1"):
        await dismiss_common_consent(
            page,
            allowed_labels=GOOGLE_SAFE_CONSENT_LABELS,
        )
    else:
        await dismiss_common_consent(page)
    return await _run_gemini_turn(page, prompt, timeout_seconds, timing=timing)


async def google_ai_state(page) -> dict:
    return await page.evaluate(
        """() => {
            const body = (document.body && document.body.innerText) || '';
            const low = body.toLowerCase();
            if (low.includes('our systems have detected unusual traffic') || low.includes('/sorry/index')) {
                return {status: 'blocked', detail: 'Google unusual-traffic / CAPTCHA page detected.', answer: '', citations: []};
            }
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 1 && r.height > 1 && getComputedStyle(el).visibility !== 'hidden';
            };
            const clean = text => (text || '').replace(/\\u00a0/g, ' ').replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
            const headings = [...document.querySelectorAll('[role="heading"], h1, h2, h3, div, span')]
                .filter(el => visible(el) && clean(el.innerText || el.textContent) === 'AI Overview');
            const heading = headings[0];
            if (!heading) {
                if (document.querySelector('#search') || body.includes('People also ask') || body.includes('Sponsored results')) {
                    return {status: 'waiting_or_not_shown', detail: 'Search results loaded; AI Overview not visible yet.', answer: '', citations: []};
                }
                return {status: 'loading', detail: 'Waiting for Google Search results.', answer: '', citations: []};
            }
            const moreButtons = [...document.querySelectorAll('button, [role="button"]')]
                .filter(el => visible(el) && /show more ai overview|show more|see more/i.test((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || el.textContent || '')));
            let best = null;
            for (let el = heading; el && el !== document.body; el = el.parentElement) {
                const text = clean(el.innerText || el.textContent || '');
                if (!text.includes('AI Overview') || text.length < 80) continue;
                const bad = (text.includes('Sponsored results') ? 1 : 0) + (text.includes('People also ask') ? 1 : 0);
                const score = bad * 100000 + text.length;
                if (!best || score < best.score) best = {el, text, score};
            }
            const root = best ? best.el : heading.parentElement;
            const clone = root.cloneNode(true);
            clone.querySelectorAll('script, style, svg, button[aria-label*="More"], button[jsname], [aria-hidden="true"]').forEach(e => e.remove());
            let answer = clean(clone.innerText || clone.textContent || '');
            const unavailablePattern = /An AI Overview is not available for this search\\s*Can't generate an AI overview right now\\. Try again later\\.?/i;
            const hadUnavailableBanner = unavailablePattern.test(answer);
            answer = answer
                .replace(unavailablePattern, '')
                .replace(/^AI Overview\\s*/i, '')
                .replace(/\\bShow more\\b[\\s\\S]*$/i, '')
                .trim();
            const citations = [];
            const seen = new Set();
            root.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || '';
                if (!href || href.startsWith('javascript:') || seen.has(href)) return;
                seen.add(href);
                const label = clean(a.innerText || a.getAttribute('aria-label') || a.textContent || '');
                citations.push({title: label.slice(0, 240), url: href});
            });
            root.querySelectorAll('.PMDqCb, .NDNGvf, [aria-label*="View related links"]').forEach(el => {
                const label = clean(el.getAttribute('aria-label') || el.innerText || el.textContent || '');
                if (!label) return;
                const key = 'label:' + label;
                if (seen.has(key)) return;
                seen.add(key);
                citations.push({title: label.slice(0, 240), url: ''});
            });
            return {
                status: answer ? 'ok' : (hadUnavailableBanner ? 'not_shown' : 'overview_empty'),
                detail: hadUnavailableBanner
                    ? 'Google showed an AI Overview unavailable banner.'
                    : (moreButtons.length ? 'AI Overview visible; expandable content available.' : 'AI Overview visible.'),
                answer,
                citations,
                showMore: moreButtons.length > 0,
            };
        }"""
    )


async def click_google_show_more(page) -> bool:
    for selector in (
        '[aria-label="Show more AI Overview"]',
        'button:has-text("Show more")',
        '[role="button"]:has-text("Show more")',
        'button:has-text("See more")',
        '[role="button"]:has-text("See more")',
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2000)
                await asyncio.sleep(1.0)
                return True
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
            pass
    return False


def google_search_contract_validation(
    url: object,
    *,
    prompt: str,
    hl: str,
    gl: str,
) -> dict[str, bool]:
    """Return prompt-free validation bits for the frozen Google URL contract."""

    try:
        parsed = urlsplit(str(url or ""))
        host = str(parsed.hostname or "").casefold()
        params = parse_qs(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return {
            "host": False,
            "path": False,
            "q": False,
            "hl": False,
            "gl": False,
            "pws": False,
            "exact": False,
        }
    checks = {
        "host": host == "google.com" or host.endswith(".google.com"),
        "path": parsed.path.rstrip("/") == "/search",
        # Reject duplicate frozen parameters as ambiguous rather than accepting
        # whichever value parse_qs happens to return first.
        "q": params.get("q") == [prompt],
        "hl": params.get("hl") == [str(hl)],
        "gl": params.get("gl") == [str(gl)],
        "pws": params.get("pws") == ["0"],
    }
    checks["exact"] = all(checks.values())
    return checks


def google_search_contract_matches(
    url: object,
    *,
    prompt: str,
    hl: str,
    gl: str,
) -> bool:
    """Verify the exact frozen query and locale flags after Google navigation."""

    return google_search_contract_validation(
        url,
        prompt=prompt,
        hl=hl,
        gl=gl,
    )["exact"]


def sanitise_google_url(url: object) -> str:
    """Keep Google contract structure while removing prompt and tracking values."""

    try:
        parsed = urlsplit(str(url or ""))
        safe_pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key not in {"q", "hl", "gl", "pws"}:
                continue
            safe_pairs.append((key, "[redacted]" if key == "q" else value))
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(safe_pairs),
                "",
            )
        )
    except (TypeError, ValueError):
        return ""


async def recognised_google_consent(page) -> Optional[str]:
    """Recognise only a Google consent surface with an exact rejection action."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
        host_consent = str(parsed.hostname or "").casefold().startswith("consent.google.")
    except ValueError:
        host_consent = False

    dialog_consent = False
    try:
        dialogs = page.locator('[role="dialog"]')
        for index in range(await dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            visible = getattr(dialog, "is_visible", None)
            if visible is not None and not await visible():
                continue
            reader = getattr(dialog, "inner_text", None) or getattr(dialog, "text_content", None)
            text = str(await reader() or "").casefold() if reader is not None else ""
            if "cookie" in text and ("google" in text or "privacy" in text):
                dialog_consent = True
                break
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise

    if not (host_consent or dialog_consent):
        return None
    for label in GOOGLE_SAFE_CONSENT_LABELS:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(f"^{re.escape(label)}$", re.I),
            )
            if not await button.count():
                continue
            item = button.first
            visible = getattr(item, "is_visible", None)
            if visible is not None and not await visible():
                continue
            enabled = getattr(item, "is_enabled", None)
            if enabled is not None and not await enabled():
                continue
            return label
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
    return None


async def dismiss_recognised_google_consent(page) -> Optional[str]:
    label = await recognised_google_consent(page)
    if label is None:
        return None
    button = page.get_by_role(
        "button",
        name=re.compile(f"^{re.escape(label)}$", re.I),
    ).first
    await button.click(timeout=5000)
    await asyncio.sleep(0.2)
    return label


async def google_contract_classification(
    page,
    *,
    prompt: str,
    hl: str,
    gl: str,
) -> str:
    """Classify the current Google surface without accepting a mismatch."""

    try:
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
        if parsed.path.casefold().startswith("/sorry"):
            return "blocked"
    except ValueError:
        pass
    if await recognised_google_consent(page):
        return "recoverable_consent"
    state = await google_ai_state(page)
    if state.get("status") == "blocked":
        return "blocked"
    if google_search_contract_matches(
        getattr(page, "url", ""),
        prompt=prompt,
        hl=hl,
        gl=gl,
    ):
        return "exact"
    return "contract_mismatch"


def google_shadow_absence_seconds() -> Optional[float]:
    """Resolve the collection-only shadow deadline without changing evidence."""

    if str(os.getenv("SNAPSHOT_STAGE") or "").strip().casefold() != "collection":
        return None
    raw = str(os.getenv("GOOGLE_AI_OVERVIEW_ABSENCE_SHADOW_SECONDS") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def navigate_google_search(
    page,
    prompt: str,
    hl: str,
    gl: str,
    *,
    navigation_state: Optional[dict] = None,
    timing: Optional[PromptTimingRecorder] = None,
) -> str:
    """Open Google like a person, with a direct-query reliability fallback."""

    spec = PROVIDER_SPECS[GOOGLE_AI_OVERVIEW]
    policy = resolve_provider_interaction_policy()
    contract_canary = _collection_flag_enabled(
        "SNAPSHOT_GOOGLE_CONTRACT_CLASSIFICATION_V1"
    )
    navigation_state = navigation_state if navigation_state is not None else {}
    navigation_state.setdefault("consent_dismissed", False)
    search_url = (
        f"{spec.start_url}?q={quote_plus(prompt)}&hl={quote_plus(hl)}"
        f"&gl={quote_plus(gl)}&pws=0"
    )
    navigation_state["requested_url"] = sanitise_google_url(search_url)

    async def navigate_exact() -> None:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        redirected_url = getattr(page, "url", "")
        navigation_state["redirected_url"] = sanitise_google_url(redirected_url)
        validation = google_search_contract_validation(
            redirected_url,
            prompt=prompt,
            hl=hl,
            gl=gl,
        )
        navigation_state["parameter_validation"] = validation
        if validation["exact"]:
            navigation_state["classification"] = "exact"
            return
        if not contract_canary:
            raise RuntimeError(
                "Google did not retain the exact frozen q/hl/gl/pws search contract"
            )

        classification = await google_contract_classification(
            page,
            prompt=prompt,
            hl=hl,
            gl=gl,
        )
        navigation_state["classification"] = classification
        if classification == "blocked":
            raise GoogleBlocked("Google unusual-traffic / CAPTCHA page detected")
        if (
            classification == "recoverable_consent"
            and not navigation_state["consent_dismissed"]
        ):
            label = await dismiss_recognised_google_consent(page)
            navigation_state["consent_dismissed"] = bool(label)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as exc:
                if is_browser_transport_error(exc) or page_is_closed(page):
                    raise
            final_url = getattr(page, "url", "")
            navigation_state["final_url"] = sanitise_google_url(final_url)
            validation = google_search_contract_validation(
                final_url,
                prompt=prompt,
                hl=hl,
                gl=gl,
            )
            navigation_state["parameter_validation"] = validation
            if validation["exact"]:
                navigation_state["classification"] = "exact"
                return
            classification = await google_contract_classification(
                page,
                prompt=prompt,
                hl=hl,
                gl=gl,
            )
            navigation_state["classification"] = classification
            if classification == "blocked":
                raise GoogleBlocked("Google unusual-traffic / CAPTCHA page detected")
        raise GoogleContractMismatch(
            "Google did not retain the exact frozen q/hl/gl/pws search contract"
        )

    if not policy.human_like:
        await navigate_exact()
        return "direct_query"

    home_url = (
        f"https://www.google.com/?hl={quote_plus(hl)}&gl={quote_plus(gl)}&pws=0"
    )
    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
        await provider_pause(
            policy.page_settle_min_seconds,
            policy.page_settle_max_seconds,
            policy=policy,
        )
        if contract_canary:
            label = await dismiss_recognised_google_consent(page)
            navigation_state["consent_dismissed"] = bool(label)
        else:
            await dismiss_common_consent(page)
        composer = await first_visible_locator(
            page,
            ("textarea[name='q']", "input[name='q']"),
            timeout_ms=10000,
        )
        if timing:
            timing.mark("composer_ready")
        await enter_textbox(page, composer, prompt, policy=policy, timing=timing)
        await provider_pause(
            policy.pre_submit_min_seconds,
            policy.pre_submit_max_seconds,
            policy=policy,
        )
        await verify_textbox_exact(composer, prompt)
        if timing:
            timing.mark("input_verified")
        press = getattr(composer, "press", None)
        if timing:
            timing.field("submission_action_method", "enter")
            timing.mark("send_action")
        if press is not None:
            await press("Enter")
        else:
            await page.keyboard.press("Enter")
        try:
            await page.wait_for_url(
                re.compile(r"/search(?:\?|$)"),
                wait_until="commit",
                timeout=10000,
            )
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
            if not google_search_contract_matches(
                getattr(page, "url", ""),
                prompt=prompt,
                hl=hl,
                gl=gl,
            ):
                raise RuntimeError("Google did not acknowledge the typed search")
        if timing:
            timing.mark("submission_acknowledged")
        if google_search_contract_matches(
            getattr(page, "url", ""),
            prompt=prompt,
            hl=hl,
            gl=gl,
        ):
            return "typed_search"
        if contract_canary:
            # The classification candidate observes the submitted surface as
            # it actually arrived.  It must not hide a contract mismatch by
            # issuing a second exact-URL navigation in the same attempt.
            current_url = getattr(page, "url", "")
            navigation_state["final_url"] = sanitise_google_url(current_url)
            navigation_state["parameter_validation"] = (
                google_search_contract_validation(
                    current_url,
                    prompt=prompt,
                    hl=hl,
                    gl=gl,
                )
            )
            classification = await google_contract_classification(
                page,
                prompt=prompt,
                hl=hl,
                gl=gl,
            )
            navigation_state["classification"] = classification
            if classification == "blocked":
                raise GoogleBlocked(
                    "Google unusual-traffic / CAPTCHA page detected"
                )
            if (
                classification == "recoverable_consent"
                and not navigation_state["consent_dismissed"]
            ):
                label = await dismiss_recognised_google_consent(page)
                navigation_state["consent_dismissed"] = bool(label)
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                except Exception as exc:
                    if is_browser_transport_error(exc) or page_is_closed(page):
                        raise
                current_url = getattr(page, "url", "")
                navigation_state["final_url"] = sanitise_google_url(current_url)
                navigation_state["parameter_validation"] = (
                    google_search_contract_validation(
                        current_url,
                        prompt=prompt,
                        hl=hl,
                        gl=gl,
                    )
                )
                if navigation_state["parameter_validation"]["exact"]:
                    navigation_state["classification"] = "exact"
                    return "typed_search"
                classification = await google_contract_classification(
                    page,
                    prompt=prompt,
                    hl=hl,
                    gl=gl,
                )
                navigation_state["classification"] = classification
                if classification == "blocked":
                    raise GoogleBlocked(
                        "Google unusual-traffic / CAPTCHA page detected"
                    )
            raise GoogleContractMismatch(
                "Google did not retain the exact frozen q/hl/gl/pws search contract"
            )
        # Google homepage forms currently omit gl and pws. Retain the human
        # entry/submit sequence, then correct to the exact frozen measurement
        # URL rather than silently changing the query contract.
        await navigate_exact()
        return "typed_then_exact_query"
    except asyncio.CancelledError:
        raise
    except (GoogleBlocked, GoogleContractMismatch):
        raise
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise
        await navigate_exact()
        return "direct_query_fallback"


async def run_google_ai_overview_prompt(
    page,
    prompt: str,
    timeout_seconds: int,
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> tuple[str, str, list[dict]]:
    spec = PROVIDER_SPECS[GOOGLE_AI_OVERVIEW]
    hl = os.getenv("GOOGLE_SEARCH_HL", "en")
    gl = os.getenv("GOOGLE_SEARCH_GL", "uk")
    policy = resolve_provider_interaction_policy()
    contract_canary = _collection_flag_enabled(
        "SNAPSHOT_GOOGLE_CONTRACT_CLASSIFICATION_V1"
    )
    navigation_state: dict = {}

    def record_navigation_timing() -> None:
        if not timing:
            return
        timing.field("google_requested_url", navigation_state.get("requested_url"))
        timing.field("google_redirected_url", navigation_state.get("redirected_url"))
        timing.field(
            "google_final_url",
            navigation_state.get("final_url")
            or navigation_state.get("redirected_url"),
        )
        timing.field(
            "google_parameter_validation",
            navigation_state.get("parameter_validation") or {},
        )

    try:
        timing.mark("navigation_started") if timing else None
        await navigate_google_search(
            page,
            prompt,
            hl,
            gl,
            navigation_state=navigation_state,
            timing=timing,
        )
        timing.mark("navigation_completed") if timing else None
    except GoogleBlocked as exc:
        record_navigation_timing()
        return "blocked", _status_answer(spec.label, "blocked", str(exc)), []
    except GoogleContractMismatch as exc:
        record_navigation_timing()
        return "invalid", _status_answer(spec.label, "invalid", str(exc)), []

    record_navigation_timing()
    if policy.human_like:
        await provider_pause(0.5, 1.4, policy=policy)
    else:
        await asyncio.sleep(1.0)

    if contract_canary:
        label = await recognised_google_consent(page)
        if label:
            if navigation_state.get("consent_dismissed"):
                return (
                    "invalid",
                    _status_answer(
                        spec.label,
                        "invalid",
                        "Google presented another consent surface after its one allowed recovery.",
                    ),
                    [],
                )
            await dismiss_recognised_google_consent(page)
            navigation_state["consent_dismissed"] = True
    else:
        await dismiss_common_consent(page)

    async def contract_outcome() -> str:
        final_url = getattr(page, "url", "")
        validation = google_search_contract_validation(
            final_url,
            prompt=prompt,
            hl=hl,
            gl=gl,
        )
        navigation_state["final_url"] = sanitise_google_url(final_url)
        navigation_state["parameter_validation"] = validation
        if timing:
            timing.field("google_final_url", navigation_state["final_url"])
            timing.field("google_parameter_validation", validation)
        if validation["exact"]:
            return "exact"
        if contract_canary:
            return await google_contract_classification(
                page,
                prompt=prompt,
                hl=hl,
                gl=gl,
            )
        raise RuntimeError(
            "Google did not retain the exact frozen q/hl/gl/pws search contract"
        )

    initial_contract = await contract_outcome()
    if initial_contract == "blocked":
        return "blocked", _status_answer(spec.label, "blocked", "Google unusual-traffic / CAPTCHA page detected."), []
    if initial_contract != "exact":
        return "invalid", _status_answer(spec.label, "invalid", "Google search contract mismatch after navigation or consent."), []

    shadow_seconds = google_shadow_absence_seconds()
    if timing and shadow_seconds is not None:
        timing.field("google_shadow_absence_threshold_seconds", shadow_seconds)
    started = time.monotonic()
    ordinary_results_started: Optional[float] = None
    ordinary_results_samples = 0
    shadow_crossed = False
    last_state = {}
    clicked_more = False
    while True:
        state = await google_ai_state(page)
        last_state = state
        now = time.monotonic()
        state_status = state.get("status")
        if state_status == "ok":
            if timing:
                timing.mark("first_response_detected")
            if shadow_crossed and timing:
                timing.field("google_overview_after_shadow_threshold", True)
            if state.get("showMore") and not clicked_more:
                clicked_more = await click_google_show_more(page)
                if clicked_more:
                    continue
            outcome = await contract_outcome()
            if outcome == "blocked":
                return "blocked", _status_answer(spec.label, "blocked", "Google unusual-traffic / CAPTCHA page detected."), []
            if outcome != "exact":
                return "invalid", _status_answer(spec.label, "invalid", "Google search contract changed before evidence acceptance."), []
            if timing:
                timing.mark("response_completed")
            return "ok", state.get("answer") or "", state.get("citations") or []
        if state_status == "blocked":
            return "blocked", _status_answer(spec.label, "blocked", state.get("detail", "")), state.get("citations") or []
        if state_status == "waiting_or_not_shown":
            ordinary_results_samples += 1
            if ordinary_results_samples == 2:
                ordinary_results_started = now
            if (
                shadow_seconds is not None
                and not shadow_crossed
                and ordinary_results_started is not None
                and now - ordinary_results_started >= shadow_seconds
            ):
                shadow_crossed = True
                if timing:
                    timing.mark("google_shadow_absence_threshold")
                    timing.field("google_overview_after_shadow_threshold", False)
        else:
            # Loading, an unstable/empty overview, or an unrelated surface
            # breaks the continuous ordinary-results interval.
            ordinary_results_started = None
            ordinary_results_samples = 0
        # Provider state is intentionally polled before this deadline check, so
        # an overview appearing exactly at the boundary wins.
        if now - started >= timeout_seconds:
            break
        await asyncio.sleep(min(1.0, max(0.0, timeout_seconds - (now - started))))

    outcome = await contract_outcome()
    if outcome == "blocked":
        return "blocked", _status_answer(spec.label, "blocked", "Google unusual-traffic / CAPTCHA page detected."), []
    if outcome != "exact":
        return "invalid", _status_answer(spec.label, "invalid", "Google search contract changed before not_shown acceptance."), []
    if timing:
        timing.mark("response_completed")
    detail = last_state.get("detail") or "AI Overview was not shown before the timeout."
    return "not_shown", _status_answer(spec.label, "not_shown", detail), last_state.get("citations") or []


async def _run_copilot_turn(
    page,
    prompt: str,
    timeout_seconds: int,
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> tuple[str, str, list[dict]]:
    spec = PROVIDER_SPECS[COPILOT]
    policy = resolve_provider_interaction_policy()
    exact_answer_selectors = spec.answer_selectors[:3]
    try:
        composer = await first_visible_locator(page, spec.composer_selectors, timeout_ms=30000)
    except Exception as exc:
        if is_browser_transport_error(exc) or page_is_closed(page):
            raise
        return "composer_missing", _status_answer(spec.label, "composer_missing", "No Copilot composer was found."), []
    if timing:
        timing.mark("composer_ready")
    try:
        before_text = _clean_answer(
            await extract_last_text(
                page,
                exact_answer_selectors,
                fallback_to_root=False,
            ),
            remove_prefixes=("Copilot said",),
        )
    except Exception:
        before_text = ""
    before_count = await page.locator(
        "[data-content='ai-message'], [data-testid='ai-message']"
    ).count()
    await enter_textbox(page, composer, prompt, policy=policy, timing=timing)
    try:
        await submit_textbox(
            page,
            composer,
            prompt,
            spec,
            policy=policy,
            timing=timing,
        )
    except ProviderSubmissionUnavailable as exc:
        return "send_missing", _status_answer(spec.label, "send_missing", str(exc)), []
    started = time.time()
    last_text = ""
    last_change_at = started
    while time.time() - started < timeout_seconds:
        try:
            text = await extract_last_text(
                page,
                exact_answer_selectors,
                fallback_to_root=False,
            )
            text = _clean_answer(text, remove_prefixes=("Copilot said",))
            response_count = await page.locator(
                "[data-content='ai-message'], [data-testid='ai-message']"
            ).count()
            is_new = response_count > before_count or (text and text != before_text)
            if is_new and text:
                if not last_text and timing:
                    timing.mark("first_response_detected")
                now = time.time()
                if text != last_text:
                    last_text = text
                    last_change_at = now
                stop_visible = await locator_has_visible_item(
                    page.locator(", ".join(spec.stop_selectors))
                )
                # Avoid saving the first streaming fragment. Completion requires
                # both stable AI-message text and the current Stop control to be
                # gone; body/user-prompt changes are never treated as an answer.
                stable_for = now - last_change_at
                if not stop_visible and stable_for >= 2.0:
                    if timing:
                        timing.mark("response_completed")
                    return "ok", last_text, []
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
        await asyncio.sleep(0.5)
    if last_text:
        return "partial", last_text, []
    return "timeout", _status_answer(spec.label, "timeout", "Copilot did not produce extractable answer text before the timeout."), []


async def run_copilot_prompt(
    page,
    prompt: str,
    timeout_seconds: int,
    *,
    timing: Optional[PromptTimingRecorder] = None,
) -> tuple[str, str, list[dict]]:
    spec = PROVIDER_SPECS[COPILOT]
    policy = resolve_provider_interaction_policy()
    if timing:
        timing.mark("navigation_started")
    await page.goto(spec.start_url, wait_until="domcontentloaded", timeout=60000)
    if timing:
        timing.mark("navigation_completed")
    if policy.human_like:
        await provider_pause(
            max(1.0, policy.page_settle_min_seconds),
            max(2.0, policy.page_settle_max_seconds),
            policy=policy,
        )
    else:
        await asyncio.sleep(2.0)
    body = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    low_body = body.casefold()
    account_changed = (
        all(marker in low_body for marker in ("account changed", "signed out of copilot"))
        or ("signed out" in low_body and "reload to continue" in low_body)
    )
    if account_changed:
        # Copilot commonly leaves this recoverable wall when another old tab
        # changes account state. Mirror the offered human action once; never loop
        # or let recovery affect another profile/tab.
        try:
            reload_button = page.get_by_role(
                "button",
                name=re.compile(r"reload(?: to continue)?", re.I),
            ).first
            if await reload_button.count() and await reload_button.is_visible():
                await _move_pointer_to(page, reload_button)
                await reload_button.click(timeout=5000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception as exc:
                    if is_browser_transport_error(exc) or page_is_closed(page):
                        raise
                await provider_pause(0.8, 1.8, policy=policy)
                body = await page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
                low_body = body.casefold()
                account_changed = (
                    all(
                        marker in low_body
                        for marker in ("account changed", "signed out of copilot")
                    )
                    or ("signed out" in low_body and "reload to continue" in low_body)
                )
        except Exception as exc:
            if is_browser_transport_error(exc) or page_is_closed(page):
                raise
    signed_out = (
        all(marker in low_body for marker in ("sign in to copilot", "sign in with microsoft"))
        or account_changed
    )
    if signed_out:
        return (
            "auth_required",
            _status_answer(
                spec.label,
                "auth_required",
                "Copilot presented a sign-in/account-changed wall. Reload or sign in to this persistent profile before collection.",
            ),
            [],
        )
    return await _run_copilot_turn(page, prompt, timeout_seconds, timing=timing)


async def run_provider_prompt(
    context,
    platform: str,
    index: int,
    prompt_id: str,
    prompt: str,
    metadata: dict,
    output_dir: Path,
    timeout_seconds: int,
    *,
    page_factory: Optional[Callable[[], Awaitable[object]]] = None,
    profile_id: Optional[str] = None,
    profile_name: Optional[str] = None,
    chrome_directory: Optional[str] = None,
    profile: Optional[str] = None,
    profile_label: Optional[str] = None,
    close_page: bool = True,
    browser_health_before: Optional[dict] = None,
    cdp_url: Optional[str] = None,
    data_dir: Optional[Path] = None,
    lane_index: int = 0,
    configured_x: int = 1,
    worker_index: int = 0,
    stage: Optional[str] = None,
    outer_attempt: Optional[int] = None,
    timing_enabled: Optional[bool] = None,
) -> PromptRunResult:
    """Run one provider prompt and persist its answer.

    ``page_factory`` is used by production to open a tab from a profile anchor.
    The optional fallback to ``context.new_page`` preserves the small injected
    contexts used by focused tests and non-CDP callers.
    """

    spec = PROVIDER_SPECS[platform]
    profile_identity = _profile_artifact_fields(
        profile_id=profile_id,
        profile_name=profile_name,
        chrome_directory=chrome_directory,
        profile=profile,
        profile_label=profile_label,
    )
    started = time.time()
    attempts = max(1, env_int("AI_PROVIDER_ATTEMPTS", 2))
    final_status = "error"
    final_answer = ""
    final_citations: list[dict] = []
    final_detail = ""
    attempt_details: list[str] = []
    transport_diagnostic: Optional[dict] = None
    final_timing: Optional[PromptTimingRecorder] = None
    final_timing_error: Optional[BaseException] = None
    final_page = None

    for attempt in range(1, attempts + 1):
        try:
            timing: Optional[PromptTimingRecorder] = PromptTimingRecorder.from_env(
                platform=platform,
                prompt_id=prompt_id,
                profile_id=profile_identity.get("profile_id"),
                lane_index=lane_index,
                configured_x=configured_x,
                worker_index=worker_index,
                data_dir=data_dir,
                stage=stage,
                outer_attempt=outer_attempt,
                enabled=timing_enabled,
            )
        except Exception:
            # Telemetry construction is outside the evidence contract. Even a
            # broken clock/path/recorder must not change provider behavior.
            timing = None
        if timing:
            timing.mark("worker_scheduled")
        page = None
        attempt_error: Optional[BaseException] = None
        try:
            will_open_page = True
            probe = getattr(page_factory, "will_open_new_page", None)
            if callable(probe):
                try:
                    will_open_page = bool(probe())
                except Exception:
                    will_open_page = False
            if timing and will_open_page:
                timing.mark("tab_open_started")
            page = await page_factory() if page_factory is not None else await context.new_page()
            if timing:
                timing.mark("tab_ready")
            if platform == GEMINI:
                status, answer, citations = await run_gemini_prompt(
                    page,
                    prompt,
                    timeout_seconds,
                    timing=timing,
                )
            elif platform == GOOGLE_AI_OVERVIEW:
                status, answer, citations = await run_google_ai_overview_prompt(
                    page,
                    prompt,
                    timeout_seconds,
                    timing=timing,
                )
            elif platform == COPILOT:
                status, answer, citations = await run_copilot_prompt(
                    page,
                    prompt,
                    timeout_seconds,
                    timing=timing,
                )
            else:
                raise ValueError(f"Platform {platform} is not handled by this browser runner")
            final_status, final_answer, final_citations = status, answer, citations
            if status == "auth_required":
                final_detail = (
                    "Copilot requires a persistent Chrome profile signed in with a supported account."
                )
            elif status == "composer_missing":
                final_detail = "The provider page exposed no usable chat composer."
            elif status == "send_missing":
                final_detail = str(answer or "").partition("\n\n")[2].strip() or (
                    "The verified prompt could not be submitted by button or Enter."
                )
            elif status == "invalid":
                final_detail = str(answer or "").partition("\n\n")[2].strip() or (
                    "The provider result did not satisfy the frozen measurement contract."
                )
            attempt_details.append(f"attempt {attempt}: {status}")
        except asyncio.CancelledError as exc:
            attempt_error = exc
            final_status = "cancelled"
            raise
        except Exception as exc:
            attempt_error = exc
            final_status = "error"
            final_detail = f"{type(exc).__name__}: {exc}"
            if page is not None and (is_browser_transport_error(exc) or page_is_closed(page)):
                transport_diagnostic = await browser_transport_diagnostic(
                    page,
                    browser_health_before,
                    cdp_url=cdp_url,
                )
                final_detail += (
                    f"; browser_transport_scope={transport_diagnostic['scope']}"
                    f"; cdp_endpoint_reachable="
                    f"{transport_diagnostic['endpoint_after'].get('reachable')}"
                    f"; browser_session_changed="
                    f"{transport_diagnostic.get('browser_session_changed')}"
                )
            final_answer = _status_answer(spec.label, "error", final_detail)
            final_citations = []
            attempt_details.append(f"attempt {attempt}: error: {final_detail}")
        finally:
            terminal_attempt = (
                final_status not in RETRYABLE_STATUSES or attempt >= attempts
            )
            if close_page and page is not None and (
                isinstance(attempt_error, asyncio.CancelledError) or not terminal_attempt
            ):
                try:
                    await page.close()
                    if timing:
                        timing.mark("tab_closed")
                except Exception:
                    pass
            elif close_page and page is not None:
                final_page = page
            if isinstance(attempt_error, asyncio.CancelledError):
                if timing:
                    timing.finish("cancelled", attempt_error)
        if final_status not in RETRYABLE_STATUSES:
            final_timing = timing
            final_timing_error = attempt_error
            break
        if attempt < attempts:
            if timing:
                timing.finish(final_status, attempt_error)
            await asyncio.sleep(1.0 + attempt)
        else:
            final_timing = timing
            final_timing_error = attempt_error

    if len(attempt_details) > 1:
        final_detail = "; ".join(attempt_details)
    persistence_error: Optional[BaseException] = None
    try:
        out = write_answer_file(
            output_dir,
            index,
            prompt_id,
            prompt,
            _metadata(
                metadata,
                platform,
                final_status,
                detail=final_detail,
                citations=final_citations,
                **profile_identity,
            ),
            final_answer,
        )
        if final_timing:
            final_timing.mark("answer_persisted")
    except Exception as exc:
        persistence_error = exc
        raise
    finally:
        if final_page is not None:
            try:
                await final_page.close()
                if final_timing:
                    final_timing.mark("tab_closed")
            except Exception:
                pass
        if persistence_error is not None and final_timing:
            final_timing.finish("error", persistence_error)
    if final_timing:
        final_timing.finish(final_status, final_timing_error)
    return PromptRunResult(
        platform,
        prompt_id,
        index,
        final_status,
        str(out),
        detail=final_detail,
        citations=final_citations,
        elapsed_seconds=time.time() - started,
        transport_diagnostic=transport_diagnostic,
        **profile_identity,
    )


async def connect_over_cdp_with_backoff(browser_type, cdp_url: str):
    """Attach to the shared browser without burning a run during startup."""

    attempts = max(1, env_int("AI_PROVIDER_CDP_ATTEMPTS", 6))
    base_delay = max(0.1, env_float("AI_PROVIDER_CDP_RETRY_DELAY_SECONDS", 1.0))
    for attempt in range(1, attempts + 1):
        try:
            return await browser_type.connect_over_cdp(cdp_url, timeout=20000)
        except Exception:
            if attempt >= attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 8.0)
            print(
                f"[browser] CDP unavailable; retrying in {delay:.1f}s "
                f"({attempt}/{attempts}).",
                flush=True,
            )
            await asyncio.sleep(delay)


async def select_provider_context(browser, platform: str):
    """Compatibility shim returning the first configured persistent context.

    Production fan-out uses profile anchors rather than this context because
    Playwright can merge every real Chrome profile into one context.  The shim
    intentionally never creates an anonymous/incognito context.
    """

    normalize_platform(platform)
    profiles = load_logged_in_profile_pool()
    anchors = await pair_profile_anchors(browser, profiles)
    return anchors[0].page.context, False


async def run_provider(
    platform: str,
    prompts_path: Path,
    data_dir: Path,
    *,
    output_dir: Optional[Path] = None,
    cdp_url: Optional[str] = None,
    parallel_tabs: Optional[int] = None,
    tabs_per_profile: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> list[PromptRunResult]:
    platform = normalize_platform(platform)
    if platform == CHATGPT:
        raise ValueError("ChatGPT uses clinic_geo.browser.run_gpt_prompts, not run_provider().")
    prompts = load_prompts(Path(prompts_path))
    if not prompts:
        raise RuntimeError(f"No prompts found in {prompts_path}")
    output_dir = Path(output_dir or (Path(data_dir) / "raw_gpt_answers"))
    output_dir.mkdir(parents=True, exist_ok=True)
    cdp_url = cdp_url or os.getenv("CDP_URL", "http://127.0.0.1:9222")
    parallel_tabs = max(1, int(parallel_tabs or env_int("AI_PROVIDER_TABS", 1)))
    # Per-profile fan-out is stage-owned and must be passed explicitly by the
    # measurement collector. Other callers (notably recommendation extraction
    # and classification) retain the historical one-tab-per-profile behavior,
    # even if they run in a process with measurement environment variables.
    tabs_per_profile = max(1, int(tabs_per_profile if tabs_per_profile is not None else 1))
    timeout_seconds = max(10, int(timeout_seconds or env_int("AI_PROVIDER_RESPONSE_TIMEOUT", 180)))
    profiles = load_logged_in_profile_pool()
    worker_tab_count = max(
        1,
        min(len(prompts), parallel_tabs, len(profiles) * tabs_per_profile),
    )
    interaction_policy = resolve_provider_interaction_policy()
    capture_browser_health = bool(str(os.getenv("SNAPSHOT_PLATFORM_ID") or "").strip())
    browser_health_before = (
        await asyncio.to_thread(cdp_endpoint_health, cdp_url)
        if capture_browser_health
        else None
    )

    from playwright.async_api import async_playwright

    results: list[PromptRunResult] = []
    async with async_playwright() as p:
        browser = await connect_over_cdp_with_backoff(p.chromium, cdp_url)
        try:
            profile_anchors = await pair_profile_anchors(browser, profiles)
            page_open_lock = asyncio.Lock()
            # Build bounded tab lanes round-robin over the profile pool. Each
            # lane owns and reuses one page, so prompts never race on a shared
            # page while one profile can still run up to tabs_per_profile lanes.
            worker_slots = [
                (worker_index, profile_anchors[worker_index % len(profile_anchors)])
                for worker_index in range(worker_tab_count)
            ]
            worker_pages: dict[int, object] = {}

            async def worker_page(worker_index: int, anchor: ProfileAnchor):
                page = worker_pages.get(worker_index)
                is_closed = getattr(page, "is_closed", False) if page is not None else True
                try:
                    closed = bool(is_closed()) if callable(is_closed) else bool(is_closed)
                except Exception:
                    closed = True
                if page is not None and not closed:
                    return page
                page = await open_profile_page(anchor, lock=page_open_lock)
                worker_pages[worker_index] = page
                return page

            async def run_worker(
                worker_index: int,
                anchor: ProfileAnchor,
                assignments: list[tuple[int, tuple[str, str, dict]]],
            ) -> list[PromptRunResult]:
                identity = anchor.identity
                worker_results = []

                async def owned_page_factory():
                    return await worker_page(worker_index, anchor)

                def will_open_new_page() -> bool:
                    page = worker_pages.get(worker_index)
                    return page is None or page_is_closed(page)

                owned_page_factory.will_open_new_page = will_open_new_page  # type: ignore[attr-defined]

                for assignment_index, (
                    index,
                    (prompt_id, prompt, metadata),
                ) in enumerate(assignments):
                    final_assignment = assignment_index == len(assignments) - 1
                    print(
                        f"[{platform}] ({index}) {prompt_id} via "
                        f"{identity.display_name} "
                        f"[{identity.id}; {identity.chrome_directory}; "
                        f"tab {worker_index // len(profile_anchors) + 1}]",
                        flush=True,
                    )
                    worker_results.append(
                        await run_provider_prompt(
                            anchor.page.context,
                            platform,
                            index,
                            prompt_id,
                            prompt,
                            metadata,
                            output_dir,
                            timeout_seconds,
                            page_factory=owned_page_factory,
                            profile_id=identity.id,
                            profile_name=identity.display_name,
                            chrome_directory=identity.chrome_directory,
                            # Persist the final answer before closing the lane
                            # page, so its timing record includes tab_closed and
                            # outer cleanup does not immediately close it again.
                            close_page=final_assignment,
                            browser_health_before=browser_health_before,
                            cdp_url=cdp_url,
                            data_dir=Path(data_dir),
                            lane_index=worker_index // len(profile_anchors),
                            configured_x=tabs_per_profile,
                            worker_index=worker_index,
                            stage=os.getenv("SNAPSHOT_STAGE"),
                        )
                    )
                return worker_results

            buckets: list[list[tuple[int, tuple[str, str, dict]]]] = [
                [] for _ in worker_slots
            ]
            for index, prompt_entry in enumerate(prompts, start=1):
                buckets[(index - 1) % len(worker_slots)].append((index, prompt_entry))
            worker_tasks = [
                asyncio.create_task(
                    run_worker(worker_index, anchor, buckets[slot_index])
                )
                for slot_index, (worker_index, anchor) in enumerate(worker_slots)
                if buckets[slot_index]
            ]
            try:
                grouped_results = await asyncio.gather(*worker_tasks)
                results = sorted(
                    (result for group in grouped_results for result in group),
                    key=lambda result: result.index,
                )
            except BaseException:
                # asyncio.gather propagates the first failure without stopping
                # siblings. Quiesce every lane before taking the cleanup page
                # snapshot, otherwise a surviving lane can open a replacement
                # page after cleanup and leak it in persistent Chrome.
                for task in worker_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
                raise
            finally:
                for page in list(worker_pages.values()):
                    if page_is_closed(page):
                        continue
                    try:
                        await page.close()
                    except Exception:
                        pass
        finally:
            # This worker owns only its CDP connection and pages. Never close
            # the persistent browser or anchor windows: other platform sessions
            # may be using their own tabs in these same profile contexts.
            pass

    browser_health_after = (
        await asyncio.to_thread(cdp_endpoint_health, cdp_url)
        if capture_browser_health
        else None
    )
    timing_path = Path(data_dir) / TIMING_FILENAME
    timing_record_count = 0
    try:
        if timing_path.is_file():
            timing_record_count = sum(
                1
                for line in timing_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    except Exception:
        timing_record_count = 0
    manifest = {
        "platform": platform,
        "platform_label": PROVIDER_SPECS[platform].label,
        "prompts_path": str(prompts_path),
        "data_dir": str(data_dir),
        "output_dir": str(output_dir),
        "interaction_policy": asdict(interaction_policy),
        "tab_strategy": "bounded_worker_owned_tabs_per_leased_profile",
        "parallel_tab_limit": parallel_tabs,
        "tabs_per_profile": tabs_per_profile,
        "worker_tab_count": worker_tab_count,
        **(
            {
                "prompt_timings_reference": TIMING_FILENAME,
                "prompt_timing_record_count": timing_record_count,
            }
            if timing_record_count
            else {}
        ),
        **(
            {
                "browser_before": browser_health_before,
                "browser_after": browser_health_after,
                "browser_session_changed": cdp_session_changed(
                    browser_health_before,
                    browser_health_after,
                ),
            }
            if browser_health_before is not None and browser_health_after is not None
            else {}
        ),
        "profile_pool": [
            _profile_artifact_fields(
                profile_id=row.id,
                profile_name=row.display_name,
                chrome_directory=row.chrome_directory,
            )
            for row in profiles
        ],
        "results": [asdict(result) for result in results],
    }
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    (Path(data_dir) / "platform_run.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return results
