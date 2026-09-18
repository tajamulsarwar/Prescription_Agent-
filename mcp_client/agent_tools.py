import asyncio
import logging
import json
import inspect
import re
import typing
from datetime import datetime, timezone, timedelta
from typing import Any, List, Dict, Callable, Optional

from .util import MCPUtil, FunctionTool
from .server import MCPServer, MCPServerSse
from livekit.agents import ChatContext, AgentSession, JobContext, FunctionTool as Tool

logger = logging.getLogger("mcp-agent-tools")

# ---------------------------------------------------------------------------
# Last-seen-date eligibility check
# ---------------------------------------------------------------------------

def _parse_last_seen_date(date_str: str | None) -> datetime | None:
    """Parse last_seen_date from the practice management system (ISO 8601) into a datetime."""
    if not date_str:
        return None
    try:
        # Handle ISO format like "2025-10-15T14:00:00-04:00"
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


def _check_last_seen(last_seen_date) -> tuple[bool, str]:
    """Check if the patient is eligible for refill based on last_seen_date.

    Returns (eligible, message_for_llm).
    """
    # last_seen_date is null → patient has never been seen → not eligible
    if last_seen_date is None:
        return False, (
            "IMPORTANT: This patient has no record of a previous visit (last_seen_date is null). "
            "The patient is NOT eligible for a prescription refill. "
            "Tell the patient: \"I can see you don't have a recent visit on file. "
            "Unfortunately, your medication is not eligible for a refill until you've been "
            "seen by the provider. You will need to schedule an appointment first. "
            "Let me connect you with our medical assistant who can help you schedule that. "
            "You can also reach us directly at XXX-XXX-XXXX.\" "
            "Then call transfer_to_human(reason=\"patient has no visit on record\")."
        )

    # last_seen_date is present → check if > 1 year ago
    parsed = _parse_last_seen_date(last_seen_date)
    if parsed is None:
        logger.warning(f"Could not parse last_seen_date: {last_seen_date}")
        return True, ""

    one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    if parsed < one_year_ago:
        return False, (
            f"IMPORTANT: This patient was last seen on {last_seen_date}, which is more than "
            "1 year ago. The patient is NOT eligible for a prescription refill. "
            "Tell the patient: \"I can see it's been over a year since your last visit. "
            "Unfortunately, your medication is no longer eligible for a refill until you've "
            "been seen by the provider. You will need to schedule an appointment first. "
            "Let me connect you with our medical assistant who can help you schedule that. "
            "You can also reach us directly at XXX-XXX-XXXX.\" "
            "Then call transfer_to_human(reason=\"patient not seen in over 1 year\")."
        )

    return True, ""


