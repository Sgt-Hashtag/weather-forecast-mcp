#!/usr/bin/env python3
"""
Run Prithvi-EO-2.0-300M-TL-Sen1Floods11 flood segmentation locally.

Default input is produced by scripts/copernicus_flood_segmentation.py.

Output classes:
  0 = land / non-flood
  1 = flood / water
  255 = nodata
"""

from __future__ import annotations

import argparse
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
DEFAULT_OUTPUT = BASE_DIR / "prithvi2_flood_prediction.tif"
DEFAULT_PREVIEW = BASE_DIR / "prithvi2_flood_prediction_preview.png"

MODEL_REPO = os.getenv("PRITHVI2_MODEL_REPO", "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-TL-Sen1Floods11")
CONFIG_FILE = "config.yaml"
CHECKPOINT_FILE = "Prithvi-EO-V2-300M-TL-Sen1Floods11.pt"
DEFAULT_BANDS = [1, 2, 3, 8, 11, 12]
NODATA_VALUE = 255


def parse_args():
    parser = argparse.ArgumentParser(description="Run Prithvi 2.0 Sen1Floods11 inference.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input GeoTIFF.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output mask GeoTIFF.")
    parser.add_argument("--preview", default=str(DEFAULT_PREVIEW), help="Output PNG preview.")
    parser.add_argument("--model-repo", default=MODEL_REPO, help="Hugging Face model repo.")
    parser.add_argument("--config", default=None, help="Local TerraTorch config.yaml.")
    parser.add_argument("--checkpoint", default=None, help="Local model checkpoint .pt.")
    parser.add_argument(
        "--bands",
        default=",".join(str(i) for i in DEFAULT_BANDS),
        help="Comma-separated 0-based input band indices. Default maps S2 B02,B03,B04,B8A,B11,B12.",
    )
    parser.add_argument("--tile-size", type=int, default=512, help="Sliding-window tile size.")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="Inference device.")
    parser.add_argument("--no-preview", action="store_true", help="Skip PNG preview.")
    return parser.parse_args()


def require_dependencies():
    global np, rasterio
    missing = []
    for module in ["numpy", "rasterio", "torch", "yaml", "einops", "huggingface_hub", "mmcv"]:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if "mmcv" not in missing:
        _install_mmcv_ops_compat()
    try:
        __import__("terratorch")
    except ImportError:
        missing.append("terratorch")
    if missing:
        print("Missing Prithvi 2.0 inference dependencies:", ", ".join(missing), file=sys.stderr)
        print("Install the model stack, for example:", file=sys.stderr)
        print("  pip install torch torchvision timm einops rasterio huggingface_hub terratorch mmcv==1.7.2", file=sys.stderr)
        sys.exit(2)
    import numpy as _np
    import rasterio as _rasterio

    np = _np
    rasterio = _rasterio


