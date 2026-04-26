import json
import logging
import os
import shutil
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
    """Initialize Earth Engine and return credentials (or None) plus resolved project."""
    if creds_path:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        client_email = data.get("client_email")
        if not client_email:
            raise RuntimeError(f"Missing client_email in credentials file: {creds_path}")
        log.info("GEE init: using service-account file at %s", creds_path)
        # earthengine-api compatible path for this environment
        credentials = ee.ServiceAccountCredentials(client_email, creds_path)
        resolved_project = gee_project or data.get("project_id")
        ee.Initialize(credentials=credentials, project=(resolved_project or None))
        return credentials, resolved_project

    if gee_project and gee_project.strip() not in _INVALID_PROJECTS:
        project = gee_project.strip()
        log.info("GEE init: using ambient credentials with project '%s'", project)
        ee.Initialize(project=project)
        return None, project

    raise RuntimeError(
        "GEE configuration missing. Provide /app/secrets/credentials.json "
        "(or /app/secrets/service-account.json)."
    )


def _patch_agribound_setup_gee(credentials, gee_project: str | None) -> None:
    """
    Force agribound internals to reuse already-initialized EE credentials.

    Some agribound versions call ee.Authenticate() internally when setup_gee fails.
    This patch avoids that path (which requires gcloud in container).
    """

    def _setup_gee_override(project=None):
        ee.Initialize(credentials=credentials, project=(project or gee_project or None))

    patched = 0
    try:
        import agribound.auth as ag_auth

        ag_auth.setup_gee = _setup_gee_override
        patched += 1
    except Exception as e:
        log.warning("Could not patch agribound.auth.setup_gee: %s", e)

    try:
        import agribound.composites.gee as ag_gee

        ag_gee.setup_gee = _setup_gee_override
        patched += 1
    except Exception as e:
        log.warning("Could not patch agribound.composites.gee.setup_gee: %s", e)

    if patched:
        log.info("Patched agribound setup_gee in %d module(s)", patched)
    else:
        log.warning("No agribound setup_gee patch points found")


_COMPOSITE_SRC = Path("/tmp/.agribound_cache/sentinel2_2026_composite.tif")
_PATCHES_ROOT = Path("/tmp/patches")


def _save_composite_snapshot(lat, lon) -> None:
    import numpy as np
    import rasterio

    if not _COMPOSITE_SRC.exists():
        log.warning("Composite not found at %s; skipping snapshot", _COMPOSITE_SRC)
        return
    dest_dir = _PATCHES_ROOT / f"{float(lat):.6f}_{float(lon):.6f}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_COMPOSITE_SRC, dest_dir / "sentinel2_composite.tif")
    with rasterio.open(_COMPOSITE_SRC) as src:
        arr = src.read()
    np.save(dest_dir / "sentinel2_composite.npy", arr)
    log.info("Composite snapshot saved to %s (shape=%s)", dest_dir, arr.shape)


def _resolve_device() -> str:
    val = os.getenv("GPU_INFERENCE", "false").strip().lower()
    return "auto" if val in ("1", "true", "yes") else "cpu"


