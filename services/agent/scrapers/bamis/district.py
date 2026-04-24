"""EN district bulletins (one folder per district from site select)."""

import re

from . import bulletin_view, core

DISTRICT_SELECT_PAGE_URL = f"{core.BASE_URL}/en/bulletin/district/current/0"


def district_folder_name_from_label(label, dist_id):
    """
    Derive a single path segment for ``district_bulletins/<name>/``.

    Uses the English segment before ``(`` in the district ``<option>`` label,
    strips apostrophe-like characters and Windows-forbidden path symbols,
    collapses whitespace to underscores, and truncates to 120 characters.
    Falls back to ``District_<dist_id>`` if the result would be empty.
    """
    text = (label or "").split("(", 1)[0].strip() or f"District_{dist_id}"
    text = re.sub("['\u2018\u2019\u201a\u201b\u02bc\u02bb]", "", text)
    for c in '<>:"/\\|?*':
        text = text.replace(c, "_")
    text = text.strip().strip(".")
    text = re.sub(r"\s+", "_", text)
    if not text:
        text = f"District_{dist_id}"
    return text[:120]


def iter_districts_en():
    """
    Enumerate districts from the EN district bulletin placeholder page.

    Parses every ``<select><option value="N">`` with ``N > 0`` on
    ``DISTRICT_SELECT_PAGE_URL``. Returns tuples ``(district_id, label, folder_name)``
    sorted by ``district_id``. ``folder_name`` comes from ``district_folder_name_from_label``;
    if two labels collide after sanitization, the second and later use ``<base>_<district_id>``.

    Raises:
        RuntimeError: if no district options are found (site markup changed).
    """
    soup, _ = core.fetch_soup(DISTRICT_SELECT_PAGE_URL)
    by_id = {}
    for sel in soup.find_all("select"):
        for opt in sel.find_all("option"):
            v = (opt.get("value") or "").strip()
            if not v.isdigit():
                continue
            n = int(v)
            if n <= 0:
                continue
            by_id[n] = opt.get_text(strip=True) or ""
    if not by_id:
        raise RuntimeError("No district ids parsed from BAMIS district select; check DISTRICT_SELECT_PAGE_URL")

    used = set()
    out = []
    for n in sorted(by_id):
        label = by_id[n]
        base = district_folder_name_from_label(label, n)
        name = base
        if name in used:
            name = f"{base}_{n}"
        used.add(name)
        out.append((n, label, name))
    return tuple(out)


def collect_district_bulletins(out_root):
    """
    Fetch EN district bulletin current and archive for every district from
    ``iter_districts_en``.

    Each district is stored as ``out_root/<folder_name>/{current,archive}/``,
    where ``folder_name`` is derived from the select label. URLs use the
    numeric district id. Same per-section rules as national: empty current
    listing → ``district_current_rendered.pdf``; each archive view → native
    PDF or ``view_<id>_rendered.pdf``.
    """
    results = []
    try:
        districts = iter_districts_en()
    except Exception as e:
        core.log.warning("district/listing failed: %s", e)
        return [f"===== FAILED district/listing: {e}"]
    core.log.info("district: %d entries from EN select", len(districts))

    for dist_id, _label, folder_name in districts:
        cur_u = f"{core.BASE_URL}/en/bulletin/district/current/{dist_id}"
        arc_u = f"{core.BASE_URL}/en/bulletin/district/archive/{dist_id}"
        base = out_root / folder_name
        current_dir = base / "current"
        archive_dir = base / "archive"

        try:
            soup, html = core.fetch_soup(cur_u)
            core.track_pdfs_in_page(soup, html)
            ids = bulletin_view.iter_bulletin_view_ids(soup, html)
            if ids:
                core.log.info("district %s (%s)/current: %d view ids", folder_name, dist_id, len(ids))
                for vid in ids:
                    results.append(bulletin_view.process_bulletin_view(vid, current_dir))
            else:
                core.log.info("district %s (%s)/current: no view links; rendering listing page", folder_name, dist_id)
                dest = current_dir / "district_current_rendered.pdf"
                results.append(bulletin_view.render_url_to_pdf(cur_u, dest))
        except Exception as e:
            core.log.warning("district %s (%s)/current failed: %s", folder_name, dist_id, e)
            results.append(f"===== FAILED district/{folder_name}/{dist_id}/current: {e}")

        try:
            soup, html = core.fetch_soup(arc_u)
            core.track_pdfs_in_page(soup, html)
            ids = bulletin_view.iter_bulletin_view_ids(soup, html)
            core.log.info("district %s (%s)/archive: %d view ids", folder_name, dist_id, len(ids))
            for vid in ids:
                results.append(bulletin_view.process_bulletin_view(vid, archive_dir))
        except Exception as e:
            core.log.warning("district %s (%s)/archive failed: %s", folder_name, dist_id, e)
            results.append(f"===== FAILED district/{folder_name}/{dist_id}/archive: {e}")

    return results
