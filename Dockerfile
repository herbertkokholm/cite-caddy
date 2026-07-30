FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app

# --frozen: install exactly what's pinned in uv.lock (fail instead of
# re-resolving) so the image matches what's tested locally/in CI, instead
# of silently picking up whatever's newest on PyPI at build time.
RUN uv sync --frozen --no-dev

# Deploy host's nginx vhost proxies to 127.0.0.1:<port>, mapped via
# `docker run -p 127.0.0.1:<port>:8000`. $PORT set (to 8000, matching this
# image) is what tells app/mcp_server.py to serve streamable-http instead
# of stdio.
ENV PORT=8000
EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "app.mcp_server"]
