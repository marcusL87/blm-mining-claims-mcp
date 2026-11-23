"""Tests for tool 1, mining_claims_lookup_by_serial (tools/serial_lookup.py).

No test calls the live BLM service. `fetch_json` is replaced by a stand-in
that records each query and returns a made-up ArcGIS-style reply, and the tool
is called through FastMCP's in-memory client, as a real MCP client would.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mining_claims_mcp.claims import (
    INCOMPLETE_ERROR,
    MLRS_LAST_RESORT,
    NOT_IN_DATA_NOTE,
    describe_claim_type,
    describe_quality,
    parse_legal_description,
    service_looks_incomplete,
)
from mining_claims_mcp.config import settings
from mining_claims_mcp.tools import serial_lookup
from mining_claims_mcp.tools.serial_lookup import NATIONWIDE_TIMEOUT, clean_serial
from mining_claims_mcp.utils import DEFAULT_TIMEOUT, UpstreamServiceError, sql_quote

TOOL = "mining_claims_lookup_by_serial"
FULL_TOTAL = 4_401_824  # layer 0 on Oct 19, 2026 (D-035)

CLAIM = {
    "CSE_NR": "MT101334253",
    "LEG_CSE_NR": "MMC114339",
    "CSE_NAME": "POPS 3",
    "CSE_DISP": "Active",
    "CSE_TYPE_NR": "384101",
    "CSE_META": "MT 20 0270N 0310W 016 A NW | SD 07 0020N 0030E 010 Lot4",
    "QLTY": "9: mapped to section",
}


def _row(n: int) -> dict[str, Any]:
    return {
        "CSE_NR": f"MT1013342{n:02d}",
        "LEG_CSE_NR": "",
        "CSE_NAME": f"CLAIM {n}",
        "CSE_DISP": "Active",
    }


class FakeService:
    """Stand-in for the BLM service: records queries, returns set replies."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.timeouts: dict[str, list[float]] = {}
        self.exact: list[dict[str, Any]] = []
        self.count = 0
        self.matches: list[dict[str, Any]] = []
        # Layer 0's total, for the partial-reload check (D-036).
        self.total = FULL_TOTAL
        # Set to "exact", "count", "list", or "total" to make that query fail.
        self.fail_on: str | None = None
        self.failure: Any = UpstreamServiceError("boom")

    @staticmethod
    def kind(params: dict[str, Any]) -> str:
        if params["where"] == "1=1":
            return "total"
        if params.get("returnCountOnly") == "true":
            return "count"
        if " LIKE " in params["where"]:
            return "list"
        return "exact"

    async def fetch_json(
        self, path: str, params: dict[str, Any], timeout: float
    ) -> Any:
        assert path == "0/query"
        self.calls.append(params)
        kind = self.kind(params)
        self.timeouts.setdefault(kind, []).append(timeout)
        if kind == self.fail_on:
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure
        if kind == "total":
            return {"count": self.total}
        if kind == "count":
            return {"count": self.count}
        rows = self.exact if kind == "exact" else self.matches
        return {"features": [{"attributes": r} for r in rows]}

    def wheres(self, kind: str) -> list[str]:
        return [c["where"] for c in self.calls if self.kind(c) == kind]


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()
    monkeypatch.setattr(serial_lookup, "fetch_json", fake.fetch_json)
    return fake


async def call(**args: Any) -> dict[str, Any]:
    mcp = FastMCP("test")
    serial_lookup.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, args)
    return result.structured_content


# ── Input cleaning and validation ────────────────────────────────────────────


def test_clean_serial_drops_spaces_dashes_periods_and_uppercases():
    assert clean_serial(" mmc-114.339 ") == "MMC114339"
    assert clean_serial("mt 1013\t34253") == "MT101334253"


async def test_tool_searches_with_cleaned_serial(service):
    result = await call(serial_number=" mmc-114.339 ")
    assert result["cleaned_serial"] == "MMC114339"
    assert service.wheres("exact") == [
        "CSE_NR = 'MMC114339' OR LEG_CSE_NR = 'MMC114339'"
    ]


@pytest.mark.parametrize("bad", ["", "   ", " - . "])
async def test_empty_input_is_rejected_without_query(service, bad):
    result = await call(serial_number=bad)
    assert result["error"] == "No serial number was given."
    assert "hint" in result
    assert service.calls == []


