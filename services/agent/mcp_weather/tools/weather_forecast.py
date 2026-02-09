import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List
import urllib3

# Disable SSL warnings for government sites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Bengali to English district name mapping
BENGALI_TO_ENGLISH_DISTRICTS = {
    "পাবনা": "Pabna", "ঢাকা": "Dhaka", "চট্টগ্রাম": "Chattogram",
    "কক্সবাজার": "Cox's Bazar", "সিলেট": "Sylhet", "রাজশাহী": "Rajshahi",
    "খুলনা": "Khulna", "রংপুর": "Rangpur", "বরিশাল": "Barishal",
    "ময়মনসিংহ": "Mymensingh", "কুমিল্লা": "Cumilla", "দিনাজপুর": "Dinajpur",
    "যশোর": "Jashore", "বগুড়া": "Bogura", "নোয়াখালী": "Noakhali",
    "ফেনী": "Feni", "জামালপুর": "Jamalpur", "কিশোরগঞ্জ": "Kishoreganj",
    "মানিকগঞ্জ": "Manikganj", "মুন্সিগঞ্জ": "Munshiganj", "ফরিদপুর": "Faridpur",
    "গোপালগঞ্জ": "Gopalganj", "মাদারীপুর": "Madaripur", "রাজবাড়ী": "Rajbari",
    "শরীয়তপুর": "Shariatpur", "পটুয়াখালী": "Patuakhali", "পিরোজপুর": "Pirojpur",
    "বরগুনা": "Barguna", "ভোলা": "Bhola", "ঝালকাঠি": "Jhalokati",
    "লক্ষ্মীপুর": "Lakshmipur", "সাতক্ষীরা": "Satkhira", "বাগেরহাট": "Bagerhat",
    "বান্দরবান": "Bandarban", "রাঙ্গামাটি": "Rangamati", "খাগড়াছড়ি": "Khagrachari",
    "ঠাকুরগাঁও": "Thakurgaon", "পঞ্চগড়": "Panchagarh", "লালমনিরহাট": "Lalmonirhat",
    "কুড়িগ্রাম": "Kurigram", "গাইবান্ধা": "Gaibandha", "নীলফামারী": "Nilphamari",
    "চাঁপাইনবাবগঞ্জ": "Chapai Nawabganj", "নওগাঁ": "Naogaon", "নড়াইল": "Narail",
    "নারায়ণগঞ্জ": "Narayanganj", "নরসিংদী": "Narsingdi", "নাটোর": "Natore",
    "নেত্রকোনা": "Netrokona", "হবিগঞ্জ": "Habiganj", "মৌলভীবাজার": "Moulvibazar",
    "সুনামগঞ্জ": "Sunamganj", "টাঙ্গাইল": "Tangail", "গাজীপুর": "Gazipur",
    "চাঁদপুর": "Chandpur", "ব্রাহ্মণবাড়িয়া": "Brahmanbaria", "চুয়াডাঙ্গা": "Chuadanga",
    "ঝিনাইদহ": "Jhenaidah", "জয়পুরহাট": "Joypurhat", "কুষ্টিয়া": "Kushtia",
    "মাগুরা": "Magura", "মেহেরপুর": "Meherpur", "শেরপুর": "Sherpur",
    "সিরাজগঞ্জ": "Sirajganj"
}

class BMDWeatherScraper:
    @staticmethod
    def _normalize_district_name(name: str) -> str:
        """Normalize district name for matching (remove spaces, special chars)"""
        return re.sub(r'[\s\'\-]', '', name.lower())

    @staticmethod
    def scrape_forecast(days: int, target_district: str) -> dict:
        url = f"https://www.bamis.gov.bd/en/bmd/wrf/table/all/{days}/"
        print(f"Scraping BMD WRF {days}-day forecast from: {url}")
        
        # 1. Handle Bengali to English conversion
        search_name = target_district.lower().strip()
        if any('\u0980' <= c <= '\u09FF' for c in search_name):
            for bengali, english in BENGALI_TO_ENGLISH_DISTRICTS.items():
                if bengali in search_name or search_name in bengali:
                    search_name = english.lower()
                    print(f"🔄 Translated {target_district} to {english}")
                    break

        try:
            print(f"✅ Input district '{target_district}' assumed to be English, no translation used.")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table')
            if not table: return None
            
            rows = table.find_all('tr')[1:] 
            norm_search = BMDWeatherScraper._normalize_district_name(search_name)
            print(f"DEBUG: Searching for district '{search_name}' -> normalized '{norm_search}'")

            for row in rows:
                cols = row.find_all(['td', 'th'])
                if not cols or len(cols) < 12: continue
                
                db_district = cols[0].get_text(strip=True).lower()
                norm_db = BMDWeatherScraper._normalize_district_name(db_district)
                print(f"DEBUG: Table row district='{db_district}' -> normalized='{norm_db}'")

                if norm_search in norm_db or norm_db in norm_search:
                    print(f"✅ Match found: {db_district}")
                    try:
                        # BAMIS Indices: 1:MinT, 3:MaxT, 5:Hum, 10:Rain
                        t_min = float(re.search(r"[\d\.]+", cols[1].text).group())
                        t_max = float(re.search(r"[\d\.]+", cols[3].text).group())
                        hum   = float(re.search(r"[\d\.]+", cols[5].text).group())
                        rain  = float(re.search(r"[\d\.]+", cols[10].text).group())
                    except (ValueError, AttributeError, IndexError):
                        continue

                    daily_rain = round(rain / days, 1) if days > 0 else 0
                    forecast = []
                    for i in range(days):
                        forecast.append({
                            "date": (datetime.now() + timedelta(days=i+1)).strftime("%Y-%m-%d"),
                            "parameters": {
                                "temperature": {"min": t_min, "max": t_max, "unit": "Celsius"},
                                "precipitation": {"value": daily_rain, "unit": "mm", "probability": min(daily_rain / 10, 1) },
                                "humidity": {"value": hum, "unit": "percent"}
                            }
                        })
                    
                    return {
                        "location": {"area_name": db_district.title()},
                        "forecast": forecast
                    }
            return None
        except Exception as e:
            print(f"❌ Scraper error: {e}")
            return None

def retrieve_weather_forecast(district: str, forecast_days: int, parameters: List[str] = None) -> str:
    data = BMDWeatherScraper.scrape_forecast(forecast_days, district)
    
    if not data:
        return json.dumps({
            "error": f"District '{district}' not found in BAMIS table.",
            "available_districts_hint": "Try English or Bengali script (পাবনা)."
        })
    
    return json.dumps(data)