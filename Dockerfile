FROM python:3.11-slim

# System libraries required by opencv (pulled in via paddleocr) and PDF/image
# rendering — without these, paddleocr/opencv fail to import inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Deployment files shipped inside the image, so a server that can only reach
# Docker Hub can copy them out with: docker run --rm <image> cat /deploy/<file>
COPY docker-compose.yml /deploy/docker-compose.yml
COPY nginx/nginx.docker.conf /deploy/nginx/nginx.docker.conf

# Non-root runtime user. Home is where paddleocr caches its downloaded models
# (~/.paddleocr) — docker-compose.yml's paddleocr_models volume must target
# this same path (/home/appuser/.paddleocr), not the old root-user default.
# The .paddleocr dir must exist and be owned by appuser *before* the volume
# mounts over it: on first use, Docker seeds a fresh named volume from
# whatever's already at that path in the image (ownership included) — skip
# this and the volume comes up root-owned, and paddleocr can't write to it.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p uploads masked_pdfs vendor_exchanges logs /home/appuser/.paddleocr \
    && chown -R appuser:appuser /app /home/appuser/.paddleocr
USER appuser

EXPOSE 8000

# Uses Python (already present) instead of curl to avoid an extra apt package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
