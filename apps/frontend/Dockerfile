# syntax=docker/dockerfile:1
#
# Frontend image: Next.js (standalone output). Multi-stage so the runtime
# image has no toolchain and no dev dependencies.

ARG NODE_VERSION=22-alpine

# --------------------------------------------------------------------------- #
# 1. Dependencies
# --------------------------------------------------------------------------- #
FROM node:${NODE_VERSION} AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi

# --------------------------------------------------------------------------- #
# 2. Build
# --------------------------------------------------------------------------- #
FROM node:${NODE_VERSION} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# BACKEND_ORIGIN is read at runtime by the standalone server (rewrites are
# resolved per-request), so build-time values are only a default.
RUN npm run build

# --------------------------------------------------------------------------- #
# 3. Runtime
# --------------------------------------------------------------------------- #
FROM node:${NODE_VERSION} AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:3000/login || exit 1
CMD ["node", "server.js"]
