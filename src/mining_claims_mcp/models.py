"""Pydantic models and enums for tool parameters and responses.

Typed parameters give MCP clients a precise schema, which helps the LLM call
your tools correctly. Prefer enums for closed sets of values and Pydantic
models for structured inputs/outputs.

These are illustrative — replace them with models for your own service.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ResponseFormat(str, Enum):
    """How a tool should shape its return value.

    - JSON     → machine-readable dict/list (best for chaining tool calls)
    - MARKDOWN → human-readable text (best for direct display)
    """

    JSON = "json"
    MARKDOWN = "markdown"


class PaginationParams(BaseModel):
    """Reusable pagination inputs for list-style tools.

    Federal APIs almost always paginate. Expose these to the caller (or handle
    paging internally) and return `has_more` / `next_offset` in your response.
    """

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of items to return (1–100).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip from the start of the result set.",
    )


class OfficeCode(str, Enum):
    """BLM state office codes, the prefix of every current serial number.

    MT is the Montana/Dakotas office: its claims in South and North Dakota
    also have MT serials. ES is Eastern States. ALL means no office filter.
    """

    MT = "MT"
    AK = "AK"
    AZ = "AZ"
    CA = "CA"
    CO = "CO"
    ES = "ES"
    ID = "ID"
    NM = "NM"
    NV = "NV"
    OR = "OR"
    UT = "UT"
    WY = "WY"
    ALL = "ALL"

    @property
    def label(self) -> str:
        return _OFFICE_LABELS[self.value]


_OFFICE_LABELS = {
    "MT": "Montana/Dakotas",
    "AK": "Alaska",
    "AZ": "Arizona",
    "CA": "California",
    "CO": "Colorado",
    "ES": "Eastern States",
    "ID": "Idaho",
    "NM": "New Mexico",
    "NV": "Nevada",
    "OR": "Oregon",
    "UT": "Utah",
    "WY": "Wyoming",
    "ALL": "all offices",
}


# Two-letter postal codes for the 50 states and DC, as stored in GNIS
# `state_alpha`.
US_STATES = (
    "AK",
    "AL",
    "AR",
    "AZ",
    "CA",
    "CO",
    "CT",
    "DC",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
)
OFFICE_STATES = ("MT", "SD", "ND")

# Unlike OfficeCode, these are real states: MT means Montana only. OFFICE is
# the three states the Montana/Dakotas office covers; ALL means nationwide.
PlaceState = Enum(
    "PlaceState",
    {"OFFICE": "OFFICE", **{code: code for code in US_STATES}, "ALL": "ALL"},
    type=str,
)
PlaceState.__doc__ = (
    "A two-letter state code, OFFICE (MT, SD, ND), or ALL (nationwide)."
)


class FeatureType(str, Enum):
    """Kinds of places the locate tool can search for. ANY searches them all.

    Each maps to GNIS layers and feature classes in tools/locate_place.py.
    """

    ANY = "any"
    POPULATED_PLACE = "populated_place"
    SUMMIT = "summit"
    RIDGE = "ridge"
    VALLEY = "valley"
    BASIN = "basin"
    FLAT = "flat"
    GAP = "gap"
    STREAM = "stream"
    SPRING = "spring"
    LAKE = "lake"


class ClaimStatus(str, Enum):
    """Which claims the find-near and search tools search. Each maps to a BLM
    layer (LAYERS in tools/find_near_location.py): active = 1, closed = 2,
    all = 0."""

    ACTIVE = "active"
    CLOSED = "closed"
    ALL = "all"


class OfficialTopic(str, Enum):
    """Procedure topics for the official pages tool. Each (except LIST) has a
    row in TOPICS in official_pages.py; LIST returns the topic names."""

    LIST = "list"
    STAKING = "staking"
    WHERE_ALLOWED = "where_allowed"
    RECORDING = "recording"
    FEES = "fees"
    SMALL_MINER_WAIVER = "small_miner_waiver"
    FORMS = "forms"
    TRANSFERS_AND_CHANGES = "transfers_and_changes"
    MLRS_ACCOUNT = "mlrs_account"
    RULES = "rules"


class LandState(str, Enum):
    """The state where the land is, for a township and range search. Each
    picks a meridian code in CSE_META (MERIDIAN_CODES in
    tools/search_claims.py): MT = 20, SD = 07, ND = 05."""

    MT = "MT"
    SD = "SD"
    ND = "ND"
