from google import genai
import os
import asyncio
import base64
import json
import time
from utils.logger import logger
from utils.code_formatter import format_code_for_whatsapp, truncate_message
from google.genai import types
from services.mcp_manager import MCPManager


class GeminiService:
    def __init__(self, mcp_manager: MCPManager):
        api_key = os.environ.get('GEMINI_API_KEY')
        if not api_key:
            logger.error("Gemini API key not found in environment variables")
            raise ValueError("Gemini API key not found. Please set GEMINI_API_KEY in your environment variables.")
        
        self.client = genai.Client(api_key=api_key)
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
        self.mcp_manager = mcp_manager
        
        # Get GitHub token from environment
        self.default_github_token = os.environ.get('GITHUB_TOKEN')
        if self.default_github_token:
            logger.info("GitHub token loaded from environment variables")
        else:
            logger.warning("No GitHub token found in environment variables")
        
        # Get Netlify token from environment
        self.netlify_token = os.environ.get('NETLIFY_API_KEY')
        if self.netlify_token:
            logger.info("Netlify token loaded from environment variables")
        else:
            logger.warning("No Netlify token found in environment variables")
            
        logger.info("Gemini AI service initialized successfully")
        
        # Define system prompt for coding assistant
        self.system_prompt = """
You are an AI Coding Assistant on WhatsApp.

Your job: help users build, manage, and improve apps or websites using the tools when needed.

IMAGES & DESIGN-TO-CODE:
- Users often send screenshots, mockups, UI designs, logos, or Figma-style layouts. You receive these as images alongside their text.
- Study layout, hierarchy, colors, typography, spacing, and components before answering.
- When they want "this as code", infer stack from context (HTML/CSS, React, etc.) or ask once if unclear.
- Describe structure (sections, grids, cards) and key styles in words; avoid dumping huge code on WhatsApp.
- If text is missing, assume they want the design translated or summarized into an implementation plan or repo changes via tools.
- For small snippets only, keep code minimal; prefer offering to create/update files via GitHub tools when the change is non-trivial.

TOOL USE (critical):
- Pick tools that match the user's request. Read each tool's name and description before calling.
- If you still need tools, do NOT output [SATISFIED]. Call the right tools first.
- After tools return, summarize outcomes briefly for the user.
- Prefer the smallest set of tools that completes the task (avoid exploratory searches unless needed).

GitHub tools: repos, issues, PRs, files, branches.
Netlify tools: deploys, sites, env vars.
Pinecone tools: only when the user asks about vectors / semantic search / their Pinecone index.

FINAL REPLY STYLE (WhatsApp):
- Short: 2–4 sentences unless the user asked for detail.
- No long code blocks; describe what changed in plain language.
- End with exactly one tag on its own line (used only by the app, then stripped):
  [SATISFIED] — you are done and need no more tools for this request.
  [NEEDS_CLARIFICATION] — you must ask the user one clear question before you can continue.
If you need tools, output normal reasoning or nothing extra; do not use [SATISFIED] until finished.
"""
        
    def _make_content(self, role: str, text: str):
        return types.Content(role=role, parts=[types.Part(text=text)])

    def _history_item_to_content(self, msg):
        """Build Gemini Content from a chat_history entry (dict with optional images or legacy shape)."""
        if isinstance(msg, str):
            return self._make_content("user", msg)
        if not isinstance(msg, dict):
            return self._make_content("user", str(msg))
        role = msg.get("role", "user")
        gemini_role = (
            "model" if role in ("assistant", "model") else "user"
        )
        if gemini_role == "model":
            return self._make_content("model", msg.get("content", ""))
        text = msg.get("content") or ""
        images = msg.get("images_b64") or []
        parts = []
        if text:
            parts.append(types.Part(text=text))
        for im in images:
            raw = base64.b64decode(im["data_b64"])
            mt = im.get("mime_type") or "image/jpeg"
            parts.append(types.Part.from_bytes(data=raw, mime_type=mt))
        if not parts:
            parts.append(types.Part(text="(empty message)"))
        return types.Content(role="user", parts=parts)

    def _build_current_user_content(self, text: str, image_parts=None):
        """Current turn: text plus optional list of {mime_type, data: bytes}."""
        image_parts = image_parts or []
        parts = []
        if text:
            parts.append(types.Part(text=text))
        for im in image_parts:
            parts.append(
                types.Part.from_bytes(
                    data=im["data"],
                    mime_type=im.get("mime_type") or "image/jpeg",
                )
            )
        if not parts:
            parts.append(types.Part(text="(empty message)"))
        return types.Content(role="user", parts=parts)

    @staticmethod
    def _mcp_result_to_function_response_dict(raw: str, max_chars: int = 12000) -> dict:
        """Build a JSON object for Gemini function_response (dict)."""
        if not raw:
            return {"result": ""}
        s = raw if len(raw) <= max_chars else raw[:max_chars] + "\n... [truncated]"
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
            return {"result": parsed}
        except json.JSONDecodeError:
            return {"result": s}

    @staticmethod
    def _strip_internal_tags(text: str) -> str:
        if not text:
            return text
        for tag in ("[SATISFIED]", "[NEEDS_CLARIFICATION]"):
            text = text.replace(tag, "").strip()
        return text

    @staticmethod
    def _is_transient_gemini_error(exc: BaseException) -> bool:
        """True for likely-retryable API failures (rate limits, Google-side 5xx)."""
        name = type(exc).__name__
        if name in (
            "ServiceUnavailable",
            "InternalServerError",
            "TooManyRequests",
            "ResourceExhausted",
            "DeadlineExceeded",
        ):
            return True
        msg = str(exc).upper()
        markers = (
            "429",
            "500",
            "503",
            "UNAVAILABLE",
            "RESOURCE_EXHAUSTED",
            "DEADLINE",
            "INTERNAL",
        )
        return any(m in msg for m in markers)

    def _generate_content_with_retry(self, conversation_messages, config):
        """Call Gemini with short backoff on transient errors."""
        delays = (1.0, 2.0, 4.0)
        last_exc = None
        for attempt, delay in enumerate((*delays, None)):
            try:
                return self.client.models.generate_content(
                    model=self.model_name,
                    contents=conversation_messages,
                    config=config,
                )
            except Exception as e:
                last_exc = e
                if delay is not None and self._is_transient_gemini_error(e):
                    logger.warning(
                        "Gemini request failed (transient), retry %s/%s after %.1fs: %s",
                        attempt + 1,
                        len(delays),
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise
        raise last_exc

    def _format_chat_history(self, chat_history):
        """Convert chat history to list[types.Content] for Gemini (excludes last user turn)."""
        if not chat_history:
            return []
        formatted = []
        for msg in chat_history[:-1]:
            formatted.append(self._history_item_to_content(msg))
        return formatted
    
    def generate_response(
        self,
        user_message,
        github_token=None,
        chat_history=None,
        image_parts=None,
    ):
        """Generate a response using Gemini API with MCP integration.

        image_parts: optional list of {"mime_type": str, "data": bytes} for the current user turn.
        """
        try:
            logger.info("Generating AI response for user message")
            
            token_to_use = github_token or self.default_github_token
            
            return asyncio.run(self._generate_response_async(
                user_message,
                token_to_use,
                chat_history,
                self.system_prompt,
                image_parts,
            ))
                
        except Exception as e:
            logger.error(f"Error generating response: {str(e)}")
            return "Sorry, I encountered an error. Please try again."
    
    async def _generate_response_async(
        self,
        user_message,
        github_token,
        chat_history,
        enhanced_system_prompt,
        image_parts=None,
    ):
        """Async version of response generation with internal satisfaction loop"""
        try:
            # Prepare base conversation
            base_messages = [
                self._make_content("user", enhanced_system_prompt),
                self._make_content("model", "I understand. I'm your AI coding assistant ready to help you build apps and websites. How can I assist you today?")
            ]
            
            if chat_history:
                formatted = self._format_chat_history(chat_history)
                base_messages.extend(formatted)
                logger.info(f"Using chat history with {len(formatted)} previous messages")

            ip = image_parts or []
            if ip:
                logger.info("Current turn includes %s image(s) for Gemini", len(ip))
            
            base_messages.append(
                self._build_current_user_content(user_message, ip)
            )
            
            # MCP tools: loaded once at app startup on a dedicated background asyncio loop
            tools = []
            tool_to_server_map = self.mcp_manager.get_tool_server_map()
            token_to_use = github_token or self.default_github_token

            cached = self.mcp_manager.get_cached_tool_declarations()
            if cached:
                tools = [types.Tool(function_declarations=cached)]
                logger.info(f"Using persistent MCP tool cache: {len(cached)} tools")
            
            # Tool loop: Gemini turns use model Content + user Content with function_response parts
            max_iterations = 8
            iteration = 0
            conversation_messages = base_messages.copy()
            final_response = None
            
            while iteration < max_iterations:
                iteration += 1
                logger.info(f"Internal loop iteration {iteration}/{max_iterations}")
                
                config = types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=4096,
                )
                
                if tools:
                    config.tools = tools
                
                logger.info("Sending request to Gemini...")
                response = self._generate_content_with_retry(
                    conversation_messages, config
                )
                function_calls = []
                response_text = None
                is_satisfied = False
                needs_clarification = False

                cand0 = (
                    response.candidates[0]
                    if response.candidates and len(response.candidates) > 0
                    else None
                )
                parts = getattr(getattr(cand0, "content", None), "parts", None)

                if parts:
                    for part in parts:
                        if hasattr(part, "function_call") and part.function_call:
                            function_calls.append(part.function_call)
                            logger.info(f"Function call: {part.function_call.name}")
                        elif hasattr(part, "text") and part.text:
                            response_text = part.text

                if response_text:
                    if "[NEEDS_CLARIFICATION]" in response_text:
                        needs_clarification = True
                        response_text = response_text.replace(
                            "[NEEDS_CLARIFICATION]", ""
                        ).strip()
                    elif "[SATISFIED]" in response_text:
                        is_satisfied = True
                        response_text = response_text.replace("[SATISFIED]", "").strip()
                    response_text = response_text.strip()
                
                # Tool round: append model turn + user function_response parts (Gemini protocol)
                if function_calls:
                    if not cand0 or not cand0.content:
                        logger.error("Function calls present but no model content")
                        final_response = "I couldn't complete the tool call. Please try again."
                        break
                    try:
                        model_turn = cand0.content
                        if not getattr(model_turn, "role", None):
                            model_turn = types.Content(
                                role="model",
                                parts=model_turn.parts or [],
                            )
                        conversation_messages.append(model_turn)
                        fr_parts = []
                        for function_call in function_calls:
                            st = tool_to_server_map.get(function_call.name)
                            logger.info(
                                f"Executing {function_call.name} on {st or 'unknown'} (MCP)"
                            )
                            raw_result = await asyncio.to_thread(
                                self.mcp_manager.execute_function_call_sync,
                                function_call,
                                token_to_use,
                            )
                            payload = self._mcp_result_to_function_response_dict(raw_result)
                            fr_parts.append(
                                types.Part.from_function_response(
                                    name=function_call.name,
                                    response=payload,
                                )
                            )
                        conversation_messages.append(
                            types.Content(role="user", parts=fr_parts)
                        )
                        logger.info("Function responses appended; continuing model loop")
                    except Exception as function_error:
                        logger.error(f"Error executing function calls: {str(function_error)}")
                        final_response = (
                            f"I hit an error running tools: {str(function_error)}"
                        )
                        break
                    continue

                # No function calls — final text to user
                if needs_clarification and response_text:
                    final_response = response_text
                    logger.info("Ending with clarification question for user")
                    break
                if is_satisfied and response_text:
                    final_response = response_text
                    logger.info("Ending with satisfied response")
                    break
                if response_text:
                    # No tags but model returned text only
                    final_response = response_text
                    logger.info("Ending with plain text response (no tags)")
                    break

                logger.warning(f"No usable text or tools at iteration {iteration}")
                if iteration >= max_iterations:
                    final_response = (
                        "I couldn't finish that in time. Try a shorter, more specific request."
                    )
                    break
            
            # Fallback if no final response
            if not final_response:
                final_response = "I apologize, but I couldn't generate a response. Please try rephrasing your question."

            final_response = self._strip_internal_tags(final_response)
            
            # Format for WhatsApp
            final_response = format_code_for_whatsapp(final_response)
            final_response = truncate_message(final_response, max_length=1500)
            
            logger.info("Code formatting applied for WhatsApp display")
            logger.info(f"AI response generated successfully ({len(final_response)} chars)")
            return final_response
            
        except Exception as e:
            logger.error("Error in async response generation: %s", e, exc_info=True)
            return (
                "Sorry — the AI service hit a temporary error. "
                "Please try again in a moment; if it keeps happening, say it in fewer steps."
            )
