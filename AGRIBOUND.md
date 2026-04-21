# Agribound: End-to-End Field Delineation Pipeline

This document explains exactly how **agribound** is integrated into this project — from a user's GPS coordinates to a GeoPackage file of detected agricultural field boundaries.

---

## What Problem This Solves

Given a latitude/longitude point (e.g. a farmer's location), the system automatically:

1. Fetches satellite imagery of the surrounding area from **Google Earth Engine**
2. Runs **AI-based segmentation** (SAM — Segment Anything Model) on the image
3. Returns **geographic polygons** of each detected field boundary

No manual digitising. No GIS expertise needed.

---

## High-Level Architecture

```
User Query (lat, lon)
        │
        ▼
┌───────────────────┐
│   FastAPI Server  │  main.py
│   /query endpoint │
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│   WeatherAgent    │  agent.py
│   (Gemini LLM)    │
└────────┬──────────┘
         │  calls MCP tool
         ▼
┌───────────────────────┐
│  get_field_analysis() │  agri_engine/tools/agri_analysis.py
│  (MCP Tool)           │
└────────┬──────────────┘
         │
         ▼
┌───────────────────────┐
│   AgriProcessor       │  agri_engine/processor.py
│   .process_field()    │
└────────┬──────────────┘
         │
         ├──► create_aoi_file()        → temp_aoi.geojson
         │    agri_engine/utils.py
         │
         └──► agribound.delineate()
                    │
                    ▼
         ┌──────────────────────┐
         │  Google Earth Engine │
         │  Sentinel-2 imagery  │
         │  (median composite)  │
         └──────────┬───────────┘
                    │
                    ▼
         ┌──────────────────────┐
         │  SAM Segmentation    │
         │  (delineate-anything)│
         │  via samgeo          │
         └──────────┬───────────┘
                    │
                    ▼
              field_results.gpkg
         (GeoPackage with field polygons)
```

---

## Step-by-Step Breakdown

### Step 1 — MCP Tool receives coordinates

The entry point is an MCP-registered tool that the Gemini agent calls when a farming-related query is detected.

**File:** `services/agent/agri_engine/tools/agri_analysis.py`

```python
from mcp.server.fastmcp import FastMCP
from ..processor import AgriProcessor
import os

processor = AgriProcessor(gee_project=os.getenv("GEE_PROJECT_ID"))

def register_agri_tools(mcp_server: FastMCP):

    @mcp_server.tool()
    def get_field_analysis(lat: float, lon: float) -> str:
        """
        Delineates field boundaries and analyzes crops using Agribound.
        """
        try:
            result = processor.process_field(lat, lon)
            return (f"Field analysis successful. Found {result['field_count']} fields. "
                    f"Boundaries exported to: {result['boundaries_file']}")
        except Exception as e:
            return f"Analysis failed: {str(e)}"
```

The `GEE_PROJECT_ID` environment variable (e.g. `mewa-493916`) is injected at startup. This is your Google Earth Engine project — agribound uses it to authenticate API calls to GEE.

---

### Step 2 — Build the Area of Interest (AOI)

Before fetching any imagery, agribound needs to know **what area** to look at. The `create_aoi_file()` utility converts a single lat/lon point into a bounding box polygon saved as a GeoJSON file.

**File:** `services/agent/agri_engine/utils.py`

```python
def create_aoi_file(lat, lon, output_path="temp_aoi.geojson", buffer=0.01):
    bbox = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon - buffer, lat - buffer],
                    [lon + buffer, lat - buffer],
                    [lon + buffer, lat + buffer],
                    [lon - buffer, lat + buffer],
                    [lon - buffer, lat - buffer]   # closed ring
                ]]
            },
            "properties": {"name": "User Field"}
        }]
    }
    with open(output_path, "w") as f:
        json.dump(bbox, f)
    return output_path
```

**What `buffer=0.01` means:**

`0.01` degrees ≈ 1.1 km at the equator. So the bounding box is roughly a **2.2 km × 2.2 km** square centred on the user's coordinates.

```
          lon-0.01          lon+0.01
              │                  │
lat+0.01  ───┌──────────────────┐
              │                  │
              │   ~2.2km × 2.2km │
              │      AOI box     │
              │        ★         │  ← user's lat/lon
              │                  │
lat-0.01  ───└──────────────────┘
```

Output: `temp_aoi.geojson` — a standard GeoJSON file that agribound reads as `study_area`.

---

### Step 3 — Fetch Sentinel-2 imagery from Google Earth Engine

**File:** `services/agent/agri_engine/processor.py`

```python
import agribound
from .utils import create_aoi_file

class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project

    def process_field(self, lat, lon):
        aoi_path = create_aoi_file(lat, lon)
        output_path = "field_results.gpkg"

        gdf = agribound.delineate(
            study_area=aoi_path,          # GeoJSON bounding box
            source="sentinel2",           # satellite data source
            year=2026,                    # imagery year
            engine="delineate-anything",  # SAM-based segmentation
            output_path=output_path,      # where to save results
            gee_project=self.gee_project, # GEE auth project ID
            composite_method="median",    # how to merge images
            min_area=100                  # ignore tiny segments (pixels)
        )

        return {
            "status": "Success",
            "boundaries_file": output_path,
            "field_count": len(gdf)
        }
```

**What each parameter does:**

| Parameter | Value | Explanation |
|---|---|---|
| `study_area` | `temp_aoi.geojson` | The bounding box polygon agribound will clip imagery to |
| `source` | `"sentinel2"` | Uses ESA Sentinel-2 multispectral satellite (10m resolution) |
| `year` | `2026` | Filters the image collection to this year |
| `composite_method` | `"median"` | Merges all available images using median pixel values — eliminates clouds and noise |
| `engine` | `"delineate-anything"` | Uses SAM (Segment Anything Model) for boundary detection |
| `gee_project` | env var | Your GEE project used for authentication |
| `min_area` | `100` | Ignores segments smaller than 100 pixels — removes noise |
| `output_path` | `field_results.gpkg` | Saves the detected boundaries as a GeoPackage file |

---

### Step 4 — What GEE does internally

When agribound calls into Google Earth Engine, the following happens on GEE's servers:

```
GEE Image Collection: COPERNICUS/S2_SR_HARMONIZED
        │
        ├── Filter by: date range (year 2026)
        ├── Filter by: AOI bounding box
        ├── Filter by: cloud cover < threshold
        │
        ▼
   Stack of cloud-free Sentinel-2 images
        │
        ▼
   Median Composite
   (for each pixel, take the median value
    across all images in the stack)
        │
        ▼
   Single clean image of the study area
   (RGB + near-infrared bands, 10m/pixel)
```

**Why median composite?**

A single satellite pass may have clouds, shadows, or sensor noise. By stacking many observations and taking the median, those anomalies are statistically eliminated — leaving a clean, representative image of the land surface.

```
Image 1:  [120, 140, 80,  CLOUD, 95 ]
Image 2:  [118, 142, 79,  130,   CLOUD]
Image 3:  [122, 139, 81,  128,   97 ]
Image 4:  [119, 141, 80,  132,   96 ]
                               ↓
Median:   [119, 141, 80,  130,   96 ]   ← clouds removed
```

---

### Step 5 — SAM Segmentation via samgeo

With the clean Sentinel-2 image prepared, agribound feeds it through **SAM (Segment Anything Model)** using the `samgeo` library.

```
Sentinel-2 RGB Composite (GEE output)
        │
        ▼
┌───────────────────────────────┐
│   SAM — Segment Anything Model│
│   (Meta AI foundation model)  │
│                               │
│  Detects boundaries between   │
│  spectrally distinct regions  │
│  (fields, roads, water, trees)│
└───────────────┬───────────────┘
                │
                ▼
   Binary mask per segment
        │
        ▼
   Vectorise masks → Polygons
        │
        ▼
   Filter by min_area (100px)
        │
        ▼
   GeoDataFrame (gdf)
   — one row per detected field
   — geometry column = polygon with real GPS coordinates
```

SAM doesn't need to be told what a "field" looks like. It finds boundaries wherever pixels change meaningfully — field edges, hedgerows, roads, water. The `min_area=100` filter then removes fragments too small to be real fields.

---

### Step 6 — Output: GeoPackage file

The result `gdf` is a **GeoPandas GeoDataFrame** — a table where each row is a detected field, with its polygon stored as a geometry column with real-world GPS coordinates.

It is saved to `field_results.gpkg` (GeoPackage format — an SQLite-based standard for geospatial vector data).

```
field_results.gpkg
┌─────┬──────────────┬─────────────────────────────────────┐
│  id │   area_m2    │              geometry               │
├─────┼──────────────┼─────────────────────────────────────┤
│   1 │     12450    │  POLYGON((90.123 23.456, 90.124 ...))│
│   2 │      8900    │  POLYGON((90.131 23.461, 90.132 ...))│
│   3 │     21000    │  POLYGON((90.115 23.452, ...))       │
│  ...│     ...      │  ...                                 │
└─────┴──────────────┴─────────────────────────────────────┘
```

This file can be opened in QGIS, ArcGIS, or any GIS tool to visualise the detected field boundaries overlaid on a satellite basemap.

---

## Dependencies

```
agribound[gee,samgeo]
```

| Extra | Libraries pulled in | Purpose |
|---|---|---|
| `gee` | `earthengine-api` | Authenticates with and queries Google Earth Engine |
| `samgeo` | `segment-geospatial`, `leafmap`, `samgeo` | Runs SAM on geospatial imagery, converts masks to polygons |

Supporting libraries used for geometry operations:

```
shapely==2.0.3    — geometry objects (polygons, buffers)
pyproj==3.6.1     — coordinate reference system transformations
geojson==3.1.0    — reading/writing GeoJSON files
```

---

## Environment Configuration

One environment variable is required:

```env
GEE_PROJECT_ID=your-gee-project-id
```

This must be a valid Google Earth Engine project that your service account or application default credentials have access to. Agribound handles the authentication internally using Google's default credential chain (works with service account keys or workload identity in GCP).

---

## Data Flow Summary

```
lat=23.456, lon=90.123
        │
        ▼
create_aoi_file()
→ temp_aoi.geojson
  (2.2km × 2.2km bounding box)
        │
        ▼
agribound.delineate()
  → GEE: fetch Sentinel-2, year=2026, median composite
  → SAM: segment the image, vectorise masks
  → filter: remove segments < 100px
        │
        ▼
field_results.gpkg
  (N field polygons with GPS coordinates)
        │
        ▼
MCP tool response:
  "Found 14 fields. Boundaries exported to: field_results.gpkg"
```

---

## File Structure

```
services/agent/
├── agri_engine/
│   ├── processor.py          ← calls agribound.delineate()
│   ├── utils.py              ← builds the AOI GeoJSON
│   └── tools/
│       └── agri_analysis.py  ← MCP tool (entry point)
├── agent.py                  ← Gemini agent (decides when to call the tool)
├── main.py                   ← FastAPI server
└── requirements.txt          ← agribound[gee,samgeo] + deps
```
