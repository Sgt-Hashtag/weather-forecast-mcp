#!/usr/bin/env python3
"""
Optional: Test script to verify BMD scraping works for different day ranges.
Run manually: python services/agent/scripts/fetch_bmd_data.py
"""
import sys
import os

# Add parent directory to path so we can import mcp_weather
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_weather.tools.weather_forecast import BMDWeatherScraper

def test_scraping():
    """Test scraping for different day ranges"""
    test_days = [1, 3, 5, 7]
    
    print("\n" + "="*60)
    print("BMD WRF FORECAST SCRAPING TEST")
    print("="*60 + "\n")
    
    for days in test_days:
        print(f"{'='*60}")
        print(f"Testing {days}-day forecast scraping...")
        print(f"{'='*60}")
        
        result = BMDWeatherScraper.scrape_forecast(days)
        stations = result.get("stations", [])
        
        if stations:
            print(f"✅ Success! Scraped {len(stations)} stations")
            print(f"   Example: {stations[0]['name']} - {len(stations[0]['forecast'])} days")
            print(f"   First station forecast dates:")
            for day in stations[0]['forecast'][:3]:  # Show first 3 days
                print(f"     - {day['date']}: {day['parameters']['temperature']['min']}-{day['parameters']['temperature']['max']}°C, "
                    f"{day['parameters']['precipitation']['value']}mm rain")
        else:
            print(f"❌ Failed to scrape {days}-day forecast")
        print()

if __name__ == "__main__":
    test_scraping()