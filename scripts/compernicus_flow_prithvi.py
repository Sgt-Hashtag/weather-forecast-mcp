import openeo
import pandas as pd
import matplotlib.pyplot as plt
import datetime
from datetime import timedelta
import os
import glob
import numpy as np
import rasterio
from pathlib import Path
import traceback

# ====================== CONFIG ======================
FARM_GEOJSON = {  
    "type": "Polygon",
    "coordinates": [
        [
            [90.35, 23.75], [90.45, 23.75], 
            [90.45, 23.85], [90.35, 23.85], 
            [90.35, 23.75]
        ]
    ]
}
LAST_RUN_FILE = "last_processed.txt"
# ===================================================

def connect_openeo():
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    con.authenticate_oidc()
    return con

def get_prithvi_stacked_tif(con, farm_geojson):
    """
    Downloads 3 monthly composites (6 bands each) and stacks them into a single 18-band GeoTIFF.
    Optimized for size (uint16 + ZSTD compression + Nodata handling).
    """
    print("Fetching Prithvi-compatible data (3 months, 6 bands)...")
    
    # 1. Date Range
    end_date = datetime.date.today()
    start_date = end_date - timedelta(days=90)
    
    # 2. Load Collection
    s2 = con.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=farm_geojson,
        temporal_extent=[start_date.isoformat(), end_date.isoformat()],
        bands=["B02", "B03", "B04", "B08", "B11", "B12", "SCL"],
        max_cloud_cover=50
    )
    
    # 3. Cloud Masking
    scl = s2.band("SCL")
    mask = (scl == 4) | (scl == 5) | (scl == 6) | (scl == 11) | (scl == 12)
    s2_clean = s2.mask(mask)
    
    # 4. Filter Bands (Remove SCL)
    prithvi_bands = ["B02", "B03", "B04", "B08", "B11", "B12"]
    s2_spectral = s2_clean.filter_bands(prithvi_bands)
    
    # 5. Aggregate Temporal
    print("Creating monthly composites...")
    s2_monthly = s2_spectral.aggregate_temporal_period(
        period="month", 
        reducer="median"
    )
    
    # 6. Download Parts
    print("Downloading monthly parts...")
    job = s2_monthly.save_result(format="GTiff")
    batch_job = job.create_job(title="Prithvi Monthly Parts")
    
    batch_job.start_and_wait()
    
    download_dir = "temp_prithvi_data"
    os.makedirs(download_dir, exist_ok=True)
    batch_job.get_results().download_files(download_dir)
    
    # 7. Stack Locally with MAXIMUM Optimization
    tif_files = sorted(glob.glob(os.path.join(download_dir, "*.tif")))
    
    if not tif_files:
        raise FileNotFoundError("No TIFF files downloaded.")
        
    if len(tif_files) > 3:
        print(f"Found {len(tif_files)} months. Selecting the latest 3...")
        tif_files = tif_files[-3:]
    elif len(tif_files) < 3:
        raise ValueError(f"Only found {len(tif_files)} months. Need 3.")
        
    print(f"Stacking {len(tif_files)} files into optimized 18-band TIFF...")
    
    # Define optimal profile manually to override source settings
    opt_profile = {
        'driver': 'GTiff',
        'dtype': rasterio.uint16,
        'count': 18,
        'compress': 'zstd',      # Better compression than LZW for this data
        'predictor': 2,          # Horizontal differencing (helps with uint16)
        'tiled': True,           # Essential for large rasters
        'blockxsize': 256,
        'blockysize': 256,
        'nodata': 0,             # Explicitly define 0 as NoData
        'interleave': 'band'     # Standard for multi-band
    }
    
    # Get dimensions from first file
    with rasterio.open(tif_files[0]) as src:
        opt_profile['height'] = src.height
        opt_profile['width'] = src.width
        opt_profile['crs'] = src.crs
        opt_profile['transform'] = src.transform

    with rasterio.open("prithvi_input_stacked.tif", "w", **opt_profile) as dst:
        for idx, tif_path in enumerate(tif_files):
            with rasterio.open(tif_path) as src:
                # Read as uint16
                data = src.read().astype(rasterio.uint16)
                
                # Optional: Set background/clouds to 0 if they aren't already
                # This helps compression significantly
                # data[data == src.nodata] = 0 
                
                start_idx = idx * 6 + 1
                end_idx = start_idx + 6
                dst.write(data, indexes=range(start_idx, end_idx))
    
    # Check final size
    final_size_mb = os.path.getsize("prithvi_input_stacked.tif") / (1024 * 1024)
    print(f"✅ Stacked TIFF created: prithvi_input_stacked.tif ({final_size_mb:.2f} MB)")
    return "prithvi_input_stacked.tif"

