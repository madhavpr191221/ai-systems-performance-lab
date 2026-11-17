FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY fixtures ./fixtures
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER 10001

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "ai_systems_performance_lab.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
