"""Regression tests for the starter's outbound request boundary."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mining_claims_mcp import utils
from mining_claims_mcp.config import settings


@pytest.mark.parametrize(
    "path",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://localhost/admin",
        "//127.0.0.1/admin",
        "/absolute/path",
        "../admin",
        "0/../../admin",
        "%2e%2e/admin",
        "%252e%252e/admin",
        "%2f%2f127.0.0.1/admin",
        "0/query?target=http://127.0.0.1",
        "0/query#fragment",
        "\\\\127.0.0.1\\admin",
        "",
    ],
)
def test_validate_relative_path_rejects_destination_override(path: str) -> None:
    with pytest.raises(ValueError, match="relative API path"):
        utils._validate_relative_path(path)


def test_validate_relative_path_accepts_expected_api_path() -> None:
    assert utils._validate_relative_path("0/query") == "0/query"


class _FakeClient:
    """Capture client policy while returning a controlled upstream response."""

    init_kwargs: dict[str, Any] = {}
    response = httpx.Response(200, json={"results": []})

    def __init__(self, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    class _Stream:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response

        async def __aenter__(self) -> httpx.Response:
            return self.response

        async def __aexit__(self, *args: object) -> None:
            return None

    def stream(self, method: str, path: str, **kwargs: Any) -> _Stream:
        self.response.request = httpx.Request(
            method, f"{type(self).init_kwargs['base_url']}{path}"
        )
        return self._Stream(self.response)


@pytest.mark.asyncio
async def test_fetch_json_uses_fixed_origin_and_safe_client_policy(monkeypatch) -> None:
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)

    assert await utils.fetch_json("0/query", params={"f": "json"}) == {"results": []}
    assert _FakeClient.init_kwargs["base_url"] == settings.blm_mining_claims_url
    assert _FakeClient.init_kwargs["follow_redirects"] is False
    assert _FakeClient.init_kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_fetch_geonames_json_uses_fixed_origin_and_safe_client_policy(
    monkeypatch,
) -> None:
    _FakeClient.response = httpx.Response(200, json={"results": []})
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)

    assert await utils.fetch_geonames_json("5/query", params={"f": "json"}) == {
        "results": []
    }
    assert _FakeClient.init_kwargs["base_url"] == settings.usgs_geonames_url
    assert _FakeClient.init_kwargs["follow_redirects"] is False
    assert _FakeClient.init_kwargs["trust_env"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch", [utils.fetch_json, utils.fetch_geonames_json])
async def test_fetch_does_not_send_rejected_path(monkeypatch, fetch) -> None:
    class UnexpectedClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("HTTP client must not be created for rejected input")

    monkeypatch.setattr(utils.httpx, "AsyncClient", UnexpectedClient)

    with pytest.raises(ValueError, match="relative API path"):
        await fetch("http://169.254.169.254/latest/meta-data")


@pytest.mark.asyncio
async def test_upstream_error_does_not_expose_sensitive_response(
    monkeypatch, caplog
) -> None:
    sensitive = "SSN 123-45-6789 DOB 1940-01-01 token=secret"
    _FakeClient.response = httpx.Response(500, text=sensitive)
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(utils.UpstreamServiceError) as captured:
        await utils.fetch_json("0/query")

    message = str(captured.value)
    assert message == "Upstream service returned status 500."
    assert sensitive not in message
    assert sensitive not in caplog.text


@pytest.mark.asyncio
async def test_invalid_json_is_sanitized(monkeypatch) -> None:
    sensitive = "not-json SSN 123-45-6789"
    _FakeClient.response = httpx.Response(200, content=sensitive)
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(utils.UpstreamServiceError) as captured:
        await utils.fetch_json("0/query")

    assert str(captured.value) == "Upstream service returned invalid JSON."
    assert sensitive not in str(captured.value)


def test_base_url_is_blm_mining_claims_service() -> None:
    """Relative tool paths must resolve under the BLM MapServer, not beside it."""
    client = httpx.AsyncClient(base_url=settings.blm_mining_claims_url)
    assert str(client.build_request("GET", "0/query").url) == (
        "https://nlsdbpubgis.blm.gov/arcgis/rest/services/"
        "Mining_Claims/MiningClaims/MapServer/0/query"
    )


def test_base_url_is_usgs_geonames_service() -> None:
    """Relative tool paths must resolve under the GNIS MapServer, not beside it."""
    client = httpx.AsyncClient(base_url=settings.usgs_geonames_url)
    assert str(client.build_request("GET", "5/query").url) == (
        "https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer/5/query"
    )
