from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
import base64
import threading
from services.twilio_service import TwilioService
from services.ai_service import GeminiService
from services.mcp_manager import MCPManager
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
    logger.info("Services initialized successfully")
except Exception as e:
    logger.error(f"Error initializing services: {str(e)}")
    raise

# Session management for user conversations
user_sessions = {}

def ensure_user_session(user_id):
    """
    Initialize user session if it doesn't exist (used by both WhatsApp and API endpoints)
    GitHub token is always loaded from environment variables
    """
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "github_token": os.environ.get('GITHUB_TOKEN'),  # Load from env
            "chat_history": []
        }

def _parse_twilio_media(request, twilio_service):
    """Download WhatsApp media; returns list of {mime_type, data: bytes}."""
    out = []
    try:
        n = int(request.values.get("NumMedia", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    max_bytes = 5 * 1024 * 1024
    max_items = 5
    for i in range(min(n, max_items)):
        url = request.values.get(f"MediaUrl{i}")
        if not url:
            continue
        try:
            data, mime = twilio_service.download_media(url)
        except Exception as e:
            logger.warning(f"Failed to download media {i}: {e}")
            continue
        if len(data) > max_bytes:
            logger.warning(f"Skipping media {i}: size {len(data)} exceeds cap")
            continue
        out.append({"mime_type": mime, "data": data})
    return out

def _retain_only_last_user_images(history):
    """Drop image payloads from older user turns to limit memory and token use."""
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return
    for i, m in enumerate(history):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if i == last_user_idx or not m.get("images_b64"):
            continue
        new_m = dict(m)
        new_m.pop("images_b64", None)
        c = new_m.get("content") or ""
        if "[earlier image omitted]" not in c:
            new_m["content"] = c + " [earlier image omitted]"
        history[i] = new_m

def process_message_background(sender, incoming_msg, image_parts=None):
    """
    Run Gemini + MCP after Twilio has received a fast webhook ack.
    Sends the real reply via Twilio REST API.
    """
    try:
        logger.info(f"[BG] AI processing for {sender}")
        github_token = user_sessions[sender].get("github_token")
        chat_history = user_sessions[sender].get("chat_history", [])

        ai_response = gemini_service.generate_response(
            incoming_msg,
            github_token,
            chat_history,
            image_parts=image_parts or [],
        )

        user_sessions[sender]["chat_history"].append({
            "role": "assistant",
            "content": ai_response
        })
        _retain_only_last_user_images(user_sessions[sender]["chat_history"])
        if len(user_sessions[sender]["chat_history"]) > 20:
            user_sessions[sender]["chat_history"] = user_sessions[sender]["chat_history"][-20:]

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
        incoming_msg = request.values.get('Body', '').strip()
        sender = request.values.get('From', '')

        image_parts = _parse_twilio_media(request, twilio_service)
        
        preview = incoming_msg[:50] if incoming_msg else "(no text)"
        logger.info(
            f"Received from {sender}: {preview}... "
            f"media={len(image_parts)}"
        )
        
        if not sender:
            logger.warning("Sender missing")
            return twilio_service.create_response("Error: sender missing.")
        if not incoming_msg and not image_parts:
            logger.warning("Message or sender information missing")
            return twilio_service.create_response("Error: Message or sender information missing.")
        
        # Ensure user session exists
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
        user_sessions[sender]["chat_history"].append(user_entry)

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
        
        # Ensure user session exists
        ensure_user_session(user_id)
        
        # Add user message to chat history
        user_sessions[user_id]["chat_history"].append({
            "role": "user",
            "content": message
        })
        
        # Pass session info and chat history to AI service
        github_token = user_sessions[user_id].get("github_token")
        chat_history = user_sessions[user_id].get("chat_history", [])
        
        ai_response = gemini_service.generate_response(message, github_token, chat_history)
        
        # Add AI response to chat history
        user_sessions[user_id]["chat_history"].append({
            "role": "assistant",
            "content": ai_response
        })
        
        # Keep chat history manageable (limit to last 20 messages)
        if len(user_sessions[user_id]["chat_history"]) > 20:
            user_sessions[user_id]["chat_history"] = user_sessions[user_id]["chat_history"][-20:]
        
        logger.info(f"API: Response generated, length: {len(ai_response)}")
        
        return jsonify({
            "response": ai_response,
            "user_id": user_id,
            "message_type": "ai_response",
            "chat_history_length": len(user_sessions[user_id]["chat_history"]),
            "has_github_token": bool(github_token)
        })
        
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return jsonify({"error": "Sorry, I encountered an error. Please try again."}), 500

@app.route('/api/chat/history/<user_id>', methods=['GET'])
def get_chat_history(user_id):
    """Get chat history for a specific user"""
    try:
        if user_id not in user_sessions:
            return jsonify({"error": "User session not found"}), 404
        
        return jsonify({
            "user_id": user_id,
            "chat_history": user_sessions[user_id]["chat_history"],
            "has_github_token": bool(user_sessions[user_id].get("github_token"))
        })
    except Exception as e:
        logger.error(f"Error getting chat history: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/chat/clear/<user_id>', methods=['DELETE'])
def clear_chat_history(user_id):
    """Clear chat history for a specific user"""
    try:
        if user_id in user_sessions:
            # Keep the GitHub token but clear chat history
            github_token = user_sessions[user_id].get("github_token")
            user_sessions[user_id] = {
                "github_token": github_token,
                "chat_history": []
            }
            logger.info(f"Chat history cleared for {user_id}")
            return jsonify({"message": "Chat history cleared", "user_id": user_id})
        else:
            return jsonify({"error": "User session not found"}), 404
    except Exception as e:
        logger.error(f"Error clearing chat history: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        return jsonify({
            "status": "healthy", 
            "service": "waWeb",
            "services": {
                "twilio": "initialized",
                "gemini": "initialized"
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