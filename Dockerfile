FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (layer cache)
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

# Copy project
COPY . .
RUN uv sync --no-dev

# Render (and many hosts) set PORT at runtime; default keeps local `docker run` simple.
EXPOSE 8000

CMD ["sh", "-c", "uv run uvicorn src.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
