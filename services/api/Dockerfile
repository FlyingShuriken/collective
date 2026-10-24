FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.13-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY services/api/pyproject.toml services/api/uv.lock services/api/
COPY services/api/src services/api/src
RUN uv sync --project services/api --frozen --no-dev --no-cache
COPY fixtures/sources fixtures/sources
COPY fixtures/gold fixtures/gold
COPY scripts/generate_replay_fixtures.py scripts/generate_replay_fixtures.py
RUN services/api/.venv/bin/python scripts/generate_replay_fixtures.py

FROM python:3.12.13-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /app/services/api services/api
COPY --from=build /app/fixtures/replay fixtures/replay
COPY fixtures/matching fixtures/matching
RUN useradd --uid 10001 --create-home collective && chmod -R a+rX /app/fixtures
USER collective
ENV PATH="/app/services/api/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn collective.main:create_app --factory --host 0.0.0.0 --port ${PORT} --no-access-log"]
