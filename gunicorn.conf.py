# Gunicorn — production-style WSGI server for Flask.
#
# Windows: Gunicorn does not run on Windows (requires Unix fcntl). Use instead:
#   python serve_waitress.py
#
# IMPORTANT: use workers = 1 for this project. Each worker process would spawn its own
# MCP stdio subprocesses (GitHub/Netlify/Pinecone) and Gemini clients; multiple workers
# mean duplicated MCP servers and higher memory. For horizontal scale, run one gunicorn
# with 1 worker + separate RQ workers, or externalize MCP to a sidecar later.
#
# Run from project root:
#   gunicorn -c gunicorn.conf.py app:app

import os

bind = os.environ.get("BIND", "0.0.0.0:5000")
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
