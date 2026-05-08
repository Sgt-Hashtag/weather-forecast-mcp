#!/usr/bin/env python3
"""
Prepare Sentinel-2 input for Prithvi-EO-2.0-300M-TL-Sen1Floods11 flood segmentation.

This script fetches Sentinel-2 data from Copernicus Data Space Ecosystem (CDSE)
and creates a GeoTIFF input for the local Prithvi 2.0 flood segmentation runner.

The model expects a normal Sentinel-2 stack and uses the model config to extract:
B02, B03, B04, B8A, B11, B12. Data must be in reflectance units multiplied by
10,000.

Author: Generated for flood mapping workflow
"""

import openeo
import datetime
from datetime import timedelta
import os
import numpy as np
import rasterio
from pathlib import Path
import traceback
import matplotlib.pyplot as plt

# ====================== CONFIG ======================
BASE_DIR = Path(__file__).resolve().parent

# Focus on a flood-prone area (e.g., near rivers in Dhaka, Bangladesh)
FARM_GEOJSON = {
    "type": "Polygon",
    "coordinates": [
        [
            [90.38, 23.78], [90.40, 23.78],
            [90.40, 23.80], [90.38, 23.80],
            [90.38, 23.78]
        ]
    ]
}

LAST_RUN_FILE = BASE_DIR / "last_processed_flood.txt"
TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_flood_data"
OUTPUT_TIF = BASE_DIR / "prithvi_flood_input.tif"
CACHE_VERSION = "scl-mask-full-s2-v2"

# The Prithvi 2.0 runner keeps the same Sentinel-2 band subset:
# B02, B03, B04, B8A, B11, B12.
S2_ALL_BANDS = [
    "B01", "B02", "B03", "B04", "B05", "B06", "B07",
    "B08", "B8A", "B09", "B10", "B11", "B12"
]
S2_DOWNLOAD_BANDS = [band for band in S2_ALL_BANDS if band != "B10"]
MODEL_BANDS = ["B02", "B03", "B04", "B8A", "B11", "B12"]
MODEL_BAND_INDICES = [S2_ALL_BANDS.index(band) for band in MODEL_BANDS]
BAND_DESCRIPTIONS = {
    "B01": "Coastal aerosol (60m)",
    "B02": "Blue (10m)",
    "B03": "Green (10m)",
    "B04": "Red (10m)",
    "B05": "Red edge 1 (20m)",
    "B06": "Red edge 2 (20m)",
    "B07": "Red edge 3 (20m)",
    "B08": "NIR (10m)",
    "B8A": "Narrow NIR (20m)",
    "B09": "Water vapor (60m)",
    "B10": "Cirrus (60m)",
    "B11": "SWIR 1 (20m)",
    "B12": "SWIR 2 (20m)"
}

# Sentinel-2 Scene Classification Layer classes to remove.
SCL_INVALID_CLASSES = [0, 1, 2, 3, 8, 9, 10, 11]

# ===================================================

def connect_openeo():
    """Connect to Copernicus Data Space Ecosystem"""
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    con.authenticate_oidc()
    return con

def get_optical_data(con, geojson, start_date, end_date):
    """
    Fetches Sentinel-2 L2A data (Surface Reflectance).

    Returns a median composite of the full 13-band Sentinel-2 stack expected by
    the model before its band extraction step.
    Cloud masking is applied using the SCL (Scene Classification Layer).
    """
    print("Fetching Sentinel-2 Optical data...")
    print(f"  Bands: {', '.join(S2_DOWNLOAD_BANDS)}")
    print(f"  Date range: {start_date} to {end_date}")

    s2 = con.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=geojson,
        temporal_extent=[start_date, end_date],
        bands=S2_DOWNLOAD_BANDS + ["SCL"],
        max_cloud_cover=20
    )

    # Cloud Masking using SCL band
    scl = s2.band("SCL")
    invalid_mask = scl == SCL_INVALID_CLASSES[0]
    for invalid_class in SCL_INVALID_CLASSES[1:]:
        invalid_mask = invalid_mask | (scl == invalid_class)
    s2_clean = s2.mask(invalid_mask)

    # Filter to keep only spectral bands (remove SCL)
    s2_spectral = s2_clean.filter_bands(S2_DOWNLOAD_BANDS)

    # Temporal Median Composite - reduces noise and cloud contamination
    print("  Creating temporal median composite...")
    s2_median = s2_spectral.reduce_dimension(dimension="t", reducer="median")

    # Save as GeoTIFF
    job = s2_median.save_result(format="GTiff")
    batch_job = job.create_job(title="Sentinel-2 Flood Input")
    print("  Starting OpenEO job...")
    batch_job.start_and_wait()

    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    opt_path = TEMP_DOWNLOAD_DIR / "optical_composite.tif"
    if opt_path.exists():
        opt_path.unlink()
    batch_job.get_results().download_file(str(opt_path))
    print(f"  Downloaded to: {opt_path}")

    return str(opt_path)

