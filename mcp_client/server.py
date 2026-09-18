import asyncio
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple
import logging

# Import from the installed mcp package
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio import ClosedResourceError
import mcp.types
from mcp.types import CallToolResult, JSONRPCMessage, Tool as MCPTool
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

# Base class for MCP servers
class MCPServer:
    async def connect(self):
        """Connect to the server."""
        raise NotImplementedError

    @property
    def name(self) -> str:
        """A readable name for the server."""
        raise NotImplementedError

    async def list_tools(self) -> List[MCPTool]:
        """List the tools available on the server."""
        raise NotImplementedError

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> CallToolResult:
        """Invoke a tool on the server."""
        raise NotImplementedError

    async def cleanup(self):
        """Cleanup the server."""
        raise NotImplementedError

# Base class for MCP servers that use a ClientSession
class _MCPServerWithClientSession(MCPServer):
    """Base class for MCP servers that use a ClientSession to communicate with the server."""

    # Heartbeat interval in seconds — a lightweight `list_tools` call is sent
    # to each active session every HEARTBEAT_INTERVAL seconds to stop the SSE
    # connection from timing out during long idle periods (e.g. while the agent
    # is talking to the patient and not invoking any tools).
    HEARTBEAT_INTERVAL: int = 30  # seconds

    def __init__(self, cache_tools_list: bool):
        """
        Args:
            cache_tools_list: Whether to cache the tools list. If True, the tools list will be
            cached and only fetched from the server once. If False, the tools list will be
            fetched from the server on each call to list_tools(). You should set this to True
            if you know the server will not change its tools list, because it can drastically
            improve latency.
        """
        self.session: Optional[ClientSession] = None
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.cache_tools_list = cache_tools_list

        # The cache is always dirty at startup, so that we fetch tools at least once
        self._cache_dirty = True
        self._tools_list: Optional[List[MCPTool]] = None
        self.logger = logging.getLogger(__name__)
        # Maintain per-token sessions to avoid reconnect churn
        self._sessions: Dict[Optional[str], ClientSession] = {}
        self._exit_stacks: Dict[Optional[str], AsyncExitStack] = {}
        self._session_locks: Dict[Optional[str], asyncio.Lock] = {}
        # Background heartbeat task handle
        self._heartbeat_task: Optional[asyncio.Task] = None

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[
        Tuple[
            MemoryObjectReceiveStream[JSONRPCMessage | Exception],
            MemoryObjectSendStream[JSONRPCMessage],
        ]
    ]:
        """Create the streams for the server."""
        raise NotImplementedError

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    def invalidate_tools_cache(self):
        """Invalidate the tools cache."""
        self._cache_dirty = True

    async def _is_connection_alive(self, token: Optional[str]) -> bool:
        """Check if the connection is still alive for a specific token."""
        return token in self._sessions and self._sessions[token] is not None

    async def _get_session(self, token: Optional[str]) -> ClientSession:
        """Get or create a session for the specified token without tearing down others."""
        if token not in self._session_locks:
            self._session_locks[token] = asyncio.Lock()

        async with self._session_locks[token]:
            if token in self._sessions and self._sessions[token] is not None:
                return self._sessions[token]

            exit_stack = AsyncExitStack()
            try:
                transport = await exit_stack.enter_async_context(self.create_streams(token))
                read, write = transport
                session = await exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._exit_stacks[token] = exit_stack
                self._sessions[token] = session
                self.logger.info(f"Connected to MCP server: {self.name} (token={'set' if token else 'default'})")
                return session
            except Exception as e:
                # Ensure stack is cleaned if init fails
                try:
                    await exit_stack.aclose()
                except Exception:
                    pass
                self._exit_stacks.pop(token, None)
                self._sessions.pop(token, None)
                self.logger.error(f"Error initializing MCP server: {e}")
                raise

    async def connect(self, token: Optional[str] = None):
        """Eagerly establish a session for a token (no-op if already present)."""
        await self._get_session(token)
        # Start the heartbeat loop if not already running
        self._start_heartbeat()

    async def list_tools(self) -> List[MCPTool]:
        """List the tools available on the server."""
        # Return from cache if caching is enabled, we have tools, and the cache is not dirty
        if self.cache_tools_list and not self._cache_dirty and self._tools_list:
            return self._tools_list

        # Ensure default-token session exists
        session = await self._get_session(None)

        # Reset the cache dirty to False
        self._cache_dirty = False

        try:
            # Fetch the tools from the server
            result = await session.list_tools()
            self._tools_list = result.tools
            return self._tools_list
        except Exception as e:
            self.logger.error(f"Error listing tools: {e}")
            raise

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> CallToolResult:
        """Invoke a tool on the server with automatic reconnection on connection errors."""
        # Get the token for this tool if mapping exists
        required_token = self.tool_token_mappings.get(tool_name)

        # Obtain or create session for this token without disrupting others
        session: ClientSession = await self._get_session(required_token)

        arguments = arguments or {}
        
        # Try to call the tool, with automatic reconnection on connection errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await session.call_tool(tool_name, arguments)
            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e) if str(e) else repr(e)
                
                # Check if it's a connection error that we can recover from
                # Handle ClosedResourceError, McpError with "Connection closed",
                # and httpx.RemoteProtocolError ("Server disconnected")
                is_connection_error = (
                    isinstance(e, ClosedResourceError) or 
                    ('Connection closed' in error_msg) or
                    ('connection' in error_msg.lower()) or
                    ('disconnected' in error_msg.lower()) or
                    ('RemoteProtocolError' in error_type) or
                    ('server disconnected' in error_msg.lower())
                )
                
                if is_connection_error and attempt < max_retries - 1:
                    self.logger.warning(
                        f"Connection error while calling tool {tool_name}: {error_msg}. "
                        f"Attempting to reconnect (attempt {attempt + 1}/{max_retries})..."
                    )
                    try:
                        # Clean up only the affected session and reconnect
                        await self._cleanup_single_session(required_token)
                        import asyncio
                        await asyncio.sleep(0.5)
                        session = await self._get_session(required_token)
                        self.logger.info(f"Successfully reconnected to MCP server: {self.name}")
                        continue
                    except Exception as reconnect_error:
                        self.logger.error(
                            f"Failed to reconnect to MCP server {self.name}: {reconnect_error}",
                            exc_info=True
                        )
                        # If reconnection fails, raise the original error
                        raise e from reconnect_error
                
                # Get more detailed error information
                # Log with more context
                self.logger.error(
                    f"Error calling tool {tool_name}: {error_type}: {error_msg}",
                    exc_info=True  # Include full traceback
                )
                # Re-raise with a more descriptive message if the original was empty
                if not error_msg:
                    raise RuntimeError(f"Error calling tool {tool_name}: {error_type} (no error message provided)") from e
                raise

    # ── Heartbeat / keepalive ─────────────────────────────────────────────
    def _start_heartbeat(self) -> None:
        """Launch the background heartbeat task (idempotent)."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
            self.logger.info(
                f"[HEARTBEAT] Started keepalive loop (interval={self.HEARTBEAT_INTERVAL}s)"
            )

    async def _heartbeat_loop(self) -> None:
        """Periodically ping every active session with a lightweight `list_tools`
        call so the SSE connection does not time out during idle periods."""
        while True:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                self.logger.info("[HEARTBEAT] Cancelled — stopping keepalive loop")
                return

            tokens = list(self._sessions.keys())
            for token in tokens:
                session = self._sessions.get(token)
                if session is None:
                    continue
                try:
                    # A lightweight RPC that keeps the SSE transport alive
                    await session.send_ping()
                    self.logger.debug(
                        f"[HEARTBEAT] Ping OK (token={'set' if token else 'default'})"
                    )
                except Exception as exc:
                    self.logger.warning(
                        f"[HEARTBEAT] Ping failed (token={'set' if token else 'default'}): {exc}  "
                        f"— will reconnect on next tool call"
                    )
                    # Proactively tear down the dead session so the next
                    # `call_tool` triggers a fresh reconnect immediately.
                    try:
                        await self._cleanup_single_session(token)
                    except Exception:
                        pass

    def _stop_heartbeat(self) -> None:
        """Cancel the background heartbeat task if running."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self.logger.info("[HEARTBEAT] Stopped keepalive loop")

    async def _cleanup_single_session(self, token: Optional[str]):
        """Cleanup a single session for a specific token."""
        stack = self._exit_stacks.pop(token, None)
        session = self._sessions.pop(token, None)
        try:
            if stack:
                await stack.aclose()
        except RuntimeError as e:
            error_msg = str(e).lower()
            if "cancel scope" in error_msg or "different task" in error_msg:
                self.logger.debug(
                    f"Exit stack cleanup skipped (different task context): {e}"
                )
            else:
                raise
        except Exception:
            pass

    async def cleanup(self):
        """Cleanup all sessions."""
        self._stop_heartbeat()
        async with self._cleanup_lock:
            try:
                tokens = list(self._sessions.keys())
                for token in tokens:
                    await self._cleanup_single_session(token)
                self.logger.info(f"Cleaned up MCP server: {self.name}")
            except Exception as e:
                self.logger.error(f"Error cleaning up server: {e}")
            finally:
                self._sessions = {}
                self._exit_stacks = {}
                self._session_locks = {}