def _install_mmcv_ops_compat():
    """
    TerraTorch imports MMSeg registries in this environment. MMSegmentation 0.x
    imports a few mmcv.ops symbols during registry setup, but Python 3.12 cannot
    use old mmcv-full wheels. These stubs let registration complete as long as
    the selected model does not actually request those compiled ops.
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
                "This model requested an MMCV compiled op unavailable with "
                "plain mmcv. Use a compatible mmcv-full env if this op is required."
            )

    ops.sigmoid_focal_loss = sigmoid_focal_loss
    ops.point_sample = point_sample
    ops.PSAMask = _UnavailableOp
    ops.CrissCrossAttention = _UnavailableOp
    ops.get_onnxruntime_op_path = lambda: ""

    sys.modules["mmcv.ops"] = ops
    setattr(mmcv, "ops", ops)


def choose_device(device_arg):
    import torch

    if device_arg == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device_arg == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested, but torch.cuda.is_available() is False.")
    return torch.device(device_arg)


def download_model_files(model_repo, config_path, checkpoint_path):
    if config_path and checkpoint_path:
        return config_path, checkpoint_path
    if config_path or checkpoint_path:
        raise ValueError("--config and --checkpoint must be provided together.")

    from huggingface_hub import hf_hub_download

    config = hf_hub_download(repo_id=model_repo, filename=CONFIG_FILE)
    checkpoint = hf_hub_download(repo_id=model_repo, filename=CHECKPOINT_FILE)
    return config, checkpoint


def parse_bands(value):
    bands = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not bands:
        raise ValueError("No band indices parsed from --bands.")
    return bands


def load_geotiff(input_tif, bands):
    with rasterio.open(input_tif) as src:
        data = src.read().astype(np.float32)
        profile = src.profile.copy()
        nodata = src.nodata

    missing = [band for band in bands if band < 0 or band >= data.shape[0]]
    if missing:
        raise ValueError(f"Band indices out of range for {data.shape[0]}-band input: {missing}")

    selected = data[bands]
    nodata_mask = np.zeros(selected.shape[1:], dtype=bool)
    if nodata is not None:
        nodata_mask = np.any(data == nodata, axis=0)
        selected = np.where(selected == nodata, 0, selected)

    if np.nanmax(selected) > 2.0:
        selected = selected / 10000.0
    selected = np.nan_to_num(selected, nan=0.0, posinf=0.0, neginf=0.0)
    selected = selected[np.newaxis, :, np.newaxis, :, :]
    return selected, nodata_mask, profile, data


def run_inference(config_path, checkpoint_path, image, device, tile_size):
    import torch
    import yaml
    from einops import rearrange
    from terratorch.cli_tools import LightningInferenceModel

    runtime_config_path = disable_pretrained_backbone(config_path)
    lightning_model = LightningInferenceModel.from_config(
        runtime_config_path,
        checkpoint_path,
    )
    model = lightning_model.model.to(device)
    model.eval()

    image = torch.from_numpy(image).float()
    image = rearrange(image, "b c t h w -> b h w (c t)")

    batch_size, height, width, channels = image.shape
    pad_h = (tile_size - height % tile_size) % tile_size
    pad_w = (tile_size - width % tile_size) % tile_size
    pad_mode = "reflect" if pad_h < height and pad_w < width else "replicate"
    image = torch.nn.functional.pad(image, (0, 0, 0, pad_w, 0, pad_h), mode=pad_mode)
    _, padded_height, padded_width, _ = image.shape
    patches = image.unfold(1, tile_size, tile_size).unfold(2, tile_size, tile_size)
    patches = patches.contiguous().view(-1, tile_size, tile_size, channels)

    processed = []
    for patch in patches:
        sample = lightning_model.datamodule.test_transform(image=patch.numpy())
        processed.append(sample["image"])
    data = torch.stack(processed).to(device)
    data = apply_datamodule_aug(lightning_model.datamodule, data)
    data = data.unsqueeze(2)

    predictions = []
    with torch.no_grad():
        for batch in data.split(1):
            output = model(
                batch,
                temporal_coords=None,
                location_coords=None,
            )
            predictions.append(output.output.argmax(dim=1).cpu())

    predictions = torch.cat(predictions, dim=0)
    predictions = predictions.view(batch_size, padded_height // tile_size, padded_width // tile_size, tile_size, tile_size)
    predictions = predictions.permute(0, 1, 3, 2, 4).contiguous()
    predictions = predictions.view(batch_size, padded_height, padded_width)
    predictions = predictions[:, :height, :width]
    return predictions.squeeze(0).numpy().astype(np.uint8)


def apply_datamodule_aug(datamodule, data):
    aug = getattr(datamodule, "aug", None)
    if aug is None:
        return data
    try:
        return aug(data, data_keys=["input"])
    except TypeError:
        return aug(data)
    except ValueError as exc:
        if "data keys" not in str(exc):
            raise
        # Kornia's AugmentationSequential API changed across versions. In this
        # model the test augmentation is normalization-only, so setting the data
        # keys explicitly and retrying is equivalent to the HF inference script.
        if hasattr(aug, "data_keys"):
            aug.data_keys = ["input"]
        if hasattr(getattr(aug, "transform_op", None), "data_keys"):
            aug.transform_op.data_keys = ["input"]
        return aug(data)


def disable_pretrained_backbone(config_path):
    import yaml

    config = yaml.safe_load(Path(config_path).read_text())
    changed = _disable_pretrained_fields(config)

    if not changed:
        return config_path

    temp_dir = Path(tempfile.mkdtemp(prefix="prithvi2_config_"))
    runtime_config_path = temp_dir / "config_no_pretrained.yaml"
    runtime_config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"Using runtime config with pretrained backbone disabled: {runtime_config_path}")
    return str(runtime_config_path)


def _disable_pretrained_fields(value):
    changed = False
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key in {"pretrained", "backbone_pretrained"} and child is not False:
                value[key] = False
                changed = True
            elif key in {
                "pretrained_cfg",
                "pretrained_path",
                "pretrained_weights",
                "checkpoint_path",
                "backbone_checkpoint",
            } and child:
                value[key] = None
                changed = True
            else:
                changed = _disable_pretrained_fields(child) or changed
    elif isinstance(value, list):
        for child in value:
            changed = _disable_pretrained_fields(child) or changed
    return changed


def write_mask(prediction, nodata_mask, profile, output_path):
    out = prediction.copy()
    out[nodata_mask] = NODATA_VALUE
    profile.update(count=1, dtype=rasterio.uint8, nodata=NODATA_VALUE, compress="zstd")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(out, 1)
        dst.set_band_description(1, "Prithvi 2.0 Sen1Floods11 prediction: 0=land, 1=flood/water, 255=nodata")
    valid = out != NODATA_VALUE
    water = out == 1
    pct = 100.0 * int(water.sum()) / int(valid.sum()) if valid.sum() else 0.0
    print(f"Prediction written: {output_path}")
    print(f"Flood/water pixels: {int(water.sum())}/{int(valid.sum())} ({pct:.2f}% of valid area)")
    return out


def write_preview(all_bands, mask, preview_path):
    import matplotlib.pyplot as plt

    rgb = np.stack([all_bands[3], all_bands[2], all_bands[1]], axis=-1)
    valid = rgb[np.isfinite(rgb) & (rgb > 0)]
    if valid.size:
        lo, hi = np.percentile(valid, [2, 98])
        if hi <= lo:
            hi = lo + 1
        rgb = np.clip((rgb - lo) / (hi - lo), 0, 1)
    else:
        rgb = np.zeros_like(rgb)
    rgb = np.nan_to_num(rgb, nan=0)

    overlay = rgb.copy()
    flood = mask == 1
    overlay[flood] = overlay[flood] * 0.35 + np.array([0.0, 0.65, 1.0]) * 0.65

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    axes[0].imshow(rgb)
    axes[0].set_title("Input RGB")
    axes[0].axis("off")
    axes[1].imshow(overlay)
    axes[1].set_title("Prithvi 2.0 Flood/Water Overlay")
    axes[1].axis("off")
    plt.tight_layout()
    preview_path = Path(preview_path)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(preview_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Preview written: {preview_path}")


def main():
    args = parse_args()
    require_dependencies()
    input_tif = Path(args.input)
    if not input_tif.exists():
        print(f"Input TIFF not found: {input_tif}", file=sys.stderr)
        return 1

    device = choose_device(args.device)
    config_path, checkpoint_path = download_model_files(args.model_repo, args.config, args.checkpoint)
    bands = parse_bands(args.bands)
    print(f"Using device: {device}")
    print(f"Using bands: {bands}")

    start = time.time()
    image, nodata_mask, profile, all_bands = load_geotiff(input_tif, bands)
    pred = run_inference(config_path, checkpoint_path, image, device, args.tile_size)
    mask = write_mask(pred, nodata_mask, profile, Path(args.output))
    if not args.no_preview:
        write_preview(all_bands, mask, Path(args.preview))
    print(f"Inference completed in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
