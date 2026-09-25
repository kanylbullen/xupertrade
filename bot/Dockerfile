# Pinned uv. A named stage, not `COPY --from=<image>` inline, so
# Dependabot's docker ecosystem sees it as a FROM line and bumps tag and
# digest together (.github/dependabot.yml). .github/workflows/ci.yml
# reads the version from this line, so CI installs with the same uv as
# the image; keep the `FROM ghcr.io/astral-sh/uv:<version>` shape.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.13-slim

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
# --locked: a uv.lock that no longer matches pyproject.toml fails the
# build instead of shipping an image without the new dependency (CI's
# `bot` job fails the PR the same way).
RUN uv sync --locked --no-dev

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
# docker-compose.yml's build args; build time is taken here, so it
# needs no input. Last on purpose: a new SHA only reruns these steps.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"
RUN printf '{"sha": "%s", "built_at": "%s"}\n' \
      "${GIT_SHA}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /app/build-info.json

CMD ["uv", "run", "python", "-m", "hypertrade.main"]
