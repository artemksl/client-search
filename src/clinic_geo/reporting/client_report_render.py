"""Self-contained HTML and unrestricted-page PDF rendering for client reports."""

from __future__ import annotations

import os
import tempfile
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

from clinic_geo.reporting.renderer import (
    ExternalResourceError,
    ReportRenderError,
    embedded_asset_css,
    normalize_pdf_metadata,
)


class _GeneratedAtParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.generated_at: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        if values.get("name") == "client-report-generated-at":
            self.generated_at = values.get("content")


def _generated_at_from_html(document: str) -> str:
    parser = _GeneratedAtParser()
    parser.feed(document)
    value = str(parser.generated_at or "").strip()
    if not value:
        raise ReportRenderError("Client-report HTML has no generation timestamp")
    return value


def render_client_report_html(
    model: Mapping,
    *,
    assets_dir: str | Path | None = None,
    asset_overrides: Mapping[str, Any] | None = None,
) -> str:
    """Render the ordered client-visible model as standalone escaped HTML."""

    if not isinstance(model, Mapping):
        raise ReportRenderError("Client-report render model must be a mapping")
    sections = model.get("sections")
    if not isinstance(sections, list):
        raise ReportRenderError("Client-report render model has no sections list")
    generated_at = str(model.get("generated_at") or "").strip()
    if not generated_at:
        raise ReportRenderError("Client-report render model has no generation timestamp")
    title = str(model.get("title") or "Diagnostic Audit")
    font_css, image_css = embedded_asset_css(
        assets_dir=assets_dir,
        asset_overrides=asset_overrides,
    )

    rendered_sections: list[str] = []
    visible_count = 0
    for section in sections:
        if not isinstance(section, Mapping):
            raise ReportRenderError("Client-report section must be a mapping")
        clauses = section.get("clauses")
        if not isinstance(clauses, list):
            raise ReportRenderError("Client-report section has no clauses list")
        rendered_clauses = []
        for clause in clauses:
            if not isinstance(clause, Mapping):
                raise ReportRenderError("Client-report clause must be a mapping")
            if clause.get("removed") is True:
                continue
            wording = escape(str(clause.get("wording") or ""))
            clause_id = escape(str(clause.get("clause_id") or ""), quote=True)
            rendered_clauses.append(
                f'<article class="report-clause" data-clause-id="{clause_id}">'
                f'<div class="report-wording">{wording}</div>'
                "</article>"
            )
            visible_count += 1
        if not rendered_clauses:
            continue
        section_title = escape(str(section.get("title") or ""))
        rendered_sections.append(
            '<section class="report-section">'
            f"<h2>{section_title}</h2>"
            f"{''.join(rendered_clauses)}"
            "</section>"
        )

    content = "".join(rendered_sections)
    if visible_count == 0:
        content = '<p class="empty-report">No clauses are included in this report.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="client-report-generated-at" content="{escape(generated_at, quote=True)}">
  <title>{escape(title)}</title>
  <style>
    {font_css}
    {image_css}
    @page {{ size: A4; margin: 18mm 17mm 19mm; }}
    * {{ box-sizing: border-box; }}
    html {{ background: #ffffff; color: #16202b; }}
    body {{
      margin: 0;
      background: #ffffff;
      color: #16202b;
      font-family: "Citeup Inter", Arial, sans-serif;
      font-size: 10.2pt;
      line-height: 1.52;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    .report-header {{
      padding-bottom: 7mm;
      border-bottom: 1.2pt solid #1d4d78;
      break-after: avoid-page;
    }}
    .report-header p {{
      margin: 0 0 1.8mm;
      color: #50708c;
      font-size: 8pt;
      font-weight: 700;
      letter-spacing: 0.13em;
      text-transform: uppercase;
    }}
    h1 {{ margin: 0; color: #123b5e; font-size: 24pt; line-height: 1.12; }}
    .report-section {{ margin-top: 9mm; }}
    h2 {{
      margin: 0 0 4mm;
      color: #174b73;
      font-size: 14pt;
      line-height: 1.25;
      break-after: avoid-page;
    }}
    .report-clause {{
      margin: 0 0 4.5mm;
      padding: 4.5mm 5mm;
      border: 0.6pt solid #cbd8e3;
      border-left: 2.4pt solid #2b6f9f;
      border-radius: 2mm;
      background: #f7fafc;
      break-inside: avoid-page;
    }}
    .report-wording {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    .empty-report {{ margin: 13mm 0 0; color: #425b70; }}
  </style>
</head>
<body>
  <header class="report-header">
    <p>Diagnostic Audit</p>
    <h1>{escape(title)}</h1>
  </header>
  <main>{content}</main>
</body>
</html>
"""


def render_client_report_pdf(
    html: str,
    pdf_path: str | Path,
    *,
    chromium_executable: str | Path | None = None,
) -> None:
    """Print standalone HTML to A4 with all non-embedded requests blocked."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - installation-specific
        raise ReportRenderError(
            "Playwright is required for client-report PDF rendering"
        ) from exc

    generated_at = _generated_at_from_html(html)
    destination = Path(pdf_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".pdf", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    blocked: list[str] = []

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            if chromium_executable:
                launch_options["executable_path"] = str(
                    Path(chromium_executable).expanduser().resolve()
                )
            browser = playwright.chromium.launch(**launch_options)
            try:
                context = browser.new_context(
                    viewport={"width": 794, "height": 1123},
                    device_scale_factor=1,
                    locale="en-GB",
                    timezone_id="Europe/London",
                )
                page = context.new_page()

                def abort_external(route: Any) -> None:
                    url = route.request.url
                    if url.startswith(("data:", "about:")):
                        route.continue_()
                    else:
                        blocked.append(url)
                        route.abort()

                page.route("**/*", abort_external)
                page.set_content(html, wait_until="load")
                page.emulate_media(media="print")
                page.evaluate("() => document.fonts.ready")
                if blocked:
                    raise ExternalResourceError(
                        "Client report attempted external requests: "
                        + ", ".join(sorted(set(blocked)))
                    )
                page.pdf(
                    path=str(temporary),
                    format="A4",
                    prefer_css_page_size=True,
                    print_background=True,
                    display_header_footer=False,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    tagged=True,
                )
                if blocked:
                    raise ExternalResourceError(
                        "Client report attempted external requests: "
                        + ", ".join(sorted(set(blocked)))
                    )
                context.close()
            finally:
                browser.close()
        if not normalize_pdf_metadata(
            temporary, payload={"generated_at": generated_at}
        ):
            raise ReportRenderError("Client-report PDF metadata could not be normalized")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except (ReportRenderError, ExternalResourceError):
        raise
    except PlaywrightError as exc:
        raise ReportRenderError(f"Chromium could not render the client report: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["render_client_report_html", "render_client_report_pdf"]
