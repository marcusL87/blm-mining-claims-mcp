"""Tests for the locate tool, mining_claims_locate_place (tools/locate_place.py).

No test calls the live GNIS service. `fetch_geonames_json` is replaced by a
stand-in that records each query and returns a made-up ArcGIS-style reply, and
the tool is called through FastMCP's in-memory client, as a real MCP client
would.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mining_claims_mcp.tools import locate_place
from mining_claims_mcp.tools.locate_place import (
    COUNTY_NOTE,
    LAYER_NAMES,
    LAYER_RECORD_CAP,
    MAX_CANDIDATES,
    MORE_NOTE,
    MULTIPLE_NOTE,
    STREAM_NOTE,
    build_where,
    clean_county,
    clean_text,
)
from mining_claims_mcp.utils import UpstreamServiceError

TOOL = "mining_claims_locate_place"
ALL_LAYERS = sorted(LAYER_NAMES)


def feature(
    gaz_id: int,
    name: str = "King Mountain",
    feature_class: str = "Summit",
    state: str = "MT",
    county: str = "Granite",
    geometry: Any = None,
) -> dict[str, Any]:
    return {
        "attributes": {
            "gaz_id": gaz_id,
            "gaz_name": name,
            "gaz_featureclass": feature_class,
            "state_alpha": state,
            "county_name": county,
        },
        "geometry": (
            geometry
            if geometry is not None
            else {"points": [[-113.181312, 46.734561], [-113.0, 46.0]]}
        ),
    }


def step_of(where: str) -> str:
    if "UPPER(gaz_name) = " in where:
        return "exact"
    if "UPPER(gaz_name) LIKE '%" in where:
        return "contains"
    return "starts_with"


class FakeGnis:
    """Stand-in for GNIS: records queries, returns set replies per layer/step."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []
        # (layer, step) -> list of features; anything unset returns no features.
        self.replies: dict[tuple[int, str], list[dict[str, Any]]] = {}
        # (layer, step) -> extra top-level keys, e.g. exceededTransferLimit.
        self.extra: dict[tuple[int, str], dict[str, Any]] = {}
        # Set to a layer number to make that layer's queries fail; set
        # fail_step too to fail only that step.
        self.fail_layer: int | None = None
        self.fail_step: str | None = None
        self.failure: Any = UpstreamServiceError("boom")

    async def fetch_geonames_json(
        self, path: str, params: dict[str, Any], **_: Any
    ) -> Any:
        m = re.fullmatch(r"(\d+)/query", path)
        assert m, path
        layer = int(m.group(1))
        self.calls.append((layer, params))
        step = step_of(params["where"])
        if layer == self.fail_layer and self.fail_step in (None, step):
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure
        key = (layer, step)
        return {"features": self.replies.get(key, []), **self.extra.get(key, {})}

    def layers(self, step: str | None = None) -> list[int]:
        return sorted(
            layer
            for layer, p in self.calls
            if step is None or step_of(p["where"]) == step
        )

    def wheres(self, step: str | None = None) -> list[str]:
        return [
            p["where"]
            for _, p in self.calls
            if step is None or step_of(p["where"]) == step
        ]


@pytest.fixture
def gnis(monkeypatch) -> FakeGnis:
    fake = FakeGnis()
    monkeypatch.setattr(locate_place, "fetch_geonames_json", fake.fetch_geonames_json)
    return fake


async def call(**args: Any) -> dict[str, Any]:
    mcp = FastMCP("test")
    locate_place.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, args)
    return result.structured_content


# ── Input cleaning and validation ────────────────────────────────────────────


def test_clean_text_trims_collapses_and_straightens_apostrophes():
    assert clean_text("  King\t  Mountain ") == "King Mountain"
    assert clean_text("O’Brien ‘Creek") == "O'Brien 'Creek"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Lewis and Clark County", "Lewis and Clark"),
        ("  granite   county ", "granite"),
        ("Granite", "Granite"),
        ("County Line", "County Line"),
    ],
)
def test_clean_county_drops_trailing_county(raw, expected):
    assert clean_county(raw) == expected


