import agribound
import os
from .utils import create_aoi_file

class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project

    def process_field(self, lat, lon):
        aoi_path = create_aoi_file(lat, lon)
        
        # Get the boundaries (Delineation Engine)\
        boundaries_gdf = agribound.delineate(
            study_area=aoi_path,
            source="sentinel2",
            year=2026,
            engine="delineate-anything", 
            gee_project=self.gee_project,
            output_path="temp_boundaries.gpkg"
        )
        
        #Identify the crops (Classification Engine)
        classified_gdf = agribound.delineate(
            study_area="temp_boundaries.gpkg",
            source="sentinel2",
            year=2026,
            engine="ftw", # or prithvi
            gee_project=self.gee_project,
            output_path="final_analysis.gpkg"
        )
        
        return {
            "status": "Success",
            "final_file": "final_analysis.gpkg",
            "field_count": len(classified_gdf),
            "summary": classified_gdf.head(5).to_dict() # check data
        }
