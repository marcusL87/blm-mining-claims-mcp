"""Locate tool: turn a place name into coordinates.

Searches the USGS Geographic Names service (GNIS, on The National Map) and
returns matching places with state, county, feature class, and NAD83
coordinates, so the agent can tell same-named places apart. Design:
docs/locate_tool_plan.md section 5 and decision D-028.

Service quirks this file handles (found in step A1, D-028):
  - The same place is listed once per county it touches (same gaz_id), so
    records are merged into one place with a list of counties.
  - Geometry is multipoint; the first point is used (for streams, the mouth).
  - Historical names end in " (historical)", so starts-with is the second step.
  - The service reports query errors inside a normal 200 response.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any

from fastmcp import FastMCP

from mining_claims_mcp.models import OFFICE_STATES, FeatureType, PlaceState
from mining_claims_mcp.utils import (
    UpstreamServiceError,
    fetch_geonames_json,
    sql_quote,
)

OUT_FIELDS = "gaz_id,gaz_name,gaz_featureclass,state_alpha,county_name"
# Records requested per layer per step. A layer at the cap sets more_available.
LAYER_RECORD_CAP = 100
MAX_CANDIDATES = 10
MAX_NAME_LENGTH = 100
MAX_COUNTY_LENGTH = 50

LAYER_NAMES = {
    1: "Incorporated Places",
    2: "Census Places",
    3: "Populated Places",
    5: "Landforms",
    6: "Streams (mouth)",
    7: "Other Hydrographic Features",
    12: "Historical Cultural-Political Points",
    13: "Historical Hydrographic Points",
    14: "Historical Physical Points",
}
STREAM_LAYER = 6
HISTORICAL_HYDRO_LAYER = 13
STREAM_CLASSES = ("Stream", "Canal")

# Layers searched for each feature type, with the gaz_featureclass values to
# keep (None = every class in that layer). Classes checked Oct 18 (D-031).
FEATURE_LAYERS: dict[FeatureType, dict[int, tuple[str, ...] | None]] = {
    FeatureType.ANY: {layer: None for layer in LAYER_NAMES},
    FeatureType.POPULATED_PLACE: {
        1: None,
        2: None,
        3: None,
        12: ("Populated Place", "Census"),
    },
    FeatureType.SUMMIT: {5: ("Summit",), 14: ("Summit",)},
    FeatureType.RIDGE: {5: ("Ridge",), 14: ("Ridge",)},
    FeatureType.VALLEY: {5: ("Valley",), 14: ("Valley",)},
    FeatureType.BASIN: {5: ("Basin",), 14: ("Basin",)},
    FeatureType.FLAT: {5: ("Flat",), 14: ("Flat",)},
    FeatureType.GAP: {5: ("Gap",), 14: ("Gap",)},
    FeatureType.STREAM: {6: STREAM_CLASSES, 13: STREAM_CLASSES},
    FeatureType.SPRING: {7: ("Spring",), 13: ("Spring",)},
    FeatureType.LAKE: {7: ("Lake", "Reservoir"), 13: ("Lake", "Reservoir")},
}

# Matching steps, tried in order until one finds something.
MATCH_STEPS = ("exact", "starts_with", "contains")

# Letters (any alphabet), digits, spaces, and ' . # -
ALLOWED_TEXT = re.compile(r"(?:[^\W_]|[ '.#\-])+")

SOURCE = "USGS Geographic Names Information System (GNIS), The National Map"
STREAM_NOTE = (
    "Stream and canal points are at the mouth (downstream end), not along the "
    "whole stream."
)
MULTIPLE_NOTE = (
    "Several places match. Ask the user which one they mean, giving the state "
    "and county of each, before searching for claims."
)
COUNTY_NOTE = (
    "A place's counties can include counties in a neighboring state; GNIS "
    "does not say which. Don't assume every county is in the place's state."
)
MORE_NOTE = (
    "More places match than were returned. Ask for the state, county, or kind "
    "of place to narrow the search."
)


def clean_text(raw: str) -> str:
    """Straighten curly apostrophes, trim, and collapse runs of whitespace."""
    return " ".join(raw.replace("’", "'").replace("‘", "'").split())


def clean_county(raw: str) -> str:
    """Clean like a name and drop a trailing word 'County' (GNIS omits it)."""
    return re.sub(r"\s+county$", "", clean_text(raw), flags=re.IGNORECASE)


def state_codes(state: PlaceState) -> tuple[str, ...] | None:
    """The state_alpha values to filter on, or None for nationwide."""
    if state is PlaceState.ALL:
        return None
    if state is PlaceState.OFFICE:
        return OFFICE_STATES
    return (state.value,)


def _in_list(values: tuple[str, ...]) -> str:
    return "(" + ",".join(sql_quote(v) for v in values) + ")"


def build_where(
    name: str,
    step: str,
    states: tuple[str, ...] | None,
    county: str | None,
    classes: tuple[str, ...] | None,
) -> str:
    """Build one layer's where clause. `name` and `county` must be uppercase."""
    if step == "exact":
        clauses = [f"UPPER(gaz_name) = {sql_quote(name)}"]
    elif step == "starts_with":
        clauses = [f"UPPER(gaz_name) LIKE {sql_quote(name + '%')}"]
    else:
        clauses = [f"UPPER(gaz_name) LIKE {sql_quote('%' + name + '%')}"]
    if states:
        clauses.append(f"state_alpha IN {_in_list(states)}")
    if county:
        clauses.append(f"UPPER(county_name) = {sql_quote(county)}")
    if classes:
        clauses.append(f"gaz_featureclass IN {_in_list(classes)}")
    clauses.append("isunknowncoords <> 1")
    return " AND ".join(clauses)