@pytest.mark.parametrize("bad", ["x'; drop", "MT#123", "MT_101", "MT%", "MT1O1é"])
async def test_invalid_characters_are_rejected_without_query(service, bad):
    result = await call(serial_number=bad)
    assert result["error"] == "That is not a valid serial number."
    assert "hint" in result
    assert service.calls == []


async def test_too_long_input_is_rejected_without_query(service):
    result = await call(serial_number="MT" + "1" * 19)  # 21 characters
    assert result["error"] == "That is not a valid serial number."
    assert service.calls == []


async def test_twenty_characters_is_allowed(service):
    result = await call(serial_number="MT" + "1" * 18)
    assert "error" not in result
    assert service.calls


async def test_unknown_state_code_is_rejected(service):
    with pytest.raises(ToolError):
        await call(serial_number="MT10133", state="SD")
    assert service.calls == []


# ── Matching ─────────────────────────────────────────────────────────────────


async def test_exact_match_on_current_serial(service):
    service.exact = [CLAIM]
    result = await call(serial_number="MT101334253")

    assert result["match_type"] == "exact"
    assert result["matched_on"] == "current serial"
    assert result["summary"] == "Exact match on current serial MT101334253."
    assert result["notes"] == [NOT_IN_DATA_NOTE]
    assert "mlrs" not in str(result).lower()  # ask first, MLRS last (D-045)
    claim = result["claims"][0]
    assert claim["name"] == "POPS 3"
    assert claim["serial_number"] == "MT101334253"
    assert claim["legacy_serial_number"] == "MMC114339"
    assert claim["status"] == "Active"
    assert claim["claim_type"] == "Lode claim"
    assert claim["states"] == ["Montana", "South Dakota"]
    assert len(claim["legal_description"]) == 2
    assert claim["mapping_quality"]["score"] == "9"
    # Exact lookups search nationwide and send only one query.
    assert len(service.calls) == 1
    assert "MT%" not in service.calls[0]["where"]


async def test_exact_match_on_legacy_serial(service):
    service.exact = [CLAIM]
    result = await call(serial_number="mmc 114339")
    assert result["matched_on"] == "legacy serial"
    assert result["summary"] == "Exact match on legacy serial MMC114339."


async def test_exact_match_with_shared_number(service):
    service.exact = [CLAIM, {**CLAIM, "CSE_NR": "MT101334254"}]
    result = await call(serial_number="MMC114339")
    assert result["summary"].endswith("2 claims share this number.")
    assert len(result["claims"]) == 2


async def test_empty_legacy_and_name_become_none(service):
    service.exact = [{**CLAIM, "LEG_CSE_NR": "", "CSE_NAME": ""}]
    claim = (await call(serial_number="MT101334253"))["claims"][0]
    assert claim["legacy_serial_number"] is None
    assert claim["name"] is None


async def test_partial_match_with_more_than_shown(service):
    service.count = 25
    service.matches = [_row(n) for n in range(10)]
    result = await call(serial_number="13342")

    assert result["match_type"] == "partial"
    assert result["total_count"] == 25
    assert len(result["matches"]) == 10
    assert result["matches"][0] == {
        "serial_number": "MT101334200",
        "legacy_serial_number": None,
        "name": "CLAIM 0",
        "status": "Active",
    }
    assert result["summary"] == (
        "Partial match: 25 claims in the Montana/Dakotas office (MT) contain "
        "13342. Showing the first 10, sorted by serial number."
    )
    assert result["hint"] == "Ask for more of the serial number to narrow the search."


async def test_partial_match_all_shown_has_no_hint(service):
    service.count = 1
    service.matches = [_row(1)]
    result = await call(serial_number="13342")
    assert result["summary"] == (
        "Partial match: 1 claim in the Montana/Dakotas office (MT) contains 13342."
    )
    assert "hint" not in result


