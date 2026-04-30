# hls-foundation-os Python 3.12 Fork Notes

The upstream `hls-foundation-os` package currently owns too many dependencies
for this project. Its `setup.py` pins `timm==0.4.12` and installs an old
`mmsegmentation` git commit. That conflicts with modern packages such as
`torchgeo`, `terratorch`, and `segmentation-models-pytorch`, which need
`timm>=1.x`.

Use a fork so the `geospatial_fm` package is installed, but dependency versions
stay controlled by `inference_requirements.txt`.

## Patch `setup.py`

Change:

```python
install_requires=[
    "mmsegmentation @ git+https://github.com/open-mmlab/mmsegmentation.git@186572a3ce64ac9b6b37e66d58c76515000c3280",
    "rasterio",
    "rioxarray",
    "einops",
    "timm==0.4.12",
    "tensorboard",
    "imagecodecs",
    "yapf==0.40.1",
]
```

to:

```python
install_requires=[
    "rasterio",
    "rioxarray",
    "einops",
    "tensorboard",
    "imagecodecs",
    "yapf==0.40.1",
]
```

Then install your fork from `inference_requirements.txt`:

```text
git+https://github.com/Sgt-Hashtag/hls-foundation-os.git
```

If you keep the Python 3.12 changes on a branch, pin that branch instead:

```text
git+https://github.com/Sgt-Hashtag/hls-foundation-os.git@py312-prithvi-inference
```

## Why

`geospatial_fm` provides the custom MMSegmentation model classes and geospatial
pipeline transforms referenced by the Prithvi sen1floods11 config. We need the
package import, but we do not want its old dependency pins to downgrade the rest
of the environment.
