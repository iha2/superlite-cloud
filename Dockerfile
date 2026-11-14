# Server image: REST API, setup page and the MCP app at /mcp.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SLCLOUD_DATA=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8787
CMD ["slcloud", "serve"]
