# Agribound Field Delineation — FTW Integration & Fixes

## What is ftw-tools?

`ftw-tools` comes from the **Fields of the World (FTW)** project (`fieldsoftheworld/ftw-baselines` on GitHub). It provides instance segmentation inference for agricultural field boundary detection, wrapping the **DelineateAnything** model with Sentinel-2 specific preprocessing (band normalization at `/3000`, MPS support, RGB reordering).

Within agribound, the `delineate-anything` engine has two internal paths for Sentinel-2:
- `_delineate_via_ftw` — calls `ftw_tools.inference.inference.run_instance_segmentation`, which is the intended path for Sentinel-2
- `_delineate_standalone` — fallback for other sensors (NAIP, Landsat, etc.) using the Delineate-Anything repo directly

The code selects the path based on whether `checkpoint_path` is set in `engine_params`:

```python
checkpoint = config.engine_params.get("checkpoint_path")
if config.source == "sentinel2" and not checkpoint:
    return self._delineate_via_ftw(raster_path, config)
return self._delineate_standalone(raster_path, config)
```

---

## What Was Broken and Why

### 1. Wrong `engine_params` key (`checkpoint` vs `checkpoint_path`)

**File:** `agri_engine/processor.py`

The original call passed `engine_params={"checkpoint": self.sam_path}`. The agribound engine checks for `"checkpoint_path"`, not `"checkpoint"`. Because the key didn't match, `checkpoint` evaluated to `None`, and the code fell into `_delineate_via_ftw` — which needs `ftw-tools` (not installed at the time). That caused:

```
ImportError: ftw-tools dev version is required for Sentinel-2 instance segmentation.
```

**Fix:** Removed the SAM checkpoint from `engine_params` entirely (`engine_params={}`), since `_delineate_via_ftw` doesn't use a custom checkpoint — it uses the DelineateAnything model via FTW's own inference pipeline.

### 2. SAM v1 checkpoint was irrelevant

The Dockerfile was downloading `sam_vit_h_4b8939.pth` (Meta's SAM ViT-H, ~2.5 GB). This is a **SAM v1** checkpoint. It is not used by:
- `delineate-anything` engine (uses YOLO-based DelineateAnything weights)
- `ftw-tools` (uses its own model via `run_instance_segmentation`)
- `samgeo_engine.py` (uses SAM **v2** from HuggingFace, not v1)

The checkpoint was passed under the wrong key anyway and served no purpose in this pipeline.

### 3. `samgeo` was not a valid engine name

The fallback in `processor.py` retried with `engine="samgeo"`. This is not a registered agribound engine. Valid engines are: `delineate-anything`, `ftw`, `geoai`, `dinov3`, `prithvi`, `embedding`, `ensemble`. `samgeo_engine.py` is a **post-processing boundary refinement step**, not a standalone delineation engine.

### 4. `ftw-tools` not installed in container

**File:** `Dockerfile`

`ftw-tools` is not published on PyPI. It must be installed from the GitHub repo. `git` was also missing from the container.

**Fix:**
```dockerfile
# added git to apt-get
RUN apt-get install -y ... git ...

# added ftw-tools install
RUN pip install --no-cache-dir git+https://github.com/fieldsoftheworld/ftw-baselines.git
```

### 5. `opencv-python` (GUI) conflicting with headless environment

`ultralytics` (pulled in by agribound) installs `opencv-python` (the GUI variant), which requires `libxcb.so.1` — a display library not present in the container. `opencv-python-headless` was already in `requirements-geo.txt` but got overridden.

**Fix:**
```dockerfile
RUN pip install --no-cache-dir --force-reinstall opencv-python-headless
```

### 6. AOI too small for FTW patch size

**File:** `agri_engine/utils.py`

The default buffer of `0.01°` created a bounding box of ~2km × 2km, yielding a ~200×200px image at Sentinel-2's 10m resolution. FTW's default patch size is 256px, causing:

```
Patch size must not be larger than the input image dimensions.
```

**Fix:** Increased buffer from `0.01` to `0.02` degrees → ~4km × 4km → ~400×400px at 10m.

---

## Summary of All Changes

| File | Change |
|------|--------|
| `Dockerfile` | Added `git` to apt-get |
| `Dockerfile` | Added `pip install git+https://github.com/fieldsoftheworld/ftw-baselines.git` |
| `Dockerfile` | Added `pip install --force-reinstall opencv-python-headless` |
| `agri_engine/processor.py` | Changed `engine_params={"checkpoint": self.sam_path}` → `engine_params={}` |
| `agri_engine/utils.py` | Increased AOI buffer from `0.01` → `0.02` degrees |
