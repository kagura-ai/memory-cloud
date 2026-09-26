# Kagura Memory Cloud v5.0 - Backend Dockerfile

FROM python:3.11-slim

# uv, pinned in lockstep with pyproject.toml [tool.uv] required-version,
# .github/workflows/*.yml and setup.sh (#1706); tests/test_uv_version_lockstep.py
# enforces it. The lock is the only thing this image installs from.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The project environment is /app/.venv; putting it first on PATH keeps the
# uvicorn / alembic / python invocations (CMD, compose `command:` overrides,
# `docker exec kagura-api python -m pytest`) unchanged. Copy mode avoids
# hard-link warnings across layers; --no-cache keeps uv's download cache out
# of the image. Bytecode is not precompiled (UV_COMPILE_BYTECODE): with uv
# 0.11.19 the precompiled sqlalchemy .pyc files failed to load ("bad marshal
# data"), so Python compiles lazily at first import, as it did before.
ENV UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, from the lock, without the project itself: this layer
# only changes when pyproject.toml / uv.lock change. --locked fails the build
# when the lock is out of date with pyproject.toml.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra dev --no-install-project --no-cache

# Copy source code
COPY src/ ./src/

# Copy alembic (database migrations)
COPY alembic.ini ./alembic.ini
COPY alembic/ ./alembic/

# Copy scripts
COPY scripts/ ./scripts/

# Copy templates (OAuth2, etc.)
COPY templates/ ./templates/

# Now the project itself (editable, as `-e .` was), dependencies untouched.
RUN uv sync --locked --extra dev --no-cache

# Create non-root user
RUN useradd -m -u 1000 kagura && chown -R kagura:kagura /app
USER kagura

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run application
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
