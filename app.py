from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
import base64
import threading
from services.twilio_service import TwilioService
from services.ai_service import GeminiService
from services.mcp_manager import MCPManager
from services.session_store import get_session_store
from services import queue_service
from utils.chat_history import retain_only_last_user_images
from utils.logger import logger
import secrets

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# Initialize services
try:
    twilio_service = TwilioService()
    mcp_manager = MCPManager()
    mcp_manager.start()
    gemini_service = GeminiService(mcp_manager)
    session_store = get_session_store()
    logger.info("Services initialized successfully")
except Exception as e:
    logger.error(f"Error initializing services: {str(e)}")
    raise


def ensure_user_session(user_id):
    """Ensure session row exists (Redis or memory)."""
    session_store.ensure_session(user_id)

def _twilio_form_get(request, key: str, default: str = "") -> str:
    """
    Twilio sends inbound webhooks as application/x-www-form-urlencoded.
    Prefer request.form (see Twilio Flask blog); fall back to request.values.
    Empty string is valid (e.g. image with no caption) — use key in form, not truthiness.
    """
    if key in request.form:
        v = request.form.get(key)
        if v is None:
            return default
        return v.strip() if isinstance(v, str) else str(v)
    v = request.values.get(key, default)
    if v is None:
        return default
    return v.strip() if isinstance(v, str) else str(v)


def _parse_twilio_media(request, twilio_service):
    """
    Download WhatsApp / MMS media from MediaUrl0, MediaUrl1, ...
    Per Twilio docs, iterate URLs until MediaUrl{i} is missing — do not rely on NumMedia alone.
    """
    out = []
    try:
        num_media = int(_twilio_form_get(request, "NumMedia", "0") or "0")
    except (TypeError, ValueError):
        num_media = 0

    max_bytes = 5 * 1024 * 1024
    max_items = 10

    for i in range(max_items):
        url = _twilio_form_get(request, f"MediaUrl{i}", "")
        if not url:
            break
        hint = _twilio_form_get(request, f"MediaContentType{i}", "") or None
        try:
            data, mime = twilio_service.download_media(url, content_type_hint=hint)
        except Exception as e:
            logger.warning("Failed to download media %s: %s", i, e)
            continue
        if len(data) > max_bytes:
            logger.warning("Skipping media %s: size %s exceeds cap", i, len(data))
            continue
        out.append({"mime_type": mime, "data": data})

    if num_media and len(out) != num_media:
        logger.warning(
            "NumMedia=%s but downloaded %s file(s); check MediaUrl fields",
            num_media,
            len(out),
        )

    return out

def process_message_background(sender, incoming_msg, image_parts=None):
    """
    Run Gemini + MCP after Twilio has received a fast webhook ack.
    Sends the real reply via Twilio REST API.
    """
    try:
        logger.info(f"[BG] AI processing for {sender}")
        sess = session_store.get_session(sender)
        github_token = sess.get("github_token")
        chat_history = sess.get("chat_history", [])

        ai_response = gemini_service.generate_response(
            incoming_msg,
            github_token,
            chat_history,
            image_parts=image_parts or [],
        )

        sess["chat_history"].append({
            "role": "assistant",
            "content": ai_response
        })
        retain_only_last_user_images(sess["chat_history"])
        if len(sess["chat_history"]) > 20:
            sess["chat_history"] = sess["chat_history"][-20:]
        session_store.save_session(sender, sess)

        logger.info(f"[BG] Response ready for {sender}, length: {len(ai_response)}")
        twilio_service.send_message(sender, ai_response)
    except Exception as e:
        logger.error(f"[BG] Error processing message for {sender}: {str(e)}")
        try:
            twilio_service.send_message(
                sender,
                "Sorry, I hit an error while processing your message. Please try again.",
            )
        except Exception as send_err:
            logger.error(f"[BG] Failed to send error notice: {str(send_err)}")

