#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
import types
from pathlib import Path

np = None
rasterio = None
torch = None
exposure = None

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR.parent / "scripts" / "crop" / "prithvi_input_stacked.tif"
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"

REPO_ID = "ibm-nasa-geospatial/Prithvi-EO-1.0-100M-multi-temporal-crop-classification"
CONFIG_NAME = "multi_temporal_crop_classification_Prithvi_100M.py"
CKPT_NAME = "multi_temporal_crop_classification_Prithvi_100M.pth"

CDL_COLOR_MAP = {
    0: (0, 0, 0),
    1: (233, 255, 190),
    2: (149, 206, 147),
    3: (255, 212, 0),
    4: (38, 115, 0),
    5: (128, 179, 179),
    6: (156, 156, 156),
    7: (77, 112, 163),
    8: (168, 112, 0),
    9: (255, 168, 227),
    10: (191, 191, 122),
    11: (255, 38, 38),
    12: (255, 158, 15),
    13: (0, 175, 77),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Prithvi multi-temporal crop classification locally."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Path to 18-band HLS/Prithvi GeoTIFF.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for outputs.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="Inference device: auto, cpu, cuda, cuda:0, cuda:1, ...",
    )
    parser.add_argument(
        "--save-previews",
        action="store_true",
        help="Save T1/T2/T3 RGB previews.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Local config path. Skips Hugging Face download when provided.",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Local checkpoint path. Skips Hugging Face download when provided.",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Do not contact Hugging Face. Requires --config and --checkpoint.",
    )
    return parser.parse_args()


def require_runtime_dependencies():
    missing = []
    modules = [
        ("numpy", "numpy"),
        ("rasterio", "rasterio"),
        ("torch", "torch"),
        ("huggingface_hub", "huggingface_hub"),
        ("mmcv", "mmcv==1.7.2"),
        ("mmseg", "mmsegmentation==0.30.0"),
        ("skimage", "scikit-image"),
    ]

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
    print(
        "\nThis script uses the legacy MMSegmentation/MMCV 1.x Prithvi stack. "
        "If your environment does not have CUDA-enabled MMCV, this runner "
        "now falls back to a lightweight compatibility shim for mmcv.ops."
    )
    sys.exit(2)


def _load_core_dependencies():
    global np, rasterio, torch, exposure

    if np is None:
        import numpy as _np

        np = _np
    if rasterio is None:
        import rasterio as _rasterio

        rasterio = _rasterio
    if torch is None:
        import torch as _torch

        torch = _torch
    if exposure is None:
        from skimage import exposure as _exposure

        exposure = _exposure


