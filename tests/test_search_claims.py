"""Tests for the search tool, mining_claims_search (tools/search_claims.py).

No test calls the live BLM service. `fetch_json` is replaced by a stand-in
that records each query and returns made-up ArcGIS-style replies: a count
and a sorted list per layer, plus layer 0's total for the partial-reload
check (D-036). The tool is called through FastMCP's in-memory client, as a
real MCP client would.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mining_claims_mcp.claims import (
    INCOMPLETE_ERROR,
    MLRS_LAST_RESORT,
    NOT_IN_DATA_NOTE,
)
from mining_claims_mcp.config import settings
from mining_claims_mcp.models import LandState
from mining_claims_mcp.tools import search_claims
from mining_claims_mcp.tools.find_near_location import DETAIL_FIELDS
from mining_claims_mcp.tools.search_claims import (
    CLAIMANT_NOTE,
    DETAILS_HINT,
    LEGAL_NOTE,
    LOCATE_HINT,
    MAX_CLAIMS_SHOWN,
    NAME_NOTE,
    OFFICE_LABEL,
    SECTION_NOTE,
    build_where,
    clean_name,
    location_pattern,
    parse_range,
    parse_township,
)
from mining_claims_mcp.utils import UpstreamServiceError

TOOL = "mining_claims_search"
FULL_TOTAL = 4_401_824  # layer 0 on Oct 19, 2026 (D-035)
OFFICE = "CSE_NR LIKE 'MT%'"
RG_NO = {"township": "12N", "range": "13W", "section": 26}  # D-038
ROW_COUNT = object()  # FakeBlm.count default: count the stored records


def record(
    n: int,
    serial: str | None = None,
    name: str | None = None,
    type_nr: str | None = "384101",
    disp: str | None = "Active",
) -> dict[str, Any]:
    return {
        "CSE_NR": serial if serial is not None else f"MT{101645000 + n}",
        "LEG_CSE_NR": f"MMC{n}",
        "CSE_NAME": name if name is not None else f"CLAIM {n}",
        "CSE_DISP": disp,
        "CSE_TYPE_NR": type_nr,
        "CSE_META": "MT 20 0120N 0130W 026 A NE",
        "QLTY": "1: Matched",
    }


class FakeBlm:
    """Stand-in for the BLM service: records per layer, count and sorted list."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []
        self.records: dict[int, list[dict[str, Any]]] = {}
        # Overrides the search count (e.g. 69 matches but only 25 listed).
        self.count: Any = ROW_COUNT
        # Layer 0's total, for the partial-reload check (D-036).
        self.total: Any = FULL_TOTAL
        # "count", "list" or "total" fails that query.
        self.fail_on: str | None = None
        self.failure: Any = UpstreamServiceError("boom")

    def _fail(self) -> Any:
        if isinstance(self.failure, Exception):
            raise self.failure
        return self.failure

    async def fetch_json(self, path: str, params: dict[str, Any], **_: Any) -> Any:
        m = re.fullmatch(r"(\d+)/query", path)
        assert m, path
        layer = int(m.group(1))
        self.calls.append((layer, params))
        rows = self.records.get(layer, [])
        if params.get("where") == "1=1":
            if self.fail_on == "total":
                return self._fail()
            return {"count": self.total}
        if params.get("returnCountOnly") == "true":
            if self.fail_on == "count":
                return self._fail()
            return {"count": len(rows) if self.count is ROW_COUNT else self.count}
        if self.fail_on == "list":
            return self._fail()
        rows = sorted(rows, key=lambda r: r["CSE_NR"])
        return {
            "features": [{"attributes": r} for r in rows[: params["resultRecordCount"]]]
        }

    def search_calls(self) -> list[tuple[int, dict[str, Any]]]:
        return [c for c in self.calls if c[1].get("where") != "1=1"]

    def count_call(self) -> tuple[int, dict[str, Any]]:
        (call,) = [c for c in self.search_calls() if "returnCountOnly" in c[1]]
        return call

    def list_call(self) -> tuple[int, dict[str, Any]]:
        (call,) = [c for c in self.search_calls() if "returnCountOnly" not in c[1]]
        return call

    def total_calls(self) -> list[tuple[int, dict[str, Any]]]:
        return [c for c in self.calls if c[1].get("where") == "1=1"]


