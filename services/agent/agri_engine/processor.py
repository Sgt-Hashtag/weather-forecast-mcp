import agribound
import os
import json
import random
import ee
from .utils import create_aoi_file

# Initialize GEE if credentials provided
def init_gee():
    """Initialize Google Earth Engine"""
    import os
    gee_project = os.getenv("GEE_PROJECT_ID", "").strip()
    
    print(f"GEE init with project: '{gee_project}'")
    
    # Try using project ID - works if EE is enabled for the project
    if gee_project and gee_project not in ('', 'your-project-id', 'your-gee-project-id'):
        try:
            # First try initializing with project
            ee.Initialize(project=gee_project)
            print(f"GEE initialized with project: {gee_project}")
            return True
        except Exception as e:
            print(f"GEE init with project failed: {e}")
            # Try without project name - might use Application Default Credentials
            try:
                ee.Initialize()
                print("GEE initialized (ADC)")
                return True
            except Exception as e2:
                print(f"GEE init failed: {e2}")
    
    print("No GEE credentials - using simulated field boundaries")
    return False

# Try to initialize GEE on module load
GEE_INITIALIZED = init_gee()

class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project or os.getenv("GEE_PROJECT_ID")
        self.sam_path = os.getenv("SAM_CHECKPOINT", "/app/models/sam_vit_h_4b8939.pth")

    def process_field(self, lat, lon, boundaries_path=None):
        """
        Delineate agricultural field boundaries using agribound with GEE.
        Then classify crops using FTW (Field of the World).
        Only uses GEE if properly authenticated, otherwise falls back to simulated.
        """
        import os
        aoi_path = create_aoi_file(lat, lon)
        boundaries_out = boundaries_path or "/tmp/field_boundaries.geojson"
        classified_out = "/tmp/classified_fields.geojson"
        gee_project = self.gee_project or os.getenv("GEE_PROJECT_ID")
        
        # Try AGRIBOUND with GEE if credentialed
        use_gee = False
        
        # Check for credentials file first
        creds_paths = ["/app/secrets/credentials.json", "/app/secrets/service-account.json"]
        creds_found = None
        for cp in creds_paths:
            if os.path.exists(cp):
                creds_found = cp
                break
        
        if creds_found or (gee_project and gee_project not in ('', 'your-project-id')):
            try:
                print(f"Attempting GEE delineation for {lat}, {lon}")
                
                # Try initializing if not already done
                try:
                    import ee
                    if creds_found:
                        credentials = ee.ServiceAccountCredentials.from_service_account_file(creds_found)
                        ee.Initialize(credentials=credentials)
                    elif gee_project:
                        ee.Initialize(project=gee_project)
                    print(f"GEE initialized")
                except Exception as init_err:
                    print(f"GEE already initialized or: {init_err}")
                
                # Step 1: Delineate field boundaries using delineate-anything
                agribound.delineate(
                    study_area=aoi_path,
                    source="sentinel2",
                    year=2026,
                    engine="delineate-anything", 
                    engine_params={"checkpoint": self.sam_path},
                    gee_project=gee_project,
                    output_path=boundaries_out
                )
                print("Step 1: Field boundaries delineated!")
                use_gee = True
                
                # Step 2: Classify crops using FTW
                try:
                    print("Step 2: Running FTW crop classification...")
                    agribound.delineate(
                        study_area=boundaries_out,
                        source="sentinel2",
                        year=2026,
                        engine="ftw",
                        gee_project=gee_project,
                        output_path=classified_out
                    )
                    print("Step 2: Crop classification complete!")
                    
                    # Use the classified results
                    import geopandas as gpd
                    gdf = gpd.read_file(classified_out)
                    
                    # Extract crop types from classification
                    crop_info = []
                    for idx, row in gdf.iterrows():
                        props = row.to_dict()
                        crop_type = props.get('crop_type', props.get('predicted_crop', 'Unknown'))
                        confidence = props.get('confidence', props.get('score', 0.8))
                        crop_info.append({
                            'crop': crop_type,
                            'confidence': confidence
                        })
                    
                    return {
                        "status": "Success",
                        "bounds_file": boundaries_out,
                        "field_count": len(gdf),
                        "bounds_geojson": json.loads(gdf.to_json()),
                        "source": "gee",
                        "crop_classification": crop_info
                    }
                except Exception as ftw_err:
                    print(f"FTW classification failed: {ftw_err}")
                    # Still return the boundary results even if FTW fails
                    import geopandas as gpd
                    gdf = gpd.read_file(boundaries_out)
                    return {
                        "status": "Success",
                        "bounds_file": boundaries_out,
                        "field_count": len(gdf),
                        "bounds_geojson": json.loads(gdf.to_json()),
                        "source": "gee",
                        "crop_classification": None,
                        "note": "FTW classification unavailable"
                    }
                    
            except Exception as e:
                print(f"GEE agribound failed: {e}")
                print("Falling back to simulated field boundaries")
        
        if not use_gee:
            # Fallback: generate realistic field polygons (simulated)
            print("Using SIMULATED field boundaries (GEE not authenticated)")
            fields_geojson = self._generate_field_polygons(lat, lon)
            
            with open(boundaries_out, 'w') as f:
                json.dump(fields_geojson, f)
            
            return {
                "status": "Success",
                "bounds_file": boundaries_out,
                "field_count": len(fields_geojson.get("features", [])),
                "bounds_geojson": fields_geojson,
                "source": "simulated"
            }
    
    def _generate_field_polygons(self, lat: float, lon: float, num_fields: int = None) -> dict:
        """
        Generate realistic agricultural field polygons.
        This simulates what agribound's SAM model would detect.
        """
        # Use consistent random based on location hash
        seed = int(lat * 10000 + lon * 10000)
        random.seed(seed)
        
        # Random number of fields (3-8)
        num_fields = num_fields or random.randint(3, 8)
        
        features = []
        base_lat, base_lon = lat, lon
        
        # Generate field polygons spread around the center point
        for i in range(num_fields):
            # Random offset from center (roughly 100-500m)
            offset_lat = (random.random() - 0.5) * 0.004
            offset_lon = (random.random() - 0.5) * 0.004
            
            center_lat = base_lat + offset_lat
            center_lon = base_lon + offset_lon
            
            # Field size varies (roughly 0.5-2 hectares)
            field_size = 0.002 + random.random() * 0.003
            
            # Generate rectangular-ish polygon (common for agricultural fields)
            points = [
                [center_lon - field_size, center_lat - field_size * 0.6],
                [center_lon + field_size, center_lat - field_size * 0.6],
                [center_lon + field_size, center_lat + field_size * 0.6],
                [center_lon - field_size, center_lat + field_size * 0.6],
                [center_lon - field_size, center_lat - field_size * 0.6]
            ]
            
            # Random crop type
            crops = ["Rice", "Wheat", "Maize", "Potato", "Vegetables", "Pulses"]
            crop = random.choice(crops)
            
            features.append({
                "type": "Feature",
                "properties": {
                    "field_id": f"field_{i+1}",
                    "crop_type": crop,
                    "area_ha": round(field_size * field_size * 10000 * 0.8, 2),
                    "confidence": round(0.7 + random.random() * 0.25, 2)
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [points]
                }
            })
        
        return {
            "type": "FeatureCollection",
            "features": features
        }