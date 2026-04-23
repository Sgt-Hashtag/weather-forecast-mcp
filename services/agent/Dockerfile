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
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for npx (Mapbox MCP)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

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

ENV SAM_CHECKPOINT=/app/models/sam_vit_h_4b8939.pth

# Copy application code
COPY . .

WORKDIR /app

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]