async def test_tool_searches_with_cleaned_uppercase_name(gnis):
    result = await call(name="  king’s   mountain ")
    assert result["cleaned_name"] == "KING'S MOUNTAIN"
    assert all(
        w.startswith("UPPER(gaz_name) = 'KING''S MOUNTAIN' AND ")
        for w in gnis.wheres("exact")
    )


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
async def test_empty_name_is_rejected_without_query(gnis, bad):
    result = await call(name=bad)
    assert result["error"] == "No place name was given."
    assert "hint" in result
    assert gnis.calls == []


@pytest.mark.parametrize(
    "bad", ["x'; drop table", "King_Mountain", "50%", "a<b>", "King (historical)"]
)
async def test_invalid_name_is_rejected_without_query_or_echo(gnis, bad):
    result = await call(name=bad)
    assert result["error"] == "That is not a valid place name."
    assert bad not in str(result)
    assert gnis.calls == []


@pytest.mark.parametrize(
    "good", ["Mount St. Helens", "O'Brien Creek", "Mine #3", "Sixty-Six", "Peña Peak"]
)
async def test_allowed_characters_are_accepted(gnis, good):
    result = await call(name=good)
    assert "error" not in result
    assert gnis.calls


async def test_name_length_limit(gnis):
    assert "error" not in await call(name="A" * 100)
    calls = len(gnis.calls)
    result = await call(name="A" * 101)
    assert result["error"] == "That is not a valid place name."
    assert len(gnis.calls) == calls


@pytest.mark.parametrize("bad", ["Granite;", "G" * 51, "Lewis_Clark"])
async def test_invalid_county_is_rejected_without_query_or_echo(gnis, bad):
    result = await call(name="King Mountain", county=bad)
    assert result["error"] == "That is not a valid county name."
    assert bad not in str(result)
    assert gnis.calls == []


@pytest.mark.parametrize(
    "args", [{"state": "XX"}, {"state": "ES"}, {"feature_type": "mine"}]
)
async def test_unknown_state_or_feature_type_is_rejected(gnis, args):
    with pytest.raises(ToolError):
        await call(name="King Mountain", **args)
    assert gnis.calls == []


# ── Query text and settings ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "step, name_clause",
    [
        ("exact", "UPPER(gaz_name) = 'KING MOUNTAIN'"),
        ("starts_with", "UPPER(gaz_name) LIKE 'KING MOUNTAIN%'"),
        ("contains", "UPPER(gaz_name) LIKE '%KING MOUNTAIN%'"),
    ],
)
def test_build_where_name_clause_per_step(step, name_clause):
    assert build_where("KING MOUNTAIN", step, None, None, None) == (
        f"{name_clause} AND isunknowncoords <> 1"
    )


def test_build_where_with_all_filters_escapes_quotes():
    where = build_where("O'BRIEN", "exact", ("MT", "SD"), "O'NEILL", ("Summit",))
    assert where == (
        "UPPER(gaz_name) = 'O''BRIEN' AND state_alpha IN ('MT','SD') "
        "AND UPPER(county_name) = 'O''NEILL' AND gaz_featureclass IN ('Summit') "
        "AND isunknowncoords <> 1"
    )


async def test_default_state_is_office_states(gnis):
    result = await call(name="King Mountain")
    assert result["state"] == "OFFICE"
    assert all("state_alpha IN ('MT','SD','ND')" in w for w in gnis.wheres())


async def test_single_state_filter(gnis):
    await call(name="King Mountain", state="SD")
    assert all("state_alpha IN ('SD')" in w for w in gnis.wheres())


async def test_state_all_has_no_state_filter(gnis):
    result = await call(name="King Mountain", state="ALL")
    assert all("state_alpha" not in w for w in gnis.wheres())
    assert "nationwide" in result["summary"]


