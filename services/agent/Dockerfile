FROM python:3.11-slim AS base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential libgeos-dev libproj-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Install Heavy AI Dependencies (Cache this once)
COPY requirements-core.txt .
RUN pip install --no-cache-dir --prefer-binary \
    --index-url https://download.pytorch.org/whl/cpu \
    -r requirements-core.txt

# 2. Install Geospatial Dependencies (Cache this once)
COPY requirements-geo.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements-geo.txt

# 3. Install App Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy model checkpoints
RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/*

#model (approx 2.5GB)

RUN mkdir -p /app/models && \
    wget -O /app/models/sam_vit_h_4b8939.pth https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth

# 3. Set the path so your processor code can find it
ENV SAM_CHECKPOINT=/app/models/sam_vit_h_4b8939.pth

# Copy application code last (Changes often, doesn't break cache)
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]