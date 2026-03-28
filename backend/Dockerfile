# Kagura Memory Cloud v5.0 - Backend Dockerfile

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN pip install --no-cache-dir uv

# Copy pyproject.toml and README.md first (for layer caching)
COPY pyproject.toml README.md ./

# Install dependencies (including dev dependencies for testing)
RUN uv pip install --system -e ".[dev]"

# Copy source code
COPY src/ ./src/

# Copy migrations
COPY migrations/ ./migrations/

# Copy scripts
COPY scripts/ ./scripts/

# Copy templates (OAuth2, etc.)
COPY templates/ ./templates/

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
