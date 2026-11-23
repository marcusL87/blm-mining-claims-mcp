"""Test a deployed Databricks Apps MCP server from your own computer.

Signs in with a Databricks CLI OAuth profile, connects to <app-url>/mcp, lists
the server's tools, and optionally calls one. Use this instead of the AI
Playground, which in our workspace has no option to attach an MCP server.

Needs the Databricks CLI with a working OAuth profile (`databricks auth login
--profile <name>`). The profile defaults to `blm-mcp`; set
DATABRICKS_CONFIG_PROFILE to use another.

Usage (from the repo root):
  # List tools only:
  uv run python deploy/databricks/remote_test.py https://<app-url>

  # List tools, then call one with JSON arguments:
  uv run python deploy/databricks/remote_test.py https://<app-url> \
      <tool_name> '{"<argument>": "<value>"}'
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

from fastmcp import Client
from fastmcp.client.auth import BearerAuth


def get_token(profile: str) -> str:
    """Get a short-lived OAuth access token from the Databricks CLI."""
    output = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(output)["access_token"]


async def main(base_url: str, tool: str | None, args: dict) -> None:
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "blm-mcp")
    url = base_url.rstrip("/") + "/mcp"
    async with Client(url, auth=BearerAuth(get_token(profile))) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])
        if tool:
            # Time only the tool call, not startup or sign-in.
            start = time.perf_counter()
            try:
                result = await client.call_tool(tool, args)
            except Exception as exc:
                elapsed = time.perf_counter() - start
                print(f"Call failed after {elapsed:.1f} s: {type(exc).__name__}: {exc}")
                raise
            print(f"Call took {time.perf_counter() - start:.1f} s")
            content = result.structured_content
            if content is None:
                content = [getattr(c, "text", str(c)) for c in result.content]
            print(f"Result of {tool}:")
            print(json.dumps(content, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: remote_test.py <app-url> [tool-name] [json-args]")
    tool_name = sys.argv[2] if len(sys.argv) > 2 else None
    tool_args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    asyncio.run(main(sys.argv[1], tool_name, tool_args))
