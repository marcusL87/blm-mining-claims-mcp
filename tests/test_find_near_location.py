"""Tests for the find-near tool, mining_claims_find_near_location
(tools/find_near_location.py).

No test calls the live BLM service. `fetch_json` is replaced by a stand-in
that records each query and returns made-up ArcGIS-style replies: an ID list
for the distance query and records for each fetch by ID. The tool is called
through FastMCP's in-memory client, as a real MCP client would.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mining_claims_mcp.claims import INCOMPLETE_ERROR
from mining_claims_mcp.config import settings
from mining_claims_mcp.tools import find_near_location
from mining_claims_mcp.tools.find_near_location import (
    DETAIL_FIELDS,
    DETAILS_HINT,
    ID_BATCH_SIZE,
    MAPPING_NOTE,
    MAX_CLAIMS_DESCRIBED,
    MAX_CLAIMS_SHOWN,
    MAX_PARALLEL_BATCHES,
    NONE_NOTE,
    check_point,
)
from mining_claims_mcp.utils import UpstreamServiceError

TOOL = "mining_claims_find_near_location"
FULL_TOTAL = 4_401_824  # layer 0 on Oct 19, 2026 (D-035)
MARYSVILLE = {"latitude": 46.75049, "longitude": -112.30029}


def record(
    oid: int,
    serial: str | None = None,
    qlty: str | None = "1: Matched",
    type_nr: str | None = "384101",
    disp: str | None = "Active",
) -> dict[str, Any]:
    return {
        "OBJECTID": oid,
        "CSE_NR": serial if serial is not None else f"MT{100000000 + oid}",
        "LEG_CSE_NR": f"MMC{oid}",
        "CSE_NAME": f"CLAIM {oid}",
        "CSE_DISP": disp,
        "CSE_TYPE_NR": type_nr,
        "CSE_META": "MT 20 0110N 0060W 002 A NE",
        "QLTY": qlty,
    }


class FakeBlm:
    """Stand-in for the BLM service: an ID list per layer, records by ID."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []
        self.ids: dict[int, list[int]] = {}
        self.records: dict[int, dict[str, Any]] = {}
        # Layer 0's total, for the partial-reload check (D-036).
        self.total = FULL_TOTAL
        # "ids" fails the distance query, "total" the layer 0 count; a number
        # fails that fetch batch (0 = first).
        self.fail_on: str | int | None = None
        self.failure: Any = UpstreamServiceError("boom")
        self.batches_seen = 0
        self.active = 0
        self.max_active = 0

    def add(self, layer: int, records: list[dict[str, Any]]) -> None:
        self.ids[layer] = [r["OBJECTID"] for r in records]
        self.records.update({r["OBJECTID"]: r for r in records})

    def _fail(self) -> Any:
        if isinstance(self.failure, Exception):
            raise self.failure
        return self.failure

    async def fetch_json(self, path: str, params: dict[str, Any], **_: Any) -> Any:
        m = re.fullmatch(r"(\d+)/query", path)
        assert m, path
        layer = int(m.group(1))
        self.calls.append((layer, params))
        if params.get("returnCountOnly") == "true":
            if self.fail_on == "total":
                return self._fail()
            return {"count": self.total}
        if params.get("returnIdsOnly") == "true":
            if self.fail_on == "ids":
                return self._fail()
            return {"objectIdFieldName": "OBJECTID", "objectIds": self.ids.get(layer)}
        batch = self.batches_seen
        self.batches_seen += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)  # let other batches start, to measure overlap
            if self.fail_on == batch:
                return self._fail()
            wanted = [int(i) for i in params["objectIds"].split(",")]
            return {
                "features": [
                    {"attributes": self.records[i]} for i in wanted if i in self.records
                ]
            }
        finally:
            self.active -= 1

    def id_calls(self) -> list[tuple[int, dict[str, Any]]]:
        return [c for c in self.calls if c[1].get("returnIdsOnly") == "true"]

    def total_calls(self) -> list[tuple[int, dict[str, Any]]]:
        return [c for c in self.calls if c[1].get("returnCountOnly") == "true"]

    def fetch_calls(self) -> list[tuple[int, dict[str, Any]]]:
        return [c for c in self.calls if "objectIds" in c[1]]


@pytest.fixture
def blm(monkeypatch) -> FakeBlm:
    fake = FakeBlm()
    monkeypatch.setattr(find_near_location, "fetch_json", fake.fetch_json)
    return fake


async def call(**args: Any) -> dict[str, Any]:
    mcp = FastMCP("test")
    find_near_location.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, {**MARYSVILLE, **args})
    return result.structured_content


