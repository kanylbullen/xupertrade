FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY hypertrade/ hypertrade/
COPY alembic/ alembic/
COPY alembic.ini ./

CMD ["uv", "run", "python", "-m", "hypertrade.main"]