async def test_county_filter_is_uppercase_without_county_word(gnis):
    result = await call(name="Ophir Creek", county="lewis and clark county")
    assert result["county"] == "lewis and clark"
    assert all("UPPER(county_name) = 'LEWIS AND CLARK'" in w for w in gnis.wheres())


async def test_no_county_means_no_county_filter(gnis):
    result = await call(name="King Mountain")
    assert "county" not in result
    assert all("county_name" not in w for w in gnis.wheres())


async def test_every_query_sends_fixed_settings(gnis):
    await call(name="King Mountain")
    for _, params in gnis.calls:
        assert params["where"].endswith(" AND isunknowncoords <> 1")
        assert params["outSR"] == "4269"
        assert params["returnGeometry"] == "true"
        assert params["returnZ"] == "false"
        assert params["resultRecordCount"] == LAYER_RECORD_CAP
        assert params["orderByFields"] == "gaz_name,state_alpha"
        assert params["outFields"] == (
            "gaz_id,gaz_name,gaz_featureclass,state_alpha,county_name"
        )
        assert params["f"] == "json"


async def test_any_feature_type_searches_all_layers_without_class_filter(gnis):
    result = await call(name="King Mountain")
    assert "feature_type" not in result
    assert gnis.layers("exact") == ALL_LAYERS
    assert all("gaz_featureclass" not in w for w in gnis.wheres())


@pytest.mark.parametrize(
    "feature_type, layers, class_clause",
    [
        ("summit", [5, 14], "gaz_featureclass IN ('Summit')"),
        ("valley", [5, 14], "gaz_featureclass IN ('Valley')"),
        ("stream", [6, 13], "gaz_featureclass IN ('Stream','Canal')"),
        ("spring", [7, 13], "gaz_featureclass IN ('Spring')"),
        ("lake", [7, 13], "gaz_featureclass IN ('Lake','Reservoir')"),
    ],
)
async def test_feature_type_selects_layers_and_classes(
    gnis, feature_type, layers, class_clause
):
    result = await call(name="King", feature_type=feature_type)
    assert result["feature_type"] == feature_type
    assert gnis.layers("exact") == layers
    assert all(class_clause in w for w in gnis.wheres())


async def test_populated_place_filters_class_only_on_historical_layer(gnis):
    await call(name="Marysville", feature_type="populated_place")
    assert gnis.layers("exact") == [1, 2, 3, 12]
    for layer, params in gnis.calls:
        has_filter = (
            "gaz_featureclass IN ('Populated Place','Census')" in (params["where"])
        )
        assert has_filter == (layer == 12)


# ── Matching order ───────────────────────────────────────────────────────────


async def test_exact_match_stops_after_exact_step(gnis):
    gnis.replies[(5, "exact")] = [feature(773047)]
    result = await call(name="King Mountain")
    assert result["match_type"] == "exact"
    assert gnis.layers() == ALL_LAYERS
    assert "(exact name)" in result["summary"]


async def test_starts_with_used_when_no_exact_match(gnis):
    gnis.replies[(12, "starts_with")] = [
        feature(1, "Marysville (historical)", "Populated Place")
    ]
    result = await call(name="Marysville")
    assert result["match_type"] == "starts_with"
    assert gnis.layers("exact") == ALL_LAYERS
    assert gnis.layers("starts_with") == ALL_LAYERS
    assert gnis.layers("contains") == []
    assert "starting with" in result["summary"]


async def test_contains_used_when_no_exact_or_starts_with_match(gnis):
    gnis.replies[(5, "contains")] = [feature(2, "Little King Mountain")]
    result = await call(name="King Mountain")
    assert result["match_type"] == "contains"
    assert len(gnis.calls) == 3 * len(ALL_LAYERS)
    assert "containing" in result["summary"]