def _assert_service_error(result: dict[str, Any]) -> None:
    assert result["error"].startswith("The BLM mining claims service")
    assert "hint" in result
    assert "claims" not in result
    assert "total_claims" not in result


# ── Point and radius checks ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lat, lon",
    [
        (46.75, -112.30),  # Montana
        (64.84, -147.72),  # Alaska
        (19.5, -155.5),  # Hawaii
        (18.2, -66.5),  # Puerto Rico
        (17.0, -180.0),  # box corners are inside
        (72.0, -64.0),
    ],
)
def test_us_points_are_accepted(lat, lon):
    assert check_point(lat, lon) is None


@pytest.mark.parametrize(
    "lat, lon", [(math.nan, -112.3), (46.7, math.inf), (-math.inf, -112.3)]
)
def test_non_numbers_are_rejected(lat, lon):
    result = check_point(lat, lon)
    assert result["error"] == "The latitude or longitude is not a number."


def test_positive_longitude_hints_at_missing_minus_sign():
    result = check_point(46.75, 112.3)
    assert result["error"] == "That point is outside the United States."
    assert "minus sign" in result["hint"]


def test_swapped_values_hint():
    result = check_point(-112.3, 46.75)
    assert "swapped" in result["hint"]
    assert "minus sign" not in result["hint"]


@pytest.mark.parametrize("lat, lon", [(51.5, -0.1), (10.0, -80.0), (46.7, -60.0)])
def test_points_outside_the_us_are_rejected(lat, lon):
    result = check_point(lat, lon)
    assert result["error"] == "That point is outside the United States."
    assert "swapped" not in result["hint"]


@pytest.mark.parametrize("lat, lon", [(46.75, 112.3), (95.0, -112.3), (0, 0)])
async def test_bad_point_is_rejected_without_query(blm, lat, lon):
    result = await call(latitude=lat, longitude=lon)
    assert result["error"] == "That point is outside the United States."
    assert blm.calls == []


@pytest.mark.parametrize("radius", [0, -1, 10.01, 100])
async def test_bad_radius_is_rejected_without_query(blm, radius):
    result = await call(radius_miles=radius)
    assert result["error"] == "That radius is not allowed."
    assert "10" in result["hint"]
    assert blm.calls == []


@pytest.mark.parametrize("radius", [0.1, 2.5, 10])
async def test_radius_up_to_cap_is_accepted(blm, radius):
    result = await call(radius_miles=radius)
    assert "error" not in result
    assert blm.id_calls()[0][1]["distance"] == radius


@pytest.mark.parametrize(
    "args", [{"status": "open"}, {"status": "ACTIVE"}, {"latitude": "north"}]
)
async def test_bad_status_or_type_is_rejected_by_schema(blm, args):
    with pytest.raises(ToolError):
        await call(**args)
    assert blm.calls == []


# ── Query text and settings ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, layer", [(None, 1), ("active", 1), ("closed", 2), ("all", 0)]
)
async def test_status_maps_to_layer(blm, status, layer):
    blm.add(layer, [record(1)])
    args = {} if status is None else {"status": status}
    result = await call(**args)
    assert result["total_claims"] == 1
    assert {c[0] for c in blm.calls} == {layer}
    assert result["search"]["status"] == (status or "active")


async def test_distance_query_asks_for_ids_only(blm):
    await call(radius_miles=2)
    ((layer, params),) = blm.id_calls()
    assert params == {
        "where": "1=1",
        "geometry": "-112.30029,46.75049",
        "geometryType": "esriGeometryPoint",
        "inSR": "4269",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": 2,
        "units": "esriSRUnit_StatuteMile",
        "returnIdsOnly": "true",
        "f": "json",
    }


async def test_default_radius_is_one_mile(blm):
    result = await call()
    assert blm.id_calls()[0][1]["distance"] == 1
    assert result["search"]["radius_miles"] == 1


async def test_no_query_ever_sorts_on_the_server(blm):
    blm.add(1, [record(i) for i in range(1, 30)])
    await call()
    assert blm.calls
    assert all("orderByFields" not in p for _, p in blm.calls)


async def test_fetch_asks_for_detail_fields_without_geometry(blm):
    blm.add(1, [record(5), record(3)])
    await call()
    ((layer, params),) = blm.fetch_calls()
    assert layer == 1
    assert params["objectIds"] == "3,5"
    assert params["outFields"] == DETAIL_FIELDS
    assert params["returnGeometry"] == "false"
    assert "geometry" not in params
    assert params["f"] == "json"


