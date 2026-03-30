FROM python:3.12-slim

# System deps: node/npm for npx MCP servers, build tools for pip wheels.
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    nodejs \
    npm \
    git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code.
COPY . /app

ENV PYTHONUNBUFFERED=1
ENV PORT=5000

EXPOSE 5000

# Default: run the web server.
CMD ["python", "serve_waitress.py"]

