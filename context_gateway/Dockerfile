FROM node:22-alpine AS web-builder

WORKDIR /web
COPY --from=web /package.json /package-lock.json ./
RUN npm ci
COPY --from=web . ./
RUN npm test && npm run build

FROM python:3.13-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir uv==0.12.18
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY --from=web-builder /web/dist /app/web-dist
ENV CONTEXT_GATEWAY_WEB_DIR=/app/web-dist
RUN mkdir -p /var/lib/context-gateway/snapshots && chown -R 10001:10001 /var/lib/context-gateway

USER 10001:10001
EXPOSE 8787
CMD ["uvicorn", "context_gateway.web_public:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--no-server-header"]
