"""
Chat sessions and Twilio webhook idempotency.

- REDIS_URL set  → Redis (survives restarts, shareable across processes)
- REDIS_URL unset → in-memory (dev / single-process only)
"""
from __future__ import annotations

import json
import os
import threading
from abc import ABC, abstractmethod
from typing import Any

from utils.logger import logger

_IDEMPOTENCY_TTL = int(os.environ.get("TWILIO_IDEMPOTENCY_TTL_SECONDS", "86400"))


def _default_session(user_id: str) -> dict[str, Any]:
    return {
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "chat_history": [],
    }


class SessionStore(ABC):
    @abstractmethod
    def ensure_session(self, user_id: str) -> None:
        pass

    @abstractmethod
    def get_session(self, user_id: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def save_session(self, user_id: str, data: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def session_exists(self, user_id: str) -> bool:
        pass

    @abstractmethod
    def try_claim_twilio_message(self, message_sid: str) -> bool:
        """
        Return True if this delivery should be processed (first time).
        Return False if Twilio retried the same MessageSid (duplicate webhook).
        """
        pass

    @abstractmethod
    def ping(self) -> bool:
        """For health checks; always True for memory."""
        pass


class MemorySessionStore(SessionStore):
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._seen_sids: dict[str, float] = {}

    def ensure_session(self, user_id: str) -> None:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = _default_session(user_id)

    def get_session(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = _default_session(user_id)
            return json.loads(json.dumps(self._sessions[user_id]))

    def save_session(self, user_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._sessions[user_id] = json.loads(json.dumps(data))

    def session_exists(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._sessions

    def try_claim_twilio_message(self, message_sid: str) -> bool:
        if not message_sid:
            return True
        with self._lock:
            if message_sid in self._seen_sids:
                return False
            self._seen_sids[message_sid] = 1.0
            return True

    def ping(self) -> bool:
        return True


class RedisSessionStore(SessionStore):
    def __init__(self, url: str):
        from redis import Redis

        self._r = Redis.from_url(url, decode_responses=True)
        self._session_prefix = os.environ.get("REDIS_SESSION_PREFIX", "waweb:session:")
        self._msg_prefix = os.environ.get("REDIS_IDEMPOTENCY_PREFIX", "waweb:msg:")

    def _session_key(self, user_id: str) -> str:
        return f"{self._session_prefix}{user_id}"

    def ensure_session(self, user_id: str) -> None:
        key = self._session_key(user_id)
        if not self._r.exists(key):
            self._r.set(key, json.dumps(_default_session(user_id)))

    def get_session(self, user_id: str) -> dict[str, Any]:
        key = self._session_key(user_id)
        raw = self._r.get(key)
        if not raw:
            data = _default_session(user_id)
            self._r.set(key, json.dumps(data))
            return data
        return json.loads(raw)

    def save_session(self, user_id: str, data: dict[str, Any]) -> None:
        self._r.set(self._session_key(user_id), json.dumps(data))

    def session_exists(self, user_id: str) -> bool:
        return bool(self._r.exists(self._session_key(user_id)))

    def try_claim_twilio_message(self, message_sid: str) -> bool:
        if not message_sid:
            return True
        key = f"{self._msg_prefix}{message_sid}"
        ok = self._r.set(key, "1", nx=True, ex=_IDEMPOTENCY_TTL)
        return bool(ok)

    def ping(self) -> bool:
        try:
            return self._r.ping()
        except Exception as e:
            logger.warning("Redis ping failed: %s", e)
            return False


_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    global _store
    with _store_lock:
        if _store is None:
            url = (os.environ.get("REDIS_URL") or "").strip()
            if url:
                logger.info("Session store: Redis")
                _store = RedisSessionStore(url)
            else:
                logger.info("Session store: in-memory (set REDIS_URL for Redis)")
                _store = MemorySessionStore()
        return _store


def reset_session_store_for_tests():
    """Optional test hook."""
    global _store
    with _store_lock:
        _store = None
