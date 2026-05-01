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

RUN chmod +x scripts/start_web_and_worker.sh

CMD ["sh", "scripts/start_web_and_worker.sh"]