async def test_partial_queries_send_expected_settings(service):
    service.count = 1
    service.matches = [_row(1)]
    await call(serial_number="13342")

    exact, *partial = service.calls
    assert exact["returnGeometry"] == "false"
    count = next(c for c in partial if c.get("returnCountOnly") == "true")
    listing = next(c for c in partial if c is not count)
    assert listing["returnGeometry"] == "false"
    assert listing["resultRecordCount"] == 10
    assert listing["orderByFields"] == "CSE_NR"
    assert all(c["f"] == "json" for c in service.calls)


async def test_too_short_for_partial(service):
    result = await call(serial_number="MT12")
    assert result["match_type"] == "none"
    assert result["summary"] == "No claim has the exact serial number MT12."
    assert "at least 5 characters" in result["hint"]
    # Exact search and the layer 0 total (D-036); no partial search.
    assert [service.kind(c) for c in service.calls] == ["exact", "total"]


async def test_no_matches_suggests_state_all(service):
    result = await call(serial_number="13342")
    assert result["match_type"] == "none"
    assert result["summary"] == (
        "No claims found matching 13342 in the Montana/Dakotas office (MT)."
    )
    assert "search with state ALL" in result["hint"]
    assert "Check the number with the caller" in result["hint"]
    # D-045: MLRS only after the nationwide search.
    assert "mlrs" not in str(result).lower()


async def test_no_matches_nationwide_omits_state_all_hint(service):
    result = await call(serial_number="13342", state="ALL")
    assert result["summary"] == "No claims found matching 13342 nationwide."
    assert "state ALL" not in result["hint"]
    assert result["hint"].endswith(MLRS_LAST_RESORT)


async def test_partial_match_does_not_mention_mlrs(service):
    service.matches = [CLAIM]
    service.count = 1
    result = await call(serial_number="13342")
    assert result["match_type"] == "partial"
    assert result["notes"] == [NOT_IN_DATA_NOTE]
    assert "mlrs" not in str(result).lower()


async def test_too_short_does_not_mention_mlrs(service):
    result = await call(serial_number="MT12")
    assert "mlrs" not in str(result).lower()


async def test_default_state_filters_on_mt_prefix(service):
    await call(serial_number="13342")
    assert set(service.wheres("count") + service.wheres("list")) == {
        "CSE_NR LIKE 'MT%' AND (CSE_NR LIKE '%13342%' OR LEG_CSE_NR LIKE '%13342%')"
    }


async def test_other_office_filters_on_its_prefix(service):
    service.count = 1
    service.matches = [_row(1)]
    result = await call(serial_number="13342", state="NV")
    assert "in the Nevada office (NV)" in result["summary"]
    assert set(service.wheres("list")) == {
        "CSE_NR LIKE 'NV%' AND (CSE_NR LIKE '%13342%' OR LEG_CSE_NR LIKE '%13342%')"
    }


async def test_state_all_has_no_prefix_filter(service):
    service.count = 1
    service.matches = [_row(1)]
    result = await call(serial_number="13342", state="ALL")
    assert "nationwide" in result["summary"]
    assert set(service.wheres("list")) == {
        "(CSE_NR LIKE '%13342%' OR LEG_CSE_NR LIKE '%13342%')"
    }


async def test_state_all_partial_uses_longer_timeout(service):
    await call(serial_number="13342", state="ALL")
    assert service.timeouts["exact"] == [DEFAULT_TIMEOUT]
    assert service.timeouts["count"] == [NATIONWIDE_TIMEOUT]
    assert service.timeouts["list"] == [NATIONWIDE_TIMEOUT]


async def test_office_partial_uses_default_timeout(service):
    await call(serial_number="13342", state="NV")
    assert set(service.timeouts["count"] + service.timeouts["list"]) == {
        DEFAULT_TIMEOUT
    }


# ── Safety ───────────────────────────────────────────────────────────────────


def test_sql_quote_escapes_single_quotes():
    assert sql_quote("MT123") == "'MT123'"
    assert sql_quote("O'Brien") == "'O''Brien'"
    assert sql_quote("''") == "''''''"


@pytest.mark.parametrize(
    "bad", ["ZQXW'; DROP TABLE", "ZQXW" + "9" * 30, "ZQXW<script>"]
)
async def test_error_messages_do_not_echo_input(service, bad):
    result = await call(serial_number=bad)
    assert "error" in result
    assert "ZQXW" not in str(result)


# ── Error handling ───────────────────────────────────────────────────────────


