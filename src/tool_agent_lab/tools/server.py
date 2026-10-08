"""Serve the after-sales tool catalog over MCP stdio."""

import argparse
import asyncio
import logging
import sqlite3
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from tool_agent_lab.knowledge.search import PolicySearch
from tool_agent_lab.settings import load_settings
from tool_agent_lab.storage.business_repository import BusinessRepository
from tool_agent_lab.storage.database import connect
from tool_agent_lab.tools.contracts import (
    TOOL_CONTRACTS, OperationLookup, ToolError, ToolFailure, ToolName, tool_definitions,
)

logger = logging.getLogger(__name__)


def create_server(*, database: Path, owner_id: str, policies: PolicySearch) -> Server:
    """Bind local application identity at startup, outside model arguments."""
    server = Server("tool-agent-lab", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool.model_validate(item) for item in tool_definitions()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        context = server.request_context
        metadata = context.meta.model_dump() if context.meta else {}
        call_id = metadata.get("tool_agent_lab_call_id", str(context.request_id))

        def failure(code: str, message: str) -> ToolFailure:
            return ToolFailure(call_id=call_id, status="error", error=ToolError(
                code=code, message=message, outcome="not_committed",
            ))

        contract = TOOL_CONTRACTS.get(name)
        if contract is None:
            result = failure("unknown_tool", f"Unknown tool: {name}")
        else:
            try:
                args = contract.parse_arguments(arguments)
            except ValidationError as error:
                # Do not echo arbitrary argument values (including injected identity).
                fields = ", ".join(".".join(map(str, item["loc"])) for item in error.errors())
                result = failure("invalid_arguments", f"Invalid fields: {fields}")
            else:
                if not contract.read_only:
                    result = failure("execution_context_required", "A trusted execution binding is required for writes.")
                else:
                    try:
                        if name == ToolName.SEARCH_POLICY:
                            data = policies.search_policy(args)
                        elif name == ToolName.READ_POLICY:
                            data = policies.catalog.read_policy(args)
                        elif not database.is_file():
                            data = None
                            result = failure("database_not_initialized", "Initialize the application database first.")
                        else:
                            with connect(database) as connection:
                                business = BusinessRepository(connection)
                                if name == ToolName.GET_ORDER:
                                    data = business.get_order(args.order_id, owner_id)
                                    if data is None:
                                        result = failure("order_not_found", "Order not found for the application user.")
                                else:
                                    data = OperationLookup(
                                        operation_key=args.operation_key,
                                        operation=business.get_operation(args.operation_key, owner_id),
                                    )
                        if data is not None:
                            result = contract.parse_result({
                                "call_id": call_id, "status": "ok", "data": data.model_dump(mode="json"),
                            }, call_id=call_id)
                    except KeyError:
                        result = failure("policy_not_found", "Policy ID, version or section was not found.")
                    except sqlite3.Error:
                        logger.exception("Application database read failed")
                        result = failure("database_error", "Database read failed; inspect server stderr.")
        payload = result.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_dump_json())],
            structuredContent=payload, isError=result.status == "error",
        )

    return server


async def serve(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Defaults to the configured app_db_path")
    parser.add_argument("--data-dir", type=Path, help="Defaults to the configured business_data_dir")
    parser.add_argument("--owner-id", help="Local application user; never a tool argument")
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        policies = PolicySearch.from_data_dir(args.data_dir or settings.business_data_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    server = create_server(
        database=(args.database or settings.app_db_path).resolve(),
        owner_id=args.owner_id or settings.dev_owner_id, policies=policies,
    )
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(server))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
