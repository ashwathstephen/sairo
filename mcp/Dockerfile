FROM python:3.12-slim

# Security: non-root user
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -m appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create tmp dir for any scratch files
RUN mkdir -p /tmp/mcp && chown appuser:appgroup /tmp/mcp

USER appuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/healthz')"

ENTRYPOINT ["python", "server.py"]
