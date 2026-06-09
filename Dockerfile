# NERVE — container image for Google Cloud Run.
# Secrets are injected as env vars from Secret Manager at runtime; no .env is
# ever copied into the image (see .dockerignore and CLAUDE.md invariant 8).

FROM python:3.11-slim

# No .pyc files, unbuffered stdout (so structlog JSON streams to Cloud Logging),
# no pip cache in the image layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements change.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Then copy the application code.
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 1000 nerve && chown -R nerve:nerve /app
USER nerve

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
