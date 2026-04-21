"""EN special bulletins (current + archive via /en/page/ view links)."""

from . import core

SPECIAL_CURRENT_URL = f"{core.BASE_URL}/en/bulletin/special/current/"
SPECIAL_ARCHIVE_URL = f"{core.BASE_URL}/en/bulletin/special/archive/"


def collect_special_bulletins(out_dir):
    """Download current and archive Special Bulletins (English PDFs)."""
    results = []

    try:
        soup, html = core.fetch_soup(SPECIAL_CURRENT_URL)
        core.track_pdfs_in_page(soup, html)
        pdf_url = core.extract_pages_pdf(soup, html)
        if pdf_url:
            results.append(core.download_pdf(pdf_url, out_dir / core.pdf_filename_from_url(pdf_url)))
        else:
            results.append("======= No PDF: special/current")
    except Exception as e:
        core.log.warning("special/current failed: %s", e)
        results.append(f"===== FAILED special/current: {e}")

    try:
        soup, html = core.fetch_soup(SPECIAL_ARCHIVE_URL)
        core.track_pdfs_in_page(soup, html)

        view_links = {
            core._to_en_page(core.absolutize(a["href"]))
            for a in soup.select("a[href*='/page/']")
            if "btn" in (a.get("class") or []) or a.get_text(strip=True).lower() == "view"
        }
        core.log.info("special/archive: %d view links", len(view_links))
        if not view_links:
            core.log.warning("special/archive: no 'View' buttons matched - check selectors / pagination")

        for view_url in sorted(view_links):
            try:
                vsoup, vhtml = core.fetch_soup(view_url)
                core.track_pdfs_in_page(vsoup, vhtml)
                pdf_url = core.extract_pages_pdf(vsoup, vhtml)
                if not pdf_url:
                    results.append(f"======= No PDF: {view_url}")
                    continue
                results.append(core.download_pdf(pdf_url, out_dir / core.pdf_filename_from_url(pdf_url)))
            except Exception as e:
                core.log.warning("special/archive item %s: %s", view_url, e)
                results.append(f"===== FAILED {view_url}: {e}")
    except Exception as e:
        core.log.warning("special/archive failed: %s", e)
        results.append(f"===== FAILED special/archive: {e}")

    return results
