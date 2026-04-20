# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.13-alpine AS builder

# Apply latest Alpine security patches, then add build deps
RUN apk upgrade --no-cache && apk add --no-cache gcc musl-dev

WORKDIR /app

# Install uv for fast, reproducible dependency resolution
RUN pip install --no-cache-dir uv

# Copy only the project manifest first so Docker can cache the layer
COPY pyproject.toml .
# README is required by hatchling; keep it minimal
COPY README.md .

# Install all runtime dependencies into the system Python
RUN uv pip install --system .

# Copy remaining source
COPY . .

# ── Stage 2: production ───────────────────────────────────────────────────────
FROM python:3.13-alpine AS production

# Apply latest Alpine security patches
RUN apk upgrade --no-cache

WORKDIR /app

# Copy fully-installed /app (source + site-packages written in by uv --system)
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Non-root user for K8s / container security hardening
RUN adduser -D -u 1001 mcp

# Persistent directories — owned by mcp so the server can write at runtime
RUN mkdir -p /app/data/diskcache /app/runbooks \
    && chown -R mcp:mcp /app/data /app/runbooks

USER mcp

# Python runtime flags
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# K8s liveness probe port (asyncio HTTP server in services/health.py)
EXPOSE 8080

# MCP servers communicate over stdio by default; do NOT expose a TCP MCP port.
# The entrypoint must remain the Python process — no shell wrapper — so the
# MCP host can attach stdin/stdout directly.
CMD ["python", "-m", "main"]
