FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy

# git: the API resolves session refs with `git ls-remote`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pinned brev CLI for the worker. 0.6.336 is tagged but has no release archives, so
# 0.6.335 is the newest installable version. Checksums are from the release's
# brev-cli_<ver>_checksums.txt; update all three ARGs together.
ARG BREV_CLI_VERSION=0.6.335
ARG BREV_CLI_SHA256_AMD64=89d778e6f1e5e52495f3e0f10393f1666a1b16d180001b13faec6f906955e6f8
ARG BREV_CLI_SHA256_ARM64=3efeaaf4e07d293403115b9d1e87bdc9c5277b26e1241edf3d4d7312e21bccb4
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sha256="$BREV_CLI_SHA256_AMD64" ;; \
        arm64) sha256="$BREV_CLI_SHA256_ARM64" ;; \
        *) echo "No brev CLI build for $arch" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/brevdev/brev-cli/releases/download/v${BREV_CLI_VERSION}/brev-cli_${BREV_CLI_VERSION}_linux_${arch}.tar.gz"; \
    python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "$url" /tmp/brev.tar.gz; \
    echo "$sha256  /tmp/brev.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/brev.tar.gz -C /usr/local/bin brev; \
    rm /tmp/brev.tar.gz; \
    brev --version

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable \
    && useradd --uid 10001 --create-home app
USER app

EXPOSE 8000
# Probes the API, or the worker heartbeat when PROCESS_ROLE=worker (see healthcheck.py).
HEALTHCHECK --interval=30s --timeout=20s --start-period=30s --retries=3 \
    CMD ["python", "-m", "saronic_api.healthcheck"]
# The worker app overrides this with `python -m saronic_api.worker`.
CMD ["uvicorn", "saronic_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
