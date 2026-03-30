"""
RQ worker entry: process WhatsApp messages after webhook ack.

Run worker from project root:
  rq worker -u REDIS_URL waweb
"""
from __future__ import annotations

import base64

from dotenv import load_dotenv

from utils.chat_history import retain_only_last_user_images
from utils.logger import logger

_runtime = None


def _get_runtime():
    global _runtime
    if _runtime is None:
        load_dotenv()
        from services.twilio_service import TwilioService
        from services.mcp_manager import MCPManager
        from services.ai_service import GeminiService
        from services.session_store import get_session_store

        twilio_service = TwilioService()
        mcp_manager = MCPManager()
        mcp_manager.start()
        gemini_service = GeminiService(mcp_manager)
        session_store = get_session_store()
        _runtime = (twilio_service, gemini_service, session_store)
        logger.info("RQ worker runtime initialized (Twilio + MCP + Gemini + store)")
    return _runtime


def process_whatsapp_message(
    sender: str,
    display_text: str,
    images_payload: list | None,
    message_sid: str,
):
    """
    images_payload: list of {"mime_type": str, "data_b64": str} (JSON-serializable).
    message_sid: for logging only (idempotency handled before enqueue).
    """
    images_payload = images_payload or []
    twilio_service, gemini_service, session_store = _get_runtime()
    try:
        logger.info("[RQ] AI processing for %s MessageSid=%s", sender, message_sid or "n/a")
        sess = session_store.get_session(sender)
        github_token = sess.get("github_token")
        chat_history = sess.get("chat_history", [])

        image_parts = [
            {"mime_type": p["mime_type"], "data": base64.b64decode(p["data_b64"])}
            for p in images_payload
        ]

        ai_response = gemini_service.generate_response(
            display_text,
            github_token,
            chat_history,
            image_parts=image_parts,
        )

        sess["chat_history"].append({"role": "assistant", "content": ai_response})
        retain_only_last_user_images(sess["chat_history"])
        if len(sess["chat_history"]) > 20:
            sess["chat_history"] = sess["chat_history"][-20:]
        session_store.save_session(sender, sess)

        logger.info("[RQ] Response ready for %s, length=%s", sender, len(ai_response))
        twilio_service.send_message(sender, ai_response)
    except Exception as e:
        logger.error("[RQ] Error processing message for %s: %s", sender, e)
        try:
            twilio_service.send_message(
                sender,
                "Sorry, I hit an error while processing your message. Please try again.",
            )
        except Exception as send_err:
            logger.error("[RQ] Failed to send error notice: %s", send_err)
