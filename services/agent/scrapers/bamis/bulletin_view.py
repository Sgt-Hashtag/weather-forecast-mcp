"""National/district bulletin detail pages: view ids, native PDF, Playwright fallback."""

import re
from pathlib import Path

from . import core

BULLETIN_VIEW_ID_RE = re.compile(r"/bulletin/view/(\d+)", re.IGNORECASE)


def iter_bulletin_view_ids(soup, page_text=""):
    """Sorted distinct bulletin view ids from ``href`` attributes and optional raw ``page_text``."""
    ids = set()
    for a in soup.find_all("a", href=True):
        m = BULLETIN_VIEW_ID_RE.search(a["href"])
        if m:
            ids.add(int(m.group(1)))
    for m in BULLETIN_VIEW_ID_RE.finditer(page_text or ""):
        ids.add(int(m.group(1)))
    return sorted(ids)


def bulletin_view_en_url(view_id):
    """Canonical English bulletin detail URL for numeric ``view_id``."""
    return f"{core.BASE_URL}/en/bulletin/view/{int(view_id)}/"


def render_url_to_pdf(page_url, dest):
    """
    Save ``page_url`` as a PDF using headless Chromium (Playwright).

    Uses print media, injects ``PRINT_PDF_KEEP_TOGETHER_CSS`` to reduce ugly
    table row splits, and records a synthetic dedupe key ``playwright:<url>/``
    in ``seen_pdfs`` on success. Skips if that key is already present or
    ``dest`` already exists.

    Requires the ``playwright`` package and an installed browser
    (``python -m playwright install chromium``).
    """
    key = f"playwright:{page_url.rstrip('/')}/"
    if key in core.seen_pdfs:
        return f"====== Duplicate skipped (render): {dest.name}"
    if dest.exists():
        core.seen_pdfs.add(key)
        return f"+++ Exists: {dest.name}"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        core.log.warning("playwright not installed; cannot render %s", page_url)
        return (
            f"===== FAILED {dest.name}: install playwright "
            f"(pip install playwright && python -m playwright install chromium)"
        )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(page_url, wait_until="load", timeout=120_000)
            page.wait_for_timeout(5000)
            page.add_style_tag(content=core.PRINT_PDF_KEEP_TOGETHER_CSS)
            page.emulate_media(media="print")
            page.pdf(
                path=str(dest),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
            )
            browser.close()
        data = dest.read_bytes()
        if not data.startswith(b"%PDF"):
            return f"===== FAILED {dest.name}: rendered output not PDF"
        core.seen_pdfs.add(key)
        return f"==== Rendered PDF: {dest.name}"
    except Exception as e:
        core.log.warning("playwright render failed %s: %s", page_url, e)
        return f"===== FAILED {dest.name}: {e}"


def process_bulletin_view(view_id, out_dir):
    """
    Persist one national or district bulletin item under ``out_dir``.

    Opens ``/en/bulletin/view/<view_id>/``. If ``extract_pdf_url`` or
    ``extract_pages_pdf`` finds a BAMIS ``/res/public/...pdf`` URL, downloads
    bytes and checks ``%PDF``. Otherwise writes ``view_<id>_rendered.pdf``
    via ``render_url_to_pdf``.
    """
    view_url = bulletin_view_en_url(view_id)
    vsoup, vhtml = core.fetch_soup(view_url)
    core.track_pdfs_in_page(vsoup, vhtml)
    pdf_url = core.extract_pdf_url(vsoup) or core.extract_pages_pdf(vsoup, vhtml)
    if pdf_url:
        return core.download_pdf(pdf_url, Path(out_dir) / core.pdf_filename_from_url(pdf_url))
    return render_url_to_pdf(view_url, Path(out_dir) / f"view_{int(view_id)}_rendered.pdf")
