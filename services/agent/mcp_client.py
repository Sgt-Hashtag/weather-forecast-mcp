import asyncio
import os
import json
import re
from contextlib import AsyncExitStack
from typing import Dict, Any

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession

class MCPClientManager:
    def __init__(self):
        self.exit_stack = AsyncExitStack()
        self.sessions: Dict[str, ClientSession] = {}
        # Fallback locations (Safety net)
        self.FALLBACK_LOCATIONS = {
            "dhaka": {"latitude": 23.8103, "longitude": 90.4125, "area_name": "Dhaka, Bangladesh"},
            "chittagong": {"latitude": 22.3569, "longitude": 91.7832, "area_name": "Chittagong, Bangladesh"}
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

    async def geocode_location(self, location_name: str) -> Dict[str, Any]:
        print(f"🔍 Geocoding: '{location_name}'")
        if "mapbox" in self.sessions:
            try:
                tools_resp = await self.sessions["mapbox"].list_tools()
                tool_names = [t.name for t in tools_resp.tools]
                
                # Priority: search_and_geocode_tool -> forward_geocode_tool
                target_tool = None
                for t in ["search_and_geocode_tool", "forward_geocode_tool", "mapbox_address_search"]:
                    if t in tool_names:
                        target_tool = t
                        break
                
                if not target_tool:
                    target_tool = next((n for n in tool_names if "search" in n and "category" not in n), None)
                
                if not target_tool: raise ValueError("No geocode tool found")

                # Correct argument mapping (q vs query)
                arg_name = "q" if target_tool == "search_and_geocode_tool" else "query"
                print(f"📞 Calling Mapbox tool: {target_tool}")
                
                result = await self.sessions["mapbox"].call_tool(target_tool, arguments={arg_name: location_name})
                raw_text = result.content[0].text
                
                # ✅ ROBUST PARSING STRATEGY
                try:
                    # 1. Try Standard JSON (This will fail for Bengali text output)
                    data = json.loads(raw_text)
                    feature = None
                    if "features" in data and data["features"]: feature = data["features"][0]
                    elif "type" in data and data["type"] == "Feature": feature = data
                    
                    if feature:
                        return {
                            "latitude": feature["center"][1],
                            "longitude": feature["center"][0],
                            "area_name": feature.get("place_name", location_name)
                        }
                except json.JSONDecodeError:
                    # 2. JSON Failed -> Try Regex Parsing
                    # This ignores the Bengali text and grabs the numbers: "Coordinates: 24.007..., 89.238..."
                    match = re.search(r"Coordinates:\s*([-\d\.]+),\s*([-\d\.]+)", raw_text)
                    if match:
                        lat = float(match.group(1))
                        lon = float(match.group(2))
                        print(f"📍 Mapbox (Text Parsed): {lat}, {lon}")
                        
                        # We use the clean input name 'location_name' ("Pabna") 
                        # so we don't return Bengali text to the user.
                        return {"latitude": lat, "longitude": lon, "area_name": location_name}
                    else:
                        print(f"🚨 FAILED to parse Mapbox output. Raw: {raw_text[:100]}...")

            except Exception as e:
                print(f"⚠️ Mapbox lookup failed: {e}")

        # Fallback
        return self.FALLBACK_LOCATIONS.get("dhaka")

    async def create_buffer(self, lat: float, lon: float, radius_km: float) -> Dict:
        result = await self.sessions["weather"].call_tool("buffer_point", arguments={"latitude": lat, "longitude": lon, "radius_km": radius_km})
        # Handle cases where result is wrapped in a list or content object
        if hasattr(result, 'content') and result.content:
            text = result.content[0].text
            return json.loads(text)
        return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}}

    async def get_weather_forecast(self, bbox: Dict, days: int, params: list) -> Dict:
        try:
            result = await self.sessions["weather"].call_tool("retrieve_weather_forecast", arguments={"bbox": bbox, "forecast_days": days, "parameters": params})
            data = json.loads(result.content[0].text)
            return data if "forecast" in data else {"location": {"area_name": "Error"}, "forecast": []}
        except Exception as e:
            print(f"❌ Weather forecast failed: {e}")
            return {"location": {"area_name": "Error"}, "forecast": []}

    async def shutdown(self):
        await self.exit_stack.aclose()
        self.sessions.clear()