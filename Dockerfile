# Server image: REST API, setup page and the MCP app at /mcp.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SLCLOUD_DATA=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# pyproject depends on superlite via a git URL, so pip needs git at install time. git stays in
# the image: the files routes shell out to it at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && pip install --no-cache-dir . \
 && rm -rf /var/lib/apt/lists/*

EXPOSE 8787
CMD ["slcloud", "serve"]
