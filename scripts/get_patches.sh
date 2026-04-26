#!/usr/bin/env bash
# Copy Sentinel-2 composite patches from the agent container to local disk.
# Usage: ./script/get_patches.sh [output_dir]
#   output_dir defaults to <repo>/patches

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTAINER="${CONTAINER:-weather-forecast-mcp-agent-1}"
OUTPUT="${1:-${REPO_ROOT}/patches}"

if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER}$"; then
    echo "Container '${CONTAINER}' is not running." >&2
    exit 1
fi

# Check if any patches exist yet
COUNT=$(docker exec "$CONTAINER" sh -c 'find /tmp/patches -name "*.tif" -o -name "*.npy" 2>/dev/null | wc -l')
if [ "$COUNT" -eq 0 ]; then
    echo "No patches found in container yet. Run a field delineation query first."
    exit 0
fi

mkdir -p "$OUTPUT"
docker cp "${CONTAINER}:/tmp/patches/." "$OUTPUT/"
echo "Copied ${COUNT} patch file(s) to ${OUTPUT}/"
echo
echo "Files:"
find "$OUTPUT" -name "*.tif" -o -name "*.npy" | sort | sed 's/^/  /'
