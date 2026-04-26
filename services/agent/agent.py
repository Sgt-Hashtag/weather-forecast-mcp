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
    lat: float | None = Field(
        default=None,
        description="Latitude if the user explicitly provided numeric coordinates (e.g. 'latitude 28.6'). Null if not provided."
    )
    lon: float | None = Field(
        default=None,
        description="Longitude if the user explicitly provided numeric coordinates (e.g. 'longitude 77.2'). Null if not provided."
    )
    user_context: Literal["FARMER", "CITIZEN"] = Field(
        description="Classify as FARMER if query mentions crops, irrigation, agriculture, harvest, etc. Otherwise CITIZEN."
    )
    forecast_days: int = Field(
        description="Number of days requested (1-7). If not explicitly mentioned, default to 5 for FARMER and 3 for CITIZEN."
    )
    task_type: Literal["FORECAST", "FIELD_DELINEATION", "BOTH"] = Field(
        description="Determine if user wants weather forecast, field boundary delineation, or both. Default to FORECAST."
    )

# detailed System Instruction
SYSTEM_INSTRUCTION = """
You are WeatherWise, an AI weather assistant for Bangladesh.

CONTEXT DETECTION:
- FARMER: User mentions "farmer", "crop", "field", "harvest", "irrigation", "soil", "agriculture", "plant", "seed", "paddy", "rice"
- CITIZEN: User mentions "commute", "travel", "outdoor", "event", "clothing", "daily", "umbrella", "road", "traffic"
- DEFAULT: Treat as citizen if unclear

TASK TYPE DETECTION:
- FIELD_DELINEATION: User mentions "field boundary", "delineate", "my land", "farm boundary", "field edges", "plot", "drainage"
- FORECAST: User wants weather information
- BOTH: User wants both

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
        Analyze this query: "{user_query}"

        Extract the intent into JSON matching the schema.
        - Detect the location name (e.g. "Pabna"). If only coordinates are given, use a descriptive name like "Custom Location".
        - If the query contains explicit numeric latitude/longitude values, extract them into lat and lon fields. Otherwise set both to null.
        - Detect if the user is a FARMER or CITIZEN based on intent.
        - Determine the forecast duration (e.g. "next week" = 7 days, "tomorrow" = 1 day).
        - Determine task type: FORECAST, FIELD_DELINEATION, or BOTH based on whether user mentions field boundaries, land delineation, or farm mapping.
        """
        
        try:
            # Structured Output (JSON) matching our Pydantic schema
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
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
            return WeatherIntent(location="dhaka", user_context="CITIZEN", forecast_days=5, task_type="FORECAST")

    async def process_query(self, user_query: str) -> Dict[str, Any]:
        if not self.initialized:
            raise RuntimeError("Agent not initialized")
        
        print(f"@@@@@@@@ Received Query: '{user_query}'")
        
        # AI Intent Analysis
        intent = await self._analyze_query(user_query)
        print(f"Intent extracted: {intent}")
        
        location_name = intent.location
        is_farmer = (intent.user_context == "FARMER")
        forecast_days = max(1, min(7, intent.forecast_days)) 
        radius_km = 15 if is_farmer else 20
        task_type = intent.task_type
        
        print(f"AI Intent Detected:")
        print(f"   - Location: {location_name}")
        print(f"   - Context: {intent.user_context}")
        print(f"   - Days: {forecast_days}")
        print(f"   - Task: {task_type}")
        
        # Use explicit coordinates if provided, otherwise geocode
        if intent.lat is not None and intent.lon is not None:
            location_data = {
                "latitude": intent.lat,
                "longitude": intent.lon,
                "area_name": location_name,
                "district": location_name,
            }
            print(f"Using provided coordinates: ({intent.lat}, {intent.lon})")
        else:
            location_data = await self.mcp.geocode_location(location_name)
            if not location_data:
                raise ValueError(f"Could not geocode location: {location_name}")
            print(f"Geocoded: {location_data['area_name']} ({location_data['latitude']:.4f}, {location_data['longitude']:.4f})")
        district_name = location_data.get("district")
        if not district_name:
            district_name = location_data.get("area_name", location_name)
        
        #Create Buffer
        buffer_geojson = await self.mcp.create_buffer(
            location_data["latitude"],
            location_data["longitude"],
            radius_km
        )
        
        response_data = {
            "buffer": buffer_geojson,
            "display_location": intent.location,
            "forecast": {"location": location_data, "forecast": []}
        }
        
        # Handle field delineation task
        if task_type in ("FIELD_DELINEATION", "BOTH"):
            try:
                print(f"DEBUG: Calling delineate_field_boundaries for lat={location_data['latitude']}, lon={location_data['longitude']}")
                delineation_result = await self.mcp.delineate_field_boundaries(
                    location_data["latitude"],
                    location_data["longitude"]
                )
                response_data["field_delineation"] = delineation_result
                # Also pass fields for map display
                if delineation_result.get("fields_geojson"):
                    response_data["fields"] = delineation_result["fields_geojson"]
                if delineation_result.get("field_count"):
                    response_data["field_count"] = delineation_result["field_count"]
            except Exception as e:
                print(f"⚠️ Field delineation failed: {e}")
                response_data["field_delineation"] = {"error": str(e)}
        
        # Handle weather forecast task
        if task_type in ("FORECAST", "BOTH"):
            print(f"DEBUG: Calling get_weather_forecast with district_name={district_name}, days={forecast_days}")
            forecast = await self.mcp.get_weather_forecast(
                district_name=district_name,
                days=forecast_days,
                params=["temperature", "precipitation", "humidity"]
            )
            print(f"DEBUG: Forecast raw output: {json.dumps(forecast, indent=2)}")
            print(f"DEBUG: Forecast days retrieved: {len(forecast.get('forecast', []))}")
            print(f"========= Forecast retrieved: {len(forecast.get('forecast', []))} days=========")
            
            # Generate Explanation
            explanation = await self._generate_explanation(
                user_query=user_query,
                location=location_data,
                is_farmer=is_farmer,
                forecast=forecast
            )
            print(intent.location)
            response_data["answer"] = explanation
            response_data["forecast"] = {
                "location": forecast.get("location", location_data),
                "forecast": forecast.get("forecast", [])[:forecast_days]
            }
        
        if task_type == "FIELD_DELINEATION" and "answer" not in response_data:
            response_data["answer"] = f"Agricultural field boundaries have been delineated for {location_data['area_name']}. The boundaries are shown on the map."
        
        print(f"DEBUG: Final response_data keys: {list(response_data.keys())}")
        print(f"DEBUG: Has field_delineation: {'field_delineation' in response_data}")
        return response_data
    
    async def _generate_explanation(self, user_query: str, location: Dict, is_farmer: bool, forecast: Dict) -> str:
        # chek for empty forecast
        if not forecast.get("forecast"):
            print("⚠️ Forecast is empty or missing.")
            return self._fallback_explanation(location, is_farmer, forecast)

        forecast_text = []
        days_to_summarize = forecast["forecast"][:3]
        print(f"DEBUG: Preparing explanation for {len(days_to_summarize)} days of forecast")
        
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
            except KeyError as e:
                print(f"⚠️ Missing key in forecast day {i+1}: {e}")
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
        6. Please complete the sentence

        RESPONSE (only the explanation text, no JSON or prefixes):
        """
        print("DEBUG: Prompt sent to Gemini model:")
        print(prompt)
        
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.3,
                    max_output_tokens=1024
                )
            )
            print("✅ Model explanation received:")
            explanation = response.text.strip()
            print(explanation)
            return explanation
        except Exception as e:
            print(f"⚠️ Explanation generation failed: {e}")
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