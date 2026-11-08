FROM rust:1.85-slim-bookworm AS rust

FROM python:3.12-slim-bookworm AS builder
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH="/usr/local/cargo/bin:$PATH" \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY python ./python
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/usr/local/cargo/registry \
    uv sync --locked --no-dev --no-editable --package axcutor-host

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 axcutor
USER axcutor
ENTRYPOINT ["axcutor"]
CMD ["worker"]
