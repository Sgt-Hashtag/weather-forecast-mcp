import agribound
from .utils import create_aoi_file
import os

class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project

    def process_field(self, lat, lon):
        aoi_path = create_aoi_file(lat, lon)
        output_path = "field_results.gpkg"
        
        gdf = agribound.delineate(
            study_area=aoi_path,
            source="sentinel2",
            year=2026,
            engine="delineate-anything", # SAM engine
            output_path=output_path,
            gee_project=self.gee_project,
            composite_method="median",
            min_area=100
        )
        
        return {
            "status": "Success",
            "boundaries_file": output_path,
            "field_count": len(gdf)
        }