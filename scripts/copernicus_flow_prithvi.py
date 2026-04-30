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
BASE_DIR = Path(__file__).resolve().parent
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
RAW_STACK_TIF = BASE_DIR / "prithvi_input_stacked.tif"
NORMALIZED_TIF = BASE_DIR / "prithvi_ready.tif"
NDVI_TIF = BASE_DIR / "ndvi_map.tif"
VISUALIZATION_PNG = BASE_DIR / "farm_visualization.png"
RGB_PREVIEW_PNG = BASE_DIR / "prithvi_rgb_preview.png"
TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_prithvi_data"
CACHE_VERSION = "scl-mask-v2"

# The Hugging Face demo expects raw reflectance/DN-like values scaled around
# 0-10000. Do not upload the normalized float TIFF to the Space.
CREATE_NORMALIZED_LOCAL_COPY = False

PRITHVI_BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]
PRITHVI_BAND_DESCRIPTIONS = ["Blue", "Green", "Red", "Narrow NIR", "SWIR 1", "SWIR 2"]

# Sentinel-2 Scene Classification Layer classes to remove:
# 0 No data, 1 saturated/defective, 2 dark area pixels, 3 cloud shadows,
# 7 unclassified, 8 medium cloud, 9 high cloud, 10 cirrus, 11 snow/ice.
SCL_INVALID_CLASSES = [0, 1, 2, 3, 7, 8, 9, 10, 11]
# ===================================================

def connect_openeo():
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    con.authenticate_oidc()
    return con

def get_prithvi_stacked_tif(con, farm_geojson):
    """
    Downloads 3 monthly composites (6 bands each) and stacks them into a single 18-band GeoTIFF.
    Optimized for Hugging Face Prithvi demo upload: uint16 values scaled around
    0-10000, 18 bands, 3 monthly time steps, 6 bands per time step.
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
        bands=PRITHVI_BANDS + ["SCL"],
        max_cloud_cover=20
    )
    
    # 3. Cloud Masking
    scl = s2.band("SCL")
    invalid_mask = scl == SCL_INVALID_CLASSES[0]
    for invalid_class in SCL_INVALID_CLASSES[1:]:
        invalid_mask = invalid_mask | (scl == invalid_class)
    s2_clean = s2.mask(invalid_mask)
    
    # 4. Filter Bands (Remove SCL)
    s2_spectral = s2_clean.filter_bands(PRITHVI_BANDS)
    
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
    
    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    for old_tif in TEMP_DOWNLOAD_DIR.glob("*.tif"):
        old_tif.unlink()
    batch_job.get_results().download_files(TEMP_DOWNLOAD_DIR)
    
    # 7. Stack Locally with MAXIMUM Optimization
    tif_files = sorted(glob.glob(str(TEMP_DOWNLOAD_DIR / "*.tif")))
    
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

    with rasterio.open(RAW_STACK_TIF, "w", **opt_profile) as dst:
        for idx, tif_path in enumerate(tif_files):
            with rasterio.open(tif_path) as src:
                data = src.read(masked=True)
                data = np.ma.filled(data, 0)
                data = np.nan_to_num(data, nan=0, posinf=0, neginf=0)
                if np.nanmax(data) <= 1.0:
                    data = data * 10000.0
                data = np.clip(data, 0, 65535).astype(rasterio.uint16)
                
                start_idx = idx * 6 + 1
                end_idx = start_idx + 6
                dst.write(data, indexes=range(start_idx, end_idx))
                for band_offset, description in enumerate(PRITHVI_BAND_DESCRIPTIONS, start=start_idx):
                    dst.set_band_description(band_offset, f"T{idx + 1} {description}")
    
    # Check final size
    final_size_mb = os.path.getsize(RAW_STACK_TIF) / (1024 * 1024)
    print(f"Stacked TIFF created: {RAW_STACK_TIF} ({final_size_mb:.2f} MB)")
    return str(RAW_STACK_TIF)

def validate_prithvi_stack(input_tif):
    """
    Prints basic sanity checks and writes an RGB preview for the 3 time steps.
    """
    print("Validating Prithvi stack...")

    with rasterio.open(input_tif) as src:
        if src.count != 18:
            raise ValueError(f"Expected 18 bands, found {src.count}.")
        if src.nodata is None:
            raise ValueError("Expected nodata to be set to 0 for the Hugging Face demo mask.")

        data = src.read(masked=True).astype(np.float32)
        data = np.ma.masked_where(data == src.nodata, data)
        print(f"Shape: {src.count} bands x {src.height} rows x {src.width} cols")
        print(f"CRS: {src.crs}")
        print(f"Data types: {src.dtypes}")
        print(f"NoData: {src.nodata}")

        for timestep in range(3):
            start = timestep * 6
            block = data[start:start + 6]
            valid_pixels = np.any(~np.ma.getmaskarray(block), axis=0)
            valid_ratio = float(valid_pixels.mean()) if valid_pixels.size else 0.0
            print(f"T{timestep + 1}: valid pixel ratio {valid_ratio:.1%}")
            for band_idx, band_name in enumerate(PRITHVI_BAND_DESCRIPTIONS):
                band = block[band_idx].compressed()
                if band.size == 0:
                    print(f"  {band_name}: no valid pixels")
                    continue
                p2, p50, p98 = np.percentile(band, [2, 50, 98])
                print(f"  {band_name}: p02={p2:.0f}, median={p50:.0f}, p98={p98:.0f}")

        rgb_images = []
        for timestep in range(3):
            red = data[timestep * 6 + 2].filled(0)
            green = data[timestep * 6 + 1].filled(0)
            blue = data[timestep * 6 + 0].filled(0)
            rgb = np.stack([red, green, blue], axis=-1)
            rgb_images.append(_stretch_rgb_preview(rgb))

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for idx, (axis, rgb) in enumerate(zip(axes, rgb_images), start=1):
        axis.imshow(rgb)
        axis.set_title(f"T{idx} RGB")
        axis.axis("off")
    plt.tight_layout()
    plt.savefig(RGB_PREVIEW_PNG, dpi=150)
    plt.close(fig)
    print(f"RGB preview saved to: {RGB_PREVIEW_PNG}")

def _stretch_rgb_preview(rgb):
    valid = rgb[rgb > 0]
    if valid.size == 0:
        return np.zeros_like(rgb, dtype=np.uint8)
    lo, hi = np.percentile(valid, [2, 98])
    if hi <= lo:
        hi = lo + 1
    stretched = np.clip((rgb - lo) / (hi - lo), 0, 1)
    return (stretched * 255).astype(np.uint8)

def extract_ndvi_from_stack(input_tif, output_ndvi_tif=NDVI_TIF):
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
        denom = nir + red
        ndvi = np.zeros_like(red, dtype=float)
        np.divide(nir - red, denom, out=ndvi, where=denom != 0)
        
        # Save as single band
        profile.update(count=1, dtype=rasterio.float32)
        with rasterio.open(output_ndvi_tif, 'w', **profile) as dst:
            dst.write(ndvi.astype(rasterio.float32), 1)
            
    # Calculate Mean NDVI for report
    valid_ndvi = ndvi[ndvi > 0]
    mean_ndvi = float(np.nanmean(valid_ndvi)) if valid_ndvi.size > 0 else 0.0
    print(f"Mean NDVI: {mean_ndvi:.3f}")
    return mean_ndvi

def prepare_for_prithvi(input_tif, output_tif=NORMALIZED_TIF):
    """
    Normalizes the raw stacked TIFF for a local model pipeline that expects
    already-normalized arrays.

    Do not upload this file to the Hugging Face Space. The Space reads raw
    0-10000-style values and applies its own preprocessing.
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
    plt.savefig(VISUALIZATION_PNG)
    plt.close(fig)
    print(f"Visualization saved to: {VISUALIZATION_PNG}")

