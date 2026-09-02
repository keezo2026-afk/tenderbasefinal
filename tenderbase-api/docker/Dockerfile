# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# TenderBase API — production image (API and worker share this image).
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq for psycopg, poppler/tesseract only in the optional OCR image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/tenderbase

# --- dependency layer -------------------------------------------------------
FROM base AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# --- runtime ----------------------------------------------------------------
FROM base AS runtime
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Never run as root.
RUN useradd --system --create-home --uid 10001 tenderbase \
    && chown -R tenderbase:tenderbase /srv/tenderbase
USER tenderbase

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health/live || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["api"]
