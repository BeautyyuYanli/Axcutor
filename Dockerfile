FROM rust:1.90.0-slim-bookworm AS rust

FROM rust AS wasm
RUN rustup target add wasm32-unknown-unknown \
    && cargo install wasm-bindgen-cli --version 0.2.129 --locked
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    cargo build --locked --release -p axcutor-wasm --target wasm32-unknown-unknown \
    && wasm-bindgen --target web --out-dir /wasm target/wasm32-unknown-unknown/release/axcutor_wasm.wasm

FROM node:22.22.1-bookworm-slim AS web
WORKDIR /app/playground
COPY playground/package.json playground/package-lock.json ./
RUN npm ci
COPY playground ./
COPY --from=wasm /wasm ./src/wasm-pkg
RUN npm run build

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
COPY playground/backend ./playground/backend
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/usr/local/cargo/registry \
    uv sync --locked --no-dev --no-editable --package axcutor-playground

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=web /app/playground/dist /app/playground/dist
COPY examples /app/examples
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/examples/workflows
RUN useradd --create-home --uid 10001 axcutor \
    && mkdir /data && chown axcutor:axcutor /data
USER axcutor
ENTRYPOINT ["axcutor"]
CMD ["worker"]
