# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOREFACTOR_PROFILE=full \
    AUTOREFACTOR_MCP_TRANSPORT=stdio \
    AUTOREFACTOR_MCP_HOST=0.0.0.0 \
    AUTOREFACTOR_MCP_PORT=8765 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer — invalidated only when lock-ish metadata changes, not on src edits.
COPY pyproject.toml README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p src/autorefactor \
    && printf '# stub for dependency-layer install\n' > src/autorefactor/__init__.py \
    && pip install --upgrade pip setuptools wheel \
    && pip install . \
    && pip install "semgrep>=1.80.0" "ruff>=0.6.0"

# Package layer — src-only changes reinstall the project, not numpy/semgrep.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation -e .

RUN mkdir -p /root/.autorefactor/runs

WORKDIR /workspace

EXPOSE 8765

CMD ["autorefactor-mcp"]
