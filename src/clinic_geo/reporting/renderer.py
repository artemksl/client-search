"""Self-contained HTML and Playwright PDF renderer for visibility snapshots."""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clinic_geo.reporting.template import ReportTemplateError, render_html_document


_HERE = Path(__file__).resolve().parent
_DEFAULT_ASSETS_DIR = _HERE / "assets"


class ReportRenderError(RuntimeError):
    """The report could not be safely rendered."""


class ReportLayoutError(ReportRenderError):
    """The fixed A4 layout overflowed or produced an unexpected page count."""


class ExternalResourceError(ReportRenderError):
    """The self-contained report attempted to fetch an external resource."""


@dataclass(frozen=True)
class LayoutIssue:
    page: int
    selector: str
    horizontal_overflow_px: float
    vertical_overflow_px: float


@dataclass(frozen=True)
class LayoutCheck:
    page_count: int
    issues: tuple[LayoutIssue, ...]


@dataclass(frozen=True)
class RenderResult:
    html_path: Path
    pdf_path: Path
    page_count: int
    blocked_requests: tuple[str, ...]
    metadata_normalized: bool


AssetValue = Path | str | bytes | bytearray


_ASSET_FILES = {
    "inter_400": "inter-400-latin.woff2",
    "inter_500": "inter-500-latin.woff2",
    "inter_600": "inter-600-latin.woff2",
    "inter_700": "inter-700-latin.woff2",
    "plex_400": "ibm-plex-mono-400-latin.woff2",
    "plex_500": "ibm-plex-mono-500-latin.woff2",
    "plex_600": "ibm-plex-mono-600-latin.woff2",
    "agate": "agate-desktop.webp",
}


def _asset_bytes(
    key: str,
    *,
    assets_dir: Path,
    asset_overrides: Mapping[str, AssetValue] | None,
) -> tuple[bytes, str]:
    value: AssetValue = (asset_overrides or {}).get(key, assets_dir / _ASSET_FILES[key])
    if isinstance(value, (bytes, bytearray)):
        content = bytes(value)
        suffix = Path(_ASSET_FILES[key]).suffix
    else:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ReportRenderError(f"Required embedded asset is missing: {path}")
        content = path.read_bytes()
        suffix = path.suffix
    mime = mimetypes.types_map.get(suffix.lower())
    if suffix.lower() == ".woff2":
        mime = "font/woff2"
    elif suffix.lower() == ".webp":
        mime = "image/webp"
    if not mime:
        raise ReportRenderError(f"Cannot determine MIME type for embedded asset {key!r}")
    return content, mime