@pytest.fixture
def blm(monkeypatch) -> FakeBlm:
    fake = FakeBlm()
    monkeypatch.setattr(search_claims, "fetch_json", fake.fetch_json)
    return fake


async def call(**args: Any) -> dict[str, Any]:
    mcp = FastMCP("test")
    search_claims.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, args)
    return result.structured_content


async def where_for(blm: FakeBlm, **args: Any) -> str:
    result = await call(**args)
    assert "error" not in result, result
    return blm.count_call()[1]["where"]


def _assert_service_error(result: dict[str, Any]) -> None:
    assert result["error"].startswith("The BLM mining claims service")
    assert "hint" in result
    assert "claims" not in result
    assert "total_claims" not in result


# ── Registration ─────────────────────────────────────────────────────────────


async def test_tool_is_registered_read_only():
    mcp = FastMCP("test")
    search_claims.register(mcp)
    async with Client(mcp) as client:
        (tool,) = await client.list_tools()
    assert tool.name == TOOL
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


# ── Missing or mismatched filters (no query sent) ────────────────────────────


@pytest.mark.parametrize(
    "args", [{}, {"status": "all"}, {"land_state": "SD"}, {"section": 26}]
)
async def test_no_name_and_no_location_is_rejected(blm, args):
    result = await call(**args)
    assert result["error"] == "Give a claim name, or a township and range."
    assert "mining_claims_lookup_by_serial" in result["hint"]
    assert blm.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"township": "12N"},
        {"range": "13W"},
        {"claim_name": "DUTCHESS", "township": "12N"},
        {"claim_name": "DUTCHESS", "range": "13W"},
    ],
)
async def test_township_and_range_must_come_together(blm, args):
    result = await call(**args)
    assert result["error"] == "Township and range must be given together."
    assert "hint" in result
    assert blm.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"section": 26, "claim_name": "DUTCHESS"},
        {"section": 26, "township": "12N"},
        {"section": 26, "range": "13W"},
    ],
)
async def test_section_needs_township_and_range(blm, args):
    result = await call(**args)
    assert result["error"] == "A section needs a township and range too."
    assert blm.calls == []


# ── Claim name cleaning ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, cleaned",
    [
        ("dutchess", "DUTCHESS"),
        ("  Dutchess   Oven  i ", "DUTCHESS OVEN I"),
        ("rg no", "RG NO"),
        ("abc", "ABC"),  # exactly the minimum
        ("  a   b  ", "A B"),  # collapsed, still 3
        ("o'brien #2", "O'BRIEN #2"),
        ("A-1 & B.C.", "A-1 & B.C."),
        ("tab\tand\nnewline", "TAB AND NEWLINE"),
        ("X" * 60, "X" * 60),  # exactly the maximum
    ],
)
def test_clean_name_accepts(text, cleaned):
    assert clean_name(text) == (cleaned, None)


@pytest.mark.parametrize(
    "text, error",
    [
        ("50%", "can't contain % or _"),
        ("RG_NO", "can't contain % or _"),
        ("%", "can't contain % or _"),  # wildcard check comes first
        ("ab", "at least 3 characters"),
        ("", "at least 3 characters"),
        ("   ", "at least 3 characters"),
        ("X" * 61, "longer than 60"),
        ("DUTCH!", "can't be searched"),
        ("GOLD; DROP", "can't be searched"),
        ("ÉCLAIR", "can't be searched"),
        ('SAY "HI"', "can't be searched"),
        ("A)B", "can't be searched"),
    ],
)
def test_clean_name_rejects(text, error):
    name, result = clean_name(text)
    assert name is None
    assert error in result["error"]
    assert "hint" in result