class AgriProcessor:
    def __init__(self, gee_project=None):
        self.gee_project = gee_project or os.getenv("GEE_PROJECT_ID")
        self.sam_path = os.getenv("SAM_CHECKPOINT", "/app/models/sam_vit_h_4b8939.pth")
        self.device = _resolve_device()
        log.info("Inference device: %s (GPU_INFERENCE=%s)", self.device, os.getenv("GPU_INFERENCE", "false"))

    def process_field(self, lat, lon, boundaries_path=None):
        """
        Delineate agricultural field boundaries using GEE + agribound.
        Fails fast on any error; never returns synthetic polygons.
        """
        if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
            raise ValueError(f"Invalid coordinate range: lat={lat}, lon={lon}")

        aoi_path = create_aoi_file(lat, lon)
        boundaries_out = boundaries_path or "/tmp/field_boundaries.geojson"
        classified_out = "/tmp/classified_fields.geojson"
        bounds_file = Path(boundaries_out)
        class_file = Path(classified_out)
        gee_project = (self.gee_project or os.getenv("GEE_PROJECT_ID", "")).strip()
        creds_found = _find_credentials_file()

        log.info("Field delineation requested for lat=%s lon=%s", lat, lon)
        log.info("AOI path: %s", aoi_path)
        log.info("Boundaries output path: %s", boundaries_out)
        log.info("Classification output path: %s", classified_out)
        log.info("GEE project: %s", gee_project or "<unset>")
        log.info("Credentials file: %s", creds_found or "<none>")
        log.info("SAM checkpoint path: %s (exists=%s)", self.sam_path, os.path.exists(self.sam_path))

        for f in (bounds_file, class_file):
            if f.exists():
                log.info("Removing existing output before run: %s", f)
                f.unlink()

        agribound_cache = Path("/tmp/.agribound_cache")
        if agribound_cache.exists():
            shutil.rmtree(agribound_cache)
            log.info("Cleared agribound cache for fresh run")

        try:
            credentials, resolved_project = _initialize_gee(gee_project, creds_found)
            gee_project = (resolved_project or gee_project or "").strip()
            if credentials is not None:
                _patch_agribound_setup_gee(credentials, gee_project)
            log.info("GEE initialization successful")
        except Exception as e:
            log.exception("GEE initialization failed")
            raise RuntimeError(f"GEE initialization failed: {e}") from e

        try:
            log.info("Step 1/2: agribound.delineate engine=delineate-anything")
            agribound.delineate(
                study_area=aoi_path,
                source="sentinel2",
                year=2026,
                engine="delineate-anything",
                engine_params={},
                n_workers=0,
                device=self.device,
                gee_project=gee_project or None,
                output_path=boundaries_out,
            )
            log.info("Step 1/2 completed with engine=delineate-anything")
            _save_composite_snapshot(lat, lon)
        except Exception as e:
            msg = str(e)
            needs_ftw_dev = "ftw-tools dev version is required" in msg
            if needs_ftw_dev:
                log.warning(
                    "delineate-anything unavailable (%s). Retrying with engine=samgeo.",
                    msg,
                )
                try:
                    agribound.delineate(
                        study_area=aoi_path,
                        source="sentinel2",
                        year=2026,
                        engine="samgeo",
                        engine_params={"checkpoint": self.sam_path},
                        n_workers=0,
                        device=self.device,
                        gee_project=gee_project or None,
                        output_path=boundaries_out,
                    )
                    log.info("Step 1/2 completed with engine=samgeo fallback")
                    _save_composite_snapshot(lat, lon)
                except Exception as samgeo_err:
                    log.exception("Boundary delineation failed with samgeo fallback")
                    raise RuntimeError(
                        f"Boundary delineation failed (delineate-anything unavailable; "
                        f"samgeo fallback also failed): {samgeo_err}"
                    ) from samgeo_err
            else:
                log.exception("Boundary delineation failed")
                raise RuntimeError(f"Boundary delineation failed: {e}") from e

        if not bounds_file.exists():
            raise RuntimeError(f"Delineation completed without output file: {boundaries_out}")
        if bounds_file.stat().st_size == 0:
            raise RuntimeError(f"Delineation created empty output file: {boundaries_out}")

        try:
            import geopandas as gpd

            try:
                log.info("Step 2/2: agribound.delineate engine=ftw")
                agribound.delineate(
                    study_area=boundaries_out,
                    source="sentinel2",
                    year=2026,
                    engine="ftw",
                    n_workers=0,
                    device=self.device,
                    gee_project=gee_project or None,
                    output_path=classified_out,
                )
                if class_file.exists() and class_file.stat().st_size > 0:
                    gdf = gpd.read_file(classified_out)
                    log.info("Loaded classified output with %d features", len(gdf))
                    crop_info = []
                    for _, row in gdf.iterrows():
                        props = row.to_dict()
                        crop_info.append(
                            {
                                "crop": props.get("crop_type", props.get("predicted_crop", "Unknown")),
                                "confidence": props.get("confidence", props.get("score", 0.8)),
                            }
                        )
                    return {
                        "status": "Success",
                        "bounds_file": boundaries_out,
                        "field_count": len(gdf),
                        "bounds_geojson": json.loads(gdf.to_json()),
                        "source": "gee",
                        "crop_classification": crop_info,
                    }
                log.warning("FTW output missing/empty; returning boundary-only output")
            except Exception as ftw_err:
                log.warning("FTW classification failed; returning boundary-only output: %s", ftw_err)

            gdf = gpd.read_file(boundaries_out)
            log.info("Loaded boundary output with %d features", len(gdf))
            return {
                "status": "Success",
                "bounds_file": boundaries_out,
                "field_count": len(gdf),
                "bounds_geojson": json.loads(gdf.to_json()),
                "source": "gee",
                "crop_classification": None,
                "note": "FTW classification unavailable",
            }
        except Exception as e:
            log.exception("Failed reading output files")
            raise RuntimeError(f"Failed reading agribound output: {e}") from e