async def test_no_match_tries_all_steps_and_suggests_state_all(gnis):
    result = await call(name="Nowhere Peak")
    assert len(gnis.calls) == 3 * len(ALL_LAYERS)
    assert result["status"] == "no_match"
    assert result["total_matches"] == 0
    assert result["candidates"] == []
    assert "match_type" not in result
    assert "Mines" in result["hint"]
    assert "Mount, Saint, and Fort" in result["hint"]
    assert "state ALL" in result["hint"]


async def test_no_match_nationwide_omits_state_all_hint(gnis):
    result = await call(name="Nowhere Peak", state="ALL")
    assert result["status"] == "no_match"
    assert "state ALL" not in result["hint"]


# ── Results ──────────────────────────────────────────────────────────────────


async def test_single_match_shape(gnis):
    gnis.replies[(5, "exact")] = [feature(773047)]
    result = await call(name="King Mountain")
    assert result["status"] == "single_match"
    assert result["total_matches"] == 1
    assert result["more_available"] is False
    assert "notes" not in result
    assert result["source"].startswith("USGS Geographic Names")
    assert result["candidates"] == [
        {
            "feature_id": 773047,
            "name": "King Mountain",
            "feature_class": "Summit",
            "state": "MT",
            "counties": ["Granite"],
            "latitude": 46.73456,
            "longitude": -113.18131,
            "source_layer": "Landforms",
        }
    ]


async def test_same_id_is_merged_with_all_counties(gnis):
    gnis.replies[(5, "exact")] = [
        feature(773047, county="Granite"),
        feature(773047, county="Powell"),
        feature(773047, county="Granite"),
        feature(773047, county="Unknown"),
    ]
    result = await call(name="King Mountain")
    assert result["status"] == "single_match"
    assert result["candidates"][0]["counties"] == ["Granite", "Powell"]


async def test_point_geometry_and_missing_geometry(gnis):
    gnis.replies[(7, "exact")] = [
        feature(1, "Hot Spring", "Spring", geometry={"x": -112.5, "y": 46.25}),
        feature(2, "Hot Spring", "Spring", geometry={"points": []}),
        {"attributes": {"gaz_id": 3, "gaz_name": "Hot Spring"}},
    ]
    result = await call(name="Hot Spring")
    assert result["status"] == "single_match"
    candidate = result["candidates"][0]
    assert (candidate["latitude"], candidate["longitude"]) == (46.25, -112.5)
    assert candidate["source_layer"] == "Other Hydrographic Features"


async def test_multiple_matches_sorted_office_states_first(gnis):
    gnis.replies[(5, "exact")] = [
        feature(10, state="AK", county="Nome"),
        feature(11, state="MT", county="Rosebud"),
        feature(12, state="ID", county="Shoshone"),
        feature(13, state="MT", county="Lincoln"),
    ]
    result = await call(name="King Mountain", state="ALL")
    assert result["status"] == "multiple_matches"
    assert result["total_matches"] == 4
    assert [c["feature_id"] for c in result["candidates"]] == [13, 11, 10, 12]
    assert MULTIPLE_NOTE in result["notes"]
    assert result["summary"].startswith("4 places match KING MOUNTAIN nationwide")


async def test_more_than_ten_places_shows_first_ten(gnis):
    gnis.replies[(5, "exact")] = [
        feature(100 + n, county=f"County {n:02d}") for n in range(12)
    ]
    result = await call(name="King Mountain")
    assert result["total_matches"] == 12
    assert len(result["candidates"]) == MAX_CANDIDATES
    assert f"Showing the first {MAX_CANDIDATES}." in result["summary"]


async def test_layer_at_cap_sets_more_available(gnis):
    gnis.replies[(5, "exact")] = [feature(1), feature(2)]
    gnis.extra[(5, "exact")] = {"exceededTransferLimit": True}
    result = await call(name="King Mountain")
    assert result["more_available"] is True
    assert result["summary"].startswith("At least 2 places")
    assert MORE_NOTE in result["notes"]


async def test_full_page_without_flag_also_sets_more_available(gnis):
    gnis.replies[(5, "exact")] = [feature(n) for n in range(LAYER_RECORD_CAP)]
    result = await call(name="King Mountain")
    assert result["more_available"] is True


