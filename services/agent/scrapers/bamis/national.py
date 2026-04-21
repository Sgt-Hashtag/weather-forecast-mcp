"""EN national bulletin (current + archive)."""

from . import bulletin_view, core

NATION_CURRENT_URL = f"{core.BASE_URL}/en/bulletin/nation/current/"
NATION_ARCHIVE_URL = f"{core.BASE_URL}/en/bulletin/nation/archive/"


def collect_national_bulletins(out_root):
    """
    Fetch EN national bulletin current and archive into ``out_root``.

    Writes under ``out_root/current`` and ``out_root/archive``. Current:
    if the listing exposes no ``/bulletin/view/<id>`` links, renders the
    listing URL to ``nation_current_rendered.pdf``; otherwise processes each
    view with ``bulletin_view.process_bulletin_view``. Archive: every linked view id.
    """
    results = []
    current_dir = out_root / "current"
    archive_dir = out_root / "archive"

    try:
        soup, html = core.fetch_soup(NATION_CURRENT_URL)
        core.track_pdfs_in_page(soup, html)
        ids = bulletin_view.iter_bulletin_view_ids(soup, html)
        if ids:
            core.log.info("national/current: %d view ids", len(ids))
            for vid in ids:
                results.append(bulletin_view.process_bulletin_view(vid, current_dir))
        else:
            core.log.info("national/current: no view links; rendering listing page")
            dest = current_dir / "nation_current_rendered.pdf"
            results.append(bulletin_view.render_url_to_pdf(NATION_CURRENT_URL, dest))
    except Exception as e:
        core.log.warning("national/current failed: %s", e)
        results.append(f"===== FAILED national/current: {e}")

    try:
        soup, html = core.fetch_soup(NATION_ARCHIVE_URL)
        core.track_pdfs_in_page(soup, html)
        ids = bulletin_view.iter_bulletin_view_ids(soup, html)
        core.log.info("national/archive: %d view ids", len(ids))
        for vid in ids:
            results.append(bulletin_view.process_bulletin_view(vid, archive_dir))
    except Exception as e:
        core.log.warning("national/archive failed: %s", e)
        results.append(f"===== FAILED national/archive: {e}")

    return results
