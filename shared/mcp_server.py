"""
Phase 2 / shared: MCP server exposing our document search as a standard tool.

MCP (Model Context Protocol) is a standard way for AI systems to expose and
consume tools and data. Today, search() is just a Python function you import
directly - it only works inside this exact codebase. Wrapping it in an MCP
server means ANY MCP-compatible client (Claude Desktop, another team's agent,
a different language's app) could call it the same documented way, without
ever seeing your Python code.

Key new idea: a "tool" in MCP has a NAME, a DESCRIPTION, and a declared
INPUT SCHEMA (what arguments it takes). The description matters a lot - it's
how an AI model decides when and how to call your tool, so it should read
like good documentation, not just a label.
"""

import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))
from fake_search import search

# Note: the mcp library had a major update (v2) that renamed FastMCP to
# MCPServer and moved its import path. This is written for that newer version.
from mcp.server.mcpserver import MCPServer

# This creates the MCP server itself - "compliance-search" is its name,
# which is how a client identifies which server it's talking to.
mcp = MCPServer("compliance-search")


@mcp.tool()
def search_documents(query: str, doc_type: str = "") -> list[dict]:
    """
    Search the compliance document knowledge base (policies, NIST framework
    controls, and sample environment configs) for text relevant to a query.

    Args:
        query: what to search for, e.g. "multifactor authentication"
        doc_type: optional filter - "policy", "framework", "config", "log",
                  or leave empty to search everything

    Returns:
        A list of matching chunks, each with its text, source document,
        and a citation locator (e.g. a policy section or NIST control ID).
    """
    type_filter = doc_type if doc_type else None
    return search(query, type=type_filter)


if __name__ == "__main__":
    # This starts the server and makes it listen for MCP client connections.
    # stdio transport means it talks over standard input/output - the
    # simplest way to run an MCP server locally (e.g. from Claude Desktop).
    mcp.run(transport="stdio")
