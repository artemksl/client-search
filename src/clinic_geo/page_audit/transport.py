"""One-page, one-new-conversation browser transport for the AI audit lane.

Adapted (copied, never imported) from the archived
``archive/assist-v1-triple-batch/assist/browser_model_client.py``. Differences
from the archive:

* exactly ONE prompt per engine call (one page, one new chat) — no batching;
* only the single resolved profile is passed to the engine;
* ``new_chat`` semantics with the pinned model;
* the engine's internal rate-limit retry is disabled (``assist_no_rate_limit_retry``)
  so a rate limit is a terminal failure for the item and the scheduler retries it
  in a later wave;
* per-page terminal states map onto the scheduler's closed set, including the
  distinct ``submission_integrity_unverified`` state.

Submission-integrity seam (ADR-002 §5 / research §6). ChatGPT converts pastes
over ~5,000 characters into an attachment, so packets must land as in-composer
text. The shared engine ALREADY establishes this guarantee: ``chat_engine``'s
``verify_composer_prompt`` reads the composer back and raises
``PromptVerificationError`` unless the exact frozen prompt is present, and it
refuses to send otherwise (fail-closed — no transcript is written). This module
USES that existing seam: an engine call that raises ``PromptVerificationError``
yields the terminal state ``submission_integrity_unverified``; an item whose
submission cannot be verified never proceeds silently. See the module note in
the stage hand-back for what this can and cannot guarantee through the shared
``chat_engine.run`` orchestration.

Response parsing is strict and fail-closed: transcript presence/identity is
verified and exactly one JSON container must be extractable, or the item is a
terminal non-success. Echo-field and content validation are Stage 4's job.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional

from clinic_geo.browser import chat_engine
from clinic_geo.parsing.answer_files import (
    answer_body,
    classify_answer_text,
    parse_answer_markdown,
)
from clinic_geo.persistence.outputs import (
    answer_filename,
    safe_filename,
    write_answer_file,
)
from clinic_geo.page_audit.profile import ResolvedProfile


TRANSCRIPT_RELATIVE_ROOT = Path("data/paid-audit/page-audit/transcripts")

_REGENERATE_SELECTORS = (
    'button[data-testid="regenerate-button"]',
    'button[aria-label*="Regenerate"]',
    'button:has-text("Regenerate")',
)
_LOGIN_SELECTORS = (
    'a[href*="/auth/login"]',
    'button[data-testid="login-button"]',
    'button:has-text("Log in")',
)
_CONVERSATION_MISSING_SELECTORS = (
    '[data-testid="conversation-not-found"]',
    ':text-is("Conversation not found")',
    ':text-is("Unable to load conversation")',
)
_CONTENT_POLICY_SELECTORS = (
    '[data-testid="content-policy-violation"]',
    '[data-testid="blocked-message"]',
    '[role="alert"]:has-text("usage policies")',
    '[role="alert"]:has-text("content policy")',
    ':text-is("This content may violate our usage policies")',
)
_CHAT_ERROR_SELECTORS = (
    '[data-testid="conversation-turn-error"]',
    '[data-testid="message-error"]',
    '[role="alert"]:has-text("Something went wrong")',
    ':text-is("Something went wrong")',
)

EngineEntry = tuple[int, str, str, dict]
EngineRunner = Callable[
    [chat_engine.Regime, list[EngineEntry], chat_engine.EngineConfig],
    Awaitable[object],
]


class TransportError(RuntimeError):
    """Base error for fail-closed page-audit transport behaviour."""


class TransportConfigurationError(TransportError):
    """The page-audit transport is not safely configured."""


class TranscriptVerificationError(TransportError):
    """A requested page answer has no trustworthy transcript payload."""


# --- strict single-JSON extraction (copied from the archived client) --------
def _json_container(value: object) -> object:
    if not isinstance(value, (dict, list)):
        raise TranscriptVerificationError(
            "model answer must contain one JSON object or array"
        )
    return value


def _contains_json_container(text: str) -> bool:
    """Return whether prose contains another independently parseable container."""

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


_JSON_FENCE_RE = re.compile(r"```[ \t]*json\b(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_single_json_value(answer: str) -> object:
    """Extract exactly one JSON object/array from a model answer.

    Accepted forms are the complete answer as JSON, or one fenced ``json`` block
    embedded in short prose. Zero containers, multiple blocks, malformed
    alternatives, and a second JSON value all fail closed.
    """

    if not isinstance(answer, str) or not answer.strip():
        raise TranscriptVerificationError("model answer is empty")
    stripped = answer.strip()
    try:
        return _json_container(json.loads(stripped))
    except json.JSONDecodeError:
        pass

    fences = list(_JSON_FENCE_RE.finditer(stripped))
    if len(fences) != 1:
        raise TranscriptVerificationError(
            "model answer must contain exactly one complete JSON value"
        )
    fence = fences[0]
    fenced_text = fence.group(1).strip()
    try:
        payload = _json_container(json.loads(fenced_text))
    except json.JSONDecodeError as exc:
        raise TranscriptVerificationError(
            "model answer contains malformed JSON"
        ) from exc
    outside = stripped[: fence.start()] + stripped[fence.end() :]
    if _contains_json_container(outside):
        raise TranscriptVerificationError(
            "model answer contains a second JSON value"
        )
    return payload


# --- repo-bounded, symlink-rejecting transcript root (copied) ---------------
def _reject_symlink_components(repo_root: Path, target: Path) -> None:
    relative = target.relative_to(repo_root)
    current = repo_root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise TransportConfigurationError(
                "page-audit transcript paths cannot contain symbolic links"
            )


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TransportConfigurationError(
            f"{field_name} must be a positive integer"
        )
    return value


def _nonblank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransportConfigurationError(f"{field_name} must be a non-blank string")
    return value.strip()


def _normalise_ws(value: object) -> str:
    """Collapse whitespace so DOM text compares stably across renders."""

    return re.sub(r"\s+", " ", str(value or "")).strip()


@dataclass(frozen=True)
class PageDispatchResult:
    """One page's terminal transport outcome with local provenance.

    ``payload`` is the raw parsed JSON value on success, else ``None``. Echo-field
    and content validation are deferred to Stage 4.
    """

    terminal_state: str
    payload: object
    transcript_ref: Optional[str]
    conversation_ref: Optional[str]
    model_id: str
    model_version: str
    terminal_time: float
    detail: Optional[str] = None


class PageAuditTransport:
    """Async wrapper that runs one page through one new logged-in conversation."""

    def __init__(
        self,
        repo_root: Path | str,
        profile: ResolvedProfile,
        *,
        model_id: str,
        model_version: str,
        engine_runner: Optional[EngineRunner] = None,
        cdp_url: Optional[str] = None,
        chatgpt_url: Optional[str] = None,
        profiles_dir: Path | str | None = None,
        response_timeout: int = 900,
        human_like: Optional[bool] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        if not isinstance(profile, ResolvedProfile):
            raise TransportConfigurationError(
                "profile must be a resolved ResolvedProfile (never a fallback)"
            )
        if not profile.chrome_directory.strip():
            raise TransportConfigurationError(
                "resolved profile has no Chrome directory"
            )
        self.profile = profile
        self.model_id = _nonblank(model_id, "model_id")
        self.model_version = _nonblank(model_version, "model_version")
        self.cdp_url = str(cdp_url or "http://127.0.0.1:9222").strip()
        self.chatgpt_url = str(chatgpt_url or "https://chatgpt.com/").strip()
        if not self.cdp_url or not self.chatgpt_url:
            raise TransportConfigurationError(
                "CDP and ChatGPT URLs must be non-blank"
            )
        self.profiles_dir = (
            Path(profiles_dir).expanduser()
            if profiles_dir is not None
            else self.repo_root / "config" / "chrome-profiles"
        )
        self.response_timeout = _positive_int(response_timeout, "response_timeout")
        self.human_like = (
            chat_engine.resolve_human_like() if human_like is None else bool(human_like)
        )
        self._engine_runner: EngineRunner = engine_runner or chat_engine.run
        self.clock = clock

    @property
    def transcript_root(self) -> Path:
        return self.repo_root / TRANSCRIPT_RELATIVE_ROOT

    # -- public API --------------------------------------------------------
    async def dispatch_page(
        self,
        run_ref: str,
        prompt_id: str,
        prompt_text: str,
        metadata: Optional[Mapping[str, object]] = None,
        *,
        send_only: bool = False,
    ) -> PageDispatchResult:
        """Dispatch exactly one page prompt into one new conversation.

        Always returns a terminal :class:`PageDispatchResult`; it never raises for
        a model/transport failure and never returns a spurious success. Only a
        configuration error (raised before dispatch) propagates.

        With ``send_only=True`` the engine stops after the verified send and the
        result is the non-final state ``sent_awaiting_response`` carrying the
        conversation's permanent URL in ``conversation_ref``; the answer is
        collected later by :meth:`harvest_page`.
        """

        run_ref = _nonblank(run_ref, "run_ref")
        prompt_id = _nonblank(prompt_id, "prompt_id")
        if not isinstance(prompt_text, str) or not prompt_text:
            raise TransportConfigurationError("prompt_text must be a non-empty string")

        output_dir = self._fresh_output_dir(run_ref)
        cfg = self._engine_config(output_dir, send_only=send_only)
        regime = chat_engine.resolve_regime("logged_in")
        entries: list[EngineEntry] = [
            (1, prompt_id, prompt_text, dict(metadata or {}))
        ]

        try:
            await self._engine_runner(regime, entries, cfg)
        except chat_engine.PromptVerificationError as exc:
            # Existing composer-readback seam: the exact frozen prompt could not
            # be proven in the composer (e.g. paste->attachment conversion). The
            # engine refused to send; the item never proceeds silently.
            return self._terminal(
                "submission_integrity_unverified",
                None,
                None,
                detail=str(exc) or "composer readback failed",
            )
        except chat_engine.RateLimitError as exc:
            return self._terminal(
                "failed", None, None, detail=f"rate_limited: {exc}"
            )
        except (asyncio.TimeoutError, chat_engine.PlaywrightTimeoutError) as exc:
            return self._terminal(
                "timed_out", None, None, detail=str(exc)
            )
        except SystemExit as exc:
            # The shared engine historically exits the process when CDP is
            # unreachable. Preserve that behavior for its other callers while
            # converting it to this lane's typed transport result at the seam.
            return self._terminal(
                "failed",
                None,
                None,
                detail=f"engine_system_exit: {exc.code}",
            )
        except TransportError:
            raise
        except Exception as exc:  # operational transport failure — fail closed
            return self._terminal(
                "failed", None, None, detail=f"engine_error: {exc}"
            )

        if send_only:
            # The engine detached after the verified send; the sidecar is the
            # only proof of where the answer will appear. Fail closed without it.
            try:
                sidecar = self._read_single_sidecar(output_dir, prompt_id)
            except TranscriptVerificationError as exc:
                return self._terminal(
                    "failed", None, None, detail=str(exc)
                )
            return self._terminal(
                "sent_awaiting_response",
                None,
                None,
                conversation_ref=sidecar["conversation_url"],
                detail=json.dumps({"sent_at": sidecar["sent_at"]}),
            )

        # Verify exactly one expected transcript, fail-closed on every anomaly.
        try:
            answer = self._read_single_transcript(output_dir, prompt_id)
            payload = extract_single_json_value(answer)
        except TranscriptVerificationError as exc:
            return self._terminal(
                "failed", None, None, detail=str(exc)
            )

        transcript_ref = (output_dir / answer_filename(1, prompt_id)).relative_to(
            self.repo_root
        ).as_posix()
        return self._terminal(
            "succeeded",
            payload,
            transcript_ref,
            # The logged-in answer-file contract exposes no live conversation URL;
            # the durable transcript file IS the conversation reference.
            conversation_ref=transcript_ref,
        )

    async def harvest_page(
        self,
        run_ref: str,
        prompt_id: str,
        conversation_url: str,
        *,
        identity_probe: str,
        settle_seconds: float = 5.0,
    ) -> PageDispatchResult:
        """Revisit a sent conversation and collect its finished answer.

        Returns ``still_generating`` (non-final; the caller reschedules) while
        the response is in flight, ``succeeded`` with the parsed payload once the
        answer verifies, or a distinct fail-closed UI/transport outcome.
        Identity is re-verified before assistant content
        is read: the conversation's first user
        message must contain ``identity_probe``, which the caller derives from
        PAGE-SPECIFIC packet content (e.g. the ``"page_id":"..."`` fragment) —
        the shared prompt preamble is identical across pages and must not be
        used. Harvesting the wrong chat is therefore structurally impossible.
        """

        run_ref = _nonblank(run_ref, "run_ref")
        prompt_id = _nonblank(prompt_id, "prompt_id")
        conversation_url = _nonblank(conversation_url, "conversation_url")
        probe = _normalise_ws(identity_probe)
        if not probe:
            raise TransportConfigurationError(
                "identity_probe must be a non-blank string"
            )

        from playwright.async_api import async_playwright

        project_url = (self.profile.project_url or "").strip() or None
        try:
            reference = chat_engine.validate_sent_conversation_reference(
                conversation_url,
                project_url=project_url,
            )
        except ValueError as exc:
            return self._terminal(
                "failed",
                None,
                None,
                conversation_ref=conversation_url,
                detail=f"conversation_url_invalid: {exc}",
            )
        conversation_url = reference["conversation_url"]
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(self.cdp_url)
                try:
                    # Open the tab in the SEAT'S Chrome profile, not whichever
                    # context happens to be first — other profile windows may be
                    # open, and a wrong-profile tab is not logged into the same
                    # ChatGPT account (the conversation would look missing).
                    identity = chat_engine.LoggedInProfile(
                        id=self.profile.id or "assist",
                        display_name=self.profile.display_name,
                        chrome_directory=self.profile.chrome_directory,
                        project_url=self.profile.project_url or "",
                    )
                    anchors = await chat_engine.pair_profile_anchors(
                        browser, [identity]
                    )
                    if len(anchors) != 1:
                        return self._terminal(
                            "failed",
                            None,
                            None,
                            conversation_ref=conversation_url,
                            detail=f"profile_anchor_unmatched:{len(anchors)}",
                        )
                    try:
                        # Same profile-owned popup handshake used by send. It
                        # verifies the raw CDP browser-context id before its
                        # first navigation; a generic context-created tab
                        # cannot do that when Playwright merges profiles.
                        page = await chat_engine.open_chat_tab(
                            anchors[0], conversation_url
                        )
                    except Exception as exc:
                        return self._terminal(
                            "transient_navigation_failure",
                            None,
                            None,
                            conversation_ref=conversation_url,
                            detail=f"verified_profile_navigation_failed: {exc}",
                        )
                    try:
                        await asyncio.sleep(3.0)

                        explicit = await self._explicit_harvest_state(page)
                        if explicit is not None:
                            state, detail = explicit
                            return self._terminal(
                                state,
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=detail,
                            )
                        try:
                            landed = chat_engine.validate_sent_conversation_reference(
                                page.url,
                                project_url=project_url,
                            )["conversation_url"]
                        except (AttributeError, ValueError) as exc:
                            return self._terminal(
                                "transient_navigation_failure",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=f"landed_conversation_url_invalid: {exc}",
                            )
                        if landed != conversation_url:
                            return self._terminal(
                                "transient_navigation_failure",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="landed_conversation_url_mismatch",
                            )

                        user_turns = page.locator(chat_engine.USER_SELECTOR)
                        try:
                            if await user_turns.count() == 0:
                                raise LookupError("first user turn selector found no matches")
                            first_user = _normalise_ws(
                                await user_turns.first.inner_text(timeout=20000)
                            )
                        except Exception:
                            return self._terminal(
                                "unknown_ui_state",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="first_user_turn_unavailable",
                            )
                        if probe not in first_user:
                            return self._terminal(
                                "failed",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="conversation_identity_mismatch",
                            )

                        state, detail = await self._harvest_response_state(page)
                        if state != "complete":
                            return self._terminal(
                                state,
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=detail,
                            )
                        try:
                            first_read = await chat_engine.extract_answer(page)
                        except Exception as exc:
                            return self._terminal(
                                "unknown_ui_state",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=f"answer_extractor_unavailable: {exc}",
                            )
                        if not first_read:
                            return self._terminal(
                                "unknown_ui_state",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="answer_extractor_empty",
                            )
                        await asyncio.sleep(max(0.0, float(settle_seconds)))
                        state, detail = await self._harvest_response_state(page)
                        if state != "complete":
                            return self._terminal(
                                state,
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=detail,
                            )
                        try:
                            answer_text = await chat_engine.extract_answer(page)
                        except Exception as exc:
                            return self._terminal(
                                "unknown_ui_state",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail=f"answer_extractor_unavailable: {exc}",
                            )
                        if not answer_text:
                            return self._terminal(
                                "unknown_ui_state",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="answer_extractor_empty",
                            )
                        if answer_text != first_read:
                            return self._terminal(
                                "still_generating",
                                None,
                                None,
                                conversation_ref=conversation_url,
                                detail="assistant_text_still_changing",
                            )
                    finally:
                        await page.close()
                finally:
                    await browser.close()
        except TransportError:
            raise
        except Exception as exc:  # operational failure — bounded retry, no data
            return self._terminal(
                "transient_navigation_failure",
                None,
                None,
                conversation_ref=conversation_url,
                detail=f"harvest_navigation_error: {exc}",
            )

        # Persist in the exact answer-file shape the engine writes, then reuse
        # the unchanged verification + single-JSON extraction path.
        output_dir = self._fresh_output_dir(run_ref)
        write_answer_file(
            output_dir, 1, prompt_id, "(harvested; prompt sent earlier)", None,
            answer_text,
        )
        try:
            answer = self._read_single_transcript(output_dir, prompt_id)
            payload = extract_single_json_value(answer)
        except TranscriptVerificationError as exc:
            return self._terminal(
                "failed",
                None,
                None,
                conversation_ref=conversation_url,
                detail=str(exc),
            )
        transcript_ref = (
            output_dir / answer_filename(1, prompt_id)
        ).relative_to(self.repo_root).as_posix()
        return self._terminal(
            "succeeded",
            payload,
            transcript_ref,
            conversation_ref=conversation_url,
        )

    @staticmethod
    async def _any_selector(page, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                if await locator.nth(index).is_visible():
                    return True
        return False

    @classmethod
    async def _explicit_harvest_state(cls, page) -> Optional[tuple[str, str]]:
        """Return an explicit server/auth failure before any answer is read."""

        try:
            if await cls._any_selector(page, _LOGIN_SELECTORS):
                return "login_required", "login_wall_visible"
            if await cls._any_selector(page, _CONVERSATION_MISSING_SELECTORS):
                return "conversation_missing", "conversation_missing"
            if await cls._any_selector(page, _CONTENT_POLICY_SELECTORS):
                return "content_policy_interruption", "content_policy_interruption"
            if await cls._any_selector(page, _CHAT_ERROR_SELECTORS):
                return "chat_error", "chat_error_banner"
            if await cls._any_selector(page, _REGENERATE_SELECTORS):
                return "regenerate_required", "regenerate_control_visible"
        except Exception as exc:
            return "unknown_ui_state", f"explicit_state_selectors_unreadable: {exc}"
        return None

    @classmethod
    async def _harvest_response_state(cls, page) -> tuple[str, str]:
        explicit = await cls._explicit_harvest_state(page)
        if explicit is not None:
            return explicit
        try:
            if await page.locator(chat_engine.STOP_SELECTOR).count() > 0:
                return "still_generating", "stop_control_visible"
            if await page.locator(chat_engine.ASSISTANT_SELECTOR).count() == 0:
                return "still_generating", "no_assistant_message_yet"
        except Exception as exc:
            return "unknown_ui_state", f"selectors_unreadable: {exc}"
        return "complete", ""

    def _read_single_sidecar(self, output_dir: Path, prompt_id: str) -> dict:
        """Return the single verified `*.sent.json` sidecar, failing closed."""

        matches = sorted(p.name for p in output_dir.glob("*.sent.json"))
        expected = f"001_{safe_filename(prompt_id)}.sent.json"
        if matches != [expected]:
            raise TranscriptVerificationError(
                "send_only dispatch did not record exactly the expected sidecar "
                f"{expected!r} (found: {', '.join(matches) or 'none'})"
            )
        try:
            sidecar = json.loads(
                (output_dir / expected).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise TranscriptVerificationError(
                "send_only sidecar is unreadable or malformed"
            ) from exc
        if not isinstance(sidecar, dict) or set(sidecar) != {
            "conversation_url",
            "prompt_id",
            "sent_at",
        }:
            raise TranscriptVerificationError(
                "send_only sidecar failed verification (url/prompt_id/sent_at)"
            )
        try:
            validated = chat_engine.validate_sent_conversation_reference(
                sidecar.get("conversation_url"),
                project_url=(self.profile.project_url or "").strip() or None,
                prompt_id=sidecar.get("prompt_id"),
                expected_prompt_id=prompt_id,
                sent_at=sidecar.get("sent_at"),
                require_sidecar_metadata=True,
            )
        except ValueError as exc:
            raise TranscriptVerificationError(
                "send_only sidecar failed verification (url/prompt_id/sent_at)"
            ) from exc
        return {
            "conversation_url": validated["conversation_url"],
            "sent_at": validated["sent_at"],
        }

    # -- internals ---------------------------------------------------------
    def _terminal(
        self,
        terminal_state: str,
        payload: object,
        transcript_ref: Optional[str],
        *,
        conversation_ref: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> PageDispatchResult:
        return PageDispatchResult(
            terminal_state=terminal_state,
            payload=payload,
            transcript_ref=transcript_ref,
            conversation_ref=conversation_ref,
            model_id=self.model_id,
            model_version=self.model_version,
            terminal_time=self.clock(),
            detail=detail,
        )

    def _read_single_transcript(self, output_dir: Path, prompt_id: str) -> str:
        """Return the verified answer body, failing closed on any anomaly."""

        expected = answer_filename(1, prompt_id)
        actual = {p.name for p in output_dir.glob("*.md") if p.is_file()}
        if expected not in actual:
            raise TranscriptVerificationError(
                f"page transport did not write the expected transcript {expected!r}"
            )
        unexpected = sorted(actual - {expected})
        if unexpected:
            raise TranscriptVerificationError(
                "page transport wrote unexpected transcript(s): "
                + ", ".join(unexpected)
            )
        transcript_path = output_dir / expected
        markdown = transcript_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_answer_markdown(markdown, prompt_id)
        if not parsed.has_prompt_id_header or parsed.prompt_id != prompt_id:
            raise TranscriptVerificationError(
                f"transcript ID mismatch for {prompt_id!r}"
            )
        status = classify_answer_text(markdown)
        if status != "ok":
            raise TranscriptVerificationError(
                f"transcript for {prompt_id!r} is {status}"
            )
        return answer_body(markdown)

    def _engine_config(
        self, output_dir: Path, *, send_only: bool = False
    ) -> chat_engine.EngineConfig:
        # Founder ruling 2026-08-08: when the seat has a configured ChatGPT
        # Project, every page's NEW conversation opens INSIDE that Project (its
        # instructions apply). Still exactly one new conversation per page. A
        # seat without a project_url keeps the original plain-new-chat flow.
        use_project = bool((self.profile.project_url or "").strip())
        return chat_engine.EngineConfig(
            output_dir=output_dir,
            profiles_dir=self.profiles_dir,
            cdp_url=self.cdp_url,
            chatgpt_url=self.chatgpt_url,
            target="project" if use_project else "new_chat",
            project_url=self.profile.project_url if use_project else None,
            # In project chats no picker label is forced (the Project's own
            # default applies) until a SELECTABLE label is pinned; the env pin
            # is still recorded in provenance either way.
            model_label=None,
            new_chat_model=None if use_project else self.model_id,
            num_profiles=1,
            max_per_profile=None,
            tabs=1,
            tabs_per_profile=1,
            logged_in_profiles=[
                {
                    "id": self.profile.id,
                    "display_name": self.profile.display_name,
                    "chrome_directory": self.profile.chrome_directory,
                    "project_url": self.profile.project_url,
                }
            ],
            response_timeout=self.response_timeout,
            use_vpn=False,
            rotate_vpn_on_start=False,
            human_like=self.human_like,
            assist_no_rate_limit_retry=True,
            send_only=send_only,
        )

    def _fresh_output_dir(self, run_ref: str) -> Path:
        root = self.transcript_root
        _reject_symlink_components(self.repo_root, root)
        root.mkdir(parents=True, exist_ok=True)
        if root.resolve() != root:
            raise TransportConfigurationError(
                "page-audit transcript root cannot traverse a symbolic link"
            )
        try:
            root.relative_to(self.repo_root)
        except ValueError as exc:  # pragma: no cover - defensive after resolved root
            raise TransportConfigurationError(
                "page-audit transcript root escaped the repository"
            ) from exc
        prefix = safe_filename(run_ref)[:60]
        output_dir = root / f"{prefix}-{uuid.uuid4().hex}"
        output_dir.mkdir(parents=False, exist_ok=False)
        if output_dir.resolve().parent != root:
            raise TransportConfigurationError(
                "page-audit output directory escaped its dedicated transcript root"
            )
        return output_dir


__all__ = [
    "TRANSCRIPT_RELATIVE_ROOT",
    "EngineEntry",
    "EngineRunner",
    "TransportError",
    "TransportConfigurationError",
    "TranscriptVerificationError",
    "PageDispatchResult",
    "PageAuditTransport",
    "extract_single_json_value",
]
