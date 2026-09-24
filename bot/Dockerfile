# Pinned uv. A named stage, not `COPY --from=<image>` inline, so
# Dependabot's docker ecosystem sees it as a FROM line and bumps tag and
# digest together (.github/dependabot.yml). .github/workflows/ci.yml
# reads the version from this line, so CI installs with the same uv as
# the image; keep the `FROM ghcr.io/astral-sh/uv:<version>` shape.
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv

FROM python:3.13-slim

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Every `uv run` in this image — the CMD below, and any `docker exec ...
# uv run alembic ...` — must use the environment synced above. Without
# this, `uv run` syncs the default groups, dev included, so each bot
# container start downloaded pytest, hypothesis & co. from PyPI and a
# PyPI outage could keep a bot from starting.
ENV UV_NO_DEV=1

COPY hypertrade/ hypertrade/
COPY alembic/ alembic/
COPY alembic.ini ./

# Version stamp, read back by `GET /api/version` (hypertrade/version.py)
# and logged at startup. GIT_SHA comes from the deploy environment via
# docker-compose.yml's build args; build time is taken here, so it is
# always the real build time. Last on purpose: a new SHA only rebuilds
# these two layers.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"
RUN printf '{"sha": "%s", "built_at": "%s"}\n' \
      "${GIT_SHA}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /app/build-info.json

CMD ["uv", "run", "python", "-m", "hypertrade.main"]
