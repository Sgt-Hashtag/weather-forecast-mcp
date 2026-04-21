#!/usr/bin/env python
"""CLI entry: BAMIS calendar PDFs, special/S2S bulletins, national/district bulletins."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from scrapers.bamis.calendar import CROPS, REGIONS, scrape_page
from scrapers.bamis.core import report_leftovers
from scrapers.bamis.district import collect_district_bulletins
from scrapers.bamis.national import collect_national_bulletins
from scrapers.bamis.s2s import collect_s2s_forecasts
from scrapers.bamis.special import collect_special_bulletins


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

    nation_out = out.parent / "national_bulletins"
    district_out = out.parent / "district_bulletins"

    print("\n=== National Bulletins (EN) ===")
    for line in collect_national_bulletins(nation_out):
        print(line)

    print("\n=== District Bulletins (EN) ===")
    for line in collect_district_bulletins(district_out):
        print(line)

    report_leftovers()


if __name__ == "__main__":
    main()