async def query_layer(layer: int, where: str) -> dict[str, Any]:
    """Run one GNIS layer query, raising if the service reports an error."""
    payload = await fetch_geonames_json(
        f"{layer}/query",
        params={
            "where": where,
            "outFields": OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": "4269",
            "returnZ": "false",
            "orderByFields": "gaz_name,state_alpha",
            "resultRecordCount": LAYER_RECORD_CAP,
            "f": "json",
        },
    )
    if not isinstance(payload, dict) or "error" in payload:
        raise UpstreamServiceError("The GNIS service could not run the query.")
    return payload


def _first_point(geometry: Any) -> tuple[float, float] | None:
    """(longitude, latitude) of the first point of a point or multipoint."""
    if not isinstance(geometry, dict):
        return None
    points = geometry.get("points")
    if isinstance(points, list) and points:
        point = points[0]
        if isinstance(point, list) and len(point) >= 2:
            return float(point[0]), float(point[1])
        return None
    if "x" in geometry and "y" in geometry:
        return float(geometry["x"]), float(geometry["y"])
    return None


def merge_places(
    results: list[tuple[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Merge records sharing a gaz_id into one place each, listing all counties."""
    places: dict[Any, dict[str, Any]] = {}
    for layer, payload in results:
        for feature in payload.get("features", []):
            attrs = feature.get("attributes", {})
            feature_id = attrs.get("gaz_id")
            point = _first_point(feature.get("geometry"))
            if feature_id is None or point is None:
                continue
            place = places.get(feature_id)
            if place is None:
                place = places[feature_id] = {
                    "feature_id": feature_id,
                    "name": attrs.get("gaz_name"),
                    "feature_class": attrs.get("gaz_featureclass"),
                    "state": attrs.get("state_alpha"),
                    "counties": [],
                    "latitude": round(point[1], 5),
                    "longitude": round(point[0], 5),
                    "source_layer": LAYER_NAMES.get(layer, str(layer)),
                    "_layer": layer,
                }
            county = attrs.get("county_name")
            if county and county != "Unknown" and county not in place["counties"]:
                place["counties"].append(county)
    return list(places.values())


def _sort_key(place: dict[str, Any]) -> tuple[Any, ...]:
    return (
        place["state"] not in OFFICE_STATES,
        place["state"] or "",
        place["name"] or "",
        place["counties"][0] if place["counties"] else "",
        str(place["feature_id"]),
    )


def _is_stream(place: dict[str, Any]) -> bool:
    return place["_layer"] in (STREAM_LAYER, HISTORICAL_HYDRO_LAYER) and (
        place["feature_class"] in STREAM_CLASSES
    )


def _area_label(state: PlaceState) -> str:
    if state is PlaceState.ALL:
        return "nationwide"
    if state is PlaceState.OFFICE:
        return "in " + ", ".join(OFFICE_STATES)
    return f"in {state.value}"


def _service_error() -> dict[str, Any]:
    return {
        "error": (
            "The USGS place-names service (GNIS) did not respond or returned an error."
        ),
        "hint": (
            "Try again in a minute. If it keeps failing, ask the user for the "
            "location as latitude and longitude instead."
        ),
    }


def register(mcp: FastMCP) -> None:
    """Register the place-name locate tool with the MCP server."""

    @mcp.tool(
        name="mining_claims_locate_place",
        annotations={
            "title": "Locate a Place by Name",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def mining_claims_locate_place(
        name: Annotated[
            str,
            "Place name as the user said it, e.g. 'King Mountain', 'Marysville', "
            "'Ophir Creek'. Case and extra spaces are ignored.",
        ],
        state: Annotated[
            PlaceState,
            "Two-letter state code to search (MT here means Montana only). "
            "Default OFFICE = MT, SD, and ND, the Montana/Dakotas office states. "
            "ALL searches nationwide.",
        ] = PlaceState.OFFICE,
        county: Annotated[
            str | None,
            "Optional county name to narrow the search, e.g. 'Lewis and Clark'.",
        ] = None,
        feature_type: Annotated[
            FeatureType,
            "Kind of place, if known: summit (peaks and mountains), ridge, "
            "valley (includes gulches), basin, flat, gap, stream (creeks, "
            "rivers), spring, lake (includes reservoirs), populated_place "
            "(towns, settlements). Default any.",
        ] = FeatureType.ANY,
    ) -> dict[str, Any]:
        """Find a named place (peak, gulch, creek, town, etc.) and its coordinates.

        Data source: USGS Geographic Names Information System (GNIS), on The
        National Map, queried live. It holds official federal names of natural
        features, populated places, and historical places (whose names end in
        "(historical)").

        Not in this service:
          - Mines. GNIS here has no mine features, so never tell the user a
            mine doesn't exist because this tool found nothing. Ask for a
            nearby landmark, coordinates, or a claim serial number instead.
          - Roads and street addresses.

        Matching, within the chosen state(s), county, and feature type:
        exact name first; if none, names starting with the text; if none,
        names containing it. A place that crosses county lines is returned
        once, with all its counties.

        Returns a dict with `status` ("single_match", "multiple_matches", or
        "no_match"), `match_type` ("exact", "starts_with", "contains"), a
        `summary` sentence, `total_matches`, and `candidates` (up to 10), each
        with feature_id (GNIS ID), name, feature_class, state, counties,
        latitude, longitude (NAD83 decimal degrees), and source_layer.
        `notes` carries caveats to pass on. Stream points are the stream's
        mouth, not the whole stream. `state` is the place's main state;
        `counties` can include counties in a neighboring state (GNIS does not
        say which), so don't attach the state to every county.

        With multiple_matches, ask the user which place they mean (give state
        and county) before searching for claims. `likely_match` (feature_id)
        appears only on a nationwide search when exactly one candidate is in
        the office states. With no_match, follow `hint`. Use the chosen
        place's latitude and longitude to search for mining claims near it.
        On failure, returns `error` and `hint`.
        """
        cleaned = clean_text(name)
        if not cleaned:
            return {
                "error": "No place name was given.",
                "hint": "Provide a place name, e.g. King Mountain.",
            }
        if len(cleaned) > MAX_NAME_LENGTH or not ALLOWED_TEXT.fullmatch(cleaned):
            return {
                "error": "That is not a valid place name.",
                "hint": (
                    "Use letters, digits, spaces, apostrophes, periods, hyphens, "
                    f"and #, up to {MAX_NAME_LENGTH} characters."
                ),
            }
        county_clean = clean_county(county) if county else ""
        if county_clean and (
            len(county_clean) > MAX_COUNTY_LENGTH
            or not ALLOWED_TEXT.fullmatch(county_clean)
        ):
            return {
                "error": "That is not a valid county name.",
                "hint": "Give the county name only, e.g. Lewis and Clark.",
            }

        upper_name = cleaned.upper()
        upper_county = county_clean.upper() or None
        states = state_codes(state)
        layers = FEATURE_LAYERS[feature_type]
        area = _area_label(state)
        base: dict[str, Any] = {
            "search_input": name,
            "cleaned_name": upper_name,
            "state": state.value,
            "source": SOURCE,
        }
        if upper_county:
            base["county"] = county_clean
        if feature_type is not FeatureType.ANY:
            base["feature_type"] = feature_type.value

        places: list[dict[str, Any]] = []
        more_available = False
        step = MATCH_STEPS[0]
        for step in MATCH_STEPS:
            try:
                payloads = await asyncio.gather(
                    *(
                        query_layer(
                            layer,
                            build_where(
                                upper_name, step, states, upper_county, classes
                            ),
                        )
                        for layer, classes in layers.items()
                    )
                )
            except UpstreamServiceError:
                return _service_error()
            places = merge_places(list(zip(layers, payloads)))
            if places:
                more_available = any(
                    p.get("exceededTransferLimit")
                    or len(p.get("features", [])) >= LAYER_RECORD_CAP
                    for p in payloads
                )
                break

        if not places:
            hint = (
                "Check the spelling. GNIS spells out words such as Mount, "
                "Saint, and Fort. Mines, roads, and street addresses are not in "
                "GNIS; ask for a nearby landmark or coordinates instead."
            )
            if state is not PlaceState.ALL:
                hint += " If the place may be in another state, search with state ALL."
            return {
                **base,
                "status": "no_match",
                "summary": f"No place named {upper_name} found {area}.",
                "total_matches": 0,
                "candidates": [],
                "hint": hint,
            }

        places.sort(key=_sort_key)
        shown = places[:MAX_CANDIDATES]
        total = len(places)
        how = {
            "exact": "exact name",
            "starts_with": "no exact match; names starting with the text",
            "contains": "no exact or starts-with match; names containing the text",
        }[step]

        notes: list[str] = []
        if total == 1:
            status = "single_match"
            summary = f"One place matches {upper_name} {area} ({how})."
        else:
            status = "multiple_matches"
            prefix = "At least " if more_available else ""
            summary = f"{prefix}{total} places match {upper_name} {area} ({how})."
            if total > len(shown):
                summary += f" Showing the first {len(shown)}."
            notes.append(MULTIPLE_NOTE)
        if more_available:
            notes.append(MORE_NOTE)
        if any(_is_stream(p) for p in shown):
            notes.append(STREAM_NOTE)
        # D-032: GNIS gives no state per county; border places list both sides.
        if any(len(p["counties"]) > 1 for p in shown):
            notes.append(COUNTY_NOTE)

        result: dict[str, Any] = {
            **base,
            "status": status,
            "match_type": step,
            "summary": summary,
            "total_matches": total,
            "more_available": more_available,
            "candidates": [
                {k: v for k, v in p.items() if not k.startswith("_")} for p in shown
            ],
        }
        if (
            status == "multiple_matches"
            and state is PlaceState.ALL
            and not (more_available)
        ):
            in_office = [p for p in places if p["state"] in OFFICE_STATES]
            if len(in_office) == 1:
                result["likely_match"] = in_office[0]["feature_id"]
        if notes:
            result["notes"] = notes
        return result
