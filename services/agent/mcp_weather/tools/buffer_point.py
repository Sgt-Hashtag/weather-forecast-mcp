from shapely.geometry import Point
import pyproj
import math

def create_buffer(latitude: float, longitude: float, radius_km: float) -> dict:
    """
    Create geodesic buffer using WGS84 ellipsoid (avoids Shapely degree approximation errors)
    
    Args:
        latitude: Decimal degrees (WGS84)
        longitude: Decimal degrees (WGS84)
        radius_km: Buffer radius in kilometers
    
    Returns:
        GeoJSON Polygon
    """
    # Use pyproj.Geod for accurate geodesic calculations on WGS84 ellipsoid
    geod = pyproj.Geod(ellps="WGS84")
    
    # Generate points every 10 degrees around the circle
    num_points = 36  # 360/10 = 36 points
    boundary = []
    
    for i in range(num_points + 1):  # +1 to close the polygon
        angle = i * (360 / num_points)
        # geod.fwd returns (lon, lat, back_azimuth)
        lon_pt, lat_pt, _ = geod.fwd(
            lons=longitude,
            lats=latitude,
            az=angle,
            dist=radius_km * 1000  # Convert km to meters
        )
        boundary.append([lon_pt, lat_pt])
    
    return {
        "type": "Polygon",
        "coordinates": [boundary]
    }