async def test_search_echo_is_rounded_numbers(blm):
    result = await call(latitude=46.750491234, longitude=-112.300289876)
    assert result["search"] == {
        "latitude": 46.75049,
        "longitude": -112.30029,
        "radius_miles": 1,
        "status": "active",
    }


# ── Batching ─────────────────────────────────────────────────────────────────


async def test_ids_are_fetched_in_batches_of_100(blm):
    blm.add(1, [record(i) for i in range(250, 0, -1)])
    result = await call()
    sizes = [len(p["objectIds"].split(",")) for _, p in blm.fetch_calls()]
    assert sizes == [ID_BATCH_SIZE, ID_BATCH_SIZE, 50]
    fetched = [int(i) for _, p in blm.fetch_calls() for i in p["objectIds"].split(",")]
    assert fetched == list(range(1, 251))
    assert result["total_claims"] == 250


async def test_at_most_four_batches_run_at_once(blm):
    blm.add(1, [record(i) for i in range(1, 1001)])
    await call()
    assert len(blm.fetch_calls()) == 10
    assert blm.max_active == MAX_PARALLEL_BATCHES


async def test_exactly_the_cap_is_described(blm):
    blm.add(0, [record(i) for i in range(1, MAX_CLAIMS_DESCRIBED + 1)])
    result = await call(status="all")
    assert result["total_claims"] == MAX_CLAIMS_DESCRIBED
    assert len(result["claims"]) == MAX_CLAIMS_SHOWN
    assert sum(result["counts"]["statuses"].values()) == MAX_CLAIMS_DESCRIBED


async def test_over_the_cap_returns_total_only(blm):
    blm.ids[0] = list(range(1, MAX_CLAIMS_DESCRIBED + 2))
    result = await call(status="all", radius_miles=10)
    assert result["total_claims"] == MAX_CLAIMS_DESCRIBED + 1
    assert "too many to list" in result["summary"]
    assert "claims" not in result
    assert "counts" not in result
    assert result["notes"] == [MAPPING_NOTE]
    assert "smaller radius" in result["hint"]
    assert blm.fetch_calls() == []


# ── Results ──────────────────────────────────────────────────────────────────


async def test_no_claims(blm):
    result = await call(radius_miles=2)
    assert result["total_claims"] == 0
    assert result["summary"] == (
        "No active claims found within 2 miles of 46.75049, -112.30029."
    )
    assert result["notes"] == [NONE_NOTE]
    assert "mining_claims_lookup_by_serial" in result["hint"]
    assert "claims" not in result
    assert blm.fetch_calls() == []


async def test_empty_id_list_is_no_claims(blm):
    blm.ids[1] = []
    assert (await call())["total_claims"] == 0


async def test_claims_sorted_by_serial_and_capped_at_25(blm):
    # Object IDs in the opposite order from the serials.
    blm.add(1, [record(i, serial=f"MT{200 - i:03d}") for i in range(1, 31)])
    result = await call()
    serials = [c["serial_number"] for c in result["claims"]]
    assert len(serials) == MAX_CLAIMS_SHOWN
    assert serials == sorted(serials)
    assert serials[0] == "MT170"
    assert result["total_claims"] == 30
    assert result["summary"] == (
        "30 active claims found within 1 miles of 46.75049, -112.30029. "
        "Showing the first 25, sorted by serial number."
    )


async def test_claim_has_tool_1_shape(blm):
    blm.add(1, [record(7, serial="MT101332090", type_nr="384201")])
    claim = (await call())["claims"][0]
    assert claim["serial_number"] == "MT101332090"
    assert claim["legacy_serial_number"] == "MMC7"
    assert claim["name"] == "CLAIM 7"
    assert claim["status"] == "Active"
    assert claim["claim_type"] == "Placer claim"
    assert claim["states"] == ["Montana"]
    assert claim["legal_description"][0]["section"] == "sec. 2"
    assert claim["mapping_quality"]["score"] == "1"


@pytest.mark.parametrize(
    "status, summary",
    [
        ("active", "1 active claim found"),
        ("closed", "1 closed claim found"),
        ("all", "1 claim (active and closed) found"),
    ],
)
async def test_singular_wording(blm, status, summary):
    layer = {"active": 1, "closed": 2, "all": 0}[status]
    blm.add(layer, [record(1)])
    result = await call(status=status)
    assert result["summary"].startswith(summary)
    assert "Showing" not in result["summary"]


async def test_plural_wording_for_all(blm):
    blm.add(0, [record(1), record(2, disp="Closed")])
    result = await call(status="all")
    assert result["summary"].startswith("2 claims (active and closed) found")


