"""HTML presentation for the fixed three-page visibility snapshot.

The template accepts only the normalized reporting payload. Analysis remains in
``reporting.data``; this module turns those facts into a restrained, auditable
A4 composition that can be printed deterministically by Chromium.

The composition is editorial, not dashboard: content sits directly on the
obsidian canvas and is separated by alignment, spacing and hairlines. Exactly
one focal panel is spent per page — the canvas lifted a few percent, never a
white plate — and it carries that page's single most important element: the
result, not the evidence behind it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from clinic_geo.reporting.copy_policy import assert_copy_policy, scan_rendered_narrative
from clinic_geo.reporting.presentation import build_presentation_model, is_presentation_v2


_HERE = Path(__file__).resolve().parent
_TEMPLATE_PATH = _HERE / "templates" / "visibility_snapshot.html"
_CSS_PATH = _HERE / "assets" / "report.css"


class ReportTemplateError(ValueError):
    """The normalized payload cannot fit the fixed report contract."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _get(value: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        current = value
        found = True
        for key in path.split("."):
            if not isinstance(current, Mapping) or key not in current:
                found = False
                break
            current = current[key]
        if found and current is not None:
            return current
    return default


def _text(value: Any, default: str = "Not measured") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _html(value: Any, default: str = "Not measured") -> str:
    return escape(_text(value, default), quote=True)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        percent = text.endswith("%")
        if percent:
            text = text[:-1]
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed / 100 if percent else parsed
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _rate(value: Any, *paths: str) -> float | None:
    if isinstance(value, Mapping):
        selected = _get(
            value,
            *paths,
            "rate",
            "value",
            "appearance_rate",
            "first_named_rate",
            "recognition_rate",
        )
    else:
        selected = value
    numeric = _number(selected)
    if numeric is None:
        return None
    return numeric / 100 if abs(numeric) > 1 else numeric


def _percent(value: Any) -> str:
    numeric = _rate(value)
    if numeric is None:
        return "Not measured"
    return f"{numeric * 100:.0f}%"


def _decimal(value: Any, *, places: int = 1, suffix: str = "") -> str:
    numeric = _number(value)
    if numeric is None:
        return "Not measured"
    return f"{numeric:.{places}f}{suffix}"


def _signed_gap(value: Any) -> str:
    numeric = _number(value)
    if numeric is None:
        return "Not measured"
    if abs(numeric) < 0.05:
        numeric = 0.0
    return f"{numeric:+.1f}pp" if numeric else "0.0pp"


def _count(value: Any) -> str:
    if value is None or value == "":
        return "Not measured"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _display_date(value: Any) -> str:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%d %b %Y")


def _evidence(item: Any, default: str = "Not measured") -> str:
    explicit = _get(item, "evidence", "evidence_label", "note")
    if explicit:
        return _text(explicit)
    numerator = _get(
        item,
        "appearances",
        "client_appearances",
        "recognised",
        "recognized",
        "named_outcomes",
    )
    denominator = _get(item, "valid_outcomes", "denominator", "answers")
    if numerator is not None and denominator is not None:
        return f"{_count(numerator)} of {_count(denominator)}"
    if denominator is not None:
        return f"{_count(denominator)} answers"
    return default


def _capped(value: Any, maximum: int, label: str) -> list[Any]:
    items = _sequence(value)
    if len(items) > maximum:
        raise ReportTemplateError(
            f"{label} contains {len(items)} rows; the fixed layout allows {maximum}"
        )
    return items


def _ordinal(value: Any) -> str:
    numeric = _number(value)
    if numeric is None:
        return "unranked"
    integer = int(numeric)
    if 10 <= integer % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(integer % 10, "th")
    return f"{integer}{suffix}"


def _join_names(names: Sequence[str]) -> str:
    items = [name for name in names if name]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# The report is three fixed A4 pages, so nothing in it may grow without a
# ceiling. A clinic name is the one field the pipeline cannot bound: it comes
# from whatever the AI answers called the business. Three rules keep an
# 80-character trading name from pushing a page over:
#
#   * the masthead headline steps DOWN a size rather than wrapping to a third
#     line — the client's own name is the title of the report and is never cut;
#   * a name inside a table cell is clamped to two lines by the CSS, and is
#     truncated past _NAME_LIMIT so that a narrow column cannot be handed a
#     word it has no room for;
#   * a sentence that runs long steps down a size instead of taking the page
#     with it.
#
# The full name is always in the JSON payload; only the printed cell is short.
_NAME_LIMIT = 56


def _fit(base: str, text: str, medium: int, small: int) -> str:
    """Pick a size step for a block of type from how much of it there is."""
    length = len(text)
    if length > small:
        return f"{base} {base}--s"
    if length > medium:
        return f"{base} {base}--m"
    return base


def _name(value: Any, *, limit: int = _NAME_LIMIT, suffix: str = "") -> str:
    """A clinic name as a table cell: clamped by CSS, cut only past the limit.

    ``suffix`` (the client's "You" mark) rides in a flex row beside the name.
    Left as a plain sibling it dropped onto its own line, because the clamped
    name is a block box.
    """
    text = _text(value)
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,.-&") + "\u2026"
    name = f'<span class="name">{escape(text)}</span>'
    if not suffix:
        return name
    return f'<span class="row-name">{name}{suffix}</span>'


_STATUS_ALIASES = {
    "good": ("good", "Good"),
    "mediocre": ("mediocre", "Mediocre"),
    "medium": ("mediocre", "Mediocre"),
    "warn": ("mediocre", "Mediocre"),
    "warning": ("mediocre", "Mediocre"),
    "poor": ("poor", "Poor"),
    "bad": ("poor", "Poor"),
    "not measured": ("not-measured", "Not measured"),
    "not_measured": ("not-measured", "Not measured"),
    "unmeasured": ("not-measured", "Not measured"),
    "unavailable": ("not-measured", "Not measured"),
    "unknown": ("not-measured", "Not measured"),
}