# Define parameter types for clarity
MCPServerSseParams = Dict[str, Any]
MCPServerStdioParams = Dict[str, Any]

# SSE server implementation
class MCPServerSse(_MCPServerWithClientSession):
    """MCP server implementation that uses the HTTP with SSE transport."""

    def __init__(
        self,
        params: MCPServerSseParams,
        cache_tools_list: bool = False,
        name: Optional[str] = None,
        tool_token_mappings: Optional[Dict[str, str]] = None,
    ):
        """Create a new MCP server based on the HTTP with SSE transport.

        Args:
            params: The params that configure the server including the URL, headers,
                   timeout, and SSE read timeout.
            cache_tools_list: Whether to cache the tools list.
            name: A readable name for the server.
            tool_token_mappings: Dictionary mapping tool names to token values.
                                If provided, headers will be dynamically updated per tool call.
        """
        super().__init__(cache_tools_list)
        self.params = params
        self._name = name or f"SSE Server at {self.params.get('url', 'unknown')}"
        self.tool_token_mappings = tool_token_mappings or {}
        self._current_token: Optional[str] = None
        self._token_header_name: str = "Authorization"

    def create_streams(
        self,
        token: Optional[str] = None,
    ) -> AbstractAsyncContextManager[
        Tuple[
            MemoryObjectReceiveStream[JSONRPCMessage | Exception],
            MemoryObjectSendStream[JSONRPCMessage],
        ]
    ]:
        """Create the streams for the server."""
        # Start with base headers
        headers = dict(self.params.get("headers", {}))
        
        # If a token is provided, add it to headers
        if token:
            headers[self._token_header_name] = f"Bearer {token}"
        
        return sse_client(
            url=self.params["url"],
            headers=headers if headers else None,
            timeout=self.params.get("timeout", 30),
            sse_read_timeout=self.params.get("sse_read_timeout", 60 * 5),
        )

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name

# Stdio server implementation
class MCPServerStdio(MCPServer):
    """An example (minimal) Stdio server implementation."""

    def __init__(self, params: MCPServerStdioParams, cache_tools_list: bool = False, name: Optional[str] = None):
        self.params = params
        self.cache_tools_list = cache_tools_list
        self._tools_cache: Optional[List[MCPTool]] = None
        self._name = name or f"Stdio Server: {self.params.get('command', 'unknown')}"
        self.connected = False
        self.logger = logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return self._name

    async def connect(self):
        await asyncio.sleep(0.5)
        self.connected = True
        self.logger.info(f"Connected to MCP Stdio server: {self.name}")

    async def list_tools(self) -> List[MCPTool]:
        if self.cache_tools_list and self._tools_cache is not None:
            return self._tools_cache
        # For demonstration, return an empty list or similar static tools.
        tools: List[MCPTool] = []
        if self.cache_tools_list:
            self._tools_cache = tools
        return tools

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"content": [f"Called {tool_name} with args {arguments} via Stdio"]}

    async def cleanup(self):
        self.connected = False
        self.logger.info(f"Cleaned up MCP Stdio server: {self.name}")