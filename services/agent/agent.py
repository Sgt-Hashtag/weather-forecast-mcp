import os
import json
import asyncio
from typing import Dict, Any, Literal

# NEW SDK IMPORTS
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from mcp_client import MCPClientManager

# Pydantic model for intent from query
class WeatherIntent(BaseModel):
    location: str = Field(
        description="The specific city or region name in Bangladesh. Default to 'Dhaka' if unclear."
    )
    user_context: Literal["FARMER", "CITIZEN"] = Field(
        description="Classify as FARMER if query mentions crops, irrigation, agriculture, harvest, etc. Otherwise CITIZEN."
    )
    forecast_days: int = Field(
        description="Number of days requested (1-7). If not explicitly mentioned, default to 5 for FARMER and 3 for CITIZEN."
    )

# detailed System Instruction
SYSTEM_INSTRUCTION = """
You are WeatherWise, an AI weather assistant for Bangladesh.

CONTEXT DETECTION:
- FARMER: User mentions "farmer", "crop", "field", "harvest", "irrigation", "soil", "agriculture", "plant", "seed", "paddy", "rice"
- CITIZEN: User mentions "commute", "travel", "outdoor", "event", "clothing", "daily", "umbrella", "road", "traffic"
- DEFAULT: Treat as citizen if unclear

RULES:
1. ALWAYS use the provided tools - never make up weather data.
2. Generate natural language explanation connecting weather to user needs:
    - Farmers: Focus on crop health, irrigation needs, fungal risk.
    - Citizens: Focus on commute, comfort, outdoor activities.
3. Keep explanation 2-3 sentences minimum, mention location name.
4. Use metric units (°C, mm).
"""

