"""Crop × region agri calendar PDFs (BAMIS calendar pages)."""

from bs4 import BeautifulSoup
import requests

from . import core

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


def scrape_page(task):
    crop, crop_id, region, region_id, out_dir = task

    url = f"https://www.bamis.gov.bd/en/calendar/1/{crop_id}/{region_id}/"

    try:
        resp = requests.get(url, headers=core.HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        dest = out_dir / crop / region
        dest.mkdir(parents=True, exist_ok=True)

        pdf_url = core.extract_pdf_url(soup)
        if not pdf_url:
            return f"======= No PDF: {crop} X {region}"
        pdf_url = str(pdf_url)

        if pdf_url.startswith("/"):
            pdf_url = "https://www.bamis.gov.bd" + pdf_url

        if pdf_url in core.seen_pdfs:
            return f"====== Duplicate skipped: {crop} X {region}"

        core.seen_pdfs.add(pdf_url)

        pdf_path = dest / f"{crop}_{region}.pdf"
        if pdf_path.exists():
            return f"+++ Exists: {crop} X {region}"

        pdf_resp = requests.get(pdf_url, headers=core.HEADERS, timeout=30)
        pdf_resp.raise_for_status()

        with open(pdf_path, "wb") as f:
            f.write(pdf_resp.content)

        return f"====Downloaded: {crop} X {region}"

    except Exception as e:
        return f"=====FAILED {crop} X {region}: {e}"
