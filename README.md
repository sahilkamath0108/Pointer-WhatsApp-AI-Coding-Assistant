# Pointer - WhatsApp AI Coding Assistant

A WhatsApp-first AI coding assistant powered by Google Gemini. It receives messages (and images) via Twilio webhooks, acknowledges immediately (to avoid Twilio timeouts), then generates the final response in the background and sends it via Twilio REST.

## Features

- **WhatsApp via Twilio**: inbound webhook (`POST /webhook`) + outbound replies via Twilio REST.
- **Immediate ack + background processing**: avoids webhook timeout limits; real reply is sent later.
- **Images + text**: receives WhatsApp media (`MediaUrl0..`) and sends multimodal input to Gemini.
- **Gemini tool-calling**: uses MCP tool declarations + `function_response` protocol for reliable tool execution.
- **Redis sessions + idempotency**: per-user chat history in Redis, plus MessageSid dedupe to avoid double-processing.
- **Job queue (optional)**: Redis + RQ worker for scalable background processing; fallback to threads when disabled.
- **REST API**: test the assistant without Twilio (`POST /api/chat`).

## Quick Start

### Prerequisites

- Python 3.10+
- Twilio account with WhatsApp sandbox
- Google Gemini API key
- (Optional) GitHub token (for GitHub MCP tools)
- (Optional) Netlify token (for Netlify MCP tools)
- (Recommended) Redis (for durable sessions + queue)
- Node.js / npm (only needed when running MCP locally without Docker; Docker image already includes it)

### Installation

1. **Clone the repository**
   ```bash
   git clone <your-repo-url>
   cd waWeb
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**
   
   Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` with your values (names match the current code):
   ```env
   GEMINI_API_KEY=your_gemini_api_key_here
   TWILIO_ACCOUNT_SID=your_twilio_account_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_PHONE_NUMBER=whatsapp:+14155238886
   SECRET_KEY=your_secret_key_here
   ```

4. **Run the application**

   Dev server:
   ```bash
   python run.py
   ```

   Production-style server on Windows:
   ```bash
   python serve_waitress.py
   ```

## Docker (recommended on Windows)

This project uses MCP servers started via `npx`, and background processing via Redis + RQ.
Running in Docker gives you a Linux environment even on Windows.

### Prerequisites

- Docker Desktop
- Twilio + Gemini credentials in `.env`

### Run

1. Copy env file:

```bash
cp .env.example .env
```

2. Start web + worker + Redis:

```bash
docker compose up --build
```

- Web listens on `http://localhost:5000`
- Set Twilio webhook to `https://<ngrok-domain>/webhook`
- Use ngrok from your host: `ngrok http 5000`

### Notes

- First run can take a few minutes while `npx` downloads MCP packages.
- For local dev without the queue, set `USE_RQ=0` to use threads (still supports Redis sessions).

## Usage

### WhatsApp Integration

1. **Set up Twilio WhatsApp Sandbox**
   - Go to Twilio Console > Messaging > Try it out > Send a WhatsApp message
   - Follow instructions to join the sandbox

2. **Configure Webhook**
   - Set webhook URL to: `https://your-domain.com/webhook`
   - Use ngrok for local development: `ngrok http 5000`

3. **Start Chatting**
   - Send messages to your Twilio WhatsApp number
   - The AI will respond with coding help and guidance

### GitHub / Netlify tools (optional)

MCP tools are enabled via environment variables:
- `GITHUB_TOKEN` enables GitHub MCP tools
- `NETLIFY_API_KEY` enables Netlify MCP tools

Tokens are **not** read from WhatsApp messages.

### API Testing

Test the AI assistant without WhatsApp using the REST API:

```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Help me create a simple Python web app",
    "user_id": "test_user"
  }'
```

## API Endpoints

- `POST /webhook` - Twilio WhatsApp webhook
- `POST /api/chat` - Chat with AI assistant
- `GET /api/chat/history/<user_id>` - Get chat history
- `DELETE /api/chat/clear/<user_id>` - Clear chat history
- `GET /health` - Health check
- `GET /` - API information

## Project Structure

```
waWeb/
├── app.py                 # Flask app + Twilio webhook + API endpoints
├── run.py                 # Dev runner
├── serve_waitress.py      # Production-style runner (Windows/cross-platform)
├── run_worker.py          # RQ worker runner (Linux/WSL/Docker)
├── requirements.txt       # Python dependencies
├── docker-compose.yml     # web + worker + redis
├── Dockerfile
├── .env.example           # Environment variables template
├── services/
│   ├── ai_service.py       # Google Gemini integration (multimodal + tool loop)
│   ├── twilio_service.py   # Twilio TwiML + REST send + media download
│   ├── mcp_manager.py      # Persistent MCP sessions (stdio + tool cache)
│   ├── session_store.py    # Redis/memory sessions + Twilio idempotency
│   └── queue_service.py    # RQ enqueue helper (auto-disabled on Windows)
├── jobs/
│   └── whatsapp_job.py     # Background job implementation
├── utils/
│   ├── logger.py           # Logging
│   ├── code_formatter.py   # WhatsApp-friendly formatting + truncation
│   └── chat_history.py     # Shared history helpers (image trimming)
└── logs/                 # Application logs
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Google Gemini API key | Yes |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | Yes |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | Yes |
| `TWILIO_PHONE_NUMBER` | Twilio WhatsApp number (e.g. `whatsapp:+14155238886`) | Yes |
| `SECRET_KEY` | Flask secret key | No (auto-generated) |
| `REDIS_URL` | Redis URL for sessions + idempotency + RQ | Recommended |
| `USE_RQ` | Enable RQ background jobs (requires Redis; auto-disabled on Windows) | No |
| `MCP_START_TIMEOUT` | Seconds to wait for MCP servers to start and cache tools | No |
| `GITHUB_TOKEN` | GitHub token for GitHub MCP tools | Optional |
| `NETLIFY_API_KEY` | Netlify token for Netlify MCP tools | Optional |

### Getting API Keys

1. **Google Gemini API Key**
   - Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
   - Create a new API key
   - Copy the key to your `.env` file

2. **Twilio Credentials**
   - Sign up at [Twilio](https://www.twilio.com/)
   - Go to Console Dashboard
   - Copy Account SID and Auth Token
   - Set up WhatsApp sandbox for testing

## Deployment

### Local Development
```bash
python run.py
```

### Production Deployment

#### Docker Compose (recommended)
```bash
docker compose up --build
```

#### Linux/macOS (Gunicorn)
Gunicorn is Unix-only. Use `gunicorn -c gunicorn.conf.py app:app`.

#### Windows (Waitress)
```bash
python serve_waitress.py
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

If you encounter any issues or have questions:

1. Check the logs in the `logs/` directory
2. Ensure all environment variables are correctly set
3. Verify your API keys are valid
4. Check the health endpoint: `GET /health`

## Troubleshooting

### Common Issues

1. **"Gemini API key not found"**
   - Ensure `GEMINI_API_KEY` is set in your `.env` file

2. **"Twilio authentication failed"**
   - Verify `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are correct

3. **"WhatsApp messages not received"**
   - Check your webhook URL configuration in Twilio
   - Ensure your server is publicly accessible (use ngrok for local testing)

4. **"Internal server error"**
   - Check the application logs in the `logs/` directory
   - Verify all required environment variables are set 

5. **Docker starts but MCP tools are missing**
   - First boot may take time while `npx` downloads MCP packages; wait and retry.
   - Increase `MCP_START_TIMEOUT` (e.g. `600`) if needed.