def _install_mmcv_ops_compat():
    """
    Some environments have plain mmcv without the compiled CUDA ops, which
    causes mmseg imports to crash early. This model does not need those ops, so
    we provide lightweight stubs to let registration complete.
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
                "This run requested an MMCV compiled op that is unavailable in "
                "the current environment. Install a compatible mmcv-full build "
                "or switch to a known-working Prithvi environment."
            )

    ops.sigmoid_focal_loss = sigmoid_focal_loss
    ops.point_sample = point_sample
    ops.PSAMask = _UnavailableOp
    ops.CrissCrossAttention = _UnavailableOp
    ops.get_onnxruntime_op_path = lambda: ""

    sys.modules["mmcv.ops"] = ops
    setattr(mmcv, "ops", ops)


def open_tiff(path):
    _load_core_dependencies()
    with rasterio.open(path) as src:
        arr = src.read()
        meta = src.meta.copy()
    return arr, meta


def write_tiff(arr, path, meta):
    _load_core_dependencies()
    out = meta.copy()
    if arr.ndim == 2:
        out.update(count=1, dtype=str(arr.dtype))
        with rasterio.open(path, "w", **out) as dst:
            dst.write(arr, 1)
    else:
        out.update(count=arr.shape[0], dtype=str(arr.dtype))
        with rasterio.open(path, "w", **out) as dst:
            dst.write(arr)


def stretch_rgb(rgb):
    valid = rgb[~np.isnan(rgb)]
    if valid.size == 0:
        return np.zeros_like(rgb, dtype=np.uint8)
    p_low, p_high = np.percentile(valid, (0, 100))
    stretched = exposure.rescale_intensity(rgb, in_range=(p_low, p_high))
    return np.clip(stretched, 0, 255).astype(np.uint8)


def make_rgb_preview(arr, nodata_mask, band_idx):
    rgb = arr[band_idx, :, :].transpose((1, 2, 0)) / 10000.0 * 255.0
    rgb = stretch_rgb(rgb)
    rgb[nodata_mask.squeeze(0) == 1] = 0
    return rgb


def colorize_classes(class_map):
    h, w = class_map.shape
    out = np.zeros((3, h, w), dtype=np.uint8)
    for cls, color in CDL_COLOR_MAP.items():
        mask = class_map == cls
        out[0][mask] = color[0]
        out[1][mask] = color[1]
        out[2][mask] = color[2]
    return out


def process_test_pipeline(custom_test_pipeline):
    custom_test_pipeline = copy.deepcopy(custom_test_pipeline)
    collect_index = [i for i, step in enumerate(custom_test_pipeline) if "Collect" in step["type"]]
    if collect_index:
        custom_test_pipeline[collect_index[0]]["meta_keys"] = [
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
    return custom_test_pipeline


def choose_device(device_arg):
    if device_arg == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if device_arg.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"{device_arg} was requested, but torch.cuda.is_available() is False.")
    return device_arg


def download_model_files(config_override=None, checkpoint_override=None, local_files_only=False):
    from huggingface_hub import hf_hub_download

    if config_override and checkpoint_override:
        config_path = Path(config_override).resolve()
        ckpt_path = Path(checkpoint_override).resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Local config not found: {config_path}")
        if not ckpt_path.exists():
            raise FileNotFoundError(f"Local checkpoint not found: {ckpt_path}")
        print(f"Using local config: {config_path}")
        print(f"Using local checkpoint: {ckpt_path}")
        return str(config_path), str(ckpt_path)

    if bool(config_override) != bool(checkpoint_override):
        raise ValueError("Provide both --config and --checkpoint together, or neither.")

    if local_files_only:
        raise ValueError("--local-files-only requires both --config and --checkpoint.")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    print(f"Downloading config from Hugging Face repo: {REPO_ID}")
    config_path = hf_hub_download(repo_id=REPO_ID, filename=CONFIG_NAME, token=token)
    print("Config ready.")

    print("Downloading checkpoint from Hugging Face. This file is large and may take a while...")
    ckpt_path = hf_hub_download(repo_id=REPO_ID, filename=CKPT_NAME, token=token)
    print("Checkpoint ready.")
    return config_path, ckpt_path


def _init_segmentor_with_legacy_torch_load(config, checkpoint_path, device, init_segmentor):
    """
    PyTorch 2.6 changed torch.load defaults; old MMCV checkpoints need the
    legacy behavior for trusted local downloads.
    """
    original_torch_load = torch.load

    def torch_load_legacy(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return original_torch_load(*args, **kwargs)

    torch.load = torch_load_legacy
    try:
        return init_segmentor(config, checkpoint_path, device=device)
    finally:
        torch.load = original_torch_load


def build_model(config_path, ckpt_path, device):
    from mmcv import Config
    from mmseg.apis import init_segmentor

    config = Config.fromfile(config_path)
    config.model.backbone.pretrained = None
    model = _init_segmentor_with_legacy_torch_load(config, ckpt_path, device, init_segmentor)
    custom_test_pipeline = process_test_pipeline(model.cfg.data.test.pipeline)
    return model, custom_test_pipeline


def inference_segmentor_local(model, img_path, custom_test_pipeline):
    from mmcv.parallel import collate, scatter
    from mmseg.datasets.pipelines import Compose, LoadImageFromFile

    device = next(model.parameters()).device
    test_pipeline = [LoadImageFromFile()] + custom_test_pipeline[1:]
    test_pipeline = Compose(test_pipeline)

    data = test_pipeline({"img_info": {"filename": str(img_path)}})
    data = collate([data], samples_per_gpu=1)

    if next(model.parameters()).is_cuda:
        data = scatter(data, [device])[0]
    else:
        data = {"img": data["img"], "img_metas": data["img_metas"].data[0]}

    with torch.no_grad():
        result = model(return_loss=False, rescale=True, **data)
    return result


def main():
    args = parse_args()
    require_runtime_dependencies()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"Input GeoTIFF not found: {input_path}")

    device = choose_device(args.device)
    print(f"Running on device: {device}")

    config_path, ckpt_path = download_model_files(
        args.config,
        args.checkpoint,
        args.local_files_only,
    )
    model, custom_test_pipeline = build_model(config_path, ckpt_path, device)

    arr, meta = open_tiff(input_path)
    if arr.shape[0] != 18:
        raise ValueError(f"Expected 18 bands, got {arr.shape[0]} bands.")

    st = time.time()
    result = inference_segmentor_local(model, input_path, custom_test_pipeline)
    pred = result[0][0].astype(np.uint8) + 1

    nodata = meta.get("nodata")
    if nodata is not None:
        nodata_mask = np.max(np.where(arr == nodata, 1, 0), axis=0)[None]
    else:
        nodata_mask = np.zeros((1, arr.shape[1], arr.shape[2]), dtype=np.uint8)

    pred[nodata_mask[0] == 1] = 0

    pred_meta = meta.copy()
    pred_meta.update(count=1, dtype="uint8")
    write_tiff(pred, output_dir / "crop_prediction.tif", pred_meta)

    pred_rgb = colorize_classes(pred)
    rgb_meta = meta.copy()
    rgb_meta.update(count=3, dtype="uint8")
    write_tiff(pred_rgb, output_dir / "crop_prediction_rgb.tif", rgb_meta)

    if args.save_previews:
        rgb1 = make_rgb_preview(arr, nodata_mask, [2, 1, 0])
        rgb2 = make_rgb_preview(arr, nodata_mask, [8, 7, 6])
        rgb3 = make_rgb_preview(arr, nodata_mask, [14, 13, 12])

        preview_meta = meta.copy()
        preview_meta.update(count=3, dtype="uint8")

        write_tiff(rgb1.transpose((2, 0, 1)).astype(np.uint8), output_dir / "preview_t1.tif", preview_meta)
        write_tiff(rgb2.transpose((2, 0, 1)).astype(np.uint8), output_dir / "preview_t2.tif", preview_meta)
        write_tiff(rgb3.transpose((2, 0, 1)).astype(np.uint8), output_dir / "preview_t3.tif", preview_meta)

    elapsed = time.time() - st
    print(f"Inference finished in {elapsed:.2f}s on {device}")
    print(f"Saved class map: {output_dir / 'crop_prediction.tif'}")
    print(f"Saved color map: {output_dir / 'crop_prediction_rgb.tif'}")
    if args.save_previews:
        print("Saved RGB previews: preview_t1.tif, preview_t2.tif, preview_t3.tif")


if __name__ == "__main__":
    main()
