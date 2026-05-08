FROM python:3.12-slim-bookworm

# Install GDAL, Node.js and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    wget \
    curl \
    gnupg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for npx (Mapbox MCP)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /app

# GPU_INFERENCE=true  → pass --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
# GPU_INFERENCE=false → default CPU wheels (no CUDA required)
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG DOWNLOAD_PRITHVI2_MODEL=true

ENV HF_HOME=/app/models/huggingface
ENV HF_HUB_CACHE=/app/models/huggingface/hub
ENV PRITHVI2_MODEL_REPO=ibm-nasa-geospatial/Prithvi-EO-2.0-300M-TL-Sen1Floods11
ENV PIP_DEFAULT_TIMEOUT=1200
ENV PIP_RETRIES=10

# 1. Install Heavy AI Dependencies (Cache this once)
COPY services/agent/requirements-core.txt .
RUN pip install --no-cache-dir --prefer-binary --timeout 1200 --retries 10 \
    --index-url ${TORCH_INDEX_URL} \
    -r requirements-core.txt

# 2. Install Geospatial Dependencies (Cache this once)
COPY services/agent/requirements-geo.txt .
RUN pip install --no-cache-dir --prefer-binary --timeout 1200 --retries 10 -r requirements-geo.txt

# 3. Install App Dependencies
COPY services/agent/requirements.txt .
RUN pip install --no-cache-dir --timeout 1200 --retries 10 -r requirements.txt

# 4. Install Prithvi-EO-2.0 Sen1Floods11 local inference dependencies.
COPY prithvi2_inference_requirements.txt .
RUN pip install --no-cache-dir --timeout 1200 --retries 10 "setuptools<70" wheel addict packaging yapf Pillow \
    && pip install --no-cache-dir --timeout 1200 --retries 10 --no-build-isolation mmcv==1.7.2 mmsegmentation==0.30.0 \
    && pip install --no-cache-dir --prefer-binary --timeout 1200 --retries 10 -r prithvi2_inference_requirements.txt

# Install ftw-tools (required by delineate-anything engine for Sentinel-2)
RUN pip install --no-cache-dir --timeout 1200 --retries 10 git+https://github.com/fieldsoftheworld/ftw-baselines.git

# Force headless OpenCV — ultralytics pulls in opencv-python (GUI) which needs
# libxcb display libs not present in this container. Replace with headless.
RUN pip install --no-cache-dir --timeout 1200 --retries 10 --force-reinstall opencv-python-headless

# Copy model checkpoints
RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/*

#model (approx 2.5GB)

RUN mkdir -p /app/models && \
    wget -O /app/models/sam_vit_h_4b8939.pth https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth

ENV SAM_CHECKPOINT=/app/models/sam_vit_h_4b8939.pth

# Copy application code
COPY services/agent/ .
COPY scripts/ /app/scripts/
COPY docs/ /app/docs/

# Prefetch the Prithvi 2.0 fine-tuned config/checkpoint into the image so
# inference starts without waiting for the 1.28GB checkpoint download.
RUN if [ "$DOWNLOAD_PRITHVI2_MODEL" = "true" ]; then \
      python /app/scripts/prithvi_flood_seg/download_prithvi2_model.py; \
    fi

# Setup secrets directory
RUN mkdir -p /app/secrets

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