def extract_ndvi_from_stack(input_tif, output_ndvi_tif="ndvi_map.tif"):
    """
    Extracts NDVI from the LATEST time step (bands 13-18) of the 18-band stack.
    """
    print("Extracting NDVI from latest time step...")
    
    with rasterio.open(input_tif) as src:
        data = src.read() # Shape: (18, H, W)
        profile = src.profile
        
        # The 3rd time step is the last 6 bands (Indices 12-17 in 0-based array)
        # Band Order: B02, B03, B04, B08, B11, B12
        # Index 14 = B04 (Red), Index 15 = B08 (NIR)
        red = data[14, :, :].astype(float) 
        nir = data[15, :, :].astype(float) 
        
        # Calculate NDVI
        ndvi = (nir - red) / (nir + red)
        ndvi[nir + red == 0] = 0 # Handle division by zero
        
        # Save as single band
        profile.update(count=1, dtype=rasterio.float32)
        with rasterio.open(output_ndvi_tif, 'w', **profile) as dst:
            dst.write(ndvi.astype(rasterio.float32), 1)
            
    # Calculate Mean NDVI for report
    valid_ndvi = ndvi[ndvi > 0]
    mean_ndvi = float(np.nanmean(valid_ndvi)) if valid_ndvi.size > 0 else 0.0
    print(f"Mean NDVI: {mean_ndvi:.3f}")
    return mean_ndvi

def prepare_for_prithvi(input_tif, output_tif="prithvi_ready.tif"):
    """
    Normalizes the raw stacked TIFF for Prithvi model input.
    """
    print(f"Normalizing {input_tif} for Prithvi...")
    
    with rasterio.open(input_tif) as src:
        data = src.read().astype(float)
        profile = src.profile
        
        # Convert to DN if needed (0-1 -> 0-10000)
        if np.max(data) <= 1.0:
            print("Converting Reflectance (0-1) to DN (0-10000)...")
            data = data * 10000.0
            
        # Handle NoData
        nodata = src.nodata
        if nodata is not None:
            data[data == nodata] = 0 
            
    # Prithvi Stats (Mean, Std) for 6 bands
    means = np.array([1369.03, 1597.79, 1741.10, 1958.44, 2153.01, 1899.70])
    stds  = np.array([2026.96, 1831.26, 1930.37, 1851.09, 1687.86, 1737.88])
    
    # Repeat for 3 time steps (18 bands)
    means_3t = np.tile(means, 3)
    stds_3t  = np.tile(stds, 3)
    
    # Normalize
    normalized_data = (data - means_3t[:, None, None]) / stds_3t[:, None, None]
    
    # Save
    profile.update(dtype=rasterio.float32, count=18)
    with rasterio.open(output_tif, 'w', **profile) as dst:
        dst.write(normalized_data.astype(rasterio.float32))
        
    print(f"✅ Prithvi-ready TIFF saved to: {output_tif}")

def classify_and_visualize(ndvi_value, ndvi_map_path):
    print("Generating Visualization...")
    
    with rasterio.open(ndvi_map_path) as src:
        ndvi_data = src.read(1)
        ndvi_data = np.ma.masked_where(ndvi_data <= 0, ndvi_data)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    
    # Left: Vegetation Health
    im1 = axes[0].imshow(ndvi_data, cmap='Greens', vmin=0.2, vmax=0.9)
    axes[0].set_title(f"Vegetation Health (NDVI: {ndvi_value:.2f})", fontsize=12)
    axes[0].axis('off')
    plt.colorbar(im1, ax=axes[0], label="NDVI")

    # Right: Full Context
    im2 = axes[1].imshow(ndvi_data, cmap='RdYlGn', vmin=0, vmax=1)
    axes[1].set_title("Full Scene NDVI", fontsize=12)
    axes[1].axis('off')
    plt.colorbar(im2, ax=axes[1], label="NDVI")

    plt.suptitle("Dhaka Farm Analysis", fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig("farm_visualization.png")
    plt.show()

def run_pipeline():
    today = datetime.date.today().isoformat()
    
    # Cache Check
    if Path(LAST_RUN_FILE).exists() and Path(LAST_RUN_FILE).read_text().strip() == today:
        print("Data already fetched today. Using cached files.")
        if os.path.exists("prithvi_input_stacked.tif"):
            prepare_for_prithvi("prithvi_input_stacked.tif")
            mean_ndvi = extract_ndvi_from_stack("prithvi_input_stacked.tif")
            classify_and_visualize(mean_ndvi, "ndvi_map.tif")
        return

    try:
        con = connect_openeo()
        
        # 1. Download & Stack
        raw_tif = get_prithvi_stacked_tif(con, FARM_GEOJSON)
        
        # 2. Prepare for Prithvi (Normalize)
        prepare_for_prithvi(raw_tif)
        
        # 3. Extract NDVI for Visualization
        mean_ndvi = extract_ndvi_from_stack(raw_tif)
        
        # 4. Visualize
        classify_and_visualize(mean_ndvi, "ndvi_map.tif")
        
        # Update Cache
        Path(LAST_RUN_FILE).write_text(today)
        print("✅ Pipeline Complete!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    run_pipeline()