@pytest.mark.parametrize("name", ["ab", "50%", "RG_NO", "X" * 61, "DUTCH!"])
async def test_bad_name_is_rejected_without_query(blm, name):
    result = await call(claim_name=name)
    assert "error" in result and "hint" in result
    assert blm.calls == []


async def test_bad_name_is_rejected_even_with_good_location(blm):
    result = await call(claim_name="5%", township="12N", range="13W")
    assert "% or _" in result["error"]
    assert blm.calls == []


# ── Township and range parsing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "12N",
        "12 N",
        "12n",
        "T12N",
        "T 12 N",
        "T. 12 N.",
        "t.12n.",
        "12 north",
        "12 NORTH",
        "TWP 12 N",
        "twp. 12 north",
        "Township 12 N",
        "township 12 north",
        "  T.  12   N.  ",
        "012N",
    ],
)
def test_township_forms(text):
    assert parse_township(text) == (12, "N")


@pytest.mark.parametrize("text", ["3S", "T. 3 S.", "3 south", "TWP 3 S"])
def test_south_township(text):
    assert parse_township(text) == (3, "S")


@pytest.mark.parametrize(
    "text, value", [("1N", (1, "N")), ("163N", (163, "N")), ("199N", (199, "N"))]
)
def test_township_limits_accepted(text, value):
    assert parse_township(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "0N",
        "200N",
        "1234N",
        "12",
        "N",
        "N12",
        "12X",
        "12E",
        "12 W",
        "R12N",
        "12 N W",
        "12.5N",
        "12½N",
        "-12N",
        "twelve north",
        "",
        "12 NORTHWEST",
    ],
)
def test_bad_township(text):
    assert parse_township(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "13W",
        "13 W",
        "R13W",
        "R. 13 W.",
        "r.13w.",
        "13 west",
        "RNG 13 W",
        "Range 13 west",
    ],
)
def test_range_forms(text):
    assert parse_range(text) == (13, "W")


@pytest.mark.parametrize("text", ["3E", "R. 3 E.", "3 east", "RANGE 3 E"])
def test_east_range(text):
    assert parse_range(text) == (3, "E")


@pytest.mark.parametrize("text, value", [("1E", (1, "E")), ("99W", (99, "W"))])
def test_range_limits_accepted(text, value):
    assert parse_range(text) == value


@pytest.mark.parametrize(
    "text", ["0W", "100W", "13", "13N", "13 S", "T13W", "W13", "13.5W", ""]
)
def test_bad_range(text):
    assert parse_range(text) is None


@pytest.mark.parametrize(
    "args, error",
    [
        ({"township": "200N", "range": "13W"}, "That township can't be read."),
        ({"township": "12E", "range": "13W"}, "That township can't be read."),
        ({"township": "12N", "range": "100W"}, "That range can't be read."),
        ({"township": "12N", "range": "13N"}, "That range can't be read."),
    ],
)
async def test_bad_township_or_range_is_rejected_without_query(blm, args, error):
    result = await call(**args)
    assert result["error"] == error
    assert "Fractional" in result["hint"]
    assert blm.calls == []


@pytest.mark.parametrize("section", [0, 37, -1, 100])
async def test_bad_section_is_rejected_without_query(blm, section):
    result = await call(township="12N", range="13W", section=section)
    assert result["error"] == "That section number is not allowed."
    assert "36" in result["hint"]
    assert blm.calls == []


@pytest.mark.parametrize("section", [1, 36])
async def test_section_limits_accepted(blm, section):
    where = await where_for(blm, township="12N", range="13W", section=section)
    assert f" {section:03d}%'" in where


@pytest.mark.parametrize(
    "args",
    [
        {"claim_name": "DUTCHESS", "status": "open"},
        {"claim_name": "DUTCHESS", "status": "ACTIVE"},
        {"township": "12N", "range": "13W", "land_state": "WY"},
        {"township": "12N", "range": "13W", "land_state": "mt"},
        {"township": "12N", "range": "13W", "section": "twenty"},
        {"township": "12N", "range": "13W", "section": 2.5},
        {"claim_name": 123},
    ],
)
async def test_bad_types_or_choices_are_rejected_by_schema(blm, args):
    with pytest.raises(ToolError):
        await call(**args)
    assert blm.calls == []