async def test_likely_match_when_one_office_place_in_nationwide_search(gnis):
    gnis.replies[(5, "exact")] = [
        feature(1, state="MT"),
        feature(2, state="AK"),
        feature(3, state="ID"),
    ]
    result = await call(name="King Mountain", state="ALL")
    assert result["likely_match"] == 1


@pytest.mark.parametrize(
    "state, states",
    [("ALL", ["MT", "SD", "AK"]), ("OFFICE", ["MT", "AK"]), ("ALL", ["AK", "ID"])],
)
async def test_no_likely_match_otherwise(gnis, state, states):
    gnis.replies[(5, "exact")] = [feature(n, state=s) for n, s in enumerate(states)]
    result = await call(name="King Mountain", state=state)
    assert "likely_match" not in result


async def test_no_likely_match_when_more_available(gnis):
    gnis.replies[(5, "exact")] = [feature(1, state="MT"), feature(2, state="AK")]
    gnis.extra[(5, "exact")] = {"exceededTransferLimit": True}
    result = await call(name="King Mountain", state="ALL")
    assert "likely_match" not in result


@pytest.mark.parametrize("layer", [6, 13])
async def test_stream_note_for_stream_points(gnis, layer):
    gnis.replies[(layer, "exact")] = [feature(1, "Ophir Creek", "Stream")]
    result = await call(name="Ophir Creek")
    assert result["notes"] == [STREAM_NOTE]
    assert result["candidates"][0]["source_layer"] == LAYER_NAMES[layer]


async def test_county_note_when_a_place_has_several_counties(gnis):
    gnis.replies[(6, "exact")] = [
        feature(774895, "O'Brien Creek", "Stream", county="McKenzie"),
        feature(774895, "O'Brien Creek", "Stream", county="Richland"),
    ]
    result = await call(name="O'Brien Creek")
    assert result["candidates"][0]["counties"] == ["McKenzie", "Richland"]
    assert result["notes"] == [STREAM_NOTE, COUNTY_NOTE]


async def test_no_county_note_when_each_place_has_one_county(gnis):
    gnis.replies[(5, "exact")] = [
        feature(1, county="Lincoln"),
        feature(2, county="Rosebud"),
    ]
    result = await call(name="King Mountain")
    assert COUNTY_NOTE not in result["notes"]


async def test_no_stream_note_for_lake_on_historical_hydro_layer(gnis):
    gnis.replies[(13, "exact")] = [feature(1, "Ophir Lake", "Lake")]
    result = await call(name="Ophir Lake")
    assert "notes" not in result


# ── Service errors ───────────────────────────────────────────────────────────


def _assert_service_error(result: dict[str, Any]) -> None:
    assert result["error"].startswith("The USGS place-names service")
    assert "latitude and longitude" in result["hint"]
    assert "status" not in result
    assert "candidates" not in result


async def test_one_failed_layer_returns_error(gnis):
    gnis.replies[(5, "exact")] = [feature(1)]
    gnis.fail_layer = 6
    _assert_service_error(await call(name="King Mountain"))


async def test_failure_in_later_step_returns_error_not_no_match(gnis):
    gnis.fail_layer = 5
    gnis.fail_step = "contains"
    _assert_service_error(await call(name="Nowhere Peak"))
    assert len(gnis.calls) == 3 * len(ALL_LAYERS)


@pytest.mark.parametrize(
    "failure", [{"error": {"code": 400, "message": "bad where"}}, [], "oops", None]
)
async def test_bad_reply_inside_200_is_not_treated_as_no_match(gnis, failure):
    gnis.fail_layer = 5
    gnis.failure = failure
    _assert_service_error(await call(name="King Mountain"))


async def test_error_does_not_echo_input(gnis):
    gnis.fail_layer = 1
    result = await call(name="Secret Gulch")
    assert "SECRET" not in str(result).upper()
