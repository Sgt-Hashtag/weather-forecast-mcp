# Flood Detection Flow

This workflow detects likely flood extent from recent Sentinel-2 imagery, then
filters common false positives such as grass/vegetation.

## Run Everything

From the project root, in the inference environment:

```bash
scripts/run_flood_detection_pipeline.sh
```

Optional tuning:

```bash
MODEL=prithvi2 DEVICE=cuda NDVI_MAX=0.12 MNDWI_MIN=0.05 NDWI_MIN=0.0 MIN_AREA=30 scripts/run_flood_detection_pipeline.sh
```

## Docker Setup

The agent Docker image is wired for Prithvi-EO-2.0-300M-TL-Sen1Floods11. By
default, `docker compose build agent` downloads the Hugging Face config and
fine-tuned checkpoint into `/app/models/huggingface` inside the image, so the
first inference run does not wait on the 1.28GB checkpoint.

```bash
docker compose build agent
```

To skip model prefetch during image build and let the first inference command
download it instead:

```bash
DOWNLOAD_PRITHVI2_MODEL=false docker compose build agent
```

For CUDA wheels, pass the PyTorch wheel index that matches your runtime:

```bash
TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 docker compose build agent
```

Run Prithvi 2.0 inference in the container:

```bash
docker compose run --rm agent python /app/scripts/prithvi2_flood_inference.py --device auto
```

The Compose file mounts `./scripts` to `/app/scripts`, so generated TIFF and PNG
outputs are written back to the project `scripts/` directory.

## Pipeline Stages

### 1. Create Model Input

```bash
python3 scripts/copernicus_flood_segmentation.py
```

Creates:

```text
scripts/prithvi_flood_input.tif
```

This script downloads recent Sentinel-2 imagery through OpenEO/Copernicus,
cloud-masks it, composites it, and writes a 13-band GeoTIFF.

### 2. Run Prithvi Inference

```bash
python3 scripts/prithvi_flood_inference.py --device auto
```

Creates:

```text
scripts/prithvi_flood_prediction.tif
scripts/prithvi_flood_prediction_preview.png
```

This is the raw Prithvi flood/water-like prediction. It may include false
positives on grass or other dark/vegetated pixels.

The automation script uses the newer TerraTorch-based Prithvi 2.0 Sen1Floods11
model by default. To run it directly:

```bash
python3 scripts/prithvi2_flood_inference.py --device auto
```

Creates:

```text
scripts/prithvi2_flood_prediction.tif
scripts/prithvi2_flood_prediction_preview.png
```

Then postprocess it by passing that raw mask:

```bash
python3 scripts/postprocess_flood_mask.py --raw-mask scripts/prithvi2_flood_prediction.tif
```

To use the older Prithvi 1.0/MMCV runner in the full pipeline:

```bash
MODEL=prithvi1 scripts/run_flood_detection_pipeline.sh
```

### 3. Postprocess Flood Mask

```bash
python3 scripts/postprocess_flood_mask.py
```

Creates:

```text
scripts/prithvi_flood_prediction_filtered.tif
scripts/prithvi_flood_prediction_filtered_preview.png
```

The postprocessor removes likely vegetation using NDVI, keeps water-like pixels
using MNDWI, and removes tiny connected components.

## Threshold Tuning

Default:

```bash
python3 scripts/postprocess_flood_mask.py \
  --ndvi-max 0.16 \
  --ndvi-b8a-max 0.22 \
  --mndwi-min 0.0 \
  --ndwi-min -0.08 \
  --nir-max 1600 \
  --swir1-max 1600 \
  --swir2-max 1200 \
  --min-area 20
```

If grass is still marked as flood, tighten:

```bash
python3 scripts/postprocess_flood_mask.py \
  --ndvi-max 0.12 \
  --ndvi-b8a-max 0.16 \
  --mndwi-min 0.05 \
  --ndwi-min 0.0 \
  --nir-max 1300 \
  --swir1-max 1200 \
  --swir2-max 900 \
  --min-area 30
```

If too much true flood is removed, loosen:

```bash
python3 scripts/postprocess_flood_mask.py \
  --ndvi-max 0.25 \
  --ndvi-b8a-max 0.30 \
  --mndwi-min -0.10 \
  --ndwi-min -0.15 \
  --nir-max 2200 \
  --swir1-max 2000 \
  --swir2-max 1700 \
  --min-area 10
```

## Interpretation

Mask classes:

```text
0   not flood
1   likely flood
255 nodata
```

For adverse-weather detection, treat the filtered mask as a candidate flood
signal. A stronger detector should combine it with rainfall anomaly and, when
available, subtract permanent water or a dry-period baseline.
