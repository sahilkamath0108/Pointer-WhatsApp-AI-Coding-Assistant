"""
Production-style WSGI server for Windows (and cross-platform).

Gunicorn is Unix-only. Waitress is pure Python and works on Windows.

From project root:
  python serve_waitress.py

Still use WEB_CONCURRENCY=1 mentally: one process; tune WAITRESS_THREADS only.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from waitress import serve  # noqa: E402 — after load_dotenv

from app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "8"))
    host = os.environ.get("BIND_HOST", "0.0.0.0")
    print(f"Waitress http://{host}:{port}/ (threads={threads})")
    serve(app, host=host, port=port, threads=threads)
