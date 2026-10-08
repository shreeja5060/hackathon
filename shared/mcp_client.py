"""
Day 5 (completing the loop): an MCP CLIENT that actually connects to our
mcp_server.py and calls its tool - proving the full MCP round trip works,
not just that the server starts.

This is a separate program from mcp_server.py. The client starts the server
as a subprocess (via stdio_client), performs the MCP handshake, asks it what
tools are available, then calls one.
"""

import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    # This describes HOW to start our server: run this Python file as a
    # subprocess. stdio_client will launch it and talk to it over its
    # standard input/output - the same "stdio" transport our server runs.
    server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")
    server_params = StdioServerParameters(
        command="python",
        args=[server_script],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # The MCP handshake: client and server agree on protocol version
            # and capabilities before anything else can happen.
            await session.initialize()
            print("Connected to MCP server successfully.\n")

            # Ask the server what tools it has - this calls our
            # @mcp.tool() decorated function's metadata, not the function
            # itself yet.
            tools_result = await session.list_tools()
            print("Available tools:")
            for tool in tools_result.tools:
                print(f"  - {tool.name}: {tool.description[:80]}...")

            # Now actually CALL the tool through the protocol - this is the
            # real proof the round trip works, not just that it starts.
            print("\nCalling search_documents(query='authentication')...")
            result = await session.call_tool(
                "search_documents",
                {"query": "authentication", "doc_type": "internal"}
            )

            print("\nResult:")
            for block in result.content:
                print(block.text if hasattr(block, "text") else block)


if __name__ == "__main__":
    asyncio.run(main())
