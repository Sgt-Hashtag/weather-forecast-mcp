# services/agent/Dockerfile
FROM python:3.11-slim AS base

# Install system dependencies for geospatial libraries
RUN apt-get update && apt-get install -y \
    build-essential \
    libgeos-dev \
    libproj-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 for Mapbox MCP Server
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs

# Install Mapbox MCP Server globally (CORRECT PACKAGE NAME)
RUN npm install -g @mapbox/mcp-server@latest --network-timeout=600000

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Fetch BMD data at BUILD TIME
# RUN python /app/scripts/fetch_bmd_data.py

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD python -c "import requests; exit(0) if requests.get('http://localhost:8000/health', timeout=5).status_code == 200 else exit(1)"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]