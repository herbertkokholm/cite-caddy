FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir .

# Deploy host's nginx vhost proxies to 127.0.0.1:<port>, mapped via
# `docker run -p 127.0.0.1:<port>:8000`. $PORT set (to 8000, matching this
# image) is what tells app/mcp_server.py to serve streamable-http instead
# of stdio.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-m", "app.mcp_server"]
