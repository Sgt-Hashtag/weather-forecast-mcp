import sys
import os
import math
from datetime import datetime

# Add the 'services/agent' directory to the python path
current_dir = os.path.dirname(os.path.abspath(__file__))
agent_dir = os.path.abspath(os.path.join(current_dir, "../services/agent"))
sys.path.insert(0, agent_dir)

from mcp_weather.tools.weather_forecast import BMDWeatherScraper, get_closest_station

def test_matching():
    print("📡 Fetching real station data from BMD...")
    # Get the real data your agent uses
    data = BMDWeatherScraper.scrape_forecast(1)
    stations = data.get("stations", [])
    
    if not stations:
        print("❌ Could not fetch stations. Check internet connection.")
        return

    print(f"✅ Loaded {len(stations)} active stations.\n")

    # Test Cases: Cities that might NOT be in the list by exact name
    test_locations = [
        {"name": "Chittagong City", "lat": 22.3569, "lon": 91.7832}, # Might match 'Ambagan' or 'Patenga'
        {"name": "Cox's Bazar Beach", "lat": 21.4272, "lon": 92.0058},
        {"name": "Sundarbans (Forest)", "lat": 21.9497, "lon": 89.1833}, # Remote area
        {"name": "Dhaka Airport", "lat": 23.8434, "lon": 90.4030}
    ]

    print(f"{'QUERY LOCATION':<25} | {'CLOSEST STATION FOUND':<25} | {'DISTANCE':<10}")
    print("-" * 70)

    for loc in test_locations:
        match = get_closest_station(loc["lat"], loc["lon"], stations)
        
        # Calculate distance for display
        dist = math.sqrt((match['latitude'] - loc['lat'])**2 + (match['longitude'] - loc['lon'])**2) * 111 # Approx km
        
        print(f"{loc['name']:<25} | {match['name']:<25} | {dist:.1f} km")

if __name__ == "__main__":
    test_matching()