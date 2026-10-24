# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM docker.io/library/node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS builder

ENV NEXT_TELEMETRY_DISABLED=1 \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH

RUN corepack enable \
    && corepack prepare pnpm@11.28.6 --activate

WORKDIR /workspace

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json ./frontend/package.json

RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --strict-peer-dependencies

COPY frontend ./frontend

ENV INVESTMENT_RESEARCH_RUNTIME_LOCATION=container \
    INVESTMENT_RESEARCH_API_ORIGIN=http://api:8000

RUN pnpm --filter @investment-research/frontend build

FROM gcr.io/distroless/nodejs24-debian13@sha256:bb6b03d81066993293a10feda7250e8e1cc034035fe9b61cfceededa7c8bf04d AS runtime

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
