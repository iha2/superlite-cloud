# Server image: REST API, setup page and the MCP app at /mcp.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SLCLOUD_DATA=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# pyproject depends on superlite via a git URL, so pip needs git at install time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && pip install --no-cache-dir . \
 && apt-get purge -y --auto-remove git \
 && rm -rf /var/lib/apt/lists/*

EXPOSE 8787
CMD ["slcloud", "serve"]