# ── Where clause text ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "land_state, township, range_, section, pattern",
    [
        (LandState.MT, (12, "N"), (13, "W"), 26, "20 012_N 013_W 026"),
        (LandState.MT, (12, "N"), (13, "W"), None, "20 012_N 013_W"),
        (LandState.SD, (3, "S"), (3, "E"), None, "07 003_S 003_E"),
        (LandState.ND, (163, "N"), (99, "W"), 1, "05 163_N 099_W 001"),
    ],
)
def test_location_pattern(land_state, township, range_, section, pattern):
    assert location_pattern(land_state, township, range_, section) == pattern


def test_build_where_parts():
    assert build_where(None, None) == OFFICE
    assert build_where("DUTCHESS", None) == (f"{OFFICE} AND CSE_NAME LIKE '%DUTCHESS%'")
    assert build_where(None, "20 012_N 013_W") == (
        f"{OFFICE} AND CSE_META LIKE '%20 012_N 013_W%'"
    )
    assert build_where("RG NO", "20 012_N 013_W 026") == (
        f"{OFFICE} AND CSE_NAME LIKE '%RG NO%' AND CSE_META LIKE '%20 012_N 013_W 026%'"
    )


@pytest.mark.parametrize(
    "args, where",
    [
        ({"claim_name": " dutchess "}, f"{OFFICE} AND CSE_NAME LIKE '%DUTCHESS%'"),
        (
            {"township": "T. 12 N.", "range": "R. 13 W."},
            f"{OFFICE} AND CSE_META LIKE '%20 012_N 013_W%'",
        ),
        (RG_NO, f"{OFFICE} AND CSE_META LIKE '%20 012_N 013_W 026%'"),
        (
            {"township": "3 south", "range": "3 east", "land_state": "SD"},
            f"{OFFICE} AND CSE_META LIKE '%07 003_S 003_E%'",
        ),
        (
            {"township": "150N", "range": "80W", "land_state": "ND"},
            f"{OFFICE} AND CSE_META LIKE '%05 150_N 080_W%'",
        ),
        (
            {"claim_name": "dutchess", "township": "29N", "range": "27W"},
            f"{OFFICE} AND CSE_NAME LIKE '%DUTCHESS%' "
            "AND CSE_META LIKE '%20 029_N 027_W%'",
        ),
    ],
)
async def test_where_text_sent(blm, args, where):
    assert await where_for(blm, **args) == where


async def test_single_quote_in_name_is_escaped(blm):
    where = await where_for(blm, claim_name="o'brien")
    assert where == f"{OFFICE} AND CSE_NAME LIKE '%O''BRIEN%'"


async def test_land_state_is_ignored_for_name_only(blm):
    result = await call(claim_name="DUTCHESS", land_state="SD")
    assert "CSE_META" not in blm.count_call()[1]["where"]
    assert "land_state" not in result["search"]


async def test_every_query_has_the_office_filter(blm):
    await call(claim_name="DUTCHESS", township="29N", range="27W", status="all")
    for _, params in blm.search_calls():
        assert params["where"].startswith(OFFICE + " AND ")


# ── Layers, count and list ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, layer", [(None, 1), ("active", 1), ("closed", 2), ("all", 0)]
)
async def test_status_maps_to_layer(blm, status, layer):
    blm.records[layer] = [record(1)]
    args = {"claim_name": "CLAIM"} | ({} if status is None else {"status": status})
    result = await call(**args)
    assert result["total_claims"] == 1
    assert {c[0] for c in blm.search_calls()} == {layer}
    assert result["search"]["status"] == (status or "active")


