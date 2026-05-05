#!/usr/bin/env python3
"""
Run local Prithvi sen1floods11 flood segmentation on a GeoTIFF.

Default input is produced by scripts/flood/copernicus_flood_segmentation.py.

Output classes:
  0 = land / non-water
  1 = water / flood
  255 = nodata
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import time
import types
from pathlib import Path

np = None
rasterio = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "prithvi_flood_input.tif"
DEFAULT_OUTPUT = BASE_DIR / "prithvi_flood_prediction.tif"
DEFAULT_PREVIEW = BASE_DIR / "prithvi_flood_prediction_preview.png"

MODEL_REPO = "ibm-nasa-geospatial/Prithvi-EO-1.0-100M-sen1floods11"
LEGACY_MODEL_REPO = "ibm-nasa-geospatial/Prithvi-100M-sen1floods11"
CONFIG_FILE = "sen1floods11_Prithvi_100M.py"
CHECKPOINT_FILE = "sen1floods11_Prithvi_100M.pth"

MIN_CROP_SIZE = 224
NODATA_VALUE = 255


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Prithvi sen1floods11 flood segmentation locally."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input GeoTIFF.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output flood mask GeoTIFF.")
    parser.add_argument("--preview", default=str(DEFAULT_PREVIEW), help="Output PNG preview.")
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Inference device. 'auto' uses CUDA when torch can see it.",
    )
    parser.add_argument(
        "--model-repo",
        default=MODEL_REPO,
        help="Hugging Face model repository containing the config and checkpoint.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Local MMSeg config path. Overrides --model-repo config download.",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Local checkpoint path. Overrides --model-repo checkpoint download.",
    )
    parser.add_argument(
        "--config-file",
        default=CONFIG_FILE,
        help="Config filename to download from --model-repo.",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=CHECKPOINT_FILE,
        help="Checkpoint filename to download from --model-repo.",
    )
    parser.add_argument(
        "--bands",
        default=None,
        help=(
            "Comma-separated 0-based band indices to feed the model, overriding "
            "the config's bands variable and BandsExtract step. Example: 1,2,3,8,11,12"
        ),
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip PNG preview generation.",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Do not contact Hugging Face. Requires --config and --checkpoint.",
    )
    return parser.parse_args()


def require_runtime_dependencies(require_preview: bool):
    missing = []
    modules = [
        ("numpy", "numpy"),
        ("rasterio", "rasterio"),
        ("torch", "torch"),
        ("huggingface_hub", "huggingface_hub"),
        ("mmcv", "mmcv==1.7.2"),
        ("mmseg", "mmsegmentation 0.x"),
        ("geospatial_fm", "your Sgt-Hashtag/hls-foundation-os fork"),
    ]
    if require_preview:
        modules.append(("matplotlib", "matplotlib"))
    for module_name, install_hint in modules:
        try:
            __import__(module_name)
            if module_name == "mmcv":
                _install_mmcv_ops_compat()
        except ImportError:
            missing.append((module_name, install_hint))

    if not missing:
        _load_core_dependencies()
        return

    print("Missing local inference dependencies:\n")
    for module_name, install_hint in missing:
        print(f"  - {module_name}: {install_hint}")
    requirements_hint = Path(__file__).resolve().parent / "inference_requirements.txt"
    print(
        "\nThis legacy Prithvi checkpoint is an MMSegmentation/MMCV 1.x model. "
        "For Python 3.12, install the lighter mmcv 1.x package from "
        f"{requirements_hint} instead of mmcv-full."
    )
    print("\nPython 3.12 setup:")
    print("  conda create -n prithvi-flood python=3.12 -y")
    print("  conda activate prithvi-flood")
    print("  pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu")
    print("  pip install --no-build-isolation -r scripts/flood/inference_requirements.txt")
    print("\nIf only geospatial_fm is missing, install your fork directly:")
    print("  pip install --no-build-isolation git+https://github.com/Sgt-Hashtag/hls-foundation-os.git")
    sys.exit(2)


def _load_core_dependencies():
    global np, rasterio
    if np is None:
        import numpy as _np

        np = _np
    if rasterio is None:
        import rasterio as _rasterio

        rasterio = _rasterio


def _install_mmcv_ops_compat():
    """
    Python 3.12 cannot use old mmcv-full wheels, but MMSegmentation 0.x imports
    a few mmcv.ops symbols while registering every built-in head/loss. The
    Prithvi sen1floods11 config does not use these ops, so we provide import
    stubs to let model registration complete with plain mmcv.
    """
    try:
        import mmcv
    except ImportError:
        return

    try:
        import mmcv.ops  # noqa: F401
        return
    except Exception:
        pass

    ops = types.ModuleType("mmcv.ops")

    def sigmoid_focal_loss(pred, target, gamma=2.0, alpha=0.25, weight=None, reduction="mean"):
        import torch
        import torch.nn.functional as F

        pred_sigmoid = pred.sigmoid()
        target = target.type_as(pred)
        pt = (1 - pred_sigmoid) * target + pred_sigmoid * (1 - target)
        focal_weight = (alpha * target + (1 - alpha) * (1 - target)) * pt.pow(gamma)
        loss = F.binary_cross_entropy_with_logits(pred, target, reduction="none") * focal_weight
        if weight is not None:
            loss = loss * weight
        if reduction == "sum":
            return loss.sum()
        if reduction == "mean":
            return loss.mean()
        return loss

    def point_sample(input, points, align_corners=False, **kwargs):
        import torch.nn.functional as F

        if points.dim() == 3:
            points = points.unsqueeze(2)
        grid = points * 2 - 1
        return F.grid_sample(input, grid, align_corners=align_corners, **kwargs)

    class _UnavailableOp:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "This model requested an MMCV compiled op that is unavailable "
                "with plain mmcv. Install a compatible mmcv-full build or use "
                "the legacy Python 3.9 Prithvi environment."
            )

    ops.sigmoid_focal_loss = sigmoid_focal_loss
    ops.point_sample = point_sample
    ops.PSAMask = _UnavailableOp
    ops.CrissCrossAttention = _UnavailableOp
    ops.get_onnxruntime_op_path = lambda: ""

    sys.modules["mmcv.ops"] = ops
    setattr(mmcv, "ops", ops)


def download_model_files(
    model_repo: str,
    config_file: str,
    checkpoint_file: str,
    config_override: str | None = None,
    checkpoint_override: str | None = None,
    local_files_only: bool = False,
) -> tuple[str, str]:
    from huggingface_hub import hf_hub_download

    if config_override and checkpoint_override:
        config_path = Path(config_override).resolve()
        checkpoint_path = Path(checkpoint_override).resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Local config not found: {config_path}")
        if not checkpoint_path.exists():
            raise FileNotFoundError(f"Local checkpoint not found: {checkpoint_path}")
        print(f"Using local config: {config_path}")
        print(f"Using local checkpoint: {checkpoint_path}")
        return str(config_path), str(checkpoint_path)

    if bool(config_override) != bool(checkpoint_override):
        raise ValueError("Provide both --config and --checkpoint together, or neither.")

    if local_files_only:
        raise ValueError("--local-files-only requires both --config and --checkpoint.")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    errors = []
    for repo in [model_repo, LEGACY_MODEL_REPO]:
        try:
            print(f"Downloading config from Hugging Face repo: {repo}")
            config_path = hf_hub_download(repo_id=repo, filename=config_file, token=token)
            print("Config ready.")
            print("Downloading checkpoint from Hugging Face. This file is large and may take a while...")
            checkpoint_path = hf_hub_download(repo_id=repo, filename=checkpoint_file, token=token)
            print(f"Using model files from: {repo}")
            return config_path, checkpoint_path
        except Exception as exc:  # pragma: no cover - depends on network/HF cache.
            errors.append(f"{repo}: {exc}")

    raise RuntimeError("Could not download model files:\n" + "\n".join(errors))


def choose_device(device_arg: str) -> str:
    import torch

    if device_arg == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device_arg == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested, but torch.cuda.is_available() is False.")
    return device_arg


def build_model(config_path: str, checkpoint_path: str, device: str, bands_override: list[int] | None = None):
    from mmcv import Config
    from mmseg.apis import init_segmentor
    import torch

    config = Config.fromfile(config_path)
    config.model.backbone.pretrained = None
    if bands_override is not None:
        _override_config_bands(config, bands_override)
    model = _init_segmentor_with_legacy_torch_load(config, checkpoint_path, device, init_segmentor, torch)
    model.cfg.data.test.pipeline = _process_test_pipeline(model.cfg.data.test.pipeline)
    return model


def _parse_bands(bands: str | None) -> list[int] | None:
    if not bands:
        return None
    parsed = [int(part.strip()) for part in bands.split(",") if part.strip()]
    if not parsed:
        raise ValueError("--bands was provided but no band indices were parsed.")
    return parsed


def _override_config_bands(config, bands: list[int]):
    print(f"Overriding config bands with 0-based indices: {bands}")
    config.bands = bands
    for pipeline_name in ["train_pipeline", "test_pipeline"]:
        if hasattr(config, pipeline_name):
            _override_pipeline_bands(getattr(config, pipeline_name), bands)
    for split in ["train", "val", "test"]:
        try:
            pipeline = config.data[split].pipeline
        except Exception:
            continue
        _override_pipeline_bands(pipeline, bands)


def _override_pipeline_bands(pipeline, bands: list[int]):
    for step in pipeline:
        if step.get("type") == "BandsExtract":
            step["bands"] = bands
        if step.get("type") == "Reshape":
            new_shape = list(step["new_shape"])
            new_shape[0] = len(bands)
            step["new_shape"] = tuple(new_shape)


def _init_segmentor_with_legacy_torch_load(config, checkpoint_path, device, init_segmentor, torch_module):
    """
    PyTorch 2.6 changed torch.load's default to weights_only=True. Old MMCV
    checkpoints can include numpy scalar metadata, so MMSeg/MMCV need the old
    behavior for trusted local checkpoints.
    """
    original_torch_load = torch_module.load

    def torch_load_legacy(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return original_torch_load(*args, **kwargs)

    torch_module.load = torch_load_legacy
    try:
        return init_segmentor(config, checkpoint_path, device=device)
    finally:
        torch_module.load = original_torch_load


def _process_test_pipeline(pipeline):
    pipeline = copy.deepcopy(pipeline)
    collect_index = [i for i, step in enumerate(pipeline) if "Collect" in step.get("type", "")]
    if collect_index:
        pipeline[collect_index[0]]["meta_keys"] = [
            "img_info",
            "filename",
            "ori_filename",
            "img",
            "img_shape",
            "ori_shape",
            "pad_shape",
            "scale_factor",
            "img_norm_cfg",
        ]
    return pipeline


def ensure_minimum_size(input_path: Path) -> tuple[Path, dict]:
    """Pad bottom/right to 224x224 for very small AOIs, preserving the top-left transform."""
    with rasterio.open(input_path) as src:
        profile = src.profile.copy()
        data = src.read()
        original = {
            "height": src.height,
            "width": src.width,
            "profile": profile,
            "nodata": src.nodata,
        }

    height, width = data.shape[1:]
    if height >= MIN_CROP_SIZE and width >= MIN_CROP_SIZE:
        return input_path, original

    padded_height = max(height, MIN_CROP_SIZE)
    padded_width = max(width, MIN_CROP_SIZE)
    fill_value = profile.get("nodata")
    if fill_value is None:
        fill_value = -9999.0
        profile["nodata"] = fill_value

    padded = np.full(
        (data.shape[0], padded_height, padded_width),
        fill_value,
        dtype=data.dtype,
    )
    padded[:, :height, :width] = data
    profile.update(height=padded_height, width=padded_width)

    temp_dir = Path(tempfile.mkdtemp(prefix="prithvi_flood_"))
    padded_path = temp_dir / f"{input_path.stem}_padded.tif"
    with rasterio.open(padded_path, "w", **profile) as dst:
        dst.write(padded)
        for band_idx, description in enumerate(profile.get("descriptions") or [], start=1):
            if description:
                dst.set_band_description(band_idx, description)

    print(f"Input padded from {height}x{width} to {padded_height}x{padded_width} for 224x224 crops.")
    return padded_path, original


def run_mmseg_inference(model, image_path: Path):
    import torch
    from mmcv.parallel import collate, scatter
    from mmseg.datasets.pipelines import Compose

    test_pipeline = Compose(model.cfg.data.test.pipeline)
    data = test_pipeline(
        {
            "img_info": {"filename": str(image_path)},
            "img_prefix": None,
            "seg_prefix": None,
            "seg_fields": [],
        }
    )
    data = collate([data], samples_per_gpu=1)

    if next(model.parameters()).is_cuda:
        data = scatter(data, [next(model.parameters()).device])[0]
    else:
        data = {
            "img": data["img"],
            "img_metas": data["img_metas"].data[0],
        }

    with torch.no_grad():
        result = model(return_loss=False, rescale=True, **data)
    return np.asarray(result[0], dtype=np.uint8)


def write_prediction(prediction: np.ndarray, input_path: Path, output_path: Path, original: dict):
    height = original["height"]
    width = original["width"]
    prediction = prediction[:height, :width]

    with rasterio.open(input_path) as src:
        input_data = src.read()[:, :height, :width]
        input_nodata = src.nodata

    if input_nodata is not None:
        nodata_mask = np.any(input_data == input_nodata, axis=0)
    else:
        nodata_mask = np.zeros((height, width), dtype=bool)

    out = prediction.copy()
    out[nodata_mask] = NODATA_VALUE

    profile = original["profile"].copy()
    profile.update(
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=rasterio.uint8,
        nodata=NODATA_VALUE,
        compress="zstd",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(out.astype(rasterio.uint8), 1)
        dst.set_band_description(1, "Prithvi sen1floods11 prediction: 0=land, 1=water, 255=nodata")

    water_pixels = int(np.sum(out == 1))
    valid_pixels = int(np.sum(out != NODATA_VALUE))
    water_pct = 100.0 * water_pixels / valid_pixels if valid_pixels else 0.0
    print(f"Prediction written: {output_path}")
    print(f"Water/flood pixels: {water_pixels}/{valid_pixels} ({water_pct:.2f}% of valid area)")
    return out


def write_preview(input_path: Path, prediction: np.ndarray, preview_path: Path):
    import matplotlib.pyplot as plt

    with rasterio.open(input_path) as src:
        data = src.read()[:, : prediction.shape[0], : prediction.shape[1]].astype(np.float32)
        nodata = src.nodata

    if data.shape[0] >= 4:
        rgb = np.stack([data[3], data[2], data[1]], axis=-1)
    elif data.shape[0] >= 3:
        rgb = np.stack([data[2], data[1], data[0]], axis=-1)
    else:
        rgb = np.repeat(data[0][..., None], 3, axis=-1)

    if nodata is not None:
        rgb[np.any(data == nodata, axis=0)] = np.nan

    valid = rgb[np.isfinite(rgb)]
    if valid.size:
        lo, hi = np.percentile(valid, [2, 98])
        if hi <= lo:
            hi = lo + 1
        rgb = np.clip((rgb - lo) / (hi - lo), 0, 1)
    else:
        rgb = np.zeros_like(rgb)
    rgb = np.nan_to_num(rgb, nan=0)

    overlay = rgb.copy()
    water = prediction == 1
    overlay[water] = overlay[water] * 0.35 + np.array([0.0, 0.65, 1.0]) * 0.65

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    axes[0].imshow(rgb)
    axes[0].set_title("Input RGB")
    axes[0].axis("off")
    axes[1].imshow(overlay)
    axes[1].set_title("Flood/Water Overlay")
    axes[1].axis("off")
    plt.tight_layout()
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(preview_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Preview written: {preview_path}")


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    preview_path = Path(args.preview)

    if not input_path.exists():
        print(f"Input TIFF not found: {input_path}", file=sys.stderr)
        print("Run scripts/flood/copernicus_flood_segmentation.py first.", file=sys.stderr)
        return 1

    require_runtime_dependencies(require_preview=not args.no_preview)
    device = choose_device(args.device)
    print(f"Running on device: {device}")

    bands_override = _parse_bands(args.bands)

    try:
        config_path, checkpoint_path = download_model_files(
            args.model_repo,
            args.config_file,
            args.checkpoint_file,
            config_override=args.config,
            checkpoint_override=args.checkpoint,
            local_files_only=args.local_files_only,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    model = build_model(config_path, checkpoint_path, device, bands_override=bands_override)

    inference_input, original = ensure_minimum_size(input_path)

    start = time.time()
    prediction = run_mmseg_inference(model, inference_input)
    print(f"Raw model output shape: {prediction.shape}")

    output_mask = write_prediction(prediction, inference_input, output_path, original)
    if not args.no_preview:
        write_preview(inference_input, output_mask, preview_path)

    print(f"Inference completed in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
