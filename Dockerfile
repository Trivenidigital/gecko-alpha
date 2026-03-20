FROM python:3.12-slim AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create non-root user
RUN useradd -m -s /bin/bash scout

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy application code
COPY scout/ scout/

USER scout

CMD ["uv", "run", "python", "-m", "scout.main"]