def _status(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        label = _get(value, "rating_label", "rating", "status", "label")
    else:
        label = value
    key = _text(label, "Not measured").strip().lower().replace("-", " ")
    return _STATUS_ALIASES.get(key, ("not-measured", "Not measured"))


def _status_mark(value: Any, text: str | None = None) -> str:
    """A coloured dot and the word it stands for.

    The dot is the only colour in the report and it is never the only signal:
    the label beside it always says the same thing in words. ``text`` overrides
    the label where the outcome has its own vocabulary (Win / Loss / Tie), so
    that one result is never printed twice under two different names.
    """
    css_name, label = _status(value)
    return (
        f'<span class="status status--{css_name}">'
        '<span class="status-dot" aria-hidden="true"></span>'
        f"<span>{escape(text or label)}</span></span>"
    )


_MEASURED_STATUSES = frozenset(
    {"measured", "partial", "ok", "complete", "collected", "valid"}
)


def _stage(row: Any, stage: str) -> Mapping[str, Any]:
    return _mapping(_get(row, f"collection.{stage}", default={}))


def _is_measured(row: Any, stage: str) -> bool:
    status = _text(_get(_stage(row, stage), "status"), "not_measured")
    return status.strip().lower().replace("-", "_") in _MEASURED_STATUSES


def _unavailable_reason(collection: Any) -> str:
    reasons = _sequence(_get(collection, "unavailable_reasons", default=[]))
    raw = _get(reasons[0], "status", "reason") if reasons else None
    mapping = {
        "auth_required": "sign-in required",
        "blocked": "collection blocked",
        "timeout": "collection timed out",
        # The sentence around this reason already says the platform returned
        # nothing; "missing" would only say it a second time.
        "missing": "",
        "error": "collection error",
    }
    normalized = _text(raw, "").strip().lower().replace("-", "_")
    if normalized in mapping:
        return mapping[normalized]
    return _text(raw, "").replace("_", " ")


def _unavailable_line(rows: Sequence[Any], stage: str) -> str:
    """Every unavailable platform, disclosed, in one line instead of three
    empty table rows. Platforms that failed for the same reason are named
    together; a differing reason gets its own clause."""
    groups: dict[str, list[str]] = {}
    for row in rows:
        if _is_measured(row, stage):
            continue
        reason = _unavailable_reason(_stage(row, stage))
        groups.setdefault(reason, []).append(_text(_get(row, "label", "name", "key"), "Platform"))
    if not groups:
        return ""
    clauses = "; ".join(
        f"{_join_names(names)}{f' ({reason})' if reason else ''}"
        for reason, names in groups.items()
    )
    return (
        '<p class="note note--status">'
        '<span class="status status--not-measured">'
        '<span class="status-dot" aria-hidden="true"></span>'
        "<span>Not measured</span></span>"
        f"<span>{escape(clauses)} returned no answers. They are excluded from every "
        "rate above and are never counted as a zero.</span>"
        "</p>"
    )


def _brand_mark(*, ground: str = "#1c1b19", class_name: str = "brand-mark") -> str:
    """Draw the mark in white on the obsidian canvas.

    ``ground`` fills the small nodes so the connecting stroke reads as passing
    behind them; it must match the band the mark is placed on — the deeper
    masthead plate, or the flat canvas everywhere else.
    """
    stroke = "#ffffff"
    border = "rgba(255,255,255,.2)"
    return f"""
    <svg class="{escape(class_name)}" viewBox="0 0 32 32" fill="none"
         xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <rect x="0.5" y="0.5" width="31" height="31" rx="7.5"
            fill="transparent" stroke="{border}"/>
      <path d="M8 22.5 L13.5 13 L18 18 L24 9.5" stroke="{stroke}"
            stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="8" cy="22.5" r="1.8" fill="{ground}" stroke="{stroke}" stroke-width="1.3"/>
      <circle cx="13.5" cy="13" r="1.8" fill="{ground}" stroke="{stroke}" stroke-width="1.3"/>
      <circle cx="18" cy="18" r="1.8" fill="{ground}" stroke="{stroke}" stroke-width="1.3"/>
      <circle cx="24" cy="9.5" r="3.6" fill="none" stroke="{stroke}"
              stroke-opacity=".4" stroke-width="1"/>
      <circle cx="24" cy="9.5" r="2.3" fill="{stroke}"/>
    </svg>"""


def _footer(page: int, report_id: Any) -> str:
    return f"""
      <footer class="footer">
        <span class="footer-brand">{_brand_mark(ground="#1c1b19")}<span>Citeup</span></span>
        <span class="page-number">{page} / 3&nbsp;&nbsp;&middot;&nbsp;&nbsp;{_html(report_id, "Unassigned")}</span>
      </footer>"""


def _masthead(
    title: str,
    eyebrow: str,
    *,
    compact: bool,
    generated_at: Any,
    report_id: Any,
    area: Any = None,
) -> str:
    """The one place the run's identifying metadata is stated.

    Area, date and report id are identity, not findings, so they live here and
    nowhere else.
    """
    compact_class = " masthead--section" if compact else ""
    heading_tag = "h2" if compact else "h1"
    # Page one's title is the client's name and can be any length; pages two
    # and three carry fixed titles that always fit.
    title_class = _fit("clinic-name", title, 30, 46) if not compact else "clinic-name"
    if not compact and len(title) > 62:
        title_class = "clinic-name clinic-name--xs"
    area_line = f"<span>Area <strong>{_html(area)}</strong></span>" if area else ""
    return f"""
      <header class="masthead{compact_class}">
        <div class="brand-row">
          <span class="brand-lockup">{_brand_mark(ground="#131210")}<span>Citeup</span></span>
          <span class="eyebrow">Competitive visibility snapshot</span>
        </div>
        <div class="masthead-title-row">
          <div class="masthead-title">
            <span class="eyebrow">{escape(eyebrow)}</span>
            <{heading_tag} class="{title_class}">{escape(title)}</{heading_tag}>
          </div>
          <div class="masthead-meta">
            {area_line}
            <span>Prepared <strong>{escape(_display_date(generated_at))}</strong></span>
            <span>Report <strong>{_html(report_id, "Unassigned")}</strong></span>
          </div>
        </div>
      </header>"""


def _section_head(title: str, note: str = "") -> str:
    """A section is a line of type over a hairline — never a card.

    The optional note is the section's denominator, stated at the top of the
    block so that no individual row has to carry it.
    """
    note_html = f'<span class="section-note">{escape(note)}</span>' if note else ""
    return f'<div class="section-head"><h2>{escape(title)}</h2>{note_html}</div>'


# ------------------------------------------------------------------------
# PAGE 1 — executive snapshot
# ------------------------------------------------------------------------


def _executive_statement(payload: Mapping[str, Any]) -> str:
    """Position and gap, in one sentence. The rate itself is NOT repeated here:
    it is the largest number on the page, printed beside this sentence.

    The rank is not qualified with an "of N". ``market_rank`` is the client's
    position against every clinic named anywhere in the market answers, whereas
    the leaderboard is capped at four rows for display; dividing the one by the
    other would invent a market size the payload does not carry.
    """
    client_name = _text(_get(payload, "client.name"), "The clinic")
    client = _mapping(_get(payload, "market.client", default={}))
    overall = _mapping(_get(payload, "competition.overall", default={}))
    leaderboard = _sequence(_get(payload, "competition.leaderboard", default=[]))
    valid = _number(_get(payload, "market.valid_outcomes")) or 0
    if valid <= 0:
        return "No AI answers were collected, so market visibility could not be measured."
    rank_value = _get(client, "market_rank")
    if rank_value is None:
        for index, row in enumerate(leaderboard, start=1):
            if bool(_get(row, "is_client", "client")):
                rank_value = index
                break
    leader = _text(_get(overall, "leader_name"), "the strongest measured clinic")
    gap = _number(_get(overall, "gap_pp"))
    if gap is None:
        tail = f"with no measured gap to {leader}"
    elif gap < -0.05:
        tail = f"{abs(gap):.1f} percentage points behind {leader}"
    elif gap > 0.05:
        tail = f"{gap:.1f} percentage points ahead of {leader}"
    else:
        tail = f"level with {leader}"
    if rank_value is None:
        return f"{client_name} is {tail}."
    return f"{client_name} ranks {_ordinal(rank_value)} in its market, {tail}."


def _confidence_note(payload: Mapping[str, Any]) -> str:
    """The one denominator rule for the whole report, stated once, inline.

    This replaces the boxed coverage alert. It is a caveat, not a finding, so it
    is set at footnote weight directly under the result it qualifies.
    """
    coverage = _mapping(_get(payload, "coverage", default={}))
    valid = _get(coverage, "valid_outcomes")
    expected = _get(coverage, "expected_outcomes")
    if expected and valid == expected:
        return (
            '<p class="note note--confidence">Complete coverage: every one of the '
            f"{_html(expected, '0')} planned answers was collected.</p>"
        )
    return (
        '<p class="note note--confidence">Every rate is out of the answers actually '
        f"collected &mdash; {_html(valid, '0')} of {_html(expected, '0')} planned. "
        "Platforms that returned nothing are named below and are never counted as a zero.</p>"
    )


def _headline_block(payload: Mapping[str, Any]) -> str:
    """The page's one focal panel: the result, and what it means.

    Market visibility is the primary number. Collection coverage is not a peer
    of it — it is the confidence footnote underneath.
    """
    client = _mapping(_get(payload, "market.client", default={}))
    rate = _percent(_get(client, "appearance_rate"))
    appearances = _get(client, "appearances")
    valid = _get(client, "valid_outcomes", default=_get(payload, "market.valid_outcomes"))
    evidence = (
        f"{_count(appearances)} of {_count(valid)} answers"
        if appearances is not None and valid is not None
        else "No answers collected"
    )
    # A missing rate prints the words "Not measured", and words must never be
    # set at display size.
    measured = _rate(_get(client, "appearance_rate")) is not None
    value_class = "headline-value" if measured else "headline-value headline-value--text"
    statement = _executive_statement(payload)
    return f"""
      <section class="headline">
        <div class="headline-figure">
          <span class="headline-label">Market visibility</span>
          <strong class="{value_class}">{escape(rate)}</strong>
          <span class="headline-evidence">{escape(evidence)}</span>
        </div>
        <p class="{_fit("headline-statement", statement, 96, 132)}">{escape(statement)}</p>
      </section>
      {_confidence_note(payload)}"""


def _metric(label: str, value: str, note: str) -> str:
    return f"""
        <div class="metric">
          <span class="metric-label">{escape(label)}</span>
          <div class="metric-value">{value}</div>
          <span class="metric-note">{escape(note)}</span>
        </div>"""


def _support_metrics(payload: Mapping[str, Any]) -> str:
    """Three facts the headline does not already carry.

    Rank and gap are in the statement above, so they are not repeated as tiles;
    what is left is how often the clinic is named first, where it lands when it
    is named, and how the rating logic scores the result.
    """
    client = _mapping(_get(payload, "market.client", default={}))
    overall = _mapping(_get(payload, "competition.overall", default={}))
    valid = _get(client, "valid_outcomes", default=_get(payload, "market.valid_outcomes"))
    first_named = _get(client, "first_named")
    first_note = (
        f"{_count(first_named)} of {_count(valid)} answers"
        if first_named is not None and valid is not None
        else "Not measured"
    )
    leader_name = _get(overall, "leader_name")
    leader_rate = _rate(_get(overall, "leader_rate"))
    rival_note = (
        f"{_text(leader_name)} appears in {_percent(leader_rate)}"
        if leader_name and leader_rate is not None
        else "No rival was measured"
    )
    return f"""
      <div class="metric-row">
        {_metric(
            "Named first",
            f'<strong>{escape(_percent(_get(client, "first_named_rate")))}</strong>',
            first_note,
        )}
        {_metric(
            "Average position",
            f'<strong>{escape(_decimal(_get(client, "mean_position")))}</strong>',
            "Where the clinic lands when it is named",
        )}
        {_metric(
            "Versus strongest rival",
            _status_mark(_get(overall, "rating_label", "rating")),
            rival_note,
        )}
      </div>"""


def _platform_block(rows: Sequence[Any]) -> str:
    """Only the platforms that answered get a row. The rest are disclosed in a
    single line underneath instead of three blank rows that look unfinished."""
    measured = [row for row in rows if _is_measured(row, "market")]
    if not measured:
        body = (
            '<p class="note">No platform returned market answers, so visibility '
            "could not be measured on any of them.</p>"
        )
    else:
        cells = "".join(
            f"""
            <tr>
              <th scope="row">{_name(_get(row, "label", "name", "key"), limit=40)}</th>
              <td class="numeric">{_html(_get(row, "market.valid_outcomes", "collection.market.valid_outcomes"), "0")}</td>
              <td class="numeric lead">{escape(_percent(_get(row, "market.appearance_rate")))}</td>
              <td class="numeric">{escape(_percent(_get(row, "market.first_named_rate")))}</td>
              <td class="status-cell">{_status_mark(_get(row, "competition.rating_label", "competition.rating"))}</td>
            </tr>"""
            for row in measured
        )
        body = f"""
        <table class="grid" aria-label="Visibility by platform">
          <thead>
            <tr>
              <th scope="col" style="width:34%">Platform</th>
              <th scope="col" class="numeric" style="width:14%">Answers</th>
              <th scope="col" class="numeric" style="width:16%">Appears in</th>
              <th scope="col" class="numeric" style="width:16%">Named first</th>
              <th scope="col" class="status-cell" style="width:20%">Rating</th>
            </tr>
          </thead>
          <tbody>{cells}</tbody>
        </table>"""
    return body + _unavailable_line(rows, "market")


def _brand_summary(payload: Mapping[str, Any]) -> str:
    """A summary, not the comparison. The comparison is on page 3, once.

    The sentence carries one HTML entity of its own, so every value spliced into
    it is escaped here rather than escaping the finished string.
    """
    client_name = escape(_text(_get(payload, "client.name"), "The clinic"))
    own = _mapping(_get(payload, "brand.own_brand", default={}))
    comparison = _mapping(_get(payload, "brand.comparison", default={}))
    rate = escape(_percent(_get(own, "appearance_rate", "recognition_rate")))
    evidence = escape(_evidence(own))
    leader = escape(_text(_get(comparison, "leader_name"), "the strongest tested rival"))
    gap = _number(_get(comparison, "gap_pp"))
    if _rate(_get(own, "appearance_rate", "recognition_rate")) is None:
        sentence = "No brand answers were collected, so brand recognition could not be measured."
    elif gap is None:
        sentence = (
            f"Asked for {client_name} by name, AI answers recognise the clinic in "
            f"{rate} of cases ({evidence}). No rival was measured against it."
        )
    else:
        if gap < -0.05:
            against = f"{abs(gap):.1f} points behind {leader}"
        elif gap > 0.05:
            against = f"{gap:.1f} points ahead of {leader}"
        else:
            against = f"level with {leader}"
        sentence = (
            f"Asked for {client_name} by name, AI answers recognise the clinic in "
            f"{rate} of cases ({evidence}) &mdash; {against}, the strongest rival "
            "tested by name."
        )
    return f"""
      <div class="summary">
        <p class="{_fit("summary-text", sentence, 150, 200)}">{sentence}</p>
        <p class="summary-meta">{_status_mark(_get(comparison, "rating_label", "rating"))}<span>Full comparison on page 3</span></p>
      </div>"""


def _page_one(payload: Mapping[str, Any]) -> str:
    client_name = _text(_get(payload, "client.name"), "Clinic")
    platforms = _capped(_get(payload, "platforms", default=[]), 4, "platform scorecard")
    report_id = _get(payload, "report_id")
    generated_at = _get(payload, "generated_at")
    return f"""
    <section class="report-page page-one" data-report-page="1">
      <div class="page-frame" data-overflow-root>
        {_masthead(
            client_name,
            "Executive snapshot",
            compact=False,
            generated_at=generated_at,
            report_id=report_id,
            area=_get(payload, "client.area", "client.location"),
        )}
        <div class="page-content">
          {_headline_block(payload)}
          {_support_metrics(payload)}
          {_section_head("Visibility by platform", "Market prompts")}
          {_platform_block(platforms)}
          {_section_head("Brand recognition", "Brand prompts")}
          {_brand_summary(payload)}
          {_footer(1, report_id)}
        </div>
      </div>
    </section>"""


# ------------------------------------------------------------------------
# PAGE 2 — competition
# ------------------------------------------------------------------------

# A context measured on a handful of answers can post a spectacular gap that one
# more mention would erase. The conclusion therefore prefers the widest gap among
# contexts carrying at least this many answers, and falls back to the widest gap
# of all only when nothing clears the floor — in which case the sentence still
# prints the answer count that produced it.
_EVIDENCE_FLOOR = 10


def _comparison_answers(row: Any) -> float | None:
    """The denominator carried by either a flat comparison or its client arm."""
    return _number(
        _get(
            row,
            "valid_outcomes",
            "answers",
            "denominator",
            "client.valid_outcomes",
            "client.answers",
            "client.denominator",
        )
    )


def _comparison_gap(row: Any) -> float | None:
    """The client's signed percentage-point gap to the treatment leader."""
    return _number(
        _get(
            row,
            "gap_pp",
            "gap",
            "competition.gap_pp",
            "competition.gap",
            "leader.gap_pp",
            "leader.gap",
        )
    )


def _widest_gap(rows: Sequence[Any]) -> Mapping[str, Any] | None:
    measured = [
        _mapping(row)
        for row in rows
        if (_comparison_answers(row) or 0) > 0
        and _comparison_gap(row) is not None
    ]
    if not measured:
        return None
    supported = [
        row
        for row in measured
        if (_comparison_answers(row) or 0) >= _EVIDENCE_FLOOR
    ]
    pool = supported or measured
    return min(pool, key=lambda row: _comparison_gap(row) or 0.0)


def _competition_conclusion(
    payload: Mapping[str, Any],
    comparison_rows: Sequence[Any],
    *,
    dimension: str = "prompt",
) -> str:
    """The page's one focal panel, and the answer to its own title: who is
    ahead, where the client stands, and the one gap worth acting on."""
    client_name = _text(_get(payload, "client.name"), "The clinic")
    client = _mapping(_get(payload, "market.client", default={}))
    overall = _mapping(_get(payload, "competition.overall", default={}))
    leader = _text(_get(overall, "leader_name"), "the strongest measured rival")
    gap = _number(_get(overall, "gap_pp"))
    client_rate = _percent(_get(client, "appearance_rate"))
    rank = _ordinal(_get(client, "market_rank"))

    if (_number(_get(payload, "market.valid_outcomes")) or 0) <= 0:
        answer = "No market answers were collected, so no leader could be established."
    elif gap is not None and gap > 0.05:
        answer = (
            f"{client_name} leads the measured market with {client_rate} of answers, "
            f"{gap:.1f} points clear of {leader}."
        )
    else:
        answer = (
            f"{leader} leads the measured market, appearing in "
            f"{_percent(_get(overall, 'leader_rate'))} of answers. "
            f"{client_name} ranks {rank} with {client_rate}."
        )

    widest = _widest_gap(comparison_rows)
    if widest is None:
        subject = "treatment" if dimension == "treatment" else "prompt type"
        detail = f"No {subject} carried enough answers to compare."
    else:
        gap_value = _comparison_gap(widest) or 0.0
        fallback = "one treatment" if dimension == "treatment" else "one prompt type"
        label = _text(
            _get(widest, "label", "treatment", "name", "key"), fallback
        )
        answers = _count(_comparison_answers(widest))
        rival, _ = _treatment_leader(widest) if dimension == "treatment" else (
            _text(_get(widest, "leader_name"), "the strongest rival"),
            None,
        )
        if dimension == "treatment":
            context = f"{label} treatment queries"
        else:
            # The label is free text from the prompt catalogue and may already
            # end in the word this sentence was about to add.
            context = label.lower()
            if not context.endswith(("prompt", "prompts")):
                context += " prompts"
        if gap_value < -0.05:
            prefix = "Widest treatment gap" if dimension == "treatment" else "Widest gap"
            detail = (
                f"{prefix}: {context}, where {rival} is named "
                f"{abs(gap_value):.1f} points more often ({answers} answers)."
            )
        else:
            measured_context = (
                "every measured treatment"
                if dimension == "treatment"
                else "every measured prompt type"
            )
            detail = (
                f"{client_name} is level with or ahead of the strongest rival in "
                f"{measured_context}."
            )
    return f"""
      <section class="conclusion">
        <p class="{_fit("conclusion-answer", answer, 118, 158)}">{escape(answer)}</p>
        <p class="conclusion-detail">{escape(detail)}</p>
      </section>"""


def _leaderboard(rows: Sequence[Any]) -> str:
    """Ordering is the comparison. The percentages are printed, so the bars that
    used to sit behind them added a picture of a number the reader already had —
    and at 11% against 7% they all drew the same length anyway."""
    if not rows:
        return '<p class="note">No clinic was named in the measured answers.</p>'
    body: list[str] = []
    for index, row in enumerate(rows, start=1):
        is_client = (
            bool(_get(row, "is_client", "client"))
            or _text(_get(row, "role"), "").lower() == "client"
        )
        row_class = ' class="is-client"' if is_client else ""
        marker = '<span class="you">You</span>' if is_client else ""
        body.append(
            f"""
            <tr{row_class}>
              <td class="rank">{_html(_get(row, "market_rank"), str(index))}</td>
              <th scope="row">{_name(_get(row, "name", "entity", "clinic"), suffix=marker)}</th>
              <td class="numeric lead">{escape(_percent(_get(row, "appearance_rate")))}</td>
              <td class="numeric">{_html(_get(row, "appearances", "appearance_count", "count"), "0")}</td>
              <td class="numeric">{escape(_percent(_get(row, "first_named_rate")))}</td>
              <td class="numeric">{escape(_decimal(_get(row, "mean_position")))}</td>
            </tr>"""
        )
    return f"""
      <table class="grid" aria-label="Market leaderboard">
        <thead>
          <tr>
            <th scope="col" style="width:6%">#</th>
            <th scope="col" style="width:38%">Clinic</th>
            <th scope="col" class="numeric" style="width:15%">Appears in</th>
            <th scope="col" class="numeric" style="width:13%">Answers</th>
            <th scope="col" class="numeric" style="width:14%">Named first</th>
            <th scope="col" class="numeric" style="width:14%">Avg. position</th>
          </tr>
        </thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


def _precision_note(payload: Mapping[str, Any]) -> str:
    """One mention is worth a lot when the denominator is small. Say so, once,
    rather than printing gaps to a tenth of a point and hoping nobody reads them
    as precision."""
    valid = _number(_get(payload, "market.valid_outcomes")) or 0
    if valid <= 0:
        return ""
    return (
        f'<p class="note">Rates come from {_count(int(valid))} measured answers, '
        f"where a single mention is worth {100 / valid:.1f} points. Differences of "
        "a point or two rest on one answer.</p>"
    )


def _prompt_gap_table(rows: Sequence[Any]) -> str:
    """Prompt types only. Platform availability is a different question and is
    answered separately, below — not folded into the same six-column grid."""
    measured = [row for row in rows if (_number(_get(row, "valid_outcomes")) or 0) > 0]
    if not measured:
        return '<p class="note">No prompt type returned enough answers to compare.</p>'
    body = "".join(
        f"""
        <tr>
          <th scope="row">{_name(_get(row, "label", "name", "key"), limit=40)}<span class="sub">{_count(_get(row, "valid_outcomes"))} answers</span></th>
          <td class="numeric lead">{escape(_percent(_get(row, "client_rate")))}</td>
          <td>{_name(_get(row, "leader_name"))}</td>
          <td class="numeric">{escape(_percent(_get(row, "leader_rate")))}</td>
          <td class="numeric">{escape(_signed_gap(_get(row, "gap_pp", "gap")))}</td>
          <td class="status-cell">{_status_mark(_get(row, "rating_label", "rating"))}</td>
        </tr>"""
        for row in measured
    )
    return f"""
      <table class="grid" aria-label="Visibility gap by prompt type">
        <thead>
          <tr>
            <th scope="col" style="width:23%">Prompt type</th>
            <th scope="col" class="numeric" style="width:10%">You</th>
            <th scope="col" style="width:28%">Strongest rival</th>
            <th scope="col" class="numeric" style="width:10%">Rival</th>
            <th scope="col" class="numeric" style="width:12%">Gap</th>
            <th scope="col" class="status-cell" style="width:17%">Rating</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>"""


def _treatment_client_value(row: Any, *fields: str) -> Any:
    """Read a client treatment statistic from nested or compact payloads.

    ``treatments.rows`` is deliberately a small presentation contract. Data
    producers may keep client figures under ``client`` or flatten them with a
    ``client_`` prefix; accepting both keeps the report migration additive.
    """
    paths: list[str] = []
    for field in fields:
        paths.extend((f"client.{field}", f"client_{field}"))
    paths.extend(fields)
    return _get(row, *paths)


def _treatment_label(row: Any) -> Any:
    return _get(row, "label", "treatment", "name", "key")


def _treatment_leader(row: Any) -> tuple[str, Any]:
    """Return the declared leader, falling back to the first ranked rival."""
    leader = _get(row, "leader")
    leader_mapping = _mapping(leader)
    name = _get(
        row,
        "leader_name",
        "competition.leader_name",
        "strongest_competitor.name",
    )
    rate = _get(
        row,
        "leader_rate",
        "competition.leader_rate",
        "strongest_competitor.appearance_rate",
        "strongest_competitor.rate",
    )
    if name is None and leader_mapping:
        name = _get(leader_mapping, "name", "label", "clinic", "entity")
    elif name is None and leader is not None and not isinstance(leader, Mapping):
        name = leader
    if rate is None and leader_mapping:
        rate = _get(leader_mapping, "appearance_rate", "rate", "visibility_rate")

    rivals = _sequence(_get(row, "competitors", "competition.competitors", default=[]))
    for rival in rivals:
        if bool(_get(rival, "is_client", "client")):
            continue
        rival_mapping = _mapping(rival)
        rival_name = (
            _get(rival_mapping, "name", "label", "clinic", "entity")
            if rival_mapping
            else rival
        )
        same_as_declared = (
            name is None
            or _text(rival_name, "").casefold() == _text(name, "").casefold()
        )
        if name is None:
            name = rival_name
        if rate is None and same_as_declared and rival_mapping:
            rate = _get(rival_mapping, "appearance_rate", "rate", "visibility_rate")
        if name is not None and rate is not None:
            break
    return _text(name, "Not measured"), rate


def _treatment_visibility_table(rows: Sequence[Any]) -> str:
    body = "".join(
        f"""
        <tr>
          <th scope="row">{_name(_treatment_label(row), limit=38)}</th>
          <td class="numeric">{_html(_treatment_client_value(row, "appearances", "named_outcomes"))} / {_html(_treatment_client_value(row, "valid_outcomes", "answers", "denominator"))}</td>
          <td class="numeric lead">{escape(_percent(_treatment_client_value(row, "appearance_rate", "rate", "visibility_rate")))}</td>
          <td class="numeric">{escape(_percent(_treatment_client_value(row, "first_named_rate")))}</td>
        </tr>"""
        for row in rows
    )
    return f"""
      <table class="grid grid--compact grid--treatment" aria-label="Client visibility by treatment">
        <thead>
          <tr>
            <th scope="col" style="width:39%">Treatment</th>
            <th scope="col" class="numeric" style="width:23%">Seen</th>
            <th scope="col" class="numeric" style="width:19%">Visibility</th>
            <th scope="col" class="numeric" style="width:19%">First</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>"""


def _treatment_competition_table(rows: Sequence[Any]) -> str:
    body: list[str] = []
    for row in rows:
        leader_name, leader_rate = _treatment_leader(row)
        body.append(
            f"""
            <tr>
              <th scope="row">{_name(_treatment_label(row), limit=34)}</th>
              <td>{_name(leader_name, limit=38)}</td>
              <td class="numeric">{escape(_percent(leader_rate))}</td>
              <td class="numeric lead">{escape(_signed_gap(_comparison_gap(row)))}</td>
            </tr>"""
        )
    return f"""
      <table class="grid grid--compact grid--treatment" aria-label="Strongest competitor by treatment">
        <thead>
          <tr>
            <th scope="col" style="width:30%">Treatment</th>
            <th scope="col" style="width:38%">Top rival</th>
            <th scope="col" class="numeric" style="width:16%">Rival</th>
            <th scope="col" class="numeric" style="width:16%">Gap</th>
          </tr>
        </thead>
        <tbody>{''.join(body)}</tbody>
      </table>"""


def _treatment_analysis(rows: Sequence[Any]) -> str:
    """Two linked readings in the footprint of the former prompt-gap grid."""
    return f"""
      <div class="treatment-pair">
        <div class="treatment-block">
          <span class="support-label">Your visibility</span>
          {_treatment_visibility_table(rows)}
        </div>
        <div class="treatment-block">
          <span class="support-label">Competition</span>
          {_treatment_competition_table(rows)}
        </div>
      </div>"""


def _platform_note(rows: Sequence[Any]) -> str:
    """Platform availability, in one paragraph, under the findings it limits.

    This is not a second coverage table — page 3 carries the counts. It answers
    the question the gap table provokes: why is there no platform-by-platform
    comparison here? Because there is nothing to compare it against.
    """
    measured = [row for row in rows if _is_measured(row, "market")]
    measured_names = [
        _text(_get(row, "label", "name", "key"), "Platform") for row in measured
    ]
    unmeasured = [row for row in rows if not _is_measured(row, "market")]
    if not unmeasured:
        return (
            f'<p class="note">Market answers were collected from every platform '
            f"({escape(_join_names(measured_names))}). Visibility on each one is on page 1.</p>"
        )
    groups: dict[str, list[str]] = {}
    for row in unmeasured:
        reason = _unavailable_reason(_stage(row, "market"))
        groups.setdefault(reason, []).append(
            _text(_get(row, "label", "name", "key"), "Platform")
        )
    clauses = "; ".join(
        f"{_join_names(names)}{f' ({reason})' if reason else ''}"
        for reason, names in groups.items()
    )
    # Page 1 already states that unmeasured platforms are excluded rather than
    # counted as zeroes. This note says the thing only page 2 needs: what that
    # exclusion leaves the comparison resting on.
    if not measured:
        tail = "no platform returned market answers, so no comparison could be run."
    elif len(measured) == 1:
        only = measured[0]
        answers = _count(
            _get(only, "collection.market.valid_outcomes", "market.valid_outcomes")
        )
        tail = (
            f"every rate above rests on {measured_names[0]} alone ({answers} answers), "
            "and no platform-by-platform comparison is possible."
        )
    else:
        tail = (
            f"the comparison above rests on {_join_names(measured_names)}. "
            "Visibility on each one is on page 1."
        )
    return f"""
      <p class="note note--status">
        <span class="status status--not-measured">
          <span class="status-dot" aria-hidden="true"></span>
          <span>Not measured</span>
        </span>
        <span>{escape(clauses)} returned no market answers, so {escape(tail)}</span>
      </p>"""


def _page_two(payload: Mapping[str, Any]) -> str:
    competition = _mapping(_get(payload, "competition", default={}))
    leaderboard = _capped(_get(competition, "leaderboard", default=[]), 4, "leaderboard")
    by_prompt = _capped(_get(competition, "by_stratum", default=[]), 4, "prompt types")
    treatment_rows = _capped(
        _get(payload, "treatments.rows", default=[]), 5, "treatments"
    )
    platforms = _capped(_get(payload, "platforms", default=[]), 4, "platform availability")
    report_id = _get(payload, "report_id")
    generated_at = _get(payload, "generated_at")
    comparison_rows = treatment_rows or by_prompt
    dimension = "treatment" if treatment_rows else "prompt"
    if treatment_rows:
        detail_section = (
            _section_head("Visibility by treatment", "Client and strongest rival")
            + _treatment_analysis(treatment_rows)
        )
    else:
        detail_section = (
            _section_head("Where the gap is", "By prompt type")
            + _prompt_gap_table(by_prompt)
        )
    return f"""
    <section class="report-page page-two" data-report-page="2">
      <div class="page-frame" data-overflow-root>
        {_masthead("Who is ahead, and where", "Competition", compact=True, generated_at=generated_at, report_id=report_id)}
        <div class="page-content">
          {_competition_conclusion(payload, comparison_rows, dimension=dimension)}
          {_section_head("Market leaderboard", "By share of measured answers")}
          {_leaderboard(leaderboard)}
          {_precision_note(payload)}
          {detail_section}
          {_platform_note(platforms)}
          {_footer(2, report_id)}
        </div>
      </div>
    </section>"""


# ------------------------------------------------------------------------
# PAGE 3 — brand and head-to-head
# ------------------------------------------------------------------------


def _brand_table(payload: Mapping[str, Any]) -> str:
    """The full brand comparison, shown once, here.

    Every clinic carries its own denominator, printed beside the rate: 2 of 3 is
    67%, and the reader must be able to see that before believing it.
    """
    client_name = _text(_get(payload, "client.name"), "Your clinic")
    own = _mapping(_get(payload, "brand.own_brand", default={}))
    competitors = _capped(
        _get(payload, "brand.competitor_brand.competitors", default=[]),
        3,
        "brand competitors",
    )
    comparison = _mapping(_get(payload, "brand.comparison", default={}))

    entries = [
        (client_name, _get(own, "appearance_rate", "recognition_rate"), _evidence(own), True)
    ]
    for row in competitors:
        entries.append(
            (
                _text(_get(row, "name"), "Rival"),
                _get(row, "appearance_rate"),
                _evidence(row),
                False,
            )
        )
    entries.sort(key=lambda item: (_rate(item[1]) or -1.0), reverse=True)

    body = "".join(
        f"""
        <tr{' class="is-client"' if is_client else ''}>
          <th scope="row">{_name(name, suffix='<span class="you">You</span>' if is_client else "")}</th>
          <td class="numeric lead">{escape(_percent(rate))}</td>
          <td class="numeric">{escape(evidence)}</td>
        </tr>"""
        for name, rate, evidence, is_client in entries
    )
    gap = _number(_get(comparison, "gap_pp"))
    leader = _text(_get(comparison, "leader_name"), "the strongest tested rival")
    if gap is None:
        verdict = "No rival was measured by name"
    elif gap < -0.05:
        verdict = f"{abs(gap):.1f} points behind {leader}"
    elif gap > 0.05:
        verdict = f"{gap:.1f} points ahead of {leader}"
    else:
        verdict = f"Level with {leader}"
    return f"""
      <table class="grid" aria-label="Brand recognition">
        <thead>
          <tr>
            <th scope="col" style="width:56%">Clinic</th>
            <th scope="col" class="numeric" style="width:22%">Recognised in</th>
            <th scope="col" class="numeric" style="width:22%">Answers</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
      <p class="note note--status">
        {_status_mark(_get(comparison, "rating_label", "rating"))}
        <span>{escape(verdict)}.</span>
      </p>"""


def _head_to_head_outcome(row: Any) -> tuple[str, Any]:
    """The outcome IS the first-named comparison — nothing else decides it."""
    valid = _number(_get(row, "valid_outcomes")) or 0
    client = _rate(_get(row, "client_first_rate", "client_first_named_rate"))
    rival = _rate(_get(row, "rival_first_rate", "competitor_first_named_rate"))
    rating = _get(row, "rating_label", "rating")
    if valid <= 0 or client is None or rival is None:
        return "Not measured", "not_measured"
    if client > rival:
        return "Win", rating or "good"
    if client < rival:
        return "Loss", rating or "poor"
    return "Tie", rating or "mediocre"


def _first_named_cell(count: Any, rate: Any) -> str:
    """The count leads and the percentage follows it, because 100% of three
    answers is not the same claim as 100% of thirty."""
    if count is None:
        return f'<span class="figure">{escape(_percent(rate))}</span>'
    return (
        f'<span class="figure">{_html(count, "0")}</span>'
        f'<span class="sub">{escape(_percent(rate))}</span>'
    )


def _head_to_head(rows: Sequence[Any]) -> str:
    """Page 3's one focal panel: the only block whose result is a verdict."""
    if not rows:
        body = (
            '<tr><td colspan="5" class="empty">No rival was tested by name against '
            "this clinic.</td></tr>"
        )
    else:
        rendered: list[str] = []
        for row in rows:
            outcome, rating = _head_to_head_outcome(row)
            rendered.append(
                f"""
            <tr>
              <th scope="row">{_name(_get(row, "competitor", "competitor_name", "rival", "label"))}</th>
              <td class="numeric">{_html(_get(row, "valid_outcomes"), "0")}</td>
              <td class="numeric">{_first_named_cell(_get(row, "client_first"), _get(row, "client_first_rate", "client_first_named_rate"))}</td>
              <td class="numeric">{_first_named_cell(_get(row, "rival_first"), _get(row, "rival_first_rate", "competitor_first_named_rate"))}</td>
              <td class="status-cell">{_status_mark(rating, outcome)}</td>
            </tr>"""
            )
        body = "".join(rendered)
    return f"""
      <section class="verdict">
        <p class="verdict-rule">Outcome is decided by one thing: which clinic the answer named first.</p>
        <table class="grid grid--panel" aria-label="Head-to-head brand results">
          <thead>
            <tr>
              <th scope="col" style="width:40%">Rival</th>
              <th scope="col" class="numeric" style="width:12%">Answers</th>
              <th scope="col" class="numeric" style="width:16%">You first</th>
              <th scope="col" class="numeric" style="width:16%">Rival first</th>
              <th scope="col" class="status-cell" style="width:16%">Outcome</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </section>"""


def _coverage_and_method(payload: Mapping[str, Any], rows: Sequence[Any]) -> str:
    """Supporting evidence, at supporting weight: what was collected, and the
    rules the numbers were read under. Run mode and basket version are internal
    workflow labels and are not printed for the client."""
    coverage = _mapping(_get(payload, "coverage", default={}))
    basket = _mapping(_get(payload, "basket", default={}))
    disclaimer = _text(
        _get(payload, "methodology.disclaimer"),
        "AI answers vary over time. One answer can name several clinics, so "
        "percentages may total more than 100%. This snapshot measures whether a "
        "clinic is named - not sentiment, accuracy or source quality.",
    )
    collection_rows = "".join(
        f"""
        <tr>
          <th scope="row">{_name(_get(row, "label", "name", "key"), limit=40)}</th>
          <td class="numeric">{_html(_get(row, "collection.market.valid_outcomes"), "0")}/{_html(_get(row, "collection.market.expected_outcomes"), "0")}</td>
          <td class="numeric">{_html(_get(row, "collection.brand.valid_outcomes"), "0")}/{_html(_get(row, "collection.brand.expected_outcomes"), "0")}</td>
          <td class="reason status-cell">{escape(_coverage_reason(row))}</td>
        </tr>"""
        for row in rows
    )
    prompts = " &middot; ".join(
        part
        for part in (
            f'{_html(_get(basket, "market_prompts"), "0")} market prompts'
            if _get(basket, "market_prompts") is not None
            else "",
            f'{_html(_get(basket, "brand_prompts"), "0")} brand prompts'
            if _get(basket, "brand_prompts") is not None
            else "",
        )
        if part
    )
    return f"""
      <div class="support">
        <div class="support-block">
          <span class="support-label">Answers collected</span>
          <table class="grid grid--compact" aria-label="Collection coverage by platform">
            <thead>
              <tr>
                <th scope="col" style="width:26%">Platform</th>
                <th scope="col" class="numeric" style="width:20%">Market</th>
                <th scope="col" class="numeric" style="width:20%">Brand</th>
                <th scope="col" class="status-cell" style="width:34%">Status</th>
              </tr>
            </thead>
            <tbody>{collection_rows}</tbody>
          </table>
        </div>
        <div class="support-block">
          <span class="support-label">Method</span>
          <p class="support-fact">{_html(_get(coverage, "valid_outcomes"), "0")} of {_html(_get(coverage, "expected_outcomes"), "0")} planned answers collected{f" &middot; {prompts}" if prompts else ""}.</p>
          <p class="support-text">{escape(disclaimer)}</p>
        </div>
      </div>"""


def _coverage_reason(row: Any) -> str:
    """One status per platform row: complete, or the first thing that failed."""
    market_ok = _is_measured(row, "market")
    brand_ok = _is_measured(row, "brand")
    if market_ok and brand_ok:
        return "Complete"
    market_reason = _unavailable_reason(_stage(row, "market"))
    brand_reason = _unavailable_reason(_stage(row, "brand"))
    # A platform that collected nothing at all says so once, rather than
    # listing the same absence under each stage.
    if not market_ok and not brand_ok and market_reason == brand_reason:
        return f"No answers ({market_reason})" if market_reason else "No answers collected"
    failures = []
    for label, ok, reason in (
        ("Market", market_ok, market_reason),
        ("Brand", brand_ok, brand_reason),
    ):
        if ok:
            continue
        failures.append(f"{label}: {reason}" if reason else f"No {label.lower()} answers")
    return "; ".join(failures)


def _call_to_action() -> str:
    """Restrained: a rule, a line of type and two live links. Not a banner."""
    return """
      <aside class="cta">
        <div class="cta-text">
          <span class="eyebrow">Next step</span>
          <p>A full visibility audit turns this snapshot into prioritised findings and an implementation roadmap.</p>
        </div>
        <div class="cta-links">
          <a class="cta-primary" href="https://citeup.tech/#contact">Discuss the full audit</a>
          <a class="cta-email" href="mailto:hello@citeup.tech">hello@citeup.tech</a>
        </div>
      </aside>"""


def _page_three(payload: Mapping[str, Any]) -> str:
    head_rows = _get(
        payload,
        "brand.head_to_head.by_competitor",
        "competition.head_to_head.by_competitor",
        default=[],
    )
    head_rows = _capped(head_rows, 4, "head-to-head rivals")
    platforms = _capped(_get(payload, "platforms", default=[]), 4, "platform completeness")
    report_id = _get(payload, "report_id")
    generated_at = _get(payload, "generated_at")
    return f"""
    <section class="report-page page-three" data-report-page="3">
      <div class="page-frame" data-overflow-root>
        {_masthead("Brand and head-to-head", "Brand visibility", compact=True, generated_at=generated_at, report_id=report_id)}
        <div class="page-content">
          {_section_head("Brand recognition", "Asked for each clinic by name")}
          {_brand_table(payload)}
          {_section_head("Head-to-head", "Same answer, both clinics named")}
          {_head_to_head(head_rows)}
          {_section_head("Coverage and method", "Supporting evidence")}
          {_coverage_and_method(payload, platforms)}
          {_call_to_action()}
          {_footer(3, report_id)}
        </div>
      </div>
    </section>"""


# ------------------------------------------------------------------------
# PRESENTATION V2 — bounded treatment × catchment × platform reporting
# ------------------------------------------------------------------------


def _v2_identity_attributes(model: Mapping[str, Any]) -> str:
    """Repeat immutable identity on every page for HTML/PDF agreement checks."""
    fields = {
        "data-report-id": model.get("report_id"),
        "data-scope-hash": model.get("scope_hash"),
        "data-prompt-pack-hash": model.get("prompt_pack_hash"),
        "data-results-hash": model.get("results_hash"),
        "data-copy-hash": model.get("copy_hash"),
        "data-publication-state": _get(model, "publication.state"),
    }
    return " ".join(
        f'{name}="{escape(_text(value, ""), quote=True)}"'
        for name, value in fields.items()
    )


def _v2_watermark(model: Mapping[str, Any]) -> str:
    label = _text(_get(model, "publication.watermark"), "")
    if not label:
        return ""
    return f'<div class="v2-watermark" aria-hidden="true">{escape(label)}</div>'


def _v2_verdict_status(verdict: Any, label: Any = None) -> str:
    styles = {
        "strong": "good",
        "balanced": "mediocre",
        "weak": "poor",
        "not_measured": "not-measured",
    }
    text = _text(label if label is not None else verdict, "Not measured")
    style = styles.get(_text(verdict, ""), "not-measured")
    return (
        f'<span class="status status--{style}">'
        '<span class="status-dot" aria-hidden="true"></span>'
        f'<span>{escape(text)}</span></span>'
    )


def _v2_metric(metric: Any) -> str:
    """Every reported rate keeps its numerator and denominator attached."""
    return _html(_get(metric, "display"), "Not measured")


def _v2_position(value: Any) -> str:
    numeric = _number(value)
    return "Not measured" if numeric is None else f"{numeric:.1f}"


def _v2_metric_panel(model: Mapping[str, Any]) -> str:
    primary = _mapping(model.get("primary"))
    clinic = _mapping(primary.get("clinic"))
    rival = _mapping(primary.get("strongest_competitor"))
    rival_name = _text(rival.get("name"), "No measured competitor")
    gap_value = (
        f"Visibility {_signed_gap(primary.get('appearance_gap_pp'))} · "
        f"Qualified {_signed_gap(primary.get('qualified_gap_pp'))}"
    )
    gap_note = "Positive means competitor ahead; negative means clinic ahead."
    return f"""
      <div class="v2-metric-grid" aria-label="Primary scope measurements">
        <div class="v2-metric">
          <span class="metric-label">Clinic visibility</span>
          <strong>{_v2_metric(clinic.get("visibility"))}</strong>
          <span>{_html(model.get("clinic_name"), "Clinic")}</span>
        </div>
        <div class="v2-metric">
          <span class="metric-label">Qualified recommendation</span>
          <strong>{_v2_metric(clinic.get("qualified_recommendation"))}</strong>
          <span>Recommended or positively shortlisted</span>
        </div>
        <div class="v2-metric">
          <span class="metric-label">Strongest focused competitor</span>
          <strong>{_v2_metric(rival.get("visibility")) if rival else "Not verified"}</strong>
          <span>{escape(rival_name)} · qualified {_v2_metric(rival.get("qualified_recommendation")) if rival else "Not measured"}</span>
        </div>
        <div class="v2-metric">
          <span class="metric-label">Measured competitor lead</span>
          <strong>{escape(gap_value)}</strong>
          <span>{escape(gap_note)}</span>
        </div>
      </div>"""


def _v2_page_one(model: Mapping[str, Any]) -> str:
    primary = _mapping(model.get("primary"))
    copy = _mapping(_get(model, "copy.page_1", default={}))
    report_id = model.get("report_id")
    scope_line = " · ".join(
        value for value in (
            _text(primary.get("treatment"), ""),
            _text(primary.get("area"), ""),
            _text(primary.get("platform"), ""),
        ) if value
    )
    outside_in = (
        '<div class="v2-outside-in"><p>Outside-in initial Snapshot based on publicly '
        'available treatment and location information.</p></div>'
        if model.get("outside_in") else ""
    )
    return f"""
    <section class="report-page page-one v2-page" data-report-page="1" {_v2_identity_attributes(model)}>
      {_v2_watermark(model)}
      <div class="page-frame" data-overflow-root>
        {_masthead(
            _text(model.get("clinic_name"), "Clinic"),
            "Free visibility snapshot",
            compact=False,
            generated_at=model.get("generated_at"),
            report_id=report_id,
            area=model.get("area"),
        )}
        <div class="page-content v2-page-content">
          {outside_in}
          <p class="v2-scope-copy" data-copy-policy="narrative">{_html(copy.get("scope_statement"), "")}</p>
          <section class="v2-verdict-panel">
            <div class="v2-verdict-heading">
              <span class="eyebrow">Preliminary verdict</span>
              <h2 data-copy-policy="narrative">{_html(copy.get("headline"), "")}</h2>
              <p class="v2-scope-label">{escape(scope_line)}</p>
            </div>
            <p class="v2-finding-copy" data-copy-policy="narrative">{_html(copy.get("finding_summary"), "")}</p>
          </section>
          {_v2_metric_panel(model)}
          <p class="v2-coverage-copy" data-copy-policy="narrative">{_html(copy.get("coverage_statement"), "")}</p>
          {_footer(1, report_id)}
        </div>
      </div>
    </section>"""


def _v2_scope_rows(scopes: Sequence[Any]) -> str:
    rows: list[str] = []
    for raw in scopes:
        scope = _mapping(raw)
        platform_scopes = _sequence(scope.get("platform_scopes"))
        if platform_scopes:
            branch = _text(scope.get("branch"), "Branch")
            treatment = _text(scope.get("treatment"), "Treatment")
            area = _text(scope.get("area"), "Area")
            scope_ids = ",".join(
                _text(_mapping(item).get("scope_id"), "") for item in platform_scopes
            )
            collection_lines = []
            clinic_lines = []
            rival_lines = []
            gap_lines = []
            verdict_lines = []
            if any(_text(_mapping(item).get("basket_id")) for item in platform_scopes):
                basket_labels = {
                    "treatment_local": "Local treatment",
                    "treatment_expanded": "Wider-area treatment",
                    "clinic_archetype": "General clinic profile",
                }
                grouped: dict[str, list[Mapping[str, Any]]] = {}
                for raw_item in platform_scopes:
                    item = _mapping(raw_item)
                    basket_id = _text(item.get("basket_id"), "measured_basket")
                    grouped.setdefault(basket_id, []).append(item)
                for basket_id in (
                    "treatment_local",
                    "treatment_expanded",
                    "clinic_archetype",
                    *sorted(set(grouped) - {
                        "treatment_local",
                        "treatment_expanded",
                        "clinic_archetype",
                    }),
                ):
                    items = grouped.get(basket_id) or []
                    if not items:
                        continue
                    label = _text(
                        items[0].get("basket_label"), basket_labels.get(basket_id, "Basket")
                    )
                    available = sum(
                        _text(item.get("collection_status"))
                        in {"complete", "measured", "derived_floor_average"}
                        for item in items
                    )
                    collection_lines.append(
                        f"{escape(label)}: {available}/{len(items)} platform results available"
                    )

                    def combined_metric(metric_name: str) -> str:
                        numerator = denominator = 0
                        for item in items:
                            metric = _mapping(_mapping(item.get("clinic")).get(metric_name))
                            numerator += int(_number(metric.get("numerator")) or 0)
                            denominator += int(_number(metric.get("denominator")) or 0)
                        return f"{numerator}/{denominator}" if denominator else "Not measured"

                    clinic_lines.append(
                        f"{escape(label)}: visibility {combined_metric('visibility')}; "
                        f"qualified {combined_metric('qualified_recommendation')}"
                    )
                    widest = max(
                        items,
                        key=lambda item: float(_number(item.get("appearance_gap_pp")) or 0),
                    )
                    rival = _mapping(widest.get("strongest_competitor"))
                    rival_lines.append(
                        f"{escape(label)}: {_html(rival.get('name'), 'No confirmed competitor')}"
                    )
                    gap_lines.append(
                        f"{escape(label)}: {escape(_signed_gap(widest.get('appearance_gap_pp')))} / "
                        f"{escape(_signed_gap(widest.get('qualified_gap_pp')))}"
                    )
                    labels = sorted(
                        {
                            _text(item.get("preliminary_verdict_label"), "Not measured")
                            for item in items
                        }
                    )
                    verdict_lines.append(f"{escape(label)}: {escape(', '.join(labels))}")
            else:
                for item in platform_scopes:
                    item = _mapping(item)
                    platform = _text(item.get("platform"), "Platform")
                    platform_label = escape(platform)
                    clinic = _mapping(item.get("clinic"))
                    rival = _mapping(item.get("strongest_competitor"))
                    collection_status = _text(
                        item.get("collection_status"), "not measured"
                    ).replace("_", " ")
                    collection_lines.append(
                        f"{platform_label}: {escape(collection_status)}"
                    )
                    clinic_lines.append(
                        f"{platform_label}: visibility {_v2_metric(clinic.get('visibility'))}; "
                        f"qualified {_v2_metric(clinic.get('qualified_recommendation'))}"
                    )
                    rival_lines.append(
                        f"{platform_label}: {_html(rival.get('name'), 'No confirmed competitor')}"
                    )
                    gap_lines.append(
                        f"{platform_label}: {escape(_signed_gap(item.get('appearance_gap_pp')))} / "
                        f"{escape(_signed_gap(item.get('qualified_gap_pp')))}"
                    )
                    verdict_lines.append(
                        f"{platform_label}: {_html(item.get('preliminary_verdict_label'), 'Not measured')}"
                    )
            derived_items = [
                _mapping(item)
                for item in platform_scopes
                if _text(_mapping(item).get("measurement_mode")).startswith(
                    "derived_floor_average"
                )
            ]
            if derived_items:
                collection_lines.append(
                    escape(
                        "Derived: Microsoft Copilot is the floor average of "
                        "ChatGPT, Gemini, and Google AI Overview; it is not a "
                        "browser-measured Copilot result."
                    )
                )
            line = lambda values: "".join(f'<span class="sub">{value}</span>' for value in values)
            rows.append(f"""
              <tr data-scope-id="{escape(scope_ids, quote=True)}">
                <th scope="row">
                  <span class="name">{escape(branch)} · {escape(treatment)}</span>
                  <span class="sub">{escape(area)}</span>
                  {line(collection_lines)}
                </th>
                <td>{line(clinic_lines)}</td>
                <td>{line(rival_lines)}</td>
                <td class="v2-gap-cell">{line(gap_lines)}</td>
                <td>{line(verdict_lines)}</td>
              </tr>""")
            continue
        clinic = _mapping(scope.get("clinic"))
        rival = _mapping(scope.get("strongest_competitor"))
        rival_name = _text(rival.get("name"), "No measured competitor")
        gap_parts = []
        if scope.get("appearance_gap_pp") is not None:
            gap_parts.append(f"Visibility {_signed_gap(scope.get('appearance_gap_pp'))}")
        if scope.get("qualified_gap_pp") is not None:
            gap_parts.append(f"Qualified {_signed_gap(scope.get('qualified_gap_pp'))}")
        gap = "<br>".join(escape(item) for item in gap_parts) or "Not measured"
        scope_name = f"{_text(scope.get('treatment'), 'Treatment')} · {_text(scope.get('area'), 'Area')}"
        platform = _text(scope.get("platform"), "Platform")
        collection_status = _text(scope.get("collection_status"), "not measured").replace("_", " ")
        preliminary = _mapping(scope.get("preliminary_verdict"))
        derivation_note = (
            '<span class="sub">Derived: floor average of ChatGPT, Gemini, and '
            "Google AI Overview; not browser-measured.</span>"
            if _text(scope.get("measurement_mode")).startswith("derived_floor_average")
            else ""
        )
        rows.append(f"""
          <tr data-scope-id="{escape(_text(scope.get('scope_id'), ''), quote=True)}">
            <th scope="row">
              <span class="name">{escape(scope_name)}</span>
              <span class="sub">{escape(platform)} · {escape(collection_status)}</span>
              {derivation_note}
            </th>
            <td><span class="figure">Visibility {_v2_metric(clinic.get('visibility'))}</span><span class="sub">Qualified {_v2_metric(clinic.get('qualified_recommendation'))}</span><span class="sub">First {_v2_metric(clinic.get('first_named'))} · mean {_v2_position(clinic.get('mean_position'))}</span></td>
            <td><span class="name">{escape(rival_name)}</span><span class="sub">Visibility {_v2_metric(rival.get('visibility')) if rival else 'Not measured'}</span><span class="sub">Qualified {_v2_metric(rival.get('qualified_recommendation')) if rival else 'Not measured'}</span></td>
            <td class="v2-gap-cell">{gap}</td>
            <td>{_v2_verdict_status(preliminary.get('status'), preliminary.get('label'))}</td>
          </tr>""")
    return "".join(rows)


def _v2_evidence_examples(examples: Sequence[Any]) -> str:
    if not examples:
        return '<p class="v2-empty-evidence">No evidence example was selected for the primary scope.</p>'
    cards: list[str] = []
    for raw in examples[:2]:
        item = _mapping(raw)
        rival_labels = ", ".join(
            f"{_text(_get(row, 'name'), 'Competitor')}: {_text(_get(row, 'classification'), 'Not measured')}"
            for row in _sequence(item.get("competitor_classifications"))
        )
        metadata = " · ".join(
            value for value in (
                _text(item.get("platform"), ""),
                _text(item.get("model_or_surface"), ""),
                _text(item.get("timestamp"), ""),
                f"run {_text(item.get('run'), '')}" if _text(item.get("run"), "") else "",
            ) if value
        )
        cards.append(f"""
          <article class="v2-evidence-example">
            <span class="eyebrow">Evidence example</span>
            <p class="v2-evidence-prompt">{_html(item.get("prompt"), "Prompt unavailable")}</p>
            <blockquote>{_html(item.get("excerpt"), "Excerpt unavailable")}</blockquote>
            <p class="v2-evidence-classification"><strong>Clinic:</strong> {_html(item.get("client_classification"), "Not measured")}{' · <strong>Competitors:</strong> ' + escape(rival_labels) if rival_labels else ''}</p>
            <p class="v2-evidence-meta">{escape(metadata or 'Collection reference unavailable')} · prompt {_html(item.get("prompt_id"), "unassigned")}</p>
          </article>""")
    return '<div class="v2-evidence-grid">' + "".join(cards) + "</div>"


def _v2_page_two(model: Mapping[str, Any]) -> str:
    scopes = _sequence(model.get("scope_groups")) or _sequence(model.get("scopes"))
    report_id = model.get("report_id")
    boundary = _get(model, "copy.page_2.boundary")
    density = " v2-page--dense" if len(scopes) > 4 else ""
    return f"""
    <section class="report-page page-two v2-page{density}" data-report-page="2" {_v2_identity_attributes(model)}>
      {_v2_watermark(model)}
      <div class="page-frame" data-overflow-root>
        {_masthead("Treatment and competition", "Measured scopes", compact=True, generated_at=model.get("generated_at"), report_id=report_id)}
        <div class="page-content v2-page-content">
          <p class="v2-scope-copy" data-copy-policy="narrative">{_html(boundary, "")}</p>
          <table class="grid v2-scope-grid">
            <colgroup><col class="v2-col-scope"><col class="v2-col-clinic"><col class="v2-col-rival"><col class="v2-col-gap"><col class="v2-col-verdict"></colgroup>
            <thead><tr><th>Scope and collection</th><th>{_html(model.get('clinic_name'), 'Clinic')} visibility</th><th>Strongest focused competitor</th><th>Appearance / qualified lead</th><th>Preliminary verdict<span class="sub">A full Diagnostic Audit establishes robust results.</span></th></tr></thead>
            <tbody>{_v2_scope_rows(scopes)}</tbody>
          </table>
          {_section_head("Evidence from the primary scope", "Verbatim answer excerpts")}
          {_v2_evidence_examples(_sequence(model.get("evidence_examples")))}
          {_footer(2, report_id)}
        </div>
      </div>
    </section>"""


def _v2_statement_list(items: Sequence[Any], *, css_class: str) -> str:
    return "<ul class=\"%s\">%s</ul>" % (
        css_class,
        "".join(
            f'<li data-copy-policy="narrative">{_html(item, "")}</li>'
            for item in items
        ),
    )


def _v2_investigations(items: Sequence[Any]) -> str:
    if not items:
        return '<p class="v2-empty-evidence">No further investigation is recommended from this evidence.</p>'
    rows = []
    for raw in items:
        item = _mapping(raw)
        rows.append(f"""
          <article class="v2-investigation">
            <span class="eyebrow">{_html(item.get('category'), 'Further investigation')}</span>
            <h3 data-copy-policy="narrative">{_html(item.get('question_to_answer'), '')}</h3>
            <p data-copy-policy="narrative">{_html(item.get('why_required'), '')}</p>
          </article>""")
    return '<div class="v2-investigation-grid">' + "".join(rows) + "</div>"


def _v2_cta(cta: Mapping[str, Any]) -> str:
    href = _text(cta.get("href"), "")
    label = _text(cta.get("label"), "")
    link = (
        f'<a class="cta-primary" href="{escape(href, quote=True)}">{escape(label)}</a>'
        if href and label else ""
    )
    return f"""
      <aside class="cta v2-cta v2-cta--{escape(_text(cta.get('kind'), 'none'), quote=True)}">
        <div class="cta-text">
          <span class="eyebrow">{_html(cta.get('title'), 'Conclusion')}</span>
          <p data-copy-policy="narrative">{_html(cta.get('body'), '')}</p>
        </div>
        <div class="cta-links">{link}</div>
      </aside>"""


def _v2_artifact_identity(model: Mapping[str, Any]) -> str:
    rows = (
        ("Report", model.get("report_id")),
        ("Scope", model.get("scope_hash")),
        ("Prompt pack", model.get("prompt_pack_hash")),
        ("Results", model.get("results_hash")),
        ("Copy", model.get("copy_hash")),
    )
    return '<dl class="v2-artifact-identity">' + "".join(
        f'<div><dt>{label}</dt><dd>{_html(value, "unassigned")}</dd></div>'
        for label, value in rows
    ) + "</dl>"


def _v2_page_three(model: Mapping[str, Any]) -> str:
    copy = _mapping(_get(model, "copy.page_3", default={}))
    cta = _mapping(_get(model, "copy.cta", default={}))
    report_id = model.get("report_id")
    return f"""
    <section class="report-page page-three v2-page" data-report-page="3" {_v2_identity_attributes(model)}>
      {_v2_watermark(model)}
      <div class="page-frame" data-overflow-root>
        {_masthead("Meaning and next investigation", "Evidence boundary", compact=True, generated_at=model.get("generated_at"), report_id=report_id)}
        <div class="page-content v2-page-content">
          <div class="v2-boundary-grid">
            <section>
              {_section_head("What the Snapshot establishes")}
              {_v2_statement_list(_sequence(copy.get('establishes')), css_class='v2-statement-list v2-statement-list--positive')}
            </section>
            <section>
              {_section_head("What it does not establish")}
              {_v2_statement_list(_sequence(copy.get('does_not_establish')), css_class='v2-statement-list')}
            </section>
          </div>
          {_section_head("What the Diagnostic Audit must investigate", "Investigation, not diagnosis")}
          {_v2_investigations(_sequence(copy.get("investigations")))}
          {_section_head("Coverage, method and preliminary next step")}
          {_v2_cta(cta)}
          <div class="v2-method-line">
            <p>Rates use valid observations only. Evidence excerpts are verbatim and separately labelled. Report identity:</p>
            {_v2_artifact_identity(model)}
          </div>
          {_footer(3, report_id)}
        </div>
      </div>
    </section>"""


def _render_v2_document(
    payload: Mapping[str, Any],
    *,
    embedded_fonts_css: str,
    asset_css: str,
) -> str:
    model = build_presentation_model(payload)
    pages = _v2_page_one(model) + _v2_page_two(model) + _v2_page_three(model)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    report_css = _CSS_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{DOCUMENT_TITLE}}": escape(
            f"{model['clinic_name']} - Free visibility snapshot"
            f" | report_id={model['report_id']}"
            f" | scope_hash={model['scope_hash']}"
            f" | prompt_pack_hash={model['prompt_pack_hash']}"
            f" | results_hash={model['results_hash']}"
            f" | copy_hash={model['copy_hash']}"
        ),
        "{{EMBEDDED_FONTS}}": embedded_fonts_css + asset_css,
        "{{REPORT_CSS}}": report_css,
        "{{REPORT_PAGES}}": pages,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    rendered_policy = scan_rendered_narrative(
        template,
        protected_terms=model.get("protected_terms", ()),
    )
    assert_copy_policy(rendered_policy)
    return template


def render_html_document(
    payload: Mapping[str, Any],
    *,
    embedded_fonts_css: str = "",
    asset_css: str = "",
) -> str:
    """Render a self-contained, escaped three-page HTML document."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    if is_presentation_v2(payload):
        return _render_v2_document(
            payload,
            embedded_fonts_css=embedded_fonts_css,
            asset_css=asset_css,
        )
    client_name = _text(_get(payload, "client.name"), "Clinic")
    pages = _page_one(payload) + _page_two(payload) + _page_three(payload)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    report_css = _CSS_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{DOCUMENT_TITLE}}": escape(f"{client_name} - Competitive visibility snapshot"),
        "{{EMBEDDED_FONTS}}": embedded_fonts_css + asset_css,
        "{{REPORT_CSS}}": report_css,
        "{{REPORT_PAGES}}": pages,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template