@app.route('/webhook', methods=['POST'])
def webhook():
    """WhatsApp webhook endpoint"""
    try:
        incoming_msg = _twilio_form_get(request, "Body", "")
        sender = _twilio_form_get(request, "From", "")

        image_parts = _parse_twilio_media(request, twilio_service)

        preview = incoming_msg[:50] if incoming_msg else "(no text)"
        logger.info(
            "Webhook form keys sample: From/Body/NumMedia present | "
            "NumMedia=%s | media downloaded=%s",
            _twilio_form_get(request, "NumMedia", "?"),
            len(image_parts),
        )
        logger.info("Received from %s: %s... ", sender, preview)
        
        if not sender:
            logger.warning("Sender missing")
            return twilio_service.create_response("Error: sender missing.")
        if not incoming_msg and not image_parts:
            logger.warning("Message or sender information missing")
            return twilio_service.create_response("Error: Message or sender information missing.")

        message_sid = _twilio_form_get(request, "MessageSid", "")
        if not session_store.try_claim_twilio_message(message_sid):
            logger.info("Duplicate Twilio webhook ignored MessageSid=%s", message_sid)
            ack = "Got your message — I'm working on it and will reply here in a moment."
            return twilio_service.create_response(ack)

        ensure_user_session(sender)

        display_text = incoming_msg or "(sent image(s))"
        user_entry = {"role": "user", "content": display_text}
        if image_parts:
            user_entry["images_b64"] = [
                {
                    "mime_type": p["mime_type"],
                    "data_b64": base64.b64encode(p["data"]).decode("ascii"),
                }
                for p in image_parts
            ]
        sess = session_store.get_session(sender)
        sess["chat_history"].append(user_entry)
        session_store.save_session(sender, sess)

        if queue_service.use_rq_worker():
            queue_service.enqueue_whatsapp_job(
                sender, display_text, image_parts, message_sid
            )
        else:
            threading.Thread(
                target=process_message_background,
                args=(sender, display_text, image_parts),
                daemon=True,
            ).start()

        # Immediate TwiML so Twilio does not time out (~15s) while AI runs
        ack = "Got your message — I'm working on it and will reply here in a moment."
        return twilio_service.create_response(ack)
        
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        return twilio_service.create_response("Sorry, I encountered an error. Please try again.")

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """
    API endpoint for testing without WhatsApp/Twilio
    Send POST requests with JSON: {"message": "your message", "user_id": "test_user"}
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "JSON data required"}), 400
        
        message = data.get('message', '').strip()
        user_id = data.get('user_id', 'api_user')  # Default user ID for API testing
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        logger.info(f"API: Received message from {user_id}: {message[:50]}...")
        
        ensure_user_session(user_id)

        sess = session_store.get_session(user_id)
        sess["chat_history"].append({"role": "user", "content": message})
        session_store.save_session(user_id, sess)

        github_token = sess.get("github_token")
        chat_history = sess.get("chat_history", [])

        ai_response = gemini_service.generate_response(message, github_token, chat_history)

        sess = session_store.get_session(user_id)
        sess["chat_history"].append({"role": "assistant", "content": ai_response})
        if len(sess["chat_history"]) > 20:
            sess["chat_history"] = sess["chat_history"][-20:]
        session_store.save_session(user_id, sess)

        logger.info(f"API: Response generated, length: {len(ai_response)}")

        return jsonify({
            "response": ai_response,
            "user_id": user_id,
            "message_type": "ai_response",
            "chat_history_length": len(sess["chat_history"]),
            "has_github_token": bool(github_token)
        })
        
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return jsonify({"error": "Sorry, I encountered an error. Please try again."}), 500

@app.route('/api/chat/history/<user_id>', methods=['GET'])
def get_chat_history(user_id):
    """Get chat history for a specific user"""
    try:
        if not session_store.session_exists(user_id):
            return jsonify({"error": "User session not found"}), 404

        sess = session_store.get_session(user_id)
        return jsonify({
            "user_id": user_id,
            "chat_history": sess["chat_history"],
            "has_github_token": bool(sess.get("github_token"))
        })
    except Exception as e:
        logger.error(f"Error getting chat history: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/chat/clear/<user_id>', methods=['DELETE'])
def clear_chat_history(user_id):
    """Clear chat history for a specific user"""
    try:
        if session_store.session_exists(user_id):
            sess = session_store.get_session(user_id)
            github_token = sess.get("github_token")
            session_store.save_session(
                user_id,
                {"github_token": github_token, "chat_history": []},
            )
            logger.info(f"Chat history cleared for {user_id}")
            return jsonify({"message": "Chat history cleared", "user_id": user_id})
        return jsonify({"error": "User session not found"}), 404
    except Exception as e:
        logger.error(f"Error clearing chat history: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        store_ok = session_store.ping()
        return jsonify({
            "status": "healthy" if store_ok else "degraded",
            "service": "waWeb",
            "services": {
                "twilio": "initialized",
                "gemini": "initialized",
                "session_store": "ok" if store_ok else "unavailable",
                "rq_worker": "enabled" if queue_service.use_rq_worker() else "disabled",
            }
        })
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/', methods=['GET'])
def test_endpoint():
    """Root endpoint with API information"""
    return jsonify({
        "message": "waWeb API is running", 
        "status": "operational",
        "endpoints": {
            "webhook": "/webhook (POST) - Twilio webhook",
            "api_chat": "/api/chat (POST) - API endpoint for testing",
            "chat_history": "/api/chat/history/<user_id> (GET) - Get chat history",
            "clear_history": "/api/chat/clear/<user_id> (DELETE) - Clear chat history",
            "health": "/health (GET) - Health check"
        }
    })

if __name__ == '__main__':
    logger.info("Starting waWeb server on port 5000")
    app.run(host='0.0.0.0', port=5000, debug=True) 