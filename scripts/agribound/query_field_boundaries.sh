#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/results"
OUTPUT_FILE="${OUTPUT_DIR}/result.json"

mkdir -p "${OUTPUT_DIR}"

curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{"query":"Delineate field boundaries at latitude 30.9 longitude 75.5"}' \
  -o "${OUTPUT_FILE}"

echo "Saved response to ${OUTPUT_FILE}"
