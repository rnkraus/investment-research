# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Node.js 24.22.1 security release; release-age exception approved 2026-10-24.
FROM docker.io/library/node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS builder

ENV NEXT_TELEMETRY_DISABLED=1 \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH

RUN corepack enable \
    && corepack prepare pnpm@11.28.7 --activate \
    # Builds use Corepack and pnpm, not npm's separate bundled dependency tree.
    && rm -r /usr/local/lib/node_modules/npm \
    && rm /usr/local/bin/npm /usr/local/bin/npx \
    && test ! -e /usr/local/lib/node_modules/npm \
    && test ! -e /usr/local/bin/npm \
    && test ! -e /usr/local/bin/npx \
    && pnpm --version

WORKDIR /workspace

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json ./frontend/package.json

RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --strict-peer-dependencies

COPY frontend ./frontend

ENV INVESTMENT_RESEARCH_RUNTIME_LOCATION=container \
    INVESTMENT_RESEARCH_API_ORIGIN=http://api:8000

RUN pnpm --filter @investment-research/frontend build

# Verified Node.js 24.22.1 runtime and upstream Cosign identity on 2026-10-26.
FROM gcr.io/distroless/nodejs24-debian13@sha256:206cfbab1a64af20b63645150e3aa61b7babc17869e0254914947732963d6f07 AS runtime

ENV HOME=/nonexistent \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

WORKDIR /app

COPY --from=builder --chown=65532:65532 /workspace/frontend/.next/standalone ./
COPY --from=builder --chown=65532:65532 /workspace/frontend/.next/static ./frontend/.next/static

USER 65532:65532
EXPOSE 3000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/nodejs/bin/node", "-e", "fetch('http://127.0.0.1:3000/', { signal: AbortSignal.timeout(2000) }).then((response) => { if (!response.ok) process.exit(1); }).catch(() => process.exit(1));"]

CMD ["frontend/server.js"]
