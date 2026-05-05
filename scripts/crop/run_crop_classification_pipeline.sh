#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DEVICE="${DEVICE:-auto}"
INPUT_TIF="${INPUT_TIF:-${SCRIPT_DIR}/prithvi_input_stacked.tif}"
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/local_prithvi/outputs}"
SAVE_PREVIEWS="${SAVE_PREVIEWS:-1}"
LOCAL_CONFIG="${LOCAL_CONFIG:-}"
LOCAL_CHECKPOINT="${LOCAL_CHECKPOINT:-}"
LOCAL_FILES_ONLY="${LOCAL_FILES_ONLY:-0}"

echo "== Crop classification pipeline =="
echo "Project root: ${PROJECT_ROOT}"
echo "Device: ${DEVICE}"
echo "Input TIFF: ${INPUT_TIF}"
echo "Output dir: ${OUTPUT_DIR}"
echo "Save previews: ${SAVE_PREVIEWS}"
if [[ -n "${LOCAL_CONFIG}" ]]; then
  echo "Local config: ${LOCAL_CONFIG}"
fi
if [[ -n "${LOCAL_CHECKPOINT}" ]]; then
  echo "Local checkpoint: ${LOCAL_CHECKPOINT}"
fi
if [[ "${LOCAL_FILES_ONLY}" == "1" ]]; then
  echo "Local files only: enabled"
fi
echo

cd "${PROJECT_ROOT}"

echo "[1/2] Creating Prithvi crop input GeoTIFF from Copernicus/OpenEO..."
python3 scripts/crop/copernicus_flow_prithvi.py

echo
echo "[2/2] Running Prithvi crop inference locally..."

CMD=(
  python3 local_prithvi/run_prithvi_local.py
  --input "${INPUT_TIF}"
  --output-dir "${OUTPUT_DIR}"
  --device "${DEVICE}"
)

if [[ "${SAVE_PREVIEWS}" == "1" ]]; then
  CMD+=(--save-previews)
fi

if [[ -n "${LOCAL_CONFIG}" || -n "${LOCAL_CHECKPOINT}" ]]; then
  if [[ -z "${LOCAL_CONFIG}" || -z "${LOCAL_CHECKPOINT}" ]]; then
    echo "Both LOCAL_CONFIG and LOCAL_CHECKPOINT must be set together." >&2
    exit 1
  fi
  CMD+=(--config "${LOCAL_CONFIG}" --checkpoint "${LOCAL_CHECKPOINT}")
fi

if [[ "${LOCAL_FILES_ONLY}" == "1" ]]; then
  CMD+=(--local-files-only)
fi

"${CMD[@]}"

echo
echo "Pipeline complete."
echo "Input:   ${INPUT_TIF}"
echo "Outputs: ${OUTPUT_DIR}"
