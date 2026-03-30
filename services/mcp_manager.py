import asyncio
import json
import os
import threading
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from utils.logger import logger


def _npx_command() -> str:
    """Windows uses npx.cmd; Linux/macOS/WSL use npx."""
    return "npx.cmd" if os.name == "nt" else "npx"


class MCPManager:
    """
    Runs MCP stdio transports on ONE dedicated asyncio event loop (background thread).

    Why: ClientSession / stdio_client must stay on the same loop they were created on.
    asyncio.run() per HTTP request creates a new loop each time, so persistent sessions
    cannot be reused across requests unless they live on a fixed loop.

    - At start: open long-lived stdio + ClientSession per configured server, list_tools once.
    - On each tool call: reuse the same session(s) on that loop.
    """

    def __init__(self):
        self.github_token = os.environ.get("GITHUB_TOKEN")
        self.netlify_token = os.environ.get("NETLIFY_API_KEY")
        self.pinecone_api_key = os.environ.get("PINECONE_API_KEY")

        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._shutdown = threading.Event()

        # GitHub
        self._gh_stdio_cm = None
        self._gh_session_cm = None
        self._github_session = None

        # Netlify
        self._nl_stdio_cm = None
        self._nl_session_cm = None
        self._netlify_session = None

        # Pinecone
        self._pc_stdio_cm = None
        self._pc_session_cm = None
        self._pinecone_session = None

        self._cached_tool_declarations = []
        self._cached_tool_schemas = []
        self._tool_to_server_map = {}

    def _clean_schema(self, schema):
        if isinstance(schema, dict):
            cleaned = {}
            for k, v in schema.items():
                if k in ["additionalProperties", "$schema", "const", "propertyNames"]:
                    continue
                cleaned[k] = self._clean_schema(v)
            return cleaned
        if isinstance(schema, list):
            return [self._clean_schema(item) for item in schema]
        return schema

    def _gemini_parameters_from_input_schema(self, input_schema):
        """
        Same shape as Gemini examples: parameters from inputSchema minus meta keys,
        then recursive clean for nested additionalProperties / const / etc.
        """
        if not isinstance(input_schema, dict):
            return {}
        raw = {
            k: v
            for k, v in input_schema.items()
            if k not in ("additionalProperties", "$schema")
        }
        return self._clean_schema(raw)

    def _filter_unset_parameters(self, arguments, schema):
        if not schema or not isinstance(schema, dict):
            return arguments
        required_params = schema.get("required", [])
        properties = schema.get("properties", {})
        filtered = {}
        for key, value in arguments.items():
            if key in required_params:
                filtered[key] = value
            elif key in properties and value is not None and value != "":
                filtered[key] = value
        return filtered

    def start(self, timeout: int | None = None):
        """Start background loop and persistent MCP sessions. Blocks until ready or timeout."""
        if timeout is None:
            timeout = int(os.environ.get("MCP_START_TIMEOUT", "300"))
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="mcp-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(
                f"MCPManager failed to start within {timeout}s "
                f"(first npx -y downloads can be slow on WSL or /mnt/c/; "
                f"raise MCP_START_TIMEOUT or run the project from ~/ on ext4)"
            )

    def stop(self):
        """Request shutdown (best-effort)."""
        self._shutdown.set()
        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
            try:
                fut.result(timeout=30)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._startup_async())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"MCP loop crashed: {str(e)}")
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                if hasattr(self._loop, "shutdown_asyncgens"):
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    async def _startup_async(self):
        logger.info("MCP background loop: connecting persistent stdio sessions...")
        try:
            tasks = []
            if self.github_token:
                tasks.append(self._connect_github())
            if self.netlify_token:
                tasks.append(self._connect_netlify())
            if self.pinecone_api_key:
                tasks.append(self._connect_pinecone())
            if tasks:
                logger.info(
                    "MCP: starting %s stdio server(s) in parallel (npx may download packages)...",
                    len(tasks),
                )
                await asyncio.gather(*tasks)
            await self._refresh_tool_cache()
            logger.info("MCP: tool cache ready")
        except Exception as e:
            logger.error(f"MCP startup error: {str(e)}")

    async def _connect_github(self):
        if self._github_session:
            return
        params = StdioServerParameters(
            command=_npx_command(),
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": self.github_token},
        )
        self._gh_stdio_cm = stdio_client(params)
        read, write = await self._gh_stdio_cm.__aenter__()
        self._gh_session_cm = ClientSession(read, write)
        self._github_session = await self._gh_session_cm.__aenter__()
        await self._github_session.initialize()
        logger.info("GitHub MCP session persisted on background loop")

    async def _connect_netlify(self):
        if self._netlify_session:
            return
        params = StdioServerParameters(
            command=_npx_command(),
            args=["-y", "@netlify/mcp"],
            env={"NETLIFY_API_KEY": self.netlify_token},
        )
        self._nl_stdio_cm = stdio_client(params)
        read, write = await self._nl_stdio_cm.__aenter__()
        self._nl_session_cm = ClientSession(read, write)
        self._netlify_session = await self._nl_session_cm.__aenter__()
        await self._netlify_session.initialize()
        logger.info("Netlify MCP session persisted on background loop")

    async def _connect_pinecone(self):
        if self._pinecone_session:
            return
        params = StdioServerParameters(
            command=_npx_command(),
            args=["-y", "@pinecone-database/mcp"],
            env={"PINECONE_API_KEY": self.pinecone_api_key},
        )
        self._pc_stdio_cm = stdio_client(params)
        read, write = await self._pc_stdio_cm.__aenter__()
        self._pc_session_cm = ClientSession(read, write)
        self._pinecone_session = await self._pc_session_cm.__aenter__()
        await self._pinecone_session.initialize()
        logger.info("Pinecone MCP session persisted on background loop")

    async def _refresh_tool_cache(self):
        self._cached_tool_declarations = []
        self._cached_tool_schemas = []
        self._tool_to_server_map = {}

        async def add_from_session(session, server_type):
            if not session:
                return
            mcp_tools = await session.list_tools()
            for tool in mcp_tools.tools:
                decl = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": self._gemini_parameters_from_input_schema(
                        tool.inputSchema
                    ),
                    "original_schema": tool.inputSchema,
                    "server_type": server_type,
                }
                self._cached_tool_schemas.append(decl)
                self._cached_tool_declarations.append(
                    {
                        "name": decl["name"],
                        "description": decl["description"],
                        "parameters": decl["parameters"],
                    }
                )
                self._tool_to_server_map[decl["name"]] = server_type

        await add_from_session(self._github_session, "github")
        await add_from_session(self._netlify_session, "netlify")
        await add_from_session(self._pinecone_session, "pinecone")

        logger.info(
            f"MCP tool cache built: {len(self._cached_tool_declarations)} tools "
            f"(github={bool(self._github_session)}, netlify={bool(self._netlify_session)}, "
            f"pinecone={bool(self._pinecone_session)})"
        )

    def run_coro(self, coro, timeout=120):
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("MCP loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def get_cached_tool_declarations(self):
        return list(self._cached_tool_declarations)

    def get_cached_tool_schemas(self):
        return list(self._cached_tool_schemas)

    def get_tool_server_map(self):
        return dict(self._tool_to_server_map)

    def _session_for_server(self, server_type):
        if server_type == "github":
            return self._github_session
        if server_type == "netlify":
            return self._netlify_session
        if server_type == "pinecone":
            return self._pinecone_session
        return None

    async def _execute_function_call_async(self, function_call, github_token_override=None):
        name = function_call.name
        server_type = self._tool_to_server_map.get(name)
        if not server_type:
            return json.dumps(
                {"error": f"Unknown tool {name!r} — not registered from MCP list_tools."}
            )

        if server_type == "github":
            token = github_token_override or self.github_token
            if token != self.github_token:
                return (
                    "GitHub token differs from server env; reconnect with matching GITHUB_TOKEN "
                    "or restart the app."
                )
            session = self._github_session
        else:
            session = self._session_for_server(server_type)

        if not session:
            return f"MCP server '{server_type}' not connected for tool {name}"

        original_args = dict(function_call.args)
        tool_schema = None
        for schema in self._cached_tool_schemas:
            if schema.get("name") == name:
                tool_schema = schema.get("original_schema")
                break
        filtered = (
            self._filter_unset_parameters(original_args, tool_schema)
            if tool_schema
            else original_args
        )

        result = await session.call_tool(name, arguments=filtered)
        if result.content and len(result.content) > 0:
            try:
                return json.dumps(json.loads(result.content[0].text), indent=2)
            except Exception:
                return result.content[0].text
        return "Function executed successfully but returned no content."

    def execute_function_call_sync(self, function_call, github_token_override=None, timeout=120):
        return self.run_coro(
            self._execute_function_call_async(function_call, github_token_override),
            timeout=timeout,
        )

    async def _shutdown_async(self):
        logger.info("MCP shutdown: closing sessions...")
        for sess_cm, stdio_cm in [
            (self._gh_session_cm, self._gh_stdio_cm),
            (self._nl_session_cm, self._nl_stdio_cm),
            (self._pc_session_cm, self._pc_stdio_cm),
        ]:
            try:
                if sess_cm:
                    await sess_cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                if stdio_cm:
                    await stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._github_session = self._netlify_session = self._pinecone_session = None
        self._gh_session_cm = self._nl_session_cm = self._pc_session_cm = None
        self._gh_stdio_cm = self._nl_stdio_cm = self._pc_stdio_cm = None
        if self._loop and self._loop.is_running():
            self._loop.stop()
