"""EN sub-seasonal to seasonal (S2S) forecast pages."""

from . import core

S2S_CURRENT_URL = f"{core.BASE_URL}/en/page/sub-seasonal-to-seasonal-forecast-current"
S2S_ARCHIVE_URL = f"{core.BASE_URL}/en/page/sub-seasonal-to-seasonal-forecast-archive"


def collect_s2s_forecasts(out_dir):
    """Download current and archive S2S forecasts (English PDFs)."""
    results = []

    try:
        soup, html = core.fetch_soup(S2S_CURRENT_URL)
        core.track_pdfs_in_page(soup, html)
        pdf_url = core.extract_pages_pdf(soup, html)
        if pdf_url:
            results.append(core.download_pdf(pdf_url, out_dir / core.pdf_filename_from_url(pdf_url)))
        else:
            results.append("======= No PDF: s2s/current")
    except Exception as e:
        core.log.warning("s2s/current failed: %s", e)
        results.append(f"===== FAILED s2s/current: {e}")

    try:
        soup, html = core.fetch_soup(S2S_ARCHIVE_URL)
        core.track_pdfs_in_page(soup, html)

        pdf_urls = {
            core.absolutize(a["href"])
            for a in soup.find_all("a", href=True)
            if core._is_bamis_pdf_url(a["href"])
        }
        core.log.info("s2s/archive: %d direct pdf links", len(pdf_urls))

        for pdf_url in sorted(pdf_urls):
            results.append(core.download_pdf(pdf_url, out_dir / core.pdf_filename_from_url(pdf_url)))
    except Exception as e:
        core.log.warning("s2s/archive failed: %s", e)
        results.append(f"===== FAILED s2s/archive: {e}")

    return results
