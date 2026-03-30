"""
RQ worker: process WhatsApp background jobs (Gemini + MCP + Twilio send).

Requires REDIS_URL. From project root (Linux / macOS, or WSL):

  python run_worker.py

Windows: the default RQ worker stack expects Unix ``fork``. Run the worker in WSL
or Docker, or on Windows set USE_RQ=0 and use in-process threads (see queue_service).
"""
import os
import sys

from dotenv import load_dotenv

from services.queue_service import QUEUE_NAME

load_dotenv()

if __name__ == "__main__":
    if sys.platform == "win32":
        raise SystemExit(
            "RQ worker cannot run on native Windows (RQ depends on Unix fork).\n"
            "  • On this PC: set USE_RQ=0 in .env — WhatsApp jobs use threads.\n"
            "  • Or run this script in WSL / Linux / Docker with the same REDIS_URL.\n"
        )

    from redis import Redis
    from rq import SimpleWorker
    from rq.queue import Queue

    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        raise SystemExit("REDIS_URL is required to run the RQ worker")
    conn = Redis.from_url(url)
    queues = [Queue(QUEUE_NAME, connection=conn)]
    worker = SimpleWorker(queues, connection=conn)
    print(f"RQ SimpleWorker on queue {QUEUE_NAME!r} ({url})")
    worker.work()
