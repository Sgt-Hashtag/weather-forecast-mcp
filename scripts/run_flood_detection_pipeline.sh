#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEVICE="${DEVICE:-auto}"
MODEL="${MODEL:-prithvi2}"
NDVI_MAX="${NDVI_MAX:-0.16}"
NDVI_B8A_MAX="${NDVI_B8A_MAX:-0.22}"
MNDWI_MIN="${MNDWI_MIN:-0.0}"
NDWI_MIN="${NDWI_MIN:--0.08}"
NIR_MAX="${NIR_MAX:-1600}"
SWIR1_MAX="${SWIR1_MAX:-1600}"
SWIR2_MAX="${SWIR2_MAX:-1200}"
MIN_AREA="${MIN_AREA:-20}"

INPUT_TIF="${INPUT_TIF:-${SCRIPT_DIR}/prithvi_flood_input.tif}"
if [[ "${MODEL}" == "prithvi2" ]]; then
  RAW_MASK="${RAW_MASK:-${SCRIPT_DIR}/prithvi2_flood_prediction.tif}"
  RAW_PREVIEW="${RAW_PREVIEW:-${SCRIPT_DIR}/prithvi2_flood_prediction_preview.png}"
elif [[ "${MODEL}" == "prithvi1" ]]; then
  RAW_MASK="${RAW_MASK:-${SCRIPT_DIR}/prithvi_flood_prediction.tif}"
  RAW_PREVIEW="${RAW_PREVIEW:-${SCRIPT_DIR}/prithvi_flood_prediction_preview.png}"
else
  echo "Unsupported MODEL='${MODEL}'. Use MODEL=prithvi2 or MODEL=prithvi1." >&2
  exit 2
fi
FILTERED_MASK="${FILTERED_MASK:-${SCRIPT_DIR}/prithvi_flood_prediction_filtered.tif}"
FILTERED_PREVIEW="${FILTERED_PREVIEW:-${SCRIPT_DIR}/prithvi_flood_prediction_filtered_preview.png}"

echo "== Flood detection pipeline =="
echo "Project root: ${PROJECT_ROOT}"
echo "Model: ${MODEL}"
echo "Device: ${DEVICE}"
echo "NDVI max: ${NDVI_MAX}"
echo "B8A NDVI max: ${NDVI_B8A_MAX}"
echo "MNDWI min: ${MNDWI_MIN}"
echo "NDWI min: ${NDWI_MIN}"
echo "NIR max: ${NIR_MAX}"
echo "SWIR1 max: ${SWIR1_MAX}"
echo "SWIR2 max: ${SWIR2_MAX}"
echo "Minimum blob area: ${MIN_AREA} px"
echo

cd "${SCRIPT_DIR}"

echo "[1/3] Creating Prithvi input GeoTIFF from Copernicus/OpenEO..."
python3 copernicus_flood_segmentation.py

echo
echo "[2/3] Running Prithvi flood inference..."
if [[ "${MODEL}" == "prithvi2" ]]; then
  python3 prithvi2_flood_inference.py \
    --input "${INPUT_TIF}" \
    --output "${RAW_MASK}" \
    --preview "${RAW_PREVIEW}" \
    --device "${DEVICE}"
else
  python3 prithvi_flood_inference.py \
    --input "${INPUT_TIF}" \
    --output "${RAW_MASK}" \
    --preview "${RAW_PREVIEW}" \
    --device "${DEVICE}"
fi

echo
echo "[3/3] Filtering vegetation/permanent-water-like false positives..."
python3 postprocess_flood_mask.py \
  --input "${INPUT_TIF}" \
  --raw-mask "${RAW_MASK}" \
  --output "${FILTERED_MASK}" \
  --preview "${FILTERED_PREVIEW}" \
  --ndvi-max "${NDVI_MAX}" \
  --ndvi-b8a-max "${NDVI_B8A_MAX}" \
  --mndwi-min "${MNDWI_MIN}" \
  --ndwi-min "${NDWI_MIN}" \
  --nir-max "${NIR_MAX}" \
  --swir1-max "${SWIR1_MAX}" \
  --swir2-max "${SWIR2_MAX}" \
  --min-area "${MIN_AREA}"

echo
echo "Pipeline complete."
echo "Raw mask:      ${RAW_MASK}"
echo "Filtered mask: ${FILTERED_MASK}"
echo "Preview:       ${FILTERED_PREVIEW}"
