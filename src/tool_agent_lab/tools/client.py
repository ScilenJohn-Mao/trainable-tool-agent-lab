"""Launch a local MCP stdio server and retain raw and typed tool results."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator, TextIO
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import TypeAdapter

from tool_agent_lab.schemas.actions import ExecutionContext
from tool_agent_lab.schemas.common import ContractModel, NonEmptyStr
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import TOOL_CONTRACTS, ToolName


@dataclass(frozen=True)
class ToolReply:
    raw: types.CallToolResult
    result: ContractModel


class ToolClient:
    def __init__(self, session: ClientSession, initialization: types.InitializeResult) -> None:
        self.session = session
        self.initialization = initialization

    async def list_tools(self) -> list[types.Tool]:
        return (await self.session.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict, *, call_id: str | None = None) -> ToolReply:
        contract = TOOL_CONTRACTS[name]
        call_id = TypeAdapter(NonEmptyStr).validate_python(call_id if call_id is not None else str(uuid4()))
        raw = await self.session.call_tool(name, arguments, meta={"tool_agent_lab_call_id": call_id})
        if raw.structuredContent is None:
            raise ValueError("MCP tool result is missing structuredContent")
        result = contract.parse_result(raw.structuredContent, call_id=call_id)
        if raw.isError != (result.status == "error"):
            raise ValueError("MCP isError does not match the structured result status")
        return ToolReply(raw=raw, result=result)


def local_server_parameters(
    *, database: str | Path | None = None, data_dir: str | Path | None = None,
    owner_id: str | None = None, cwd: str | Path | None = None,
    execution_context: ExecutionContext | None = None, clarification_attempted: bool = False,
) -> StdioServerParameters:
    arguments = ["-u", "-m", "tool_agent_lab.tools.server"]
    for option, value in [("--database", database), ("--data-dir", data_dir), ("--owner-id", owner_id)]:
        if value is not None:
            arguments.extend([option, str(Path(value).resolve()) if option != "--owner-id" else value])
    env = {name: value for name, value in os.environ.items() if name.startswith("TTAL_") and not name.startswith("TTAL_MCP_")}
    if execution_context is not None:
        env["TTAL_MCP_CONTEXT"] = execution_context.model_dump_json()
        env["TTAL_MCP_CLARIFIED"] = "1" if clarification_attempted else "0"
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return StdioServerParameters(
        command=sys.executable, args=arguments, env=env, cwd=str(cwd or PROJECT_ROOT), encoding="utf-8",
    )


@asynccontextmanager
async def open_tool_session(
    parameters: StdioServerParameters | None = None, *, errlog: TextIO | None = None,
) -> AsyncIterator[ToolClient]:
    """Initialize one real child process; close its streams and process on exit."""
    async with stdio_client(parameters or local_server_parameters(), errlog=errlog or sys.stderr) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            yield ToolClient(session, await session.initialize())


async def _run(args: argparse.Namespace) -> int:
    parameters = local_server_parameters(database=args.database, data_dir=args.data_dir, owner_id=args.owner_id)
    async with open_tool_session(parameters) as client:
        if args.command == "list":
            payload = {
                "protocol_version": client.initialization.protocolVersion,
                "server": client.initialization.serverInfo.model_dump(mode="json", exclude_none=True),
                "tools": [item.model_dump(mode="json", exclude_none=True) for item in await client.list_tools()],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        reply = await client.call_tool(args.name, args.arguments, call_id=args.call_id)
        print(reply.result.model_dump_json(indent=2))
        return 1 if reply.raw.isError else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--owner-id")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Initialize a real session and list the seven tools")
    call = commands.add_parser("call", help="Call one tool with JSON model arguments")
    call.add_argument("name", choices=[name.value for name in ToolName])
    call.add_argument("arguments", help="JSON object, or '-' to read UTF-8 JSON from stdin")
    call.add_argument("--call-id", help="Runtime correlation ID, sent as MCP metadata")
    args = parser.parse_args(argv)
    if args.command == "call":
        try:
            source = sys.stdin.buffer.read().decode("utf-8-sig") if args.arguments == "-" else args.arguments
            args.arguments = json.loads(source)
        except (ValueError, UnicodeError) as error:
            parser.error(str(error))
        if not isinstance(args.arguments, dict):
            parser.error("tool arguments must be a JSON object")
    sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
