import agribound
import os
from .utils import create_aoi_file

class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project
        # Fetch the path from the environment variable we set in Docker
        self.sam_path = os.getenv("SAM_CHECKPOINT", "/app/models/sam_vit_h_4b8939.pth")

    def process_field(self, lat, lon):
        aoi_path = create_aoi_file(lat, lon)
        
        # Delineate boundaries (SAM Engine)
        boundaries_path = "temp_boundaries.gpkg"
        agribound.delineate(
            study_area=aoi_path,
            source="sentinel2",
            year=2026,
            engine="delineate-anything", 
            engine_params={"checkpoint": self.sam_path},
            gee_project=self.gee_project,
            output_path=boundaries_path
        )
        
        #Identify the crops (Classification Engine)
        final_results = agribound.delineate(
            study_area=boundaries_path,
            source="sentinel2",
            year=2026,
            engine="ftw", # or Prithvi
            gee_project=self.gee_project,
            output_path="final_analysis.gpkg"
        )
        
        # Load the final result to return a summary
        # Depending on agribound version, final_results might be a GeoDataFrame or a path
        return {
            "status": "Success",
            "final_file": "final_analysis.gpkg",
            "message": "Field delineation and crop classification complete."
        }