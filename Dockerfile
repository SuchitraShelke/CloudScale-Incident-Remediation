# One image, three entrypoints (orchestrator, mcp-server, console) — selected by compose `command`.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Bake the 384-dim embedding model for the semantic cache (no runtime download, works offline).
ENV FASTEMBED_CACHE_DIR=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')" \n    && chmod -R a+rX /opt/fastembed

COPY mock-data ./mock-data
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 app
USER app