def _assert_service_error(result: dict[str, Any]) -> None:
    assert result["error"] == (
        "The BLM mining claims service did not respond or returned an error."
    )
    assert "mlrs.blm.gov" in result["hint"]
    assert "match_type" not in result


@pytest.mark.parametrize("fail_on", ["exact", "count", "list"])
async def test_service_failure_returns_error(service, fail_on):
    service.fail_on = fail_on
    _assert_service_error(await call(serial_number="13342"))


@pytest.mark.parametrize("fail_on", ["exact", "count", "list"])
async def test_error_inside_200_reply_is_not_treated_as_no_match(service, fail_on):
    service.fail_on = fail_on
    service.failure = {"error": {"code": 400, "message": "Invalid field: CSE_NRX"}}
    result = await call(serial_number="13342")
    _assert_service_error(result)
    assert "CSE_NRX" not in str(result)


async def test_non_dict_reply_returns_error(service):
    service.fail_on = "exact"
    service.failure = ["not", "a", "dict"]
    _assert_service_error(await call(serial_number="13342"))


# ── Partial-reload check (D-036) ─────────────────────────────────────────────


@pytest.mark.parametrize("serial", ["MT12", "MT99999"])
async def test_nothing_found_on_incomplete_service_is_an_error(service, serial):
    service.total = 1_168_412  # layer 0 during the Oct 18 reload (D-033)
    result = await call(serial_number=serial)
    assert result == INCOMPLETE_ERROR
    assert "match_type" not in result


async def test_empty_service_is_an_error(service):
    service.total = 0
    assert await call(serial_number="MT99999") == INCOMPLETE_ERROR


async def test_total_at_minimum_is_a_normal_no_match(service, monkeypatch):
    monkeypatch.setattr(settings, "blm_min_total_records", 1000)
    service.total = 1000
    assert (await call(serial_number="MT99999"))["match_type"] == "none"
    service.total = 999
    assert await call(serial_number="MT99999") == INCOMPLETE_ERROR


async def test_total_query_settings(service):
    await call(serial_number="MT99999")
    (total,) = [c for c in service.calls if service.kind(c) == "total"]
    assert total == {"where": "1=1", "returnCountOnly": "true", "f": "json"}


async def test_matches_skip_the_total_query(service):
    service.exact = [CLAIM]
    await call(serial_number="MT101334253")
    service.exact = []
    service.count = 1
    service.matches = [_row(1)]
    await call(serial_number="13342")
    assert "total" not in [service.kind(c) for c in service.calls]


async def test_matches_found_even_on_incomplete_service(service):
    service.total = 0
    service.exact = [CLAIM]
    assert (await call(serial_number="MT101334253"))["match_type"] == "exact"


@pytest.mark.parametrize(
    "failure", [UpstreamServiceError("boom"), {"error": {"code": 500}}, None]
)
async def test_failed_total_query_returns_error_not_no_match(service, failure):
    service.fail_on = "total"
    service.failure = failure
    _assert_service_error(await call(serial_number="MT99999"))


@pytest.mark.parametrize("total", [None, "4401824"])
async def test_total_without_a_number_returns_error(service, total):
    service.total = total
    _assert_service_error(await call(serial_number="MT99999"))


@pytest.mark.parametrize(
    "count, expected",
    [(0, True), (3_999_999, True), (4_000_000, False), (4_401_824, False)],
)
def test_service_looks_incomplete(count, expected):
    assert service_looks_incomplete({"count": count}) is expected


@pytest.mark.parametrize(
    "reply", [{}, {"count": None}, {"count": "5"}, {"count": True}, {"count": 4.4e6}]
)
def test_service_looks_incomplete_needs_a_whole_number(reply):
    with pytest.raises(UpstreamServiceError):
        service_looks_incomplete(reply)


# ── Legal description ────────────────────────────────────────────────────────


