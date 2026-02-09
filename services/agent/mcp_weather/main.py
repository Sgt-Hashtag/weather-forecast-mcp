from mcp.server.fastmcp import FastMCP
import json
from typing import List, Dict, Any

from mcp_weather.tools.buffer_point import create_buffer
from mcp_weather.tools.weather_forecast import retrieve_weather_forecast as fetch_forecast_logic

mcp = FastMCP("Weather Service")

@mcp.tool()
def buffer_point(latitude: float, longitude: float, radius_km: float) -> str:
    """
    Create a geodesic buffer zone around coordinates (WGS84 ellipsoid).
    Returns a GeoJSON string.
    """
    try:
        geojson = create_buffer(latitude, longitude, radius_km)
        return geojson
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def retrieve_weather_forecast(bbox: Dict[str, Any], forecast_days: int, parameters: List[str]) -> str:
    """
    Retrieve weather forecast for bounding box from BMD data.
    Returns a JSON string with forecast data.
    """
    try:
        return fetch_forecast_logic(bbox, forecast_days, parameters)
    except Exception as e:
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    mcp.run()