# Container image for running fpl-context-mcp as a hosted streamable-HTTP server.
#
#   docker build -t fpl-context-mcp .
#   docker run -p 8000:8000 -e MCP_AUTH_TOKEN=... fpl-context-mcp
#
# See docs/hosting.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_RATE_LIMIT_PER_MIN=120

WORKDIR /app
COPY pyproject.toml README.md LICENSE config.py server.py ./
COPY tools ./tools
RUN pip install .

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app
USER app

# Listens on $MCP_PORT, else $PORT (set by Render, Cloud Run, Fly...), else 8000.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % (os.environ.get('MCP_PORT') or os.environ.get('PORT') or '8000'), timeout=4)" || exit 1

CMD ["fpl-context-mcp"]
