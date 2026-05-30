# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build deps only in this stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source (secrets and local config excluded via .dockerignore)
COPY app.py pharmacy_functions.py app_config.json ./
COPY config/     ./config/
COPY core/       ./core/
COPY pipelines/  ./pipelines/
COPY routes/     ./routes/
COPY metrics/    ./metrics/

# Runtime directories
RUN mkdir -p recordings metrics && chown -R 1000:1000 /app

# Run as non-root
RUN useradd -m -u 1000 priya
USER priya

EXPOSE ${PORT}

CMD ["python", "app.py"]
