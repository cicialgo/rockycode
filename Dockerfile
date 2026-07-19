FROM ghcr.io/astral-sh/uv:python3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY rockycode /app/rockycode
COPY prompts /app/prompts
COPY bench /app/bench
COPY README.md /app/README.md

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV TEXTUAL_DISABLE_KITTY_KEY=1

ENTRYPOINT ["rockycode"]
CMD ["chat"]
