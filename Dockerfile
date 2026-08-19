ARG PYTHON_VERSION=3.12.11
ARG NODE_VERSION=22.16.0
ARG UV_VERSION=0.7.2

FROM node:${NODE_VERSION}-bookworm-slim AS cli-tools
ARG CODEX_CLI_VERSION=0.148.0
ARG CLAUDE_CODE_VERSION=2.1.235
RUN npm install --global --omit=dev \
        "@openai/codex@${CODEX_CLI_VERSION}" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-tools

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG RELAY_UID=10001
ARG RELAY_GID=10001

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        openssh-client \
        ripgrep \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${RELAY_GID}" relay \
    && useradd --uid "${RELAY_UID}" --gid "${RELAY_GID}" \
        --home-dir /home/relay --create-home --shell /usr/sbin/nologin relay

COPY --from=uv-tools /uv /uvx /usr/local/bin/
COPY --from=cli-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=cli-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=cli-tools /usr/local/bin/codex /usr/local/bin/codex
COPY --from=cli-tools /usr/local/bin/claude /usr/local/bin/claude

ENV HOME=/home/relay \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    XDG_CACHE_HOME=/tmp/cache

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && install -d -o relay -g relay -m 0700 \
        /data /home/relay/.codex /home/relay/.claude

USER relay:relay
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["agent-relay", "serve"]
