#!/usr/bin/env python

import logging
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("bamis")

BASE_URL = "https://www.bamis.gov.bd"

CROPS = {
    "rice_aman": 74, "rice_boro": 75, "rice_aus": 7,
    "wheat": 9, "mustard": 44, "potato": 52,
    "jute": 10, "maize": 8, "sugarcane": 12,
    "lentil": 58, "soybean": 36, "groundnut": 34,
    "cotton": 96, "tomato": 13, "onion": 46,
    "pepper": 45, "garlic": 48, "pumpkin": 32,
    "cucumber": 19, "gourd": 14, "pointed_gourd": 16,
    "mango": 65, "papaya": 67, "guava": 71,
    "jackfruit": 70, "litchi": 73, "jujube": 72,
    "pineapple": 68, "green_gram_kharif": 86,
    "green_gram_robi": 85, "groundnut_robi": 80,
    "groundnut_kharif": 84, "maize_rabi": 79,
    "maize_kharif": 78,
}

REGIONS = {
    "barisal": 5, "bogura": 1, "chittagong": 13,
    "comilla": 12, "dhaka": 9, "dinajpur": 6,
    "faridpur": 10, "jessore": 4, "khulna": 3,
    "mymensingh": 8, "rajshahi": 2, "rangamati": 14,
    "rangpur": 7, "sylhet": 11,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

seen_pdfs = set()
discovered_pdfs = set()

SPECIAL_CURRENT_URL = f"{BASE_URL}/en/bulletin/special/current/"
SPECIAL_ARCHIVE_URL = f"{BASE_URL}/en/bulletin/special/archive/"
S2S_CURRENT_URL = f"{BASE_URL}/en/page/sub-seasonal-to-seasonal-forecast-current"
S2S_ARCHIVE_URL = f"{BASE_URL}/en/page/sub-seasonal-to-seasonal-forecast-archive"

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


def scrape_page(task):
    crop, crop_id, region, region_id, out_dir = task

    url = f"https://www.bamis.gov.bd/en/calendar/1/{crop_id}/{region_id}/"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        dest = out_dir / crop / region
        dest.mkdir(parents=True, exist_ok=True)

        pdf_url = extract_pdf_url(soup)
        if not pdf_url:
            return f"======= No PDF: {crop} X {region}"
        pdf_url = str(pdf_url)

        if pdf_url.startswith("/"):
            pdf_url = "https://www.bamis.gov.bd" + pdf_url

        # remoce duplicate
        if pdf_url in seen_pdfs:
            return f"====== Duplicate skipped: {crop} X {region}"

        seen_pdfs.add(pdf_url)

        pdf_path = dest / f"{crop}_{region}.pdf"
        if pdf_path.exists():
            return f"+++ Exists: {crop} X {region}"

        pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        pdf_resp.raise_for_status()

        with open(pdf_path, "wb") as f:
            f.write(pdf_resp.content)

        return f"====Downloaded: {crop} X {region}"

    except Exception as e:
        return f"=====FAILED {crop} X {region}: {e}"


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
                y, m, d, name = tail.split("/")[:4]
                return f"{y}-{m}-{d}_{name}"
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
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser"), resp.text


def collect_special_bulletins(out_dir):
    """Download current and archive Special Bulletins (English PDFs)."""
    results = []

    try:
        soup, html = fetch_soup(SPECIAL_CURRENT_URL)
        track_pdfs_in_page(soup, html)
        pdf_url = extract_pages_pdf(soup, html)
        if pdf_url:
            results.append(download_pdf(pdf_url, out_dir / pdf_filename_from_url(pdf_url)))
        else:
            results.append("======= No PDF: special/current")
    except Exception as e:
        log.warning("special/current failed: %s", e)
        results.append(f"===== FAILED special/current: {e}")

    try:
        soup, html = fetch_soup(SPECIAL_ARCHIVE_URL)
        track_pdfs_in_page(soup, html)

        view_links = {
            _to_en_page(absolutize(a["href"]))
            for a in soup.select("a[href*='/page/']")
            if "btn" in (a.get("class") or []) or a.get_text(strip=True).lower() == "view"
        }
        log.info("special/archive: %d view links", len(view_links))
        if not view_links:
            log.warning("special/archive: no 'View' buttons matched - check selectors / pagination")

        for view_url in sorted(view_links):
            try:
                vsoup, vhtml = fetch_soup(view_url)
                track_pdfs_in_page(vsoup, vhtml)
                pdf_url = extract_pages_pdf(vsoup, vhtml)
                if not pdf_url:
                    results.append(f"======= No PDF: {view_url}")
                    continue
                results.append(download_pdf(pdf_url, out_dir / pdf_filename_from_url(pdf_url)))
            except Exception as e:
                log.warning("special/archive item %s: %s", view_url, e)
                results.append(f"===== FAILED {view_url}: {e}")
    except Exception as e:
        log.warning("special/archive failed: %s", e)
        results.append(f"===== FAILED special/archive: {e}")

    return results


def collect_s2s_forecasts(out_dir):
    """Download current and archive S2S forecasts (English PDFs)."""
    results = []

    try:
        soup, html = fetch_soup(S2S_CURRENT_URL)
        track_pdfs_in_page(soup, html)
        pdf_url = extract_pages_pdf(soup, html)
        if pdf_url:
            results.append(download_pdf(pdf_url, out_dir / pdf_filename_from_url(pdf_url)))
        else:
            results.append("======= No PDF: s2s/current")
    except Exception as e:
        log.warning("s2s/current failed: %s", e)
        results.append(f"===== FAILED s2s/current: {e}")

    try:
        soup, html = fetch_soup(S2S_ARCHIVE_URL)
        track_pdfs_in_page(soup, html)

        pdf_urls = {
            absolutize(a["href"])
            for a in soup.find_all("a", href=True)
            if _is_bamis_pdf_url(a["href"])
        }
        log.info("s2s/archive: %d direct pdf links", len(pdf_urls))

        for pdf_url in sorted(pdf_urls):
            results.append(download_pdf(pdf_url, out_dir / pdf_filename_from_url(pdf_url)))
    except Exception as e:
        log.warning("s2s/archive failed: %s", e)
        results.append(f"===== FAILED s2s/archive: {e}")

    return results


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
        print("No leftovers: every discovered PDF was classified into one of the two folders.")


def main():
    out = Path("../mcp_weather/data/agri_data/raw")

    tasks = [
        (crop, crop_id, region, region_id, out)
        for crop, crop_id in CROPS.items()
        for region, region_id in REGIONS.items()
    ]

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(scrape_page, task) for task in tasks]

        for future in as_completed(futures):
            print(future.result())

    special_out = out.parent / "special_bulletins"
    s2s_out = out.parent / "s2s_forecasts"

    print("\n=== Special Bulletins ===")
    for line in collect_special_bulletins(special_out):
        print(line)

    print("\n=== Sub-seasonal to Seasonal Forecasts ===")
    for line in collect_s2s_forecasts(s2s_out):
        print(line)

    report_leftovers()


if __name__ == "__main__":
    main()