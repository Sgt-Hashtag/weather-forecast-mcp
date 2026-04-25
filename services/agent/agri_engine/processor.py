import json
import logging
import os
from pathlib import Path

import agribound
import ee

from .utils import create_aoi_file

log = logging.getLogger("agri_engine.processor")

_INVALID_PROJECTS = {"", "your-project-id", "your-gee-project-id"}
_CREDENTIAL_CANDIDATES = (
    "/app/secrets/credentials.json",
    "/app/secrets/service-account.json",
)


def _find_credentials_file() -> str | None:
    for candidate in _CREDENTIAL_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _initialize_gee(gee_project: str | None, creds_path: str | None) -> None:
    """Initialize Earth Engine and raise with context if it fails."""
    if creds_path:
        log.info("GEE init: using service-account file at %s", creds_path)
        credentials = ee.ServiceAccountCredentials.from_service_account_file(creds_path)
        ee.Initialize(credentials=credentials)
        return

    if gee_project and gee_project.strip() not in _INVALID_PROJECTS:
        project = gee_project.strip()
        log.info("GEE init: using project '%s'", project)
        ee.Initialize(project=project)
        return

    raise RuntimeError(
        "GEE configuration missing. Provide GEE_PROJECT_ID or mount a service-account file at "
        "/app/secrets/credentials.json (or /app/secrets/service-account.json)."
    )


class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project or os.getenv("GEE_PROJECT_ID")
        self.sam_path = os.getenv("SAM_CHECKPOINT", "/app/models/sam_vit_h_4b8939.pth")

    def process_field(self, lat, lon, boundaries_path=None):
        """
        Delineate agricultural field boundaries using GEE + agribound.
        Fails fast on any error; never returns synthetic polygons.
        """
        if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
            raise ValueError(f"Invalid coordinate range: lat={lat}, lon={lon}")

        aoi_path = create_aoi_file(lat, lon)
        out_path = boundaries_path or "/tmp/field_boundaries.geojson"
        out_file = Path(out_path)
        gee_project = (self.gee_project or os.getenv("GEE_PROJECT_ID", "")).strip()
        creds_found = _find_credentials_file()

        log.info("Field delineation requested for lat=%s lon=%s", lat, lon)
        log.info("AOI path: %s", aoi_path)
        log.info("Output path: %s", out_path)
        log.info("GEE project: %s", gee_project or "<unset>")
        log.info("Credentials file: %s", creds_found or "<none>")
        log.info("SAM checkpoint path: %s (exists=%s)", self.sam_path, os.path.exists(self.sam_path))

        # Prevent stale file confusion from previous runs.
        if out_file.exists():
            log.info("Removing existing output before delineation: %s", out_path)
            out_file.unlink()

        try:
            _initialize_gee(gee_project, creds_found)
            log.info("GEE initialization successful")
        except Exception as e:
            log.exception("GEE initialization failed")
            raise RuntimeError(f"GEE initialization failed: {e}") from e

        try:
            log.info("Calling agribound.delineate(...)")
            agribound.delineate(
                study_area=aoi_path,
                source="sentinel2",
                year=2026,
                engine="delineate-anything",
                engine_params={"checkpoint": self.sam_path},
                gee_project=gee_project or None,
                output_path=out_path,
            )
            log.info("agribound.delineate completed")
        except Exception as e:
            log.exception("agribound delineation failed")
            raise RuntimeError(f"agribound delineation failed: {e}") from e

        if not out_file.exists():
            raise RuntimeError(f"Delineation completed without creating output file: {out_path}")
        if out_file.stat().st_size == 0:
            raise RuntimeError(f"Delineation created an empty output file: {out_path}")

        try:
            import geopandas as gpd

            gdf = gpd.read_file(out_path)
            log.info("Loaded output geodataframe with %d features", len(gdf))
            return {
                "status": "Success",
                "bounds_file": out_path,
                "field_count": len(gdf),
                "bounds_geojson": json.loads(gdf.to_json()),
                "source": "gee",
            }
        except Exception as e:
            log.exception("Failed to load delineation output as GeoDataFrame")
            raise RuntimeError(f"Failed reading delineation output ({out_path}): {e}") from e