class WeatherAgent:
    def __init__(self):
        self.client = None
        self.mcp = MCPClientManager()
        self.initialized = False
    
    async def initialize(self, api_key: str):
        """Initialize Gemini Client + MCP subprocesses"""
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required")
        
        self.client = genai.Client(api_key=api_key)
        await self.mcp.start_all()
        self.initialized = True
    
    async def shutdown(self):
        """Graceful shutdown"""
        await self.mcp.shutdown()
        self.initialized = False

    async def _analyze_query(self, user_query: str) -> WeatherIntent:
        """
        Uses Gemini to extract structured intent (Location, Context, Days) 
        directly into a Pydantic model. 
        """
        prompt = f"""
        Analyze this weather query: "{user_query}"
        
        Extract the intent into JSON matching the schema.
        - Detect the location (e.g. "Pabna").
        - Detect if the user is a FARMER or CITIZEN based on intent.
        - Determine the forecast duration (e.g. "next week" = 7 days, "tomorrow" = 1 day).
        """
        
        try:
            # Structured Output (JSON) matching our Pydantic schema
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash", 
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=WeatherIntent, 
                    temperature=0.0 # Force deterministic output
                )
            )
            return WeatherIntent.model_validate_json(response.text)
            
        except Exception as e:
            print(f"!!!!!!!!!!!Intent extraction failed: {e}. Using defaults.")
            return WeatherIntent(location="Dhaka", user_context="CITIZEN", forecast_days=3)

    async def process_query(self, user_query: str) -> Dict[str, Any]:
        if not self.initialized:
            raise RuntimeError("Agent not initialized")
        
        print(f"@@@@@@@@ Received Query: '{user_query}'")
        
        # AI Intent Analysis
        intent = await self._analyze_query(user_query)
        
        location_name = intent.location
        is_farmer = (intent.user_context == "FARMER")
        forecast_days = max(1, min(7, intent.forecast_days)) 
        radius_km = 15 if is_farmer else 20
        
        print(f"AI Intent Detected:")
        print(f"   - Location: {location_name}")
        print(f"   - Context: {intent.user_context}")
        print(f"   - Days: {forecast_days}")
        
        # Geocode 
        location_data = await self.mcp.geocode_location(location_name)
        print(f"Geocoded: {location_data['area_name']} ({location_data['latitude']:.4f}, {location_data['longitude']:.4f})")
        
        #Create Buffer
        buffer_geojson = await self.mcp.create_buffer(
            location_data["latitude"],
            location_data["longitude"],
            radius_km
        )
        
        # Get Weather Forecast 
        bbox = self._extract_bbox(buffer_geojson)
        forecast = await self.mcp.get_weather_forecast(
            bbox=bbox,
            days=forecast_days,
            params=["temperature", "precipitation", "humidity"]
        )
        print(f"========= Forecast retrieved: {len(forecast.get('forecast', []))} days=========")
        
        # Generate Explanation (Using RESTORED prompts)
        explanation = await self._generate_explanation(
            user_query=user_query,
            location=location_data,
            is_farmer=is_farmer,
            forecast=forecast
        )
        
        return {
            "answer": explanation,
            "buffer": buffer_geojson,
            "forecast": {
                "location": location_data,
                "forecast": forecast.get("forecast", [])[:forecast_days]
            }
        }
    
    async def _generate_explanation(self, user_query: str, location: Dict, is_farmer: bool, forecast: Dict) -> str:
        # chek for empty forecast
        if not forecast.get("forecast"):
            return self._fallback_explanation(location, is_farmer, forecast)

        forecast_text = []
        days_to_summarize = forecast["forecast"][:3]
        
        for i, day in enumerate(days_to_summarize):
            try:
                p = day["parameters"]
                date = day.get("date", f"Day {i+1}")
                forecast_text.append(
                    f"Day {i+1} ({date}): "
                    f"{p['temperature']['min']}-{p['temperature']['max']}°C, "
                    f"Rain: {p['precipitation']['value']}mm "
                    f"({p['precipitation']['probability']*100:.0f}% chance), "
                    f"Humidity: {p['humidity']['value']}%"
                )
            except KeyError:
                continue
        
        # detailed Generation Prompt
        prompt = f"""
User Query: {user_query}
Location: {location['area_name']} ({location['latitude']:.4f}°N, {location['longitude']:.4f}°E)
Context: {'FARMER (agricultural)' if is_farmer else 'CITIZEN (daily life)'}
Buffer Radius: {15 if is_farmer else 20}km
Forecast (next 3 days):
{chr(10).join(forecast_text)}

Generate a natural language explanation that:
1. Connects weather metrics to user's needs ({'crop health and irrigation' if is_farmer else 'daily commute and comfort'})
2. Gives actionable advice (not just numbers)
3. Is 2-3 sentences minimum
4. Uses metric units (mm for rain, °C for temp)
5. Mentions location name naturally

RESPONSE (only the explanation text, no JSON or prefixes):
"""
        
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.3,
                    max_output_tokens=300
                )
            )
            return response.text.strip()
        except Exception:
            return self._fallback_explanation(location, is_farmer, forecast)

    def _fallback_explanation(self, location: Dict, is_farmer: bool, forecast: Dict) -> str:
        """Robust fallback if Gemini fails"""
        loc_name = location.get("area_name", "Unknown Location")
        
        if not forecast.get("forecast") or len(forecast["forecast"]) == 0:
            return f"Weather data currently unavailable for {loc_name}. Please try again later."
        
        try:
            day1 = forecast["forecast"][0]
            rain = day1["parameters"]["precipitation"]["value"]
            temp_max = day1["parameters"]["temperature"]["max"]
            
            if is_farmer:
                advice = "irrigation recommended" if rain < 5 else "monitor fields for drainage"
                return f"For farmers near {loc_name}: {rain}mm rain expected. {advice}. Max temp {temp_max}°C."
            else:
                advice = "Carry an umbrella" if rain > 0 else "Good day for outdoor activities"
                return f"Forecast for {loc_name}: {rain}mm rain. {advice}. Max temp {temp_max}°C."
        except Exception:
            return f"Forecast available for {loc_name}, but I couldn't generate a summary."

    def _extract_bbox(self, geojson: Dict) -> Dict:
        coords = geojson["coordinates"][0]
        lons = [pt[0] for pt in coords]
        lats = [pt[1] for pt in coords]
        return {"min_lon": min(lons), "min_lat": min(lats), "max_lon": max(lons), "max_lat": max(lats)}