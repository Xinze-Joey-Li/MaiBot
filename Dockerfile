# Build the customized dashboard with the same Bun version and lockfile as WebUI CI.
FROM node:24-bookworm-slim AS dashboard-build
COPY --from=oven/bun:1.2.1 /usr/local/bin/bun /usr/local/bin/bun
WORKDIR /dashboard
COPY dashboard/package.json dashboard/bun.lock ./
RUN bun install --frozen-lockfile --ignore-scripts
COPY dashboard/ ./
RUN bun run build

# Runtime image
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Working directory
WORKDIR /MaiMBot

ENV MAIBOT_LEGACY_0X_UPGRADE_CONFIRMED=1
ENV MAIBOT_WEBUI_USE_LOCAL_DASHBOARD=1
ENV PATH="/MaiMBot/.venv/bin:${PATH}"

# Copy dependency metadata
COPY pyproject.toml uv.lock ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install runtime dependencies
RUN uv sync --locked --no-dev --no-install-project

# Install system libraries required by Playwright Chromium. The browser binary
# itself is downloaded lazily into the configured data directory at runtime.
RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy project source
COPY . .
COPY --from=dashboard-build /dashboard/dist ./dashboard/dist

RUN git clone --depth 1 --branch main https://github.com/Mai-with-u/MaiBot-Napcat-Adapter.git plugin-templates/MaiBot-Napcat-Adapter
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000 8001

ENTRYPOINT [ "./docker-entrypoint.sh" ]
