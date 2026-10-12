"""Fail-closed language policy for client-facing Free Snapshot copy.

The Snapshot may describe observed answer-side patterns and the questions a
Diagnostic Audit would investigate.  It may not state a cause, prescribe a
change, promise an outcome, or present a bounded benchmark as a complete
market analysis.  This module applies that boundary to both the structured
copy deck and the rendered narrative HTML.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, Mapping, Sequence


COPY_POLICY_VERSION = "snapshot-copy-v1"
COPY_SCANNER_VERSION = "free-snapshot-copy-scanner-v1"


@dataclass(frozen=True)
class CopyPolicyViolation:
    rule_id: str
    field_path: str
    matched_text: str
    context: str
    severity: str = "block"


@dataclass(frozen=True)
class CopyPolicyResult:
    policy_version: str
    scanner_version: str
    passed: bool
    violations: tuple[CopyPolicyViolation, ...]
    notices: tuple[CopyPolicyViolation, ...] = ()

    def as_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "scanner_version": self.scanner_version,
            "passed": self.passed,
            "violations": [
                {
                    "rule_id": row.rule_id,
                    "field_path": row.field_path,
                    "matched_text": row.matched_text,
                    "context": row.context,
                    "severity": row.severity,
                }
                for row in self.violations
            ],
            "notices": [
                {
                    "rule_id": row.rule_id,
                    "field_path": row.field_path,
                    "matched_text": row.matched_text,
                    "context": row.context,
                    "severity": row.severity,
                }
                for row in self.notices
            ],
        }


class CopyPolicyError(ValueError):
    """Client-facing narrative violates the Free Snapshot product boundary."""

    def __init__(self, result: CopyPolicyResult):
        self.result = result
        details = "; ".join(
            f"{row.rule_id} at {row.field_path}: {row.matched_text!r}"
            for row in result.violations[:8]
        )
        if len(result.violations) > 8:
            details += f"; and {len(result.violations) - 8} more"
        super().__init__("Prohibited Snapshot copy: " + (details or "unknown violation"))


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PRELIMINARY_001",
        re.compile(r"\b(?:insufficient evidence|more evidence required|missing evidence)\b", re.I),
    ),
    ("CAUSAL_001", re.compile(r"\bcaused by\b", re.I)),
    ("CAUSAL_002", re.compile(r"\bthis is happening because\b", re.I)),
    ("CAUSAL_003", re.compile(r"\bbecause (?:of )?your website\b", re.I)),
    ("SOURCE_001", re.compile(r"\bsource advantage\b", re.I)),
    ("SOURCE_002", re.compile(r"\bsources? (?:caused|gave|give|drives?|produces?)\b", re.I)),
    ("WEBSITE_001", re.compile(r"\byour website (?:is|appears to be) missing\b", re.I)),
    ("PRESCRIPTIVE_001", re.compile(r"\bmust change\b", re.I)),
    (
        "PRESCRIPTIVE_002",
        re.compile(r"\b(?:you|the clinic) (?:need|needs|should) to (?:add|change|fix)\b", re.I),
    ),
    ("PRESCRIPTIVE_003", re.compile(r"\bfix(?:es|ed|ing)?\b", re.I)),
    ("OUTCOME_001", re.compile(r"\bwill (?:improve|rank|increase|generate|drive|produce)\b", re.I)),
    (
        "OUTCOME_002",
        re.compile(
            r"\bwill (?:produce|generate|drive) (?:more )?"
            r"(?:traffic|enquir(?:y|ies)|bookings?|patients?|revenue)\b",
            re.I,
        ),
    ),
    ("GUARANTEE_001", re.compile(r"\bguarantee(?:d|s|ing)?\b", re.I)),
    (
        "MARKET_001",
        re.compile(r"\b(?:ranks?|ranked) (?:first|#\s*1) in (?:the )?market\b", re.I),
    ),
    (
        "MARKET_002",
        re.compile(
            r"\b(?:complete|full) (?:local[- ]market |market |competitor )?"
            r"(?:analysis|leaderboard|ranking|universe)\b",
            re.I,
        ),
    ),
    ("COMPLIANCE_001", re.compile(r"\b(?:non[- ]?)?compliant\b", re.I)),
)


# The contract itself requires this negative limitation.  Mask it before the
# broad guarantee rule; the report is prohibiting a promise, not making one.
_ALLOWED_PHRASES = (
    "No future ranking, patient or revenue outcome is guaranteed.",
    "No future ranking, patient, or revenue outcome is guaranteed.",
    "No outcome is guaranteed.",
    # These are explicit boundary statements required by the product
    # contract.  They negate, rather than assert, market completeness.
    "not a complete competitor analysis",
    "not the complete market",
    "not a complete market, competitor or root-cause analysis",
)


def _normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _mask_literal(text: str, literal: str) -> str:
    if not literal:
        return text
    return re.sub(re.escape(_normalized(literal)), " <protected> ", text, flags=re.I)


def _prepared(text: object, protected_terms: Iterable[object]) -> str:
    value = _normalized(text)
    for phrase in _ALLOWED_PHRASES:
        value = _mask_literal(value, phrase)
    # Proper nouns are data, not authored claims.  Mask them so a business such
    # as "Fix Clinic" cannot trip the prescriptive-language rule.
    terms = sorted(
        {_normalized(term) for term in protected_terms if _normalized(term)},
        key=len,
        reverse=True,
    )
    for term in terms:
        value = _mask_literal(value, term)
    return value


def _context(text: str, start: int, end: int, radius: int = 52) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right].strip()


def _matches(
    fragments: Mapping[str, object] | Sequence[tuple[str, object]],
    *,
    protected_terms: Iterable[object] = (),
    severity: str,
) -> tuple[CopyPolicyViolation, ...]:
    items = fragments.items() if isinstance(fragments, Mapping) else fragments
    matches: list[CopyPolicyViolation] = []
    for field_path, raw in items:
        text = _prepared(raw, protected_terms)
        for rule_id, pattern in _RULES:
            for match in pattern.finditer(text):
                matches.append(
                    CopyPolicyViolation(
                        rule_id=rule_id,
                        field_path=str(field_path),
                        matched_text=match.group(0),
                        context=_context(text, match.start(), match.end()),
                        severity=severity,
                    )
                )
    return tuple(matches)


def scan_client_copy(
    fragments: Mapping[str, object] | Sequence[tuple[str, object]],
    *,
    protected_terms: Iterable[object] = (),
) -> CopyPolicyResult:
    """Scan named authored fragments and return every blocking match."""

    violations = _matches(
        fragments,
        protected_terms=protected_terms,
        severity="block",
    )
    return CopyPolicyResult(
        policy_version=COPY_POLICY_VERSION,
        scanner_version=COPY_SCANNER_VERSION,
        passed=not violations,
        violations=violations,
    )


def scan_evidence_excerpts(
    fragments: Mapping[str, object] | Sequence[tuple[str, object]],
    *,
    protected_terms: Iterable[object] = (),
) -> CopyPolicyResult:
    """Report prohibited-language matches in verbatim evidence as notices.

    Evidence is not authored client copy, so a match never blocks generation.
    It remains review-visible so a founder can confirm the excerpt is clearly
    labelled and has not leaked into an unsupported report claim.
    """

    notices = _matches(
        fragments,
        protected_terms=protected_terms,
        severity="notice",
    )
    return CopyPolicyResult(
        policy_version=COPY_POLICY_VERSION,
        scanner_version=COPY_SCANNER_VERSION,
        passed=True,
        violations=(),
        notices=notices,
    )


def attach_copy_notices(
    blocking: CopyPolicyResult,
    evidence: CopyPolicyResult,
) -> CopyPolicyResult:
    """Keep blocking status and attach non-blocking evidence review notices."""

    return CopyPolicyResult(
        policy_version=blocking.policy_version,
        scanner_version=blocking.scanner_version,
        passed=blocking.passed,
        violations=blocking.violations,
        notices=tuple(blocking.notices) + tuple(evidence.notices),
    )


class _NarrativeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._serial = 0
        self._parts: list[str] = []
        self.fragments: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._depth:
            self._depth += 1
            return
        if attributes.get("data-copy-policy") == "narrative":
            self._depth = 1
            self._serial += 1
            self._parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.fragments.append((f"rendered.narrative[{self._serial}]", "".join(self._parts)))
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._parts.append(data)


def scan_rendered_narrative(
    document: str,
    *,
    protected_terms: Iterable[object] = (),
) -> CopyPolicyResult:
    """Scan only rendered nodes explicitly marked as report-authored copy."""

    parser = _NarrativeParser()
    parser.feed(str(document or ""))
    parser.close()
    return scan_client_copy(parser.fragments, protected_terms=protected_terms)


def assert_copy_policy(result: CopyPolicyResult) -> None:
    if not result.passed:
        raise CopyPolicyError(result)
