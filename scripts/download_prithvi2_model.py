#!/usr/bin/env python3
"""Prefetch Prithvi-EO-2.0 Sen1Floods11 assets from Hugging Face."""

from __future__ import annotations

import os
from pathlib import Path


MODEL_REPO = "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-TL-Sen1Floods11"
FILES = [
    "config.yaml",
    "Prithvi-EO-V2-300M-TL-Sen1Floods11.pt",
]


def main() -> int:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("Missing dependency: huggingface_hub. Install prithvi2_inference_requirements.txt first.")
        return 2

    repo_id = os.getenv("PRITHVI2_MODEL_REPO", MODEL_REPO)
    print(f"Prefetching Prithvi 2.0 flood model from {repo_id}")
    for filename in FILES:
        path = hf_hub_download(repo_id=repo_id, filename=filename)
        size_mb = Path(path).stat().st_size / (1024 * 1024)
        print(f"  {filename}: {path} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