def test_legal_description_quarter_section():
    (part,) = parse_legal_description("MT 20 0270N 0310W 016 A NW")
    assert part == {
        "state": "MT",
        "meridian": "Principal Meridian, Montana",
        "township": "T. 27 N.",
        "range": "R. 31 W.",
        "section": "sec. 16",
        "subdivision": "NW1/4",
        "text": "Principal Meridian, Montana, T. 27 N., R. 31 W., sec. 16, NW1/4",
        "raw": "MT 20 0270N 0310W 016 A NW",
    }


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        ("A NESW", "NE1/4SW1/4"),
        ("SENE", "SE1/4NE1/4"),
        ("Lot4", "Lot 4"),
        ("A Government Lot 4", "Lot 4"),
        ("A ALL", "all of section"),
        ("A Tract 37", "Tract 37"),
    ],
)
def test_legal_description_subdivisions(ending, expected):
    (part,) = parse_legal_description(f"MT 20 0270N 0310W 016 {ending}")
    assert part["subdivision"] == expected


def test_legal_description_meridians_and_fractions():
    parts = parse_legal_description(
        "SD 07 0020N 0030E 010 | ND 05 1390N 0990W 001 | MT 99 0271S 0312E 036"
    )
    assert [p["meridian"] for p in parts] == [
        "Black Hills Meridian",
        "Fifth Principal Meridian",
        "meridian code 99",
    ]
    assert parts[1]["township"] == "T. 139 N."
    assert parts[2]["township"] == "T. 27 1/4 S."
    assert parts[2]["range"] == "R. 31 1/2 E."


def test_legal_description_drops_section_only_part_when_subdivided():
    parts = parse_legal_description(
        "MT 20 0270N 0310W 016 | MT 20 0270N 0310W 016 A NW | MT 20 0270N 0310W 017"
    )
    assert [p["raw"] for p in parts] == [
        "MT 20 0270N 0310W 016 A NW",
        "MT 20 0270N 0310W 017",  # no subdivision of sec. 17 given, so kept
    ]
    assert parts[1]["subdivision"] is None
    assert parts[1]["text"].endswith("sec. 17")


def test_legal_description_removes_duplicates_and_handles_both_separators():
    parts = parse_legal_description(
        "MT 20 0270N 0310W 016 A NW|MT  20 0270N 0310W 016 A NW | "
        "MT 20 0270N 0310W 016 A NW|MT 20 0270N 0310W 016 A SE"
    )
    assert [p["subdivision"] for p in parts] == ["NW1/4", "SE1/4"]


@pytest.mark.parametrize("meta", [None, "", "   ", "SD      ", "SD | MT", " | "])
def test_legal_description_blank_or_state_only_is_empty(meta):
    assert parse_legal_description(meta) == []


def test_legal_description_keeps_unrecognized_text_raw():
    assert parse_legal_description("Something odd") == [
        {"raw": "Something odd", "text": "Something odd"}
    ]


# ── Mapping quality ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("qlty", "score", "meaning_start"),
    [
        ("0", "0", "Mapped from a direct match"),
        ("3: PLSS match", "3", "Mapped from a direct match"),
        ("4.1; calculated", "4.1", "Mapped as a calculated best guess"),
        ("8", "8", "Mapped as a calculated best guess"),
        ("8.2: section", "8.2", "Mapped to the whole section"),
        ("10", "10", "Mapped to the whole section"),
        ("11", "11", "No map shape"),
        ("12;", "12", "No map shape"),
        ("21: no shape", "21", "No map shape"),
        ("15", "15", "Only partly mapped"),
        ("25: county", "25", "Mapped to the whole county"),
        ("100", "100", "Map shape improved by BLM staff"),
        ("13", "13", "Mapping quality score not recognized"),
    ],
)
def test_describe_quality(qlty, score, meaning_start):
    result = describe_quality(qlty)
    assert result["score"] == score
    assert result["meaning"].startswith(meaning_start)


@pytest.mark.parametrize("qlty", [None, "", "  ", "no score"])
def test_describe_quality_not_recorded(qlty):
    assert describe_quality(qlty) == {
        "score": None,
        "meaning": "Mapping quality is not recorded.",
    }


# ── Claim type ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("384101", "Lode claim"),
        ("384103", "Lode claim"),
        ("384201", "Placer claim"),
        ("384303", "Tunnel site"),
        ("384401", "Mill site"),
        ("999901", "Unknown type (code 999901)"),
        ("", None),
        (None, None),
    ],
)
def test_describe_claim_type(code, expected):
    assert describe_claim_type(code) == expected
