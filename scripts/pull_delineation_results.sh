#!/usr/bin/env bash
set -euo pipefail

# Pull delineation outputs from the running Docker Compose agent container
# into the local repo so they are easy to inspect.

SERVICE_NAME="${1:-agent}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONTAINER_GEOJSON="/tmp/field_boundaries.geojson"
CONTAINER_HTML="/tmp/field_boundaries_preview.html"

LOCAL_GEOJSON="${REPO_ROOT}/field_boundaries.geojson"
LOCAL_HTML="${REPO_ROOT}/field_boundaries_preview.html"

cd "${REPO_ROOT}"

echo "[1/3] Copying GeoJSON from ${SERVICE_NAME}:${CONTAINER_GEOJSON} ..."
docker compose cp "${SERVICE_NAME}:${CONTAINER_GEOJSON}" "${LOCAL_GEOJSON}"

echo "[2/3] Copying HTML preview from ${SERVICE_NAME}:${CONTAINER_HTML} ..."
if docker compose cp "${SERVICE_NAME}:${CONTAINER_HTML}" "${LOCAL_HTML}" 2>/dev/null; then
  echo "Copied HTML preview from container."
else
  echo "Container preview HTML not found. Generating local preview HTML from GeoJSON instead..."
  python3 - <<'PY'
from pathlib import Path
import json

repo = Path.cwd()
geojson_path = repo / "field_boundaries.geojson"
html_path = repo / "field_boundaries_preview.html"

data = json.loads(geojson_path.read_text(encoding="utf-8"))

html = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Field Boundaries Preview</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    html, body {{ margin: 0; height: 100%; }}
    #map {{ width: 100%; height: 100%; }}
    .title {{
      position: absolute;
      top: 10px;
      left: 10px;
      z-index: 1000;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid #d0d0d0;
      border-radius: 8px;
      padding: 8px 10px;
      font-family: Arial, sans-serif;
      font-size: 14px;
      line-height: 1.3;
    }}
    .poly-label {{
      font: 600 12px/1.2 Arial, sans-serif;
      color: #111;
      text-shadow: 0 0 2px #fff, 0 0 4px #fff;
      white-space: nowrap;
    }}
  </style>
</head>
<body>
  <div class="title"><strong>Field Segmentation Result</strong><br/>Satellite basemap + polygon labels</div>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const geojson = {json.dumps(data)};
    const map = L.map('map', {{ zoomControl: true }});
    const satellite = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
      {{ maxZoom: 20, attribution: '&copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community' }}
    ).addTo(map);

    const layer = L.geoJSON(geojson, {{
      style: {{ color: '#ffae00', weight: 2, fillColor: '#ffd166', fillOpacity: 0.25 }},
      onEachFeature: (feature, l) => {{
        const p = feature.properties || {{}};
        const field = p.field_id || 'unknown';
        const crop = p.crop_type || 'unknown';
        const conf = (typeof p.confidence === 'number') ? p.confidence : 'n/a';
        const area = (typeof p.area_ha === 'number') ? p.area_ha : 'n/a';
        l.bindTooltip(`${{field}} (${{crop}})`, {{ permanent: true, direction: 'center', className: 'poly-label', opacity: 0.95 }});
        l.bindPopup(`<b>${{field}}</b><br/>Crop: ${{crop}}<br/>Area (ha): ${{area}}<br/>Confidence: ${{conf}}`);
      }}
    }}).addTo(map);

    map.fitBounds(layer.getBounds(), {{ padding: [20, 20] }});
    L.control.layers({{ 'Satellite (Esri)': satellite }}, {{ 'Field polygons': layer }}, {{ collapsed: false }}).addTo(map);
  </script>
</body>
</html>
'''

html_path.write_text(html, encoding="utf-8")
print(f"Generated {html_path}")
PY
fi

echo "[3/3] Done"
echo "GeoJSON: ${LOCAL_GEOJSON}"
echo "HTML:    ${LOCAL_HTML}"
echo "Open preview: xdg-open ${LOCAL_HTML}"
