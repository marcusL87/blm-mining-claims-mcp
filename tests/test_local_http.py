"""Tests for the local HTTP mode's network and browser limits (decision D-026).

The local mode should listen on this computer only, accept browser pages only
from the local addresses in MCP_CORS_ORIGINS, and reject requests whose Host
or Origin header points somewhere else. No live BLM calls are made.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from mining_claims_mcp.app import local_http_options, mcp
from mining_claims_mcp.config import Settings, settings

PAGE = "http://127.0.0.1:5500"
ACCEPT = "application/json, text/event-stream"
LOCAL_SERVER = "http://127.0.0.1:8000"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "mcp_cors_origins", PAGE)
    app = mcp.http_app(**local_http_options())
    with TestClient(app, base_url=LOCAL_SERVER) as test_client:
        yield test_client


def preflight(client: TestClient, origin: str):
    return client.options(
        "/mcp",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,mcp-session-id",
        },
    )


def test_default_host_is_this_computer_only():
    assert Settings().mcp_host == "127.0.0.1"


def test_no_cors_by_default(monkeypatch):
    monkeypatch.setattr(settings, "mcp_cors_origins", "")
    assert local_http_options()["middleware"] == []


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "null",
        "http://192.168.1.5:5500",
        "https://127.0.0.1:5500",
        "http://127.0.0.1:5500/page",
        "http://example.com:5500",
        f"{PAGE},*",
    ],
)
def test_non_local_cors_origins_are_refused(value):
    with pytest.raises(ValidationError):
        Settings(mcp_cors_origins=value)


def test_local_cors_origins_are_accepted():
    s = Settings(mcp_cors_origins=" http://127.0.0.1:5500 , http://localhost:5500 ")
    assert s.cors_origins == ["http://127.0.0.1:5500", "http://localhost:5500"]


def test_allowed_page_passes_preflight(client):
    response = preflight(client, PAGE)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PAGE
    assert "mcp-session-id" in response.headers["access-control-allow-headers"].lower()


def test_other_local_page_is_not_given_cors(client):
    response = preflight(client, "http://127.0.0.1:9999")
    assert "access-control-allow-origin" not in response.headers


def test_outside_web_site_is_rejected(client):
    response = client.post(
        "/mcp",
        json=INITIALIZE,
        headers={"Origin": "https://evil.example", "Accept": ACCEPT},
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_non_local_host_header_is_rejected(client):
    response = client.get("/health", headers={"Host": "attacker.example"})
    assert response.status_code == 421


def test_allowed_page_can_read_session_id(client):
    response = client.post(
        "/mcp",
        json=INITIALIZE,
        headers={"Origin": PAGE, "Accept": ACCEPT},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PAGE
    assert "mcp-session-id" in response.headers["access-control-expose-headers"].lower()
    assert response.headers.get("mcp-session-id")
