#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INPUT_TIF="${REPO_ROOT}/scripts/crop/prithvi_input_stacked.tif"
OUTPUT_DIR="${SCRIPT_DIR}/outputs"
DEVICE="${1:-cuda:0}"

if [[ ! -f "${INPUT_TIF}" ]]; then
    echo "Input TIFF not found: ${INPUT_TIF}" >&2
    echo "Generate it first with scripts/crop/copernicus_flow_prithvi.py" >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"

cd "${SCRIPT_DIR}"
python3 run_prithvi_local.py \
  --input "${INPUT_TIF}" \
  --output-dir "${OUTPUT_DIR}" \
  --device "${DEVICE}" \
  --save-previews
