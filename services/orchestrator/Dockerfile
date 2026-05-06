# syntax=docker/dockerfile:1.6
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS build
WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY libs/batterylab/pyproject.toml libs/batterylab/pyproject.toml
COPY services/cycler/pyproject.toml services/cycler/pyproject.toml
COPY services/chamber/pyproject.toml services/chamber/pyproject.toml
COPY services/ingester/pyproject.toml services/ingester/pyproject.toml
COPY services/orchestrator/pyproject.toml services/orchestrator/pyproject.toml
COPY services/watchdog/pyproject.toml services/watchdog/pyproject.toml
COPY services/parquet_export/pyproject.toml services/parquet_export/pyproject.toml
COPY services/analytics/pyproject.toml services/analytics/pyproject.toml

COPY libs/batterylab/src libs/batterylab/src
COPY services/orchestrator/src services/orchestrator/src

RUN uv sync --package orchestrator --no-dev --frozen 2>/dev/null || uv sync --package orchestrator --no-dev

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY --from=build /app /app
CMD ["python", "-m", "orchestrator.main"]
