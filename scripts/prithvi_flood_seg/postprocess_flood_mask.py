#!/usr/bin/env python3
"""
Postprocess Prithvi flood predictions to reduce vegetation false positives.

Inputs:
  - prithvi_flood_input.tif: 13-band Sentinel-2 stack from copernicus_flood_segmentation.py
  - prithvi2_flood_prediction.tif: raw model mask from prithvi2_flood_inference.py

Output classes:
  0 = not flood
  1 = likely flood
  255 = nodata
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import rasterio


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "prithvi_flood_input.tif"
DEFAULT_RAW_MASK = BASE_DIR / "prithvi2_flood_prediction.tif"
DEFAULT_OUTPUT = BASE_DIR / "prithvi_flood_prediction_filtered.tif"
DEFAULT_PREVIEW = BASE_DIR / "prithvi_flood_prediction_filtered_preview.png"

S2_BANDS = [
    "B01", "B02", "B03", "B04", "B05", "B06", "B07",
    "B08", "B8A", "B09", "B10", "B11", "B12"
]
NODATA = 255


def parse_args():
    parser = argparse.ArgumentParser(description="Filter raw flood mask false positives.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="13-band Sentinel-2 input TIFF.")
    parser.add_argument("--raw-mask", default=str(DEFAULT_RAW_MASK), help="Raw Prithvi prediction TIFF.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Filtered output mask TIFF.")
    parser.add_argument("--preview", default=str(DEFAULT_PREVIEW), help="Filtered preview PNG.")
    parser.add_argument(
        "--ndvi-max",
        type=float,
        default=0.16,
        help="Pixels with B08 NDVI above this are treated as vegetation and removed.",
    )
    parser.add_argument(
        "--ndvi-b8a-max",
        type=float,
        default=0.22,
        help="Pixels with B8A NDVI above this are treated as vegetation and removed.",
    )
    parser.add_argument(
        "--mndwi-min",
        type=float,
        default=0.0,
        help="Pixels below this MNDWI threshold are unlikely to be water and removed.",
    )
    parser.add_argument(
        "--ndwi-min",
        type=float,
        default=-0.08,
        help="Pixels below this green/NIR NDWI threshold are unlikely to be open water.",
    )
    parser.add_argument(
        "--nir-max",
        type=float,
        default=1600.0,
        help="Pixels with B08 NIR above this are likely vegetation and removed.",
    )
    parser.add_argument(
        "--swir1-max",
        type=float,
        default=1600.0,
        help="Pixels with SWIR1 above this are unlikely to be open water.",
    )
    parser.add_argument(
        "--swir2-max",
        type=float,
        default=1200.0,
        help="Pixels with SWIR2 above this are unlikely to be open water.",
    )
    parser.add_argument(
        "--min-area",
        type=int,
        default=20,
        help="Remove connected flood blobs smaller than this many pixels.",
    )
    parser.add_argument(
        "--permanent-water-mask",
        default=None,
        help="Optional mask TIFF where value 1 means permanent water to subtract.",
    )
    parser.add_argument("--no-preview", action="store_true", help="Skip preview PNG.")
    return parser.parse_args()


def read_inputs(input_tif: Path, raw_mask_tif: Path):
    with rasterio.open(input_tif) as src:
        data = src.read().astype(np.float32)
        profile = src.profile.copy()
        input_nodata = src.nodata
    with rasterio.open(raw_mask_tif) as src:
        raw_mask = src.read(1)
    if raw_mask.shape != data.shape[1:]:
        raise ValueError(f"Mask shape {raw_mask.shape} does not match input shape {data.shape[1:]}.")
    return data, raw_mask, profile, input_nodata


def normalized_difference(a, b):
    denom = a + b
    out = np.zeros_like(a, dtype=np.float32)
    np.divide(a - b, denom, out=out, where=denom != 0)
    return out


def filter_mask(data, raw_mask, input_nodata, args):
    blue = data[S2_BANDS.index("B02")]
    green = data[S2_BANDS.index("B03")]
    red = data[S2_BANDS.index("B04")]
    nir = data[S2_BANDS.index("B08")]
    b8a = data[S2_BANDS.index("B8A")]
    swir1 = data[S2_BANDS.index("B11")]
    swir2 = data[S2_BANDS.index("B12")]

    ndvi = normalized_difference(nir, red)
    ndvi_b8a = normalized_difference(b8a, red)
    mndwi = normalized_difference(green, swir1)
    ndwi = normalized_difference(green, nir)

    valid = raw_mask != NODATA
    if input_nodata is not None:
        valid &= ~np.any(data == input_nodata, axis=0)

    filtered = (raw_mask == 1) & valid
    filtered &= ndvi <= args.ndvi_max
    filtered &= ndvi_b8a <= args.ndvi_b8a_max
    filtered &= mndwi >= args.mndwi_min
    filtered &= ndwi >= args.ndwi_min
    filtered &= nir <= args.nir_max
    filtered &= swir1 <= args.swir1_max
    filtered &= swir2 <= args.swir2_max

    if args.permanent_water_mask:
        with rasterio.open(args.permanent_water_mask) as src:
            permanent = src.read(1) == 1
        if permanent.shape != filtered.shape:
            raise ValueError("Permanent water mask shape does not match prediction shape.")
        filtered &= ~permanent

    filtered = remove_small_components(filtered, args.min_area)

    out = np.zeros(raw_mask.shape, dtype=np.uint8)
    out[filtered] = 1
    out[~valid] = NODATA
    return out, ndvi, mndwi, ndwi, ndvi_b8a


def remove_small_components(mask, min_area):
    if min_area <= 1:
        return mask

    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    keep = np.zeros_like(mask, dtype=bool)
    neighbors = [(-1, 0), (1, 0), (0, -1), (0, 1)]

    for row in range(height):
        for col in range(width):
            if seen[row, col] or not mask[row, col]:
                continue
            component = []
            queue = deque([(row, col)])
            seen[row, col] = True
            while queue:
                r, c = queue.popleft()
                component.append((r, c))
                for dr, dc in neighbors:
                    nr, nc = r + dr, c + dc
                    if nr < 0 or nr >= height or nc < 0 or nc >= width:
                        continue
                    if seen[nr, nc] or not mask[nr, nc]:
                        continue
                    seen[nr, nc] = True
                    queue.append((nr, nc))
            if len(component) >= min_area:
                for r, c in component:
                    keep[r, c] = True
    return keep


def write_output(mask, profile, output_path):
    profile.update(count=1, dtype=rasterio.uint8, nodata=NODATA, compress="zstd")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(mask, 1)
        dst.set_band_description(1, "Filtered flood mask: 0=not flood, 1=likely flood, 255=nodata")


def write_preview(data, raw_mask, filtered_mask, preview_path):
    rgb = np.stack([
        data[S2_BANDS.index("B04")],
        data[S2_BANDS.index("B03")],
        data[S2_BANDS.index("B02")],
    ], axis=-1)
    valid = rgb[np.isfinite(rgb) & (rgb > 0)]
    if valid.size:
        lo, hi = np.percentile(valid, [2, 98])
        if hi <= lo:
            hi = lo + 1
        rgb = np.clip((rgb - lo) / (hi - lo), 0, 1)
    else:
        rgb = np.zeros_like(rgb)
    rgb = np.nan_to_num(rgb, nan=0)

    raw_overlay = overlay_mask(rgb, raw_mask == 1)
    filtered_overlay = overlay_mask(rgb, filtered_mask == 1)

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for ax, image, title in [
        (axes[0], rgb, "Input RGB"),
        (axes[1], raw_overlay, "Raw Prediction"),
        (axes[2], filtered_overlay, "Filtered Flood"),
    ]:
        ax.imshow(image)
        ax.set_title(title)
        ax.axis("off")
    plt.tight_layout()
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(preview_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def overlay_mask(rgb, mask):
    overlay = rgb.copy()
    overlay[mask] = overlay[mask] * 0.35 + np.array([0.0, 0.65, 1.0]) * 0.65
    return overlay


def main():
    args = parse_args()
    data, raw_mask, profile, input_nodata = read_inputs(Path(args.input), Path(args.raw_mask))
    filtered, ndvi, mndwi, ndwi, ndvi_b8a = filter_mask(data, raw_mask, input_nodata, args)
    write_output(filtered, profile, Path(args.output))

    raw_count = int(np.sum(raw_mask == 1))
    filtered_count = int(np.sum(filtered == 1))
    removed = raw_count - filtered_count
    print(f"Raw flood/water pixels: {raw_count}")
    print(f"Filtered flood pixels: {filtered_count}")
    print(f"Removed pixels: {removed}")
    print(f"Mean NDVI of kept pixels: {float(np.mean(ndvi[filtered == 1])) if filtered_count else 0.0:.3f}")
    print(f"Mean B8A NDVI of kept pixels: {float(np.mean(ndvi_b8a[filtered == 1])) if filtered_count else 0.0:.3f}")
    print(f"Mean MNDWI of kept pixels: {float(np.mean(mndwi[filtered == 1])) if filtered_count else 0.0:.3f}")
    print(f"Mean NDWI of kept pixels: {float(np.mean(ndwi[filtered == 1])) if filtered_count else 0.0:.3f}")
    print(f"Filtered mask written: {args.output}")

    if not args.no_preview:
        write_preview(data, raw_mask, filtered, Path(args.preview))
        print(f"Preview written: {args.preview}")


if __name__ == "__main__":
    main()
