import openeo
import pandas as pd
import matplotlib.pyplot as plt
import datetime
from datetime import timedelta
import os
import numpy as np
import rasterio
from pathlib import Path
import traceback

# ====================== CONFIG ======================
BASE_DIR = Path(__file__).resolve().parent

# ZOOMED IN: ~2km x 2km area in Dhaka for better segmentation detail
FARM_GEOJSON = {  
    "type": "Polygon",
    "coordinates": [
        [
            [90.3785, 23.7795],
            [90.4015, 23.7795],
            [90.4015, 23.8005],
            [90.3785, 23.8005],
            [90.3785, 23.7795]
        ]
    ]
}

LAST_RUN_FILE = BASE_DIR / "last_processed.txt"
NDVI_TIF = BASE_DIR / "ndvi_map.tif"
VISUALIZATION_PNG = BASE_DIR / "farm_visualization.png"

# Sentinel-2 SCL classes to MASK OUT (Invalid)
# 0: No data, 1: Saturated, 2: Dark, 3: Shadow, 7: Unclassified, 
# 8: Med Cloud, 9: High Cloud, 10: Cirrus, 11: Snow/Ice
SCL_INVALID_CLASSES = [0, 1, 2, 3, 7, 8, 9, 10, 11]
# ===================================================

def connect_openeo():
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    con.authenticate_oidc()
    return con

def get_latest_ndvi(con, farm_geojson):
    print("Fetching Sentinel-2 data for Dhaka (Zoomed)...")
    
    end_date = datetime.date.today()
    start_date = end_date - timedelta(days=30)
    temporal_extent = [start_date.isoformat(), end_date.isoformat()]

    # 1. Load Collection
    s2 = con.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=farm_geojson,
        temporal_extent=temporal_extent,
        bands=["B04", "B08", "SCL"],
        max_cloud_cover=50 
    )
    
    # 2. Robust Cloud Masking
    scl = s2.band("SCL")
    # Start with first invalid class
    invalid_mask = scl == SCL_INVALID_CLASSES[0]
    # OR with the rest
    for invalid_class in SCL_INVALID_CLASSES[1:]:
        invalid_mask = invalid_mask | (scl == invalid_class)
    
    # Mask the NDVI calculation directly
    red = s2.band("B04")
    nir = s2.band("B08")
    ndvi = (nir - red) / (nir + red)
    ndvi_clean = ndvi.mask(invalid_mask)
    
    # 3. Reduce time dimension (Median) to get one clean map
    print("Calculating monthly median NDVI...")
    ndvi_reduced = ndvi_clean.reduce_dimension(dimension="t", reducer="median")

    # 4. Get Stats (Mean NDVI)
    print("Extracting statistics...")
    stats_json = ndvi_clean.aggregate_spatial(geometries=farm_geojson, reducer="mean").execute()
    df = pd.json_normalize(stats_json)
    
    # Deep extraction to handle nested lists/objects from OpenEO
    possible_cols = ['mean', 'value', 'result', '0']
    data_col = next((col for col in possible_cols if col in df.columns), df.columns[-1])
    
    # Get the last valid entry (most recent)
    raw_val = df[data_col].dropna().iloc[-1] if not df[data_col].dropna().empty else 0

    def deep_get_float(val):
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, (list, tuple)):
            return deep_get_float(val[0]) 
        return float(val)

    ndvi_value = deep_get_float(raw_val)
    print(f"Mean NDVI: {ndvi_value:.3f}")
    
    # 5. Download Raster Map
    print("Downloading NDVI Map...")
    raster_job = ndvi_reduced.save_result(format="GTiff")
    job_raster = raster_job.create_job(title="Dhaka NDVI Map")
    
    job_raster.start_and_wait() 
    
    results = job_raster.get_results()
    results.download_file(str(NDVI_TIF))
    
    return ndvi_value, str(NDVI_TIF)

def classify_and_visualize(ndvi_value, ndvi_map_path):
    print("Generating Visualization...")
    
    if not os.path.exists(ndvi_map_path):
        raise FileNotFoundError(f"Map file {ndvi_map_path} not found.")

    with rasterio.open(ndvi_map_path) as src:
        ndvi_data = src.read(1)
        # Mask NoData and Invalid Values
        ndvi_data = np.ma.masked_where(ndvi_data <= 0, ndvi_data)
        ndvi_data = np.ma.masked_invalid(ndvi_data)
        
        # Create a vegetation-only mask for clearer health view
        ndvi_veg_only = np.ma.masked_where(ndvi_data < 0.2, ndvi_data)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    
    # --- Left Plot: Vegetation Health (Filtered) ---
    im1 = axes[0].imshow(ndvi_veg_only, cmap='Greens', vmin=0.2, vmax=0.9)
    axes[0].set_title(f"Vegetation Health (NDVI > 0.2)\nAvg: {ndvi_value:.2f}", fontsize=12)
    axes[0].axis('off')
    plt.colorbar(im1, ax=axes[0], label="NDVI Value")

    # --- Right Plot: Full Context (Urban + Veg) ---
    im2 = axes[1].imshow(ndvi_data, cmap='RdYlGn', vmin=0, vmax=1)
    axes[1].set_title("Full Scene NDVI Context", fontsize=12)
    axes[1].axis('off')
    plt.colorbar(im2, ax=axes[1], label="NDVI Value")

    plt.suptitle(f"Dhaka Farm Analysis - {datetime.date.today()}", fontsize=14, fontweight='bold')
    plt.tight_layout()
    
    # Save to file (works on servers)
    plt.savefig(VISUALIZATION_PNG, dpi=150, bbox_inches='tight')
    print(f"✅ Visualization saved to: {VISUALIZATION_PNG}")
    
    # Show window (works on local machines)
    try:
        plt.show()
    except Exception:
        pass # Ignore if no display is available
    finally:
        plt.close(fig)

def run_pipeline_visual():
    today = datetime.date.today().isoformat()
    
    # Cache Check
    if Path(LAST_RUN_FILE).exists() and Path(LAST_RUN_FILE).read_text().strip() == today:
        print("Data already fetched today. Using cached files...")
        if NDVI_TIF.exists():
            # Recalculate mean from cached file for consistency
            with rasterio.open(NDVI_TIF) as src:
                data = src.read(1)
                data = data[data > 0]
                mean_ndvi = float(np.mean(data)) if data.size > 0 else 0.0
            classify_and_visualize(mean_ndvi, str(NDVI_TIF))
        else:
            print("Cache found but file missing. Re-running...")
            Path(LAST_RUN_FILE).unlink()
            run_pipeline_visual()
        return

    try:
        con = connect_openeo()
        ndvi_value, ndvi_map = get_latest_ndvi(con, FARM_GEOJSON)
        
        classify_and_visualize(ndvi_value, ndvi_map)
        
        # Update Cache
        Path(LAST_RUN_FILE).write_text(today)
        print("✅ Pipeline Complete!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    run_pipeline_visual()
