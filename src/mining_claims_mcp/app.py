"""MCP server entry point.

Keep this file THIN. It only:
  1. creates the FastMCP instance,
  2. registers tools / prompts / resources / routes from the submodules, and
  3. decides which transport to run (stdio locally vs. HTTP when deployed).

All real logic lives in the submodules:
  - tools/      one file per tool (each exposes `register(mcp)`)
  - prompts/    reusable conversation starters
  - resources/  static/semi-static data the client can read directly
  - routes.py   HTTP-only infrastructure (health check, etc.)
  - config.py   settings loaded from env vars / .env
  - models.py   Pydantic models & enums for tool parameters
  - utils.py    shared helpers (HTTP client, formatting, pagination)
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mining_claims_mcp.config import settings
from mining_claims_mcp.prompts import register_prompts
from mining_claims_mcp.resources import register_resources
from mining_claims_mcp.routes import register_routes
from mining_claims_mcp.tools import register_tools

# The server name is what MCP clients display. Rename it to your service.
mcp = FastMCP(
    name="BLM Mining Claims MCP Server",
    instructions=(
        "Answers public questions about mining claims using the Bureau of Land "
        "Management's public mining claims map service (nlsdbpubgis.blm.gov). "
        "Built for BLM staff at the Montana/Dakotas State Office. When a "
        "search finds nothing or too much, ask the user for more detail (a "
        "serial number, claim name, township and range, or nearby landmark) "
        "before sending them to MLRS (https://mlrs.blm.gov). Mention MLRS only "
        "for what this data lacks (claimant names, certificates of location, "
        "case documents) or when the tools can't find the claim. For how-to "
        "questions (staking, filing, fees, waivers, forms, transfers, MLRS "
        "accounts and payments, rules), use mining_claims_official_pages and "
        "repeat only its summaries, dated fee amounts and links."
    ),
)

# Wire up everything the server exposes. Each aggregator lives in its submodule.
register_tools(mcp)
register_prompts(mcp)
register_resources(mcp)
register_routes(mcp)


def local_http_options() -> dict[str, Any]:
    """Extra HTTP options for the local (MCP_TRANSPORT) mode only.

    - Host/Origin guard ("auto" = on when bound to 127.0.0.1): rejects
      requests whose Host or Origin header isn't this computer, which stops
      web sites from reaching the server through the browser (DNS rebinding).
    - CORS: lets the browser pages listed in MCP_CORS_ORIGINS (local
      addresses only, see config.py) read the server's replies. None by default.
    """
    middleware = []
    if settings.cors_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_origins,
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=[
                    "Content-Type",
                    "Accept",
                    "Mcp-Session-Id",
                    "Mcp-Protocol-Version",
                    "Last-Event-ID",
                ],
                # The page must read the session ID to make follow-up calls.
                expose_headers=["Mcp-Session-Id"],
            )
        )
    return {"host_origin_protection": "auto", "middleware": middleware}


def main() -> None:
    """Start the server, choosing a transport based on the environment.

    Precedence:
      1. A host-supplied `PORT` → run HTTP on that port. Most cloud hosts set
         this for you; where a host uses another name, its deploy config
         copies it into `PORT` (e.g. the Databricks app.yaml).
      2. `MCP_TRANSPORT=streamable-http` in the environment → run HTTP on
         `MCP_HOST`:`MCP_PORT` (see config.py / .env).
      3. Otherwise → stdio, which is how local clients (Claude Desktop, Claude
         Code, Zed) launch the server as a subprocess.
    """
    platform_port = os.getenv("PORT")

    if platform_port:
        mcp.run(transport="http", host="0.0.0.0", port=int(platform_port))
    elif settings.mcp_transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
            **local_http_options(),
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