async def test_count_and_list_are_both_sent(blm):
    blm.records[1] = [record(1)]
    await call(**RG_NO)
    where = f"{OFFICE} AND CSE_META LIKE '%20 012_N 013_W 026%'"
    assert len(blm.search_calls()) == 2
    assert blm.count_call() == (
        1,
        {
            "where": where,
            "returnCountOnly": "true",
            "f": "json",
        },
    )
    assert blm.list_call() == (
        1,
        {
            "where": where,
            "outFields": DETAIL_FIELDS,
            "orderByFields": "CSE_NR",
            "resultRecordCount": MAX_CLAIMS_SHOWN,
            "returnGeometry": "false",
            "f": "json",
        },
    )


# ── Results ──────────────────────────────────────────────────────────────────


async def test_rg_no_search_result(blm):
    blm.records[1] = [
        record(461, serial="MT101645461", name="RG NO 3"),
        record(459, serial="MT101645459", name="RG NO 1"),
        record(460, serial="MT101645460", name="RG NO 2"),
    ]
    result = await call(**RG_NO)
    assert result["search"] == {
        "office": OFFICE_LABEL,
        "township": "T. 12 N.",
        "range": "R. 13 W.",
        "section": 26,
        "land_state": "MT",
        "land_state_name": "Montana",
        "status": "active",
    }
    assert result["total_claims"] == 3
    assert result["summary"] == (
        "3 active claims found in T. 12 N., R. 13 W., sec. 26 (Montana)."
    )
    assert [c["name"] for c in result["claims"]] == ["RG NO 1", "RG NO 2", "RG NO 3"]
    assert result["notes"] == [SECTION_NOTE, LEGAL_NOTE, CLAIMANT_NOTE]
    assert result["hint"] == DETAILS_HINT
    assert "nlsdbpubgis.blm.gov" in result["source"]


async def test_name_search_echo_and_notes(blm):
    blm.records[1] = [record(1, name="DUTCHESS")]
    result = await call(claim_name="  dutchess ")
    assert result["search"] == {
        "office": OFFICE_LABEL,
        "claim_name": "DUTCHESS",
        "status": "active",
    }
    assert result["summary"] == (
        '1 active claim found with a name containing "DUTCHESS".'
    )
    assert result["notes"] == [NAME_NOTE, LEGAL_NOTE, CLAIMANT_NOTE]


async def test_name_and_location_summary(blm):
    blm.records[0] = [record(1, disp="Closed"), record(2, disp="Closed")]
    result = await call(
        claim_name="dutchess", township="29N", range="27W", status="all"
    )
    assert result["summary"] == (
        '2 claims (active and closed) found with a name containing "DUTCHESS" '
        "in T. 29 N., R. 27 W. (Montana)."
    )
    assert result["notes"] == [NAME_NOTE, LEGAL_NOTE, CLAIMANT_NOTE]


@pytest.mark.parametrize(
    "land_state, name", [("SD", "South Dakota"), ("ND", "North Dakota")]
)
async def test_dakota_summary(blm, land_state, name):
    blm.records[2] = [record(1, disp="Closed")]
    result = await call(
        township="3S", range="3E", land_state=land_state, status="closed"
    )
    assert result["search"]["land_state"] == land_state
    assert result["summary"] == f"1 closed claim found in T. 3 S., R. 3 E. ({name})."


async def test_claim_has_tool_1_shape(blm):
    blm.records[1] = [record(7, serial="MT101332090", type_nr="384201")]
    claim = (await call(claim_name="CLAIM"))["claims"][0]
    assert claim["serial_number"] == "MT101332090"
    assert claim["legacy_serial_number"] == "MMC7"
    assert claim["name"] == "CLAIM 7"
    assert claim["status"] == "Active"
    assert claim["claim_type"] == "Placer claim"
    assert claim["states"] == ["Montana"]
    assert claim["legal_description"][0]["section"] == "sec. 26"
    assert claim["mapping_quality"]["score"] == "1"


async def test_exactly_25_is_not_more(blm):
    blm.records[1] = [record(i) for i in range(1, MAX_CLAIMS_SHOWN + 1)]
    result = await call(claim_name="CLAIM")
    assert result["total_claims"] == MAX_CLAIMS_SHOWN
    assert len(result["claims"]) == MAX_CLAIMS_SHOWN
    assert "Showing" not in result["summary"]
    assert result["hint"] == DETAILS_HINT