def prepare_prithvi_input(opt_path, output_tif=OUTPUT_TIF):
    """
    Prepares Sentinel-2 data for the Prithvi 2.0 Sen1Floods11 runner.

    Requirements:
    - 13 bands in Sen1Floods11/Sentinel-2 order
    - Reflectance scaled by 10000
    - Nodata stored as -9999
    """
    print("\nPreparing Prithvi model input...")

    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

    with rasterio.open(opt_path) as src:
        data = src.read(masked=True).astype(np.float32)
        profile = src.profile.copy()

        print(f"  Input shape: {data.shape}")
        print(f"  Bands from OpenEO: {src.descriptions}")

        data = np.ma.filled(data, np.nan)
        finite = data[np.isfinite(data)]
        if finite.size == 0:
            raise ValueError("OpenEO returned no finite pixels for the requested area/date range.")
        if float(np.nanmax(data)) <= 1.0:
            print("  Scaling 0-1 reflectance to 0-10000.")
            data = data * 10000.0

        data = np.nan_to_num(data, nan=-9999.0, posinf=-9999.0, neginf=-9999.0)
        data[data < -9999.0] = -9999.0
        print(f"  Data min: {np.min(data[data != -9999]):.2f}, max: {np.max(data[data != -9999]):.2f}")

        # Ensure band order matches model expectation
        # OpenEO should return bands in the order we requested, but verify when
        # descriptions are available.
        band_names = [_normalize_band_name(desc) for desc in src.descriptions]
        print(f"  Detected band order: {band_names}")

        expected_download_order = S2_DOWNLOAD_BANDS
        if all(name in expected_download_order for name in band_names) and band_names != expected_download_order:
            print(f"  Reordering bands to match expected order: {expected_download_order}")
            band_mapping = {name: i for i, name in enumerate(band_names)}
            reordered_data = np.zeros_like(data)
            for i, expected_band in enumerate(expected_download_order):
                if expected_band in band_mapping:
                    reordered_data[i] = data[band_mapping[expected_band]]
                else:
                    raise ValueError(f"Missing expected band: {expected_band}")
            data = reordered_data
        elif data.shape[0] != len(expected_download_order):
            raise ValueError(f"Expected {len(expected_download_order)} downloaded bands, found {data.shape[0]}.")

        full_data = np.zeros((len(S2_ALL_BANDS), data.shape[1], data.shape[2]), dtype=np.float32)
        for src_idx, band_name in enumerate(expected_download_order):
            full_data[S2_ALL_BANDS.index(band_name)] = data[src_idx]
        data = full_data

        # Update profile for output
        profile.update(
            count=len(S2_ALL_BANDS),
            dtype=rasterio.float32,
            compress='zstd',
            nodata=-9999
        )

        # Write output
        output_path = Path(output_tif)
        with rasterio.open(output_path, 'w', **profile) as dst:
            dst.write(data)
            for band_idx, band_name in enumerate(S2_ALL_BANDS, start=1):
                dst.set_band_description(band_idx, f"{band_name} {BAND_DESCRIPTIONS[band_name]}")

        print(f"\n✅ Prithvi input saved: {output_path}")
        print(f"   Shape: {data.shape} (bands, height, width)")
        print(f"   Input bands: {', '.join(S2_ALL_BANDS)}")
        print(f"   Model extracts: {', '.join(MODEL_BANDS)}")
        print(f"   CRS: {profile['crs']}")
        print(f"   Resolution: {profile['transform'][0]:.2f} m/pixel")

        return str(output_path), data

def _normalize_band_name(description):
    if not description:
        return ""
    return description.split()[0].split("_")[0]

def visualize_input(input_tif, data):
    """
    Create visualization of the input data for quality check.
    Shows RGB composite and individual band histograms.
    """
    print("\nCreating visualization...")

    # Create RGB composite (B04=Red, B03=Green, B02=Blue).
    red = data[S2_ALL_BANDS.index("B04")]
    green = data[S2_ALL_BANDS.index("B03")]
    blue = data[S2_ALL_BANDS.index("B02")]

    # Normalize for display (Sentinel-2 L2A is 0-10000)
    rgb = np.stack([red, green, blue], axis=-1)
    rgb_display = np.clip(rgb / 3000.0, 0, 1)  # Stretch for better visualization

    # Replace nodata with black
    nodata_mask = np.any(data == -9999, axis=0)
    rgb_display[nodata_mask] = 0

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # RGB Image
    axes[0].imshow(rgb_display)
    axes[0].set_title("Sentinel-2 RGB (B04-B03-B02)\nTrue Color Composite", fontsize=12)
    axes[0].axis('off')

    # Band statistics for the 6 model bands.
    band_names = [f"{band} ({BAND_DESCRIPTIONS[band].split(' (')[0]})" for band in MODEL_BANDS]
    colors = ['blue', 'green', 'red', 'orange', 'brown', 'purple']

    ax2 = axes[1]
    for band_idx, name, color in zip(MODEL_BAND_INDICES, band_names, colors):
        band_data = data[band_idx][data[band_idx] != -9999]  # Exclude nodata
        if len(band_data) > 0:
            ax2.hist(band_data.flatten(), bins=50, alpha=0.5, label=name, color=color, density=True)

    ax2.set_xlabel('Reflectance Value (×10000)', fontsize=10)
    ax2.set_ylabel('Normalized Frequency', fontsize=10)
    ax2.set_title('Band Value Distributions\n(Sentinel-2 L2A Surface Reflectance)', fontsize=12)
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    viz_path = BASE_DIR / "flood_input_preview.png"
    plt.savefig(viz_path, dpi=150, bbox_inches='tight')
    print(f"  Visualization saved: {viz_path}")
    plt.close()