def _data_uri(content: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def embedded_asset_css(
    *,
    assets_dir: str | Path | None = None,
    asset_overrides: Mapping[str, AssetValue] | None = None,
) -> tuple[str, str]:
    """Return local font CSS and local image-variable CSS as data URIs."""
    base = Path(assets_dir).expanduser().resolve() if assets_dir else _DEFAULT_ASSETS_DIR
    loaded = {
        key: _asset_bytes(key, assets_dir=base, asset_overrides=asset_overrides)
        for key in _ASSET_FILES
    }
    uris = {key: _data_uri(*value) for key, value in loaded.items()}
    font_css = f"""
@font-face {{
  font-family: "Citeup Inter";
  src: url("{uris['inter_400']}") format("woff2");
  font-style: normal;
  font-weight: 400;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Inter";
  src: url("{uris['inter_500']}") format("woff2");
  font-style: normal;
  font-weight: 500;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Inter";
  src: url("{uris['inter_600']}") format("woff2");
  font-style: normal;
  font-weight: 600;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Inter";
  src: url("{uris['inter_700']}") format("woff2");
  font-style: normal;
  font-weight: 700;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Plex Mono";
  src: url("{uris['plex_400']}") format("woff2");
  font-style: normal;
  font-weight: 400;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Plex Mono";
  src: url("{uris['plex_500']}") format("woff2");
  font-style: normal;
  font-weight: 500;
  font-display: block;
}}
@font-face {{
  font-family: "Citeup Plex Mono";
  src: url("{uris['plex_600']}") format("woff2");
  font-style: normal;
  font-weight: 600;
  font-display: block;
}}
""".strip()
    image_css = f':root {{ --agate-image: url("{uris["agate"]}"); }}'
    return font_css, image_css


def render_html(
    payload: Mapping[str, Any],
    *,
    assets_dir: str | Path | None = None,
    asset_overrides: Mapping[str, AssetValue] | None = None,
) -> str:
    """Create a standalone HTML string with no runtime resource requests."""
    font_css, image_css = embedded_asset_css(
        assets_dir=assets_dir, asset_overrides=asset_overrides
    )
    return render_html_document(
        payload,
        embedded_fonts_css=font_css,
        asset_css=image_css,
    )


_LAYOUT_CHECK_JS = r"""
() => {
  const pages = Array.from(document.querySelectorAll('.report-page'));
  const issues = [];
  const roots = [
    ...document.querySelectorAll('[data-overflow-root]'),
    ...document.querySelectorAll(
      '.headline, .metric-row, .conclusion, .verdict, .support, .grid, .cta, .footer'
    ),
    // A table-layout:fixed cell whose content is wider than its column does not
    // widen the table: it paints straight over the column beside it, so the
    // table's own scrollWidth stays clean and the overflow goes unseen. The
    // cells have to be measured individually.
    ...document.querySelectorAll('.grid th, .grid td')
  ];
  for (const root of roots) {
    const page = root.closest('.report-page');
    const pageNumber = page ? Number(page.dataset.reportPage || 0) : 0;
    const horizontal = Math.max(0, root.scrollWidth - root.clientWidth);
    const vertical = Math.max(0, root.scrollHeight - root.clientHeight);
    if (horizontal > 1 || vertical > 1) {
      const tag = root.tagName.toLowerCase();
      const classes = Array.from(root.classList).slice(0, 3).join('.');
      issues.push({
        page: pageNumber,
        selector: classes ? `${tag}.${classes}` : tag,
        horizontal_overflow_px: horizontal,
        vertical_overflow_px: vertical,
      });
    }
  }
  return {page_count: pages.length, issues};
}
"""


def _layout_check(page: Any) -> LayoutCheck:
    raw = page.evaluate(_LAYOUT_CHECK_JS)
    issues = tuple(
        LayoutIssue(
            page=int(item.get("page", 0)),
            selector=str(item.get("selector", "unknown")),
            horizontal_overflow_px=float(item.get("horizontal_overflow_px", 0)),
            vertical_overflow_px=float(item.get("vertical_overflow_px", 0)),
        )
        for item in raw.get("issues", [])
    )
    return LayoutCheck(page_count=int(raw.get("page_count", 0)), issues=issues)


def _format_layout_error(check: LayoutCheck) -> str:
    details = "; ".join(
        f"page {issue.page} {issue.selector}: +{issue.horizontal_overflow_px:.1f}px x, "
        f"+{issue.vertical_overflow_px:.1f}px y"
        for issue in check.issues[:8]
    )
    if len(check.issues) > 8:
        details += f"; and {len(check.issues) - 8} more"
    return details or "unknown overflow"


def render_pdf(
    html: str,
    pdf_path: str | Path,
    *,
    chromium_executable: str | Path | None = None,
) -> LayoutCheck:
    """Print standalone HTML to A4 with network blocked and layout checked."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on installation state
        raise ReportRenderError(
            "Playwright is required for PDF rendering. Install project dependencies first."
        ) from exc

    destination = Path(pdf_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
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
                check = _layout_check(page)
                if check.page_count != 3:
                    raise ReportLayoutError(
                        f"Expected exactly 3 report pages, found {check.page_count}"
                    )
                if check.issues:
                    raise ReportLayoutError(
                        "Fixed A4 layout overflowed: " + _format_layout_error(check)
                    )
                if blocked:
                    raise ExternalResourceError(
                        "Report attempted external requests: " + ", ".join(sorted(set(blocked)))
                    )
                page.pdf(
                    path=str(destination),
                    format="A4",
                    prefer_css_page_size=True,
                    print_background=True,
                    display_header_footer=False,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    tagged=True,
                )
                context.close()
            finally:
                browser.close()
    except ReportRenderError:
        raise
    except PlaywrightError as exc:
        raise ReportRenderError(f"Chromium could not render the report: {exc}") from exc
    return check


def _pdf_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime(2000, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("D:%Y%m%d%H%M%S+00'00'")


def normalize_pdf_metadata(
    pdf_path: str | Path,
    *,
    payload: Mapping[str, Any],
) -> bool:
    """Normalize Chromium's volatile timestamps without rewriting page objects.

    Chromium emits fixed-width PDF date strings in the uncompressed Info object.
    Replacing those bytes in place keeps every cross-reference offset and shared
    page resource untouched. Re-serializing the tagged Chromium document through
    a generic PDF writer can duplicate or corrupt shared resources even when the
    resulting file passes structural checks, so this intentionally does less.
    """
    path = Path(pdf_path).expanduser().resolve()
    content = path.read_bytes()
    target = _pdf_date(payload.get("generated_at")).encode("ascii")
    pattern = re.compile(rb"/(CreationDate|ModDate) \((D:[^)]+)\)")
    replacements = 0

    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal replacements
        current = match.group(2)
        replacement = target
        if len(current) == 17:  # Some Chromium builds use a trailing Z.
            replacement = target[:16] + b"Z"
        if len(replacement) != len(current):
            return match.group(0)
        replacements += 1
        return b"/" + match.group(1) + b" (" + replacement + b")"

    normalized = pattern.sub(replace, content)
    if replacements < 2 or len(normalized) != len(content):
        return False
    path.write_bytes(normalized)
    return True


def _pdf_page_count(path: Path) -> int | None:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        completed = subprocess.run(
            [pdfinfo, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode == 0:
            match = re.search(r"^Pages:\s+(\d+)\s*$", completed.stdout, flags=re.MULTILINE)
            if match:
                return int(match.group(1))
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return len(PdfReader(str(path)).pages)


def _pdf_type3_fonts(path: Path) -> tuple[str, ...]:
    """Return page font labels that Chromium emitted as fragile Type 3 fonts."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a project dependency
        return ()
    found: list[str] = []
    for page_number, page in enumerate(PdfReader(str(path)).pages, start=1):
        resources = page.get("/Resources") or {}
        fonts = resources.get("/Font") or {}
        for name, reference in fonts.items():
            font = reference.get_object()
            if str(font.get("/Subtype")) == "/Type3":
                found.append(f"page {page_number} {name}")
    return tuple(found)


def _temporary_sibling(path: Path, suffix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}-",
        suffix=suffix,
        dir=path.parent,
        delete=False,
    )
    handle.close()
    return Path(handle.name)


