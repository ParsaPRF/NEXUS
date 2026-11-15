# =====================================================================================
# NEXUS API image
# =====================================================================================
# Build context is ./backend, so paths here are relative to the backend directory.
# Built and used by docker-compose.yml.
#
# NOT YET VERIFIED: the Docker daemon was not running during Phase 1. See the status note in
# docker-compose.yml and docs/TECHNICAL_ASSESSMENT.md R2.
# =====================================================================================
FROM python:3.13-slim AS base

# WHY 3.13 and not 3.14: on Windows the development machine runs 3.14, but in a container we
# want the most predictable wheel availability for the optional async database driver. The
# project supports 3.12+, and both interpreters are exercised by the test suite.

# --- Environment -----------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PATH="/usr/local/bin:${PATH}"

# --- System dependencies ---------------------------------------------------------------
# curl is needed by the container healthcheck in docker-compose.yml; build-essential is not
# installed on purpose, because every dependency here has a wheel.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# --- uv (the project's dependency manager) ---------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# --- Dependencies first, for layer caching ---------------------------------------------
# Copying only the manifests means `docker build` reuses the dependency layer whenever source
# code changes but dependencies do not.
COPY pyproject.toml uv.lock* ./

# Install the full dependency set plus the PostgreSQL driver (the docker profile uses it).
RUN uv sync --no-install-project --extra postgres

# --- Application -----------------------------------------------------------------------
COPY . .

# Rendered evidence for review; the authoritative copies live in the repository root.
RUN cp .env.example .docker-env-example 2>/dev/null || true

# --- Runtime ---------------------------------------------------------------------------
# Run as an unprivileged user. A container that never needs root should not have it.
RUN useradd --create-home --uid 10001 nexus \
    && mkdir -p /app/data /app/logs \
    && chown -R nexus:nexus /app
USER nexus

ENV NEXUS_PROFILE=docker \
    NEXUS_API__HOST=0.0.0.0 \
    NEXUS_API__PORT=8000

EXPOSE 8000

# Default command; docker-compose overrides it to run migrations first. The worker service
# starts the same image with a different command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