def run_pipeline():
    today = datetime.date.today().isoformat()
    cache_key = f"{today}|{CACHE_VERSION}"
    
    # Cache Check
    if Path(LAST_RUN_FILE).exists() and Path(LAST_RUN_FILE).read_text().strip() == cache_key:
        print("Data already fetched today. Using cached files.")
        if RAW_STACK_TIF.exists():
            validate_prithvi_stack(RAW_STACK_TIF)
            if CREATE_NORMALIZED_LOCAL_COPY:
                prepare_for_prithvi(RAW_STACK_TIF)
            mean_ndvi = extract_ndvi_from_stack(RAW_STACK_TIF)
            classify_and_visualize(mean_ndvi, NDVI_TIF)
            print(f"Upload this raw stack to the Hugging Face demo: {RAW_STACK_TIF}")
        return

    try:
        con = connect_openeo()
        
        # 1. Download & Stack
        raw_tif = get_prithvi_stacked_tif(con, FARM_GEOJSON)
        validate_prithvi_stack(raw_tif)
        
        # 2. Optional local-only normalization. The Hugging Face demo wants
        # the raw 18-band stack, not this normalized output.
        if CREATE_NORMALIZED_LOCAL_COPY:
            prepare_for_prithvi(raw_tif)
        
        # 3. Extract NDVI for Visualization
        mean_ndvi = extract_ndvi_from_stack(raw_tif)
        
        # 4. Visualize
        classify_and_visualize(mean_ndvi, NDVI_TIF)
        
        # Update Cache
        Path(LAST_RUN_FILE).write_text(cache_key)
        print("Pipeline complete.")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    run_pipeline()