def render_report(
    payload: Mapping[str, Any],
    *,
    html_path: str | Path,
    pdf_path: str | Path,
    assets_dir: str | Path | None = None,
    asset_overrides: Mapping[str, AssetValue] | None = None,
    normalize_metadata: bool = True,
    chromium_executable: str | Path | None = None,
) -> RenderResult:
    """Atomically write the standalone HTML and exactly-three-page A4 PDF.

    Existing outputs remain untouched if template validation, browser rendering,
    network isolation, layout validation, or PDF page-count validation fails.
    """
    html_destination = Path(html_path).expanduser().resolve()
    pdf_destination = Path(pdf_path).expanduser().resolve()
    html_temporary = _temporary_sibling(html_destination, ".html.tmp")
    pdf_temporary = _temporary_sibling(pdf_destination, ".pdf.tmp")
    metadata_was_normalized = False
    try:
        html = render_html(
            payload,
            assets_dir=assets_dir,
            asset_overrides=asset_overrides,
        )
        html_temporary.write_text(html, encoding="utf-8")
        layout = render_pdf(
            html,
            pdf_temporary,
            chromium_executable=chromium_executable,
        )
        if normalize_metadata:
            metadata_was_normalized = normalize_pdf_metadata(
                pdf_temporary,
                payload=payload,
            )
        physical_pages = _pdf_page_count(pdf_temporary)
        if physical_pages is not None and physical_pages != 3:
            raise ReportLayoutError(
                f"Expected exactly 3 physical PDF pages, found {physical_pages}"
            )
        type3_fonts = _pdf_type3_fonts(pdf_temporary)
        if type3_fonts:
            raise ReportRenderError(
                "Rendered PDF contains unsupported Type 3 fonts: "
                + ", ".join(type3_fonts[:8])
            )
        os.replace(html_temporary, html_destination)
        os.replace(pdf_temporary, pdf_destination)
        return RenderResult(
            html_path=html_destination,
            pdf_path=pdf_destination,
            page_count=physical_pages or layout.page_count,
            blocked_requests=(),
            metadata_normalized=metadata_was_normalized,
        )
    except (ReportTemplateError, ReportRenderError):
        raise
    except OSError as exc:
        raise ReportRenderError(f"Could not write report artifacts: {exc}") from exc
    finally:
        html_temporary.unlink(missing_ok=True)
        pdf_temporary.unlink(missing_ok=True)


__all__ = [
    "ExternalResourceError",
    "LayoutCheck",
    "LayoutIssue",
    "RenderResult",
    "ReportLayoutError",
    "ReportRenderError",
    "embedded_asset_css",
    "normalize_pdf_metadata",
    "render_html",
    "render_pdf",
    "render_report",
]