async def test_more_than_25_shows_first_25_sorted(blm):
    blm.records[1] = [record(i) for i in range(30, 0, -1)]
    result = await call(claim_name="CLAIM")
    serials = [c["serial_number"] for c in result["claims"]]
    assert len(serials) == MAX_CLAIMS_SHOWN
    assert serials == sorted(serials)
    assert result["total_claims"] == 30
    assert result["summary"].endswith(" Showing the first 25, sorted by serial number.")


async def test_list_longer_than_25_is_still_capped(blm, monkeypatch):
    # If the service ignored resultRecordCount, the tool still shows 25.
    blm.records[1] = [record(i) for i in range(1, 41)]

    async def no_limit(path, params, **kw):
        return await FakeBlm.fetch_json(
            blm, path, {**params, "resultRecordCount": 1000}, **kw
        )

    monkeypatch.setattr(search_claims, "fetch_json", no_limit)
    result = await call(claim_name="CLAIM")
    assert len(result["claims"]) == MAX_CLAIMS_SHOWN


@pytest.mark.parametrize(
    "args, narrow",
    [
        ({"claim_name": "DUTCHESS"}, "To narrow it, add a township and range. "),
        (
            {"claim_name": "DUTCHESS", "status": "all"},
            "To narrow it, add a township and range or status active or closed. ",
        ),
        (
            {"township": "3S", "range": "3E"},
            "To narrow it, add a section or part of the claim name. ",
        ),
        (RG_NO, "To narrow it, add part of the claim name. "),
        (
            {"claim_name": "DUTCHESS", "township": "29N", "range": "27W"},
            "To narrow it, add a section. ",
        ),
        (
            {"claim_name": "RG NO", "status": "closed", **RG_NO},
            "Only the first 25 are shown; there is no paging. ",
        ),
        (
            {"claim_name": "RG NO", "status": "all", **RG_NO},
            "To narrow it, add status active or closed. ",
        ),
    ],
)
async def test_narrow_hint_lists_only_missing_filters(blm, args, narrow):
    layer = {"active": 1, "closed": 2, "all": 0}[args.get("status", "active")]
    blm.records[layer] = [record(i) for i in range(1, 26)]
    blm.count = 69
    result = await call(**args)
    assert result["total_claims"] == 69
    assert result["hint"] == narrow + DETAILS_HINT


# ── No claims and the partial-reload check (D-036) ───────────────────────────


@pytest.mark.parametrize(
    "args, hint",
    [
        (
            {"claim_name": "DUTCHESS"},
            "Try to check the spelling or give a shorter part of the name, or "
            f"search with status all, or {LOCATE_HINT}.",
        ),
        (
            {"claim_name": "DUTCHESS", "status": "all"},
            "Try to check the spelling or give a shorter part of the name, or "
            f"{LOCATE_HINT}.",
        ),
        (
            {"township": "12N", "range": "13W", "status": "closed"},
            "Try to search with status all, or check land_state (the meridian).",
        ),
        (
            RG_NO,
            "Try to search with status all, or drop the section, or check "
            "land_state (the meridian).",
        ),
        (
            {"claim_name": "ZZQQXX", "status": "all", **RG_NO},
            "Try to check the spelling or give a shorter part of the name, or "
            f"drop the section, or check land_state (the meridian). {MLRS_LAST_RESORT}",
        ),
        (
            {"township": "12N", "range": "13W", "status": "all"},
            f"Try to check land_state (the meridian). {MLRS_LAST_RESORT}",
        ),
    ],
)
async def test_no_claims_hint(blm, args, hint):
    result = await call(**args)
    assert result["total_claims"] == 0
    assert result["claims"] == []
    assert result["hint"] == hint
    assert result["summary"].startswith("No ")


async def test_no_claims_summary_and_notes(blm):
    result = await call(claim_name="zzqqxx")
    assert result["summary"] == (
        'No active claims found with a name containing "ZZQQXX".'
    )
    assert result["notes"] == [NAME_NOTE, LEGAL_NOTE, CLAIMANT_NOTE]
    assert result["search"]["claim_name"] == "ZZQQXX"


