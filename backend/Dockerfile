FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN chmod -R a+rX /app/src

FROM builder AS verification-builder

ENV UV_NO_DEV=0

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-groups --no-install-project

COPY tests ./tests
RUN chmod -R a+rX /app/tests

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime

# Runtime executes the uv-built environment without package installation or pip bootstrapping.
RUN python -m pip uninstall --yes pip \
    && rm -r /usr/local/lib/python3.14/ensurepip

RUN groupadd --system --gid 65532 app \
    && useradd --system --uid 65532 --gid 65532 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin app

ENV HOME=/nonexistent \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

USER 65532:65532
STOPSIGNAL SIGTERM

FROM runtime AS api

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/ready', timeout=2).close()"]

ENTRYPOINT ["/app/.venv/bin/uvicorn"]
CMD ["investment_research.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

FROM runtime AS migration

COPY --chown=65532:65532 alembic.ini ./alembic.ini
COPY --chown=65532:65532 migrations ./migrations

ENTRYPOINT ["/app/.venv/bin/alembic"]
CMD ["-c", "alembic.ini", "upgrade", "head"]

FROM runtime AS seed

COPY --chown=65532:65532 scripts/__init__.py ./scripts/__init__.py
COPY --chown=65532:65532 scripts/seed_m0_0.py ./scripts/seed_m0_0.py

ENTRYPOINT ["/app/.venv/bin/python", "-m", "scripts.seed_m0_0"]
CMD []

FROM runtime AS worker

HEALTHCHECK --interval=10s --timeout=10s --start-period=30s --retries=6 \
    CMD ["/app/.venv/bin/python", "-m", "investment_research.worker_health"]

ENTRYPOINT ["/app/.venv/bin/celery", "--app", "investment_research.worker:app", "worker"]
CMD ["--loglevel=INFO", "--concurrency=1", "--without-gossip", "--without-mingle"]

FROM runtime AS parser

LABEL org.opencontainers.image.description="Build foundation for the M0 credential-free parser runtime"
EXPOSE 8100
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD ["/app/.venv/bin/python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8100/internal/health', timeout=2).close()"]

ENTRYPOINT ["/app/.venv/bin/python", "-m", "investment_research.parser_app"]
CMD []

FROM runtime AS verification

COPY --from=verification-builder /app/.venv /app/.venv
COPY --from=verification-builder /app/tests /app/tests

ENTRYPOINT ["/app/.venv/bin/python", "-m", "pytest"]
CMD []
