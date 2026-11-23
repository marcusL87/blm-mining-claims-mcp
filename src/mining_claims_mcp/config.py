"""Server configuration loaded from environment variables or a .env file.

Every field can be overridden by an environment variable of the same name
(upper- or lower-case). Copy `.env.example` to `.env` for local development.
"""

from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Browser pages allowed by MCP_CORS_ORIGINS must be on this computer:
# http://127.0.0.1:<port> or http://localhost:<port>. Wildcards ("*") and
# "null" (pages opened from a file or a sandboxed frame) are refused.
_LOCAL_ORIGIN = re.compile(r"^http://(127\.0\.0\.1|localhost):\d{1,5}$")


class Settings(BaseSettings):
    """MCP server settings. Override any field via environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Transport ─────────────────────────────────────────────────────────────
    # "stdio"           — local subprocess (Claude Desktop / Claude Code / Zed)
    # "streamable-http" — remote HTTP server (containers, cloud deploys)
    #
    # NOTE: app.py also auto-selects HTTP when the host sets PORT, so you
    # usually do NOT need to set this on a cloud host.
    mcp_transport: str = "stdio"
    # 127.0.0.1 = this computer only; other machines on the network can't
    # connect. Hosts that set PORT (Databricks, Docker) don't use this.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000

    # Local browser pages allowed to call the server (CORS), comma-separated,
    # e.g. "http://127.0.0.1:5500" for a local test page. Empty = none.
    # Only used by the local HTTP mode (MCP_TRANSPORT=streamable-http).
    mcp_cors_origins: str = ""

    @field_validator("mcp_cors_origins")
    @classmethod
    def _only_local_origins(cls, value: str) -> str:
        for origin in (o.strip() for o in value.split(",")):
            if origin and not _LOCAL_ORIGIN.match(origin):
                raise ValueError(
                    f"MCP_CORS_ORIGINS entry {origin!r} is not allowed; use "
                    "http://127.0.0.1:<port> or http://localhost:<port>."
                )
        return value

    @property
    def cors_origins(self) -> list[str]:
        """MCP_CORS_ORIGINS as a list, blanks removed."""
        return [o.strip() for o in self.mcp_cors_origins.split(",") if o.strip()]

    # ── Data source ───────────────────────────────────────────────────────────
    # BLM public mining claims map service (ArcGIS REST, no API key). Tools
    # request paths relative to this, e.g. "0/query" for layer 0 (all cases).
    # Keep the trailing slash so relative paths join correctly. Only the
    # operator can override this (BLM_MINING_CLAIMS_URL); never a tool argument.
    blm_mining_claims_url: str = (
        "https://nlsdbpubgis.blm.gov/arcgis/rest/services/"
        "Mining_Claims/MiningClaims/MapServer/"
    )

    # Fewest records layer 0 (all cases) should hold. When a search finds
    # nothing and the total is below this, the service is probably empty or
    # mid-reload, so tools report that instead of "not found" (D-036). The
    # full total was 4,401,824 on Oct 19, 2026; lower this if BLM's real
    # total ever drops below it (BLM_MIN_TOTAL_RECORDS).
    blm_min_total_records: int = 4_000_000

    # USGS Geographic Names (GNIS) on The National Map (ArcGIS REST, no API
    # key), used to turn place names into coordinates. Same rules as above:
    # keep the trailing slash; only the operator can override this
    # (USGS_GEONAMES_URL), never a tool argument.
    usgs_geonames_url: str = (
        "https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer/"
    )

    # ── Dataset API keys ──────────────────────────────────────────────────────
    # Add a typed field for each API key your tools need, then read it from
    # `settings.<field>` in your tool. Document the matching env var in
    # `.env.example`. Example:
    #
    #   example_api_key: str = ""   # ← set EXAMPLE_API_KEY in the environment


settings = Settings()