async def test_counts_cover_every_claim_not_just_those_shown(blm):
    records = (
        [record(i, qlty="1: Matched") for i in range(1, 21)]
        + [record(i, qlty="5; guess", type_nr="384201") for i in range(21, 31)]
        + [record(31, qlty=None, type_nr=None, disp=None)]
    )
    blm.add(1, records)
    counts = (await call())["counts"]
    assert counts["mapping_quality"] == {
        "exact": 20,
        "best_guess": 10,
        "not_recorded": 1,
    }
    assert counts["claim_types"] == {
        "Lode claim": 20,
        "Placer claim": 10,
        "Not recorded": 1,
    }
    assert counts["statuses"] == {"Active": 30, "Not recorded": 1}


async def test_counts_are_most_common_first(blm):
    blm.add(1, [record(1, disp="Filed")] + [record(i) for i in range(2, 5)])
    statuses = (await call())["counts"]["statuses"]
    assert list(statuses) == ["Active", "Filed"]


async def test_mapping_note_and_details_hint_always_given(blm):
    blm.add(1, [record(1)])
    result = await call()
    assert result["notes"] == [MAPPING_NOTE]
    assert result["hint"] == DETAILS_HINT


async def test_section_and_county_level_claims_are_counted_in_a_note(blm):
    blm.add(
        1,
        [
            record(1, qlty="9: section"),
            record(2, qlty="25: county"),
            record(3, qlty="2: Matched"),
            record(4, qlty="10; section"),
        ],
    )
    result = await call()
    assert result["counts"]["mapping_quality"] == {
        "section": 2,
        "county": 1,
        "exact": 1,
    }
    assert result["notes"] == [
        MAPPING_NOTE,
        "3 of the 4 claims are mapped only to a whole section or county, so "
        "they may not really be within the radius.",
    ]


# ── Service errors ───────────────────────────────────────────────────────────


async def test_failed_distance_query_returns_error(blm):
    blm.fail_on = "ids"
    _assert_service_error(await call())
    assert blm.fetch_calls() == []


async def test_one_failed_batch_returns_error_not_partial_list(blm):
    blm.add(1, [record(i) for i in range(1, 301)])
    blm.fail_on = 2
    _assert_service_error(await call())


@pytest.mark.parametrize(
    "failure", [{"error": {"code": 400, "message": "bad query"}}, [], "oops", None]
)
@pytest.mark.parametrize("fail_on", ["ids", 0])
async def test_bad_reply_inside_200_is_not_treated_as_no_claims(blm, failure, fail_on):
    blm.add(1, [record(1)])
    blm.fail_on = fail_on
    blm.failure = failure
    _assert_service_error(await call())


# ── Partial-reload check (D-036) ─────────────────────────────────────────────


async def test_no_claims_checks_layer_0_total(blm):
    result = await call(radius_miles=2)
    assert result["total_claims"] == 0
    ((layer, params),) = blm.total_calls()
    assert layer == 0
    assert params == {"where": "1=1", "returnCountOnly": "true", "f": "json"}


@pytest.mark.parametrize("total", [0, 1_168_412])
async def test_no_claims_on_incomplete_service_is_an_error(blm, total):
    blm.total = total
    result = await call()
    assert result == INCOMPLETE_ERROR
    assert "total_claims" not in result


async def test_total_at_minimum_is_a_normal_no_claims(blm, monkeypatch):
    monkeypatch.setattr(settings, "blm_min_total_records", 1000)
    blm.total = 1000
    assert (await call())["total_claims"] == 0
    blm.total = 999
    assert await call() == INCOMPLETE_ERROR


async def test_claims_found_skip_the_total_query(blm):
    blm.total = 0
    blm.add(1, [record(1)])
    assert (await call())["total_claims"] == 1
    blm.ids[1] = list(range(1, MAX_CLAIMS_DESCRIBED + 2))
    assert (await call())["total_claims"] == MAX_CLAIMS_DESCRIBED + 1
    assert blm.total_calls() == []


@pytest.mark.parametrize(
    "failure", [UpstreamServiceError("boom"), {"error": {"code": 500}}, None]
)
async def test_failed_total_query_returns_error_not_no_claims(blm, failure):
    blm.fail_on = "total"
    blm.failure = failure
    _assert_service_error(await call())


@pytest.mark.parametrize("total", [None, "4401824"])
async def test_total_without_a_number_returns_error(blm, total):
    blm.total = total
    _assert_service_error(await call())
