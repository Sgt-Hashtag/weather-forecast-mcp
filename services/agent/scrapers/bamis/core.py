"""Shared BAMIS HTTP helpers, PDF detection/download, and classification bookkeeping."""

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("bamis")

BASE_URL = "https://www.bamis.gov.bd"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

seen_pdfs = set()
discovered_pdfs = set()

PRINT_PDF_KEEP_TOGETHER_CSS = """
@media print {
  table { break-inside: auto; }
  thead, tfoot { break-inside: avoid; page-break-inside: avoid; }
  tr {
    break-inside: avoid;
    page-break-inside: avoid;
  }
  td, th {
    break-inside: avoid;
    page-break-inside: avoid;
  }
  img {
    break-inside: avoid;
    page-break-inside: avoid;
    max-width: 100% !important;
  }
}
"""

PAGES_PREFIX = "/res/public/pages/"
ATTACHMENT_PREFIX = "/res/public/attachment/"
BAMIS_PDF_PREFIXES = (PAGES_PREFIX, ATTACHMENT_PREFIX)
BAMIS_PDF_RE = re.compile(
    r"/res/public/(?:pages|attachment)/\d{4}/\d{2}/\d{2}/[^\s\"']+\.pdf",
    re.IGNORECASE,
)


def _is_bamis_pdf_url(val):
    """Return True for BAMIS PDF URLs under pages/ or attachment/."""
    if not val:
        return False
    clean = val.lower().split("?", 1)[0]
    return clean.endswith(".pdf") and any(p in clean for p in BAMIS_PDF_PREFIXES)


def extract_pdf_url(soup):
    a = soup.select_one("a.downloads")
    if a and a.get("href"):
        return a["href"]

    iframe = soup.select_one("iframe#preview")
    if iframe and iframe.get("src"):
        return iframe["src"]

    return None


def absolutize(url):
    if not url:
        return ""
    url = str(url).strip()
    return urljoin(BASE_URL + "/", url) if url.startswith("/") else url


def _to_en_page(url):
    """Normalize /page/{id} URLs to /en/page/{id}/."""
    return re.sub(
        r"(https?://[^/]+)/(?:en/|bn/)?page/(\d+)/?",
        r"\1/en/page/\2/",
        url,
        count=1,
    )


def track_pdfs_in_page(soup, page_text=""):
    """Track all BAMIS PDF links seen in a page."""
    for tag_name, attr in [("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data")]:
        for tag in soup.find_all(tag_name):
            val = tag.get(attr, "") or ""
            if _is_bamis_pdf_url(val):
                discovered_pdfs.add(absolutize(val))
    for m in BAMIS_PDF_RE.findall(page_text or ""):
        discovered_pdfs.add(absolutize(m))


def extract_pages_pdf(soup, page_text=""):
    """Return the first BAMIS PDF link in a bulletin page."""
    for tag_name, attr in [("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data")]:
        for tag in soup.find_all(tag_name):
            val = tag.get(attr, "") or ""
            if _is_bamis_pdf_url(val):
                return absolutize(val)
    m = BAMIS_PDF_RE.search(page_text or "")
    return absolutize(m.group(0)) if m else None


def pdf_filename_from_url(pdf_url):
    """Map BAMIS PDF URL path to YYYY-MM-DD_<id>.pdf."""
    try:
        for prefix in BAMIS_PDF_PREFIXES:
            if prefix in pdf_url:
                tail = pdf_url.split(prefix, 1)[1]
                y, mo, d, name = tail.split("/")[:4]
                return f"{y}-{mo}-{d}_{name}"
    except Exception:
        pass
    return pdf_url.rsplit("/", 1)[-1]


def download_pdf(pdf_url, dest):
    """Download a PDF with dedupe, exists-skip, and %PDF checks."""
    pdf_url = absolutize(pdf_url)

    if pdf_url in seen_pdfs:
        return f"====== Duplicate skipped: {dest.name}"
    seen_pdfs.add(pdf_url)

    if dest.exists():
        return f"+++ Exists: {dest.name}"

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        content = resp.content
        if not content.startswith(b"%PDF"):
            ct = resp.headers.get("Content-Type", "")
            return f"===== SKIP non-PDF content: {dest.name} (ct={ct}, sig={content[:4]!r})"

        dest.write_bytes(content)
        return f"==== Downloaded: {dest.name}"
    except Exception as e:
        log.warning("pdf download failed %s: %s", pdf_url, e)
        return f"===== FAILED {dest.name}: {e}"


def fetch_soup(url):
    """GET ``url`` and return ``(BeautifulSoup, raw_text)``; raises on non-2xx."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser"), resp.text


def report_leftovers():
    """Print PDFs discovered but not classified/downloaded."""
    leftovers = sorted(discovered_pdfs - seen_pdfs)
    print("\n--- BAMIS PDF classification report ---")
    print(f"Discovered PDFs (across visited bulletin pages): {len(discovered_pdfs)}")
    print(f"Classified / downloaded PDFs: {len(seen_pdfs)}")
    if leftovers:
        print(f"Leftover (seen but not classified) -> {len(leftovers)}:")
        for url in leftovers:
            print(f"  - {url}")
    else:
        print("No leftovers: every discovered PDF was classified into a download folder.")
