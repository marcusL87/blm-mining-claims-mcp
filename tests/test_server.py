"""Smoke tests for the BLM mining claims MCP server.

These verify the server imports and every submodule registers without error.
Add functional tests for your own tools as you build them.
"""

from __future__ import annotations

from fastmcp import FastMCP

from mining_claims_mcp.app import mcp
from mining_claims_mcp.config import settings


def test_server_is_importable():
    assert mcp is not None


def test_server_has_name():
    assert mcp.name == "BLM Mining Claims MCP Server"


def test_default_transport_is_stdio():
    """Guard against accidentally shipping with a non-stdio default."""
    assert settings.mcp_transport == "stdio"


def test_tools_register_without_error():
    from mining_claims_mcp.tools import register_tools

    register_tools(FastMCP("test"))  # must not raise


def test_prompts_register_without_error():
    from mining_claims_mcp.prompts import register_prompts

    register_prompts(FastMCP("test"))  # must not raise


def test_resources_register_without_error():
    from mining_claims_mcp.resources import register_resources

    register_resources(FastMCP("test"))  # must not raise


def test_routes_register_without_error():
    from mining_claims_mcp.routes import register_routes

    register_routes(FastMCP("test"))  # must not raise
