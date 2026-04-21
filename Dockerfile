# syntax=docker/dockerfile:1.6

# ---------- Builder stage ---------------------------------------------------
# Compiles wheels for every dependency. matplotlib + pandas pull in C
# extensions, but python:3.11-slim already ships the common build chain so we
# don't need to install gcc ourselves for the wheels we use.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt ./
RUN pip install --prefix=/install -r requirements.txt


# ---------- Runtime stage ---------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # matplotlib caches fonts in $HOME; keep that inside /app so it survives
    # across runs when the container volume is mounted.
    MPLCONFIGDIR=/app/.matplotlib

# Copy only the installed site-packages from the builder.
COPY --from=builder /install /usr/local

# Non-root user.
RUN groupadd --system bot && useradd --system --gid bot --home /app bot \
    && mkdir -p /app/data /app/.matplotlib \
    && chown -R bot:bot /app

WORKDIR /app

# App code (see .dockerignore for what's excluded).
COPY --chown=bot:bot main.py ./
COPY --chown=bot:bot bot ./bot

USER bot

# Healthcheck endpoint port (override with HEALTHCHECK_PORT).
EXPOSE 8080

# The bot's own healthcheck server powers this probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
url=f'http://127.0.0.1:{os.getenv(\"HEALTHCHECK_PORT\",\"8080\")}/health'; \
sys.exit(0) if urllib.request.urlopen(url, timeout=3).status == 200 else sys.exit(1)"

CMD ["python", "main.py"]
