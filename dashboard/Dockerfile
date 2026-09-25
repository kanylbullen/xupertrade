FROM node:22-alpine AS base

FROM base AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
# Note: the compose healthcheck uses `wget --spider`. node:22-alpine
# ships busybox wget pre-installed at /usr/bin/wget, which supports
# `--spider` and returns the exit codes we need — no extra package
# install required.
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
# Version stamp, read back by `GET /api/version` (src/lib/build-info.ts,
# resolved against the cwd `server.js` runs in: /app). GIT_SHA comes
# from the deploy environment via docker-compose.yml's build args; build
# time is taken here, so it is always the real build time. Written as
# root and left root-owned: the server only reads it.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"
RUN printf '{"sha": "%s", "built_at": "%s"}\n' \
      "${GIT_SHA}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /app/build-info.json
USER nextjs
EXPOSE 3000
ENV PORT=3000
# Note: HOSTNAME=0.0.0.0 is set in docker-compose.yml's `environment:`
# block, NOT here. Docker auto-injects HOSTNAME=<container-id> at
# runtime which shadows any image-level ENV — so Dockerfile ENV is
# ineffective for this var. Compose `environment:` overrides the
# runtime default and actually reaches the node process.
CMD ["node", "server.js"]
