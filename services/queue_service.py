"""
Enqueue background work to RQ when REDIS_URL is set and USE_RQ is not disabled.

Do not import ``rq`` at module load time: ``import rq`` pulls in the worker stack,
which uses multiprocessing "fork" and crashes on Windows before the app starts.
We only import ``rq.queue.Queue`` inside ``enqueue_whatsapp_job`` (no fork on import).
"""
from __future__ import annotations

import base64
import os
import sys

from utils.logger import logger

QUEUE_NAME = os.environ.get("RQ_QUEUE_NAME", "waweb")


def _rq_supported_platform() -> bool:
    """RQ imports a worker stack that requires Unix fork; not usable on native Windows."""
    return sys.platform != "win32"


def use_rq_worker() -> bool:
    if os.environ.get("USE_RQ", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if not (os.environ.get("REDIS_URL") or "").strip():
        return False
    if not _rq_supported_platform():
        logger.info(
            "RQ disabled on Windows (any ``import rq`` pulls fork-based code). "
            "Using in-process threads; Redis sessions still work. Run the worker on "
            "Linux/WSL/Docker if you need RQ."
        )
        return False
    return True


def enqueue_whatsapp_job(
    sender: str,
    display_text: str,
    image_parts: list | None,
    message_sid: str,
) -> None:
    from redis import Redis
    from rq.queue import Queue

    redis_url = os.environ["REDIS_URL"].strip()
    conn = Redis.from_url(redis_url)
    q = Queue(QUEUE_NAME, connection=conn)
    payload = [
        {
            "mime_type": p["mime_type"],
            "data_b64": base64.b64encode(p["data"]).decode("ascii"),
        }
        for p in (image_parts or [])
    ]
    timeout = int(os.environ.get("RQ_JOB_TIMEOUT", "600"))
    job = q.enqueue(
        "jobs.whatsapp_job.process_whatsapp_message",
        sender,
        display_text,
        payload,
        message_sid,
        job_timeout=timeout,
    )
    logger.info("Enqueued RQ job id=%s for %s", job.id, sender)
