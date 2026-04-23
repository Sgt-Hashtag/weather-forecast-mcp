import asyncio
import os
import json
import re
from contextlib import AsyncExitStack
from typing import Dict, Any, Union
from unittest import result


from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession

class MCPClientManager:
    def __init__(self):
        self.exit_stack = AsyncExitStack()
        self.sessions: Dict[str, ClientSession] = {}
        self.FALLBACK_LOCATIONS = {
            "dhaka": {"latitude": 23.8103, "longitude": 90.4125, "area_name": "Dhaka", "district": "Dhaka"},
            "chittagong": {"latitude": 22.3569, "longitude": 91.7832, "area_name": "Chittagong", "district": "Chittagong"},
            "chattogram": {"latitude": 22.3569, "longitude": 91.7832, "area_name": "Chattogram", "district": "Chattogram"},
            "rajshahi": {"latitude": 24.3745, "longitude": 88.6042, "area_name": "Rajshahi", "district": "Rajshahi"},
            "bogura": {"latitude": 24.8479, "longitude": 89.3729, "area_name": "Bogura", "district": "Bogura"},
            "bogra": {"latitude": 24.8479, "longitude": 89.3729, "area_name": "Bogra", "district": "Bogra"},
            "khulna": {"latitude": 22.8200, "longitude": 89.5500, "area_name": "Khulna", "district": "Khulna"},
            "barisal": {"latitude": 22.7000, "longitude": 90.3500, "area_name": "Barisal", "district": "Barisal"},
            "sylhet": {"latitude": 24.8918, "longitude": 91.8698, "area_name": "Sylhet", "district": "Sylhet"},
            "rangpur": {"latitude": 25.7439, "longitude": 89.2752, "area_name": "Rangpur", "district": "Rangpur"},
            "mymensingh": {"latitude": 24.9821, "longitude": 91.4018, "area_name": "Mymensingh", "district": "Mymensingh"},
            "comilla": {"latitude": 23.4619, "longitude": 91.1853, "area_name": "Comilla", "district": "Comilla"},
            "savar": {"latitude": 23.8650, "longitude": 90.2616, "area_name": "Savar", "district": "Dhaka"},
        }
    
    async def start_all(self):
        await self._start_mapbox_mcp()
        await self._start_weather_mcp()
        print("✅ All MCP servers connected")

    async def _start_mapbox_mcp(self):
        token = os.getenv("MAPBOX_ACCESS_TOKEN")
        if not token: return
        env = os.environ.copy()
        env["MAPBOX_ACCESS_TOKEN"] = token.strip()
        
        # Use npx -y to avoid prompts
        server_params = StdioServerParameters(command="npx", args=["-y", "@mapbox/mcp-server"], env=env)
        try:
            read, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions["mapbox"] = session
            print("✓ Mapbox MCP connected")
        except Exception as e:
            print(f"⚠️ Mapbox MCP failed: {e}")

    async def _start_weather_mcp(self):
        server_params = StdioServerParameters(command="python", args=["-m", "mcp_weather.main"], env=os.environ.copy())
        read, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
        session = await self.exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.sessions["weather"] = session
        print("✓ Weather MCP connected")

    async def geocode_location(self, query: str) -> Dict[str, Any]:
        """Geocode location and extract district name from Mapbox response"""
        print(f"🔍 Geocoding query: {query}")
        
        query_lower = query.lower().strip()
        
        if query_lower in self.FALLBACK_LOCATIONS:
            print(f"Using fallback location for: {query_lower}")
            return self.FALLBACK_LOCATIONS[query_lower]
        
        if "mapbox" in self.sessions:
            try:
                tools = await self.sessions["mapbox"].list_tools()
                print("🧰 Available Mapbox MCP tools:", [t.name for t in tools.tools])
                target_tool = "search_and_geocode_tool"
                
                result = await self.sessions["mapbox"].call_tool(
                    "search_and_geocode_tool",
                    arguments={
                        "q": query,
                        "country": ["BD"],
                        "limit": 1
                    }
                )

                if result.isError:
                    raise RuntimeError(result.content[0].text)

                # ✅ USE structuredContent
                data = result.structuredContent
                if not data:
                    raise ValueError("No structured content from Mapbox MCP")

                features = data.get("features", [])
                if not features:
                    raise ValueError("No geocoding features")

                feature = features[0]

                coords = feature["geometry"]["coordinates"]
                longitude, latitude = coords
                print(feature)

                district_name = self._extract_district_from_mapbox(feature)
                
                
                props = feature.get("properties", {})

                area_name = (
                    props.get("name_preferred")
                    or props.get("name")
                    or query
                )
                if district_name == "Unknown":
                    district_name = area_name

                return {
                    "latitude": latitude,
                    "longitude": longitude,
                    "area_name": area_name,
                    "district": district_name
                }
                
            except Exception as e:
                print(f"⚠️ Mapbox lookup failed: {e}")


    def _extract_district_from_mapbox(self, feature: dict) -> str:
        """
        Extract district name from Mapbox v6 response
        """
        try:
            properties = feature.get("properties", {})
            context = properties.get("context", {})

            district_info = context.get("district")
            if isinstance(district_info, dict):
                district_name = district_info.get("name")
                if district_name:
                    print(f"📍 Extracted district from Mapbox: '{district_name}'")
                    return district_name

            # Fallback 1: feature itself is a district
            if properties.get("feature_type") == "district":
                return properties.get("name_preferred") or properties.get("name", "Unknown")

            # Fallback 2: use full_address
            full_address = properties.get("full_address", "")
            if full_address:
                return full_address.split(",")[0].strip()

            return "Unknown"

        except Exception as e:
            print(f"⚠️ Failed to extract district: {e}")
            return "Unknown"

    async def get_weather_forecast(self, district_name: str, days: int, params: list) -> Dict:
        """Harmonized to match agent.py keyword arguments"""
        try:
            print(f"📡 Requesting BAMIS forecast for district: {district_name}")
            result = await self.sessions["weather"].call_tool(
                "retrieve_weather_forecast", 
                arguments={
                    "district_name": district_name, # Matches tool in main.py
                    "forecast_days": days, 
                    "parameters": params
                }
            )
            
            # 🛠️ DEBUG: Check for empty tool response
            raw_response = result.content[0].text
            if not raw_response or raw_response.strip() == "":
                print("❌ ERROR: Weather tool returned an empty string.")
                return {"location": {"area_name": "Unavailable"}, "forecast": []}

            return json.loads(raw_response)
            
        except Exception as e:
            print(f"❌ Weather MCP call failed: {e}")
            return {"location": {"area_name": "Error"}, "forecast": []}

    async def create_buffer(self, lat: float, lon: float, radius_km: float) -> Dict:
        result = await self.sessions["weather"].call_tool("buffer_point", arguments={"latitude": lat, "longitude": lon, "radius_km": radius_km})
        if hasattr(result, 'content') and result.content:
            return json.loads(result.content[0].text)
        return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}}

    async def delineate_field_boundaries(self, lat: float, lon: float) -> Dict:
        """Delineate agricultural field boundaries using agribound"""
        try:
            print(f"DEBUG: mcp_client.delineate_field_boundaries called with ({lat}, {lon})")
            print(f"DEBUG: Available sessions: {list(self.sessions.keys())}")
            if "weather" not in self.sessions:
                print("ERROR: Weather MCP session not available!")
                return {"error": "Weather MCP not connected"}
            
            result = await self.sessions["weather"].call_tool(
                "delineate_field_boundaries",
                arguments={"latitude": lat, "longitude": lon}
            )
            print(f"DEBUG: delineate_field_boundaries raw result type: {type(result)}")
            if hasattr(result, 'content') and result.content:
                text_content = result.content[0].text
                print(f"DEBUG: text content length: {len(text_content)}")
                return json.loads(text_content)
            print("WARNING: No content in delineation result")
            return {"error": "No result from tool"}
        except Exception as e:
            print(f"❌ Field delineation MCP call failed: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    async def shutdown(self):
        await self.exit_stack.aclose()
        self.sessions.clear()