def validate_prithvi_input(input_tif):
    """Prints Prithvi 2.0 input sanity checks."""
    print("\nValidating Prithvi flood input...")
    with rasterio.open(input_tif) as src:
        if src.count != len(S2_ALL_BANDS):
            raise ValueError(f"Expected {len(S2_ALL_BANDS)} bands for the demo, found {src.count}.")
        if src.nodata != -9999:
            raise ValueError(f"Expected nodata=-9999, found {src.nodata}.")

        data = src.read(masked=True).astype(np.float32)
        height, width = src.height, src.width
        print(f"  Shape: {src.count} bands x {height} rows x {width} cols")
        print(f"  CRS: {src.crs}")
        print(f"  Nodata: {src.nodata}")
        if height < 224 or width < 224:
            print("  Warning: dimensions are below the model crop size of 224x224.")

        valid_pixels = np.all(~np.ma.getmaskarray(data), axis=0)
        print(f"  Valid pixel ratio: {float(valid_pixels.mean()):.1%}")
        for band_name in MODEL_BANDS:
            band = data[S2_ALL_BANDS.index(band_name)].compressed()
            if band.size == 0:
                print(f"  {band_name}: no valid pixels")
                continue
            p2, p50, p98 = np.percentile(band, [2, 50, 98])
            print(f"  {band_name}: p02={p2:.0f}, median={p50:.0f}, p98={p98:.0f}")

def run_flood_pipeline():
    """Main pipeline execution"""
    today = datetime.date.today().isoformat()
    cache_key = f"{today}|{CACHE_VERSION}"

    # Check if we already ran today
    if Path(LAST_RUN_FILE).exists() and Path(LAST_RUN_FILE).read_text().strip() == cache_key:
        print("⚠️  Data already fetched today. Using cached files.")
        if OUTPUT_TIF.exists():
            print(f"  Found cached input: {OUTPUT_TIF}")
            with rasterio.open(OUTPUT_TIF) as src:
                data = src.read()
            validate_prithvi_input(OUTPUT_TIF)
            visualize_input(str(OUTPUT_TIF), data)
            return True
        else:
            print("  Cached file not found, re-running pipeline...")

    try:
        # Connect to OpenEO
        con = connect_openeo()

        # Define date range (last 30 days for recent flood status)
        end_date = datetime.date.today()
        start_date = end_date - timedelta(days=30)

        print("\n" + "="*60)
        print("PRITHVI FLOOD SEGMENTATION PIPELINE")
        print("="*60)
        print(f"Area of Interest: {FARM_GEOJSON['coordinates'][0]}")
        print(f"Date Range: {start_date} to {end_date}")
        print(f"Upload Bands: {', '.join(S2_ALL_BANDS)}")
        print(f"Model Extracts: {', '.join(MODEL_BANDS)}")
        print("="*60)

        # Step 1: Fetch Sentinel-2 data
        print("\n[Step 1/3] Fetching Sentinel-2 data from Copernicus...")
        opt_path = get_optical_data(con, FARM_GEOJSON, start_date.isoformat(), end_date.isoformat())

        # Step 2: Prepare input for Prithvi model
        print("\n[Step 2/3] Preparing model input...")
        input_path, data = prepare_prithvi_input(opt_path, str(OUTPUT_TIF))
        validate_prithvi_input(input_path)

        # Step 3: Visualize
        print("\n[Step 3/3] Creating preview visualization...")
        visualize_input(input_path, data)

        # Save timestamp
        Path(LAST_RUN_FILE).write_text(cache_key)

        print("\n" + "="*60)
        print("✅ FLOOD PIPELINE COMPLETE!")
        print("="*60)
        print(f"\nInput file ready for Prithvi model: {OUTPUT_TIF}")

    except Exception as e:
        print(f"\n❌ Error in pipeline: {e}")
        traceback.print_exc()
        return False

    return True

if __name__ == "__main__":
    success = run_flood_pipeline()
    exit(0 if success else 1)
