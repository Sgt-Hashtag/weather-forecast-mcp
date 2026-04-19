import json
from pathlib import Path

def create_aoi_file(lat, lon, output_path: str = "temp_aoi.geojson", buffer=0.01):
    """
    Creates a small GeoJSON file around a coordinate point, 
    which agribound.delineate() requires as a study_area.
    """
    # Create a small bounding box
    bbox = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon - buffer, lat - buffer],
                    [lon + buffer, lat - buffer],
                    [lon + buffer, lat + buffer],
                    [lon - buffer, lat + buffer],
                    [lon - buffer, lat - buffer]
                ]]
            },
            "properties": {"name": "User Field"}
        }]
    }
    with open(output_path, "w") as f:
        json.dump(bbox, f)
    return output_path