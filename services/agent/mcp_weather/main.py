from mcp.server.fastmcp import FastMCP
import json
from typing import List, Dict, Any

from mcp_weather.tools.buffer_point import create_buffer
from mcp_weather.tools.weather_forecast import retrieve_weather_forecast as fetch_forecast_logic
from agri_engine.processor import AgriProcessor
from agri_engine.utils import create_aoi_file

mcp = FastMCP("Weather Service")
processor = AgriProcessor()

def create_buffer_from_coords(latitude: float, longitude: float, radius_km: float) -> str:
    """
    Create a geodesic buffer zone around coordinates (WGS84 ellipsoid).
    Returns a GeoJSON string.
    """
    try:
        geojson = create_buffer(latitude, longitude, radius_km)
        return json.dumps(geojson)
    except Exception as e:
        return json.dumps({"error": str(e)})

def delineate_agricultural_land(latitude: float, longitude: float) -> str:
    """
    Delineate agricultural field boundaries using agribound.
    Uses Segment Anything Model (SAM) for field boundary detection.
    Returns GeoJSON with field polygons.
    """
    try:
        result = processor.process_field(
            lat=latitude, 
            lon=longitude,
            boundaries_path="/tmp/field_boundaries.geojson"
        )
        
        source = result.get("source", "unknown")
        msg = "Agricultural land delineated via satellite imagery."
        
        return json.dumps({
            "status": "success",
            "message": msg,
            "source": source,
            "field_count": result.get("field_count", 0),
            "fields_geojson": result.get("bounds_geojson", {})
        })
    except Exception as e:
        return json.dumps({"error": str(e), "status": "failed"})

@mcp.tool()
def buffer_point(latitude: float, longitude: float, radius_km: float) -> str:
    return create_buffer_from_coords(latitude, longitude, radius_km)

@mcp.tool()
def delineate_field_boundaries(latitude: float, longitude: float) -> str:
    """
    Delineate agricultural field boundaries at a given location.
    Uses agribound with SAM for instance segmentation of agricultural fields.
    """
    return delineate_agricultural_land(latitude, longitude)

@mcp.tool()
def retrieve_weather_forecast(district_name: str, forecast_days: int, parameters: List[str]) -> str:
    """
    Retrieve weather forecast for bounding box from BMD data.
    Returns a JSON string with forecast data.
    """
    try:
        return fetch_forecast_logic(district_name, forecast_days, parameters)
    except Exception as e:
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    mcp.run()
