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
# wget is needed for the compose healthcheck probe. node:22-alpine ships
# without curl/wget; without this the healthcheck command silently exits
# 127 ("not found") and Compose marks the container unhealthy.
RUN apk add --no-cache wget
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
ENV PORT=3000
# Bind to all interfaces, not just the container's hostname address.
# Without this, Next.js standalone listens only on the docker-network
# IP (e.g. 172.18.0.x) and `wget http://localhost:3000/...` from inside
# the container fails with "connection refused" — breaking the compose
# healthcheck even though external requests via the published port
# mapping work fine.
ENV HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