async def test_no_claims_checks_layer_0_total(blm):
    await call(claim_name="ZZQQXX")
    ((layer, params),) = blm.total_calls()
    assert layer == 0
    assert params == {"where": "1=1", "returnCountOnly": "true", "f": "json"}


@pytest.mark.parametrize("total", [0, 1_168_412])
async def test_no_claims_on_incomplete_service_is_an_error(blm, total):
    blm.total = total
    result = await call(claim_name="DUTCHESS")
    assert result == INCOMPLETE_ERROR
    assert "total_claims" not in result


async def test_total_at_minimum_is_a_normal_no_claims(blm, monkeypatch):
    monkeypatch.setattr(settings, "blm_min_total_records", 1000)
    blm.total = 1000
    assert (await call(claim_name="DUTCHESS"))["total_claims"] == 0
    blm.total = 999
    assert await call(claim_name="DUTCHESS") == INCOMPLETE_ERROR


async def test_claims_found_skip_the_total_query(blm):
    blm.total = 0
    blm.records[1] = [record(1)]
    assert (await call(claim_name="CLAIM"))["total_claims"] == 1
    assert blm.total_calls() == []


@pytest.mark.parametrize(
    "failure", [UpstreamServiceError("boom"), {"error": {"code": 500}}, None]
)
async def test_failed_total_query_returns_error_not_no_claims(blm, failure):
    blm.fail_on = "total"
    blm.failure = failure
    _assert_service_error(await call(claim_name="DUTCHESS"))


@pytest.mark.parametrize("total", [None, "4401824"])
async def test_total_without_a_number_returns_error(blm, total):
    blm.total = total
    _assert_service_error(await call(claim_name="DUTCHESS"))


# ── Service errors ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        UpstreamServiceError("boom"),
        {"error": {"code": 400, "message": "bad"}},
        [],
        "oops",
        None,
    ],
)
@pytest.mark.parametrize("fail_on", ["count", "list"])
async def test_failed_query_returns_error_not_no_claims(blm, failure, fail_on):
    blm.records[1] = [record(1)]
    blm.fail_on = fail_on
    blm.failure = failure
    _assert_service_error(await call(claim_name="CLAIM"))
    assert blm.total_calls() == []


@pytest.mark.parametrize("count", [None, "3", True, 2.0])
async def test_count_without_a_whole_number_returns_error(blm, count):
    blm.records[1] = [record(1)]
    blm.count = count
    _assert_service_error(await call(claim_name="CLAIM"))


# ── Ask first, MLRS last (D-045) ─────────────────────────────────────────────


def test_claimant_note_is_a_plain_fact():
    assert CLAIMANT_NOTE == NOT_IN_DATA_NOTE
    assert "mlrs" not in (CLAIMANT_NOTE + LEGAL_NOTE + NAME_NOTE).lower()


@pytest.mark.parametrize("count", [3, 69])
async def test_claims_found_do_not_mention_mlrs(blm, count):
    blm.records[1] = [record(i) for i in range(1, min(count, 25) + 1)]
    blm.count = count
    result = await call(claim_name="CLAIM", **RG_NO)
    assert "mlrs" not in str(result).lower()


@pytest.mark.parametrize(
    "args",
    [
        {"claim_name": "DUTCHESS"},
        {"claim_name": "DUTCHESS", "status": "all"},
        RG_NO,
        {"township": "12N", "range": "13W", "status": "closed"},
    ],
)
async def test_no_claims_before_last_resort_do_not_mention_mlrs(blm, args):
    result = await call(**args)
    assert result["total_claims"] == 0
    assert "mlrs" not in str(result).lower()


async def test_name_only_no_claims_asks_for_a_location(blm):
    result = await call(claim_name="DRUMLUMMON", status="all")
    assert "township and range" in result["hint"]
    assert "mining_claims_locate_place" in result["hint"]