class MCPToolsIntegration:
    """
    Helper class for integrating MCP tools with LiveKit agents.
    Provides utilities for registering dynamic tools from MCP servers.
    """

    @staticmethod
    async def prepare_dynamic_tools(mcp_servers: List[MCPServer],
                                   convert_schemas_to_strict: bool = True,
                                   auto_connect: bool = True) -> List[Callable]:
        """
        Fetches tools from multiple MCP servers and prepares them for use with LiveKit agents.

        Args:
            mcp_servers: List of MCPServer instances
            convert_schemas_to_strict: Whether to convert JSON schemas to strict format
            auto_connect: Whether to automatically connect to servers if they're not connected

        Returns:
            List of decorated tool functions ready to be added to a LiveKit agent
        """
        prepared_tools = []

        # Ensure all servers are connected if auto_connect is True
        if auto_connect:
            for server in mcp_servers:
                if not getattr(server, 'connected', False):
                    try:
                        logger.debug(f"Auto-connecting to MCP server: {server.name}")
                        await server.connect()
                    except Exception as e:
                        logger.error(f"Failed to connect to MCP server {server.name}: {e}")

        # Process each server
        for server in mcp_servers:
            logger.info(f"Fetching tools from MCP server: {server.name}")
            try:
                mcp_tools = await MCPUtil.get_function_tools(
                    server, convert_schemas_to_strict=convert_schemas_to_strict
                )
                logger.info(f"Received {len(mcp_tools)} tools from {server.name}")
            except Exception as e:
                logger.error(f"Failed to fetch tools from {server.name}: {e}")
                continue

            for tool_instance in mcp_tools:
                try:
                    decorated_tool = MCPToolsIntegration._create_decorated_tool(tool_instance)
                    prepared_tools.append(decorated_tool)
                    logger.debug(f"Successfully prepared tool: {tool_instance.name}")
                except Exception as e:
                    logger.error(f"Failed to prepare tool '{tool_instance.name}': {e}")

        return prepared_tools

    @staticmethod
    def _create_decorated_tool(tool: FunctionTool) -> Callable:
        """
        Creates a decorated function for a single MCP tool that can be used with LiveKit agents.
        """
        from livekit.agents.llm import function_tool

        params = []
        annotations = {}
        schema_props = tool.params_json_schema.get("properties", {})
        schema_required = set(tool.params_json_schema.get("required", []))

        type_map = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }

        for p_name, p_details in schema_props.items():
            json_type = p_details.get("type", "string")
            py_type = type_map.get(json_type, typing.Any)
            annotations[p_name] = py_type

            default = inspect.Parameter.empty if p_name in schema_required else p_details.get("default", None)
            params.append(inspect.Parameter(
                name=p_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=py_type,
                default=default
            ))

        async def tool_impl(**kwargs):
            # --- Check last_seen_date right after Patient_Verification -----
            if tool.name == "Patient_Verification":
                input_json = json.dumps(kwargs)
                logger.info(f"Invoking tool '{tool.name}' with args: {kwargs}")
                result_str = await tool.on_invoke_tool(None, input_json)
                logger.info(f"Tool '{tool.name}' result: {result_str}")
                try:
                    result_data = json.loads(result_str) if isinstance(result_str, str) else result_str
                    if result_data.get("verified") is True:
                        lsd = result_data.get("last_seen_date")
                        logger.info(f"Patient verified, last_seen_date: {lsd}")
                        eligible, reason = _check_last_seen(lsd)
                        if not eligible:
                            logger.warning(f"Patient not eligible for refill: {reason}")
                            # Append eligibility warning to the result so the
                            # LLM sees it immediately and acts on it
                            if isinstance(result_data, dict):
                                result_data["refill_eligibility"] = "NOT_ELIGIBLE"
                                result_data["refill_eligibility_message"] = reason
                                return json.dumps(result_data)
                    else:
                        logger.info("Patient not verified, skipping last_seen_date check")
                except Exception as e:
                    logger.warning(f"Could not process Patient_Verification result: {e}")
                return result_str
            # ---------------------------------------------------------------

            input_json = json.dumps(kwargs)
            logger.info(f"Invoking tool '{tool.name}' with args: {kwargs}")
            result_str = await tool.on_invoke_tool(None, input_json)
            logger.info(f"Tool '{tool.name}' result: {result_str}")
            return result_str

        # Sanitize the tool name for OpenAI (must match ^[a-zA-Z0-9_-]+$)
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', tool.name)

        # Set function metadata
        tool_impl.__signature__ = inspect.Signature(parameters=params)
        tool_impl.__name__ = safe_name
        tool_impl.__doc__ = tool.description
        all_annotations = {'return': str, **annotations}
        tool_impl.__annotations__ = all_annotations

        # Python 3.14+ (PEP 649) uses __annotate__ instead of __annotations__
        # for functools.update_wrapper / typing.get_type_hints.
        ann_copy = dict(all_annotations)
        tool_impl.__annotate__ = lambda format: ann_copy

        # Apply the livekit function_tool decorator.
        # IMPORTANT: the decorator wraps tool_impl in a new callable, which may
        # not carry over __signature__ or __annotations__.  Without them,
        # typing.get_type_hints() returns an incomplete dict and livekit's
        # build_legacy_openai_schema() raises KeyError for every dynamic param
        # (e.g. KeyError: 'business_entity_id').  Copy both attributes onto the
        # decorated wrapper explicitly to prevent this crash.
        decorated = function_tool()(tool_impl)
        decorated.__signature__ = tool_impl.__signature__
        decorated.__annotations__ = tool_impl.__annotations__
        decorated.__name__ = tool_impl.__name__
        decorated.__doc__ = tool_impl.__doc__
        return decorated

    @staticmethod
    async def register_with_agent(agent, mcp_servers: List[MCPServer],
                                 convert_schemas_to_strict: bool = True,
                                 auto_connect: bool = True) -> List[Callable]:
        """
        Helper method to prepare and register MCP tools with a LiveKit agent.

        Args:
            agent: The LiveKit agent instance
            mcp_servers: List of MCPServer instances
            convert_schemas_to_strict: Whether to convert schemas to strict format
            auto_connect: Whether to auto-connect to servers

        Returns:
            List of tool functions that were registered
        """
        # Prepare the dynamic tools
        tools = await MCPToolsIntegration.prepare_dynamic_tools(
            mcp_servers,
            convert_schemas_to_strict=convert_schemas_to_strict,
            auto_connect=auto_connect
        )

        # Register with the agent
        if hasattr(agent, '_tools') and isinstance(agent._tools, list):
            agent._tools.extend(tools)
            logger.info(f"Registered {len(tools)} MCP tools with agent")

            # Log the names of registered tools
            if tools:
                tool_names = [getattr(t, '__name__', 'unknown') for t in tools]
                logger.info(f"Registered tool names: {tool_names}")
        else:
            logger.warning("Agent does not have a '_tools' attribute, tools were not registered")

        return tools

    @staticmethod
    async def create_agent_with_tools(agent_class, mcp_servers: List[MCPServer], agent_kwargs: Dict = None,
                                    convert_schemas_to_strict: bool = True) -> Any:
        """
        Factory method to create and initialize an agent with MCP tools already loaded.

        Args:
            agent_class: Agent class to instantiate
            mcp_servers: List of MCP servers to register with the agent
            agent_kwargs: Additional keyword arguments to pass to the agent constructor
            convert_schemas_to_strict: Whether to convert JSON schemas to strict format

        Returns:
            An initialized agent instance with MCP tools registered
        """
        # Connect to MCP servers
        for server in mcp_servers:
            if not getattr(server, 'connected', False):
                try:
                    logger.debug(f"Connecting to MCP server: {server.name}")
                    await server.connect()
                except Exception as e:
                    logger.error(f"Failed to connect to MCP server {server.name}: {e}")

        # Create agent instance
        agent_kwargs = agent_kwargs or {}
        agent = agent_class(**agent_kwargs)

        # Prepare tools
        tools = await MCPToolsIntegration.prepare_dynamic_tools(
            mcp_servers,
            convert_schemas_to_strict=convert_schemas_to_strict,
            auto_connect=False  # Already connected above
        )

        # Register tools with agent
        if tools and hasattr(agent, '_tools') and isinstance(agent._tools, list):
            agent._tools.extend(tools)
            logger.info(f"Registered {len(tools)} MCP tools with agent")

            # Log the names of registered tools
            tool_names = [getattr(t, '__name__', 'unknown') for t in tools]
            logger.info(f"Registered tool names: {tool_names}")
        else:
            if not tools:
                logger.warning("No tools were found to register with the agent")
            else:
                logger.warning("Agent does not have a '_tools' attribute, tools were not registered")

        return agent
