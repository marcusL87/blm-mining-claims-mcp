"""Find-near tool: list mining claims within a distance of a point.

Searches BLM's public mining claims map service for claims whose map shape is
within a radius of a latitude and longitude, and describes them in plain
terms. Design: docs/locate_tool_plan.md section 6 and decision D-035.

Service quirks this file handles (found in step B1, D-033 and D-035):
  - A distance query that returns records is slow (17-40 s), often returns
    fewer records than asked, and times out when sorted by serial. So the tool
    asks only for the IDs in the radius (about 1 s, complete even past the
    2,000-record limit), fetches the records by ID in batches, and sorts them
    itself.
  - The service reports query errors inside a normal 200 response.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from typing import Annotated, Any

from fastmcp import FastMCP

from mining_claims_mcp.claims import (
    INCOMPLETE_ERROR,
    TOTAL_COUNT_PARAMS,
    TOTAL_LAYER,
    build_claim,
    describe_claim_type,
    quality_level,
    service_looks_incomplete,
)
from mining_claims_mcp.models import ClaimStatus
from mining_claims_mcp.utils import UpstreamServiceError, fetch_json

LAYERS = {ClaimStatus.ACTIVE: 1, ClaimStatus.CLOSED: 2, ClaimStatus.ALL: 0}
DETAIL_FIELDS = "CSE_NR,LEG_CSE_NR,CSE_NAME,CSE_DISP,CSE_TYPE_NR,CSE_META,QLTY"
DEFAULT_RADIUS_MILES = 1.0
MAX_RADIUS_MILES = 10.0
MAX_CLAIMS_SHOWN = 25
# Above this many claims, only the total is returned (D-035). Near Marysville
# that happens only at 10 miles for closed or all claims.
MAX_CLAIMS_DESCRIBED = 2000
ID_BATCH_SIZE = 100
# Batches fetched at once; kept low because heavy use overloads the service.
MAX_PARALLEL_BATCHES = 4

# Rough box around the US, Alaska, Hawaii, and Puerto Rico.
MIN_LATITUDE, MAX_LATITUDE = 17.0, 72.0
MIN_LONGITUDE, MAX_LONGITUDE = -180.0, -64.0

SOURCE = "BLM mining claims map service (nlsdbpubgis.blm.gov), queried live"
COARSE_LEVELS = ("section", "county")
MAPPING_NOTE = (
    "A claim appears here when its map shape is within the radius. Claims "
    "mapped only to a whole section or county can appear near the point when "
    "the claim itself is not. Claims with no map shape never appear in this "
    "search."
)
NONE_NOTE = (
    "No claim has a map shape within the radius. Claims with no map shape "
    "never appear in this search, so this does not prove there are none."
)
DETAILS_HINT = (
    "For full details on a claim, use mining_claims_lookup_by_serial with its "
    "serial number."
)


def check_point(latitude: float, longitude: float) -> dict[str, Any] | None:
    """Return an error dict if the point is not a usable US location."""
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return {
            "error": "The latitude or longitude is not a number.",
            "hint": "Give decimal degrees, e.g. latitude 46.75, longitude -112.30.",
        }
    if (
        MIN_LATITUDE <= latitude <= MAX_LATITUDE
        and MIN_LONGITUDE <= longitude <= MAX_LONGITUDE
    ):
        return None
    hint = (
        "This tool covers the United States: latitude between "
        f"{MIN_LATITUDE:g} and {MAX_LATITUDE:g}, longitude between "
        f"{MIN_LONGITUDE:g} and {MAX_LONGITUDE:g}."
    )
    if MIN_LATITUDE <= latitude <= MAX_LATITUDE and 64 <= longitude <= 180:
        hint += " US longitudes are negative (west); check for a missing minus sign."
    elif MIN_LATITUDE <= longitude <= MAX_LATITUDE:
        hint += " Latitude and longitude may be swapped."
    return {"error": "That point is outside the United States.", "hint": hint}


def distance_params(
    latitude: float, longitude: float, radius_miles: float
) -> dict[str, Any]:
    return {
        "where": "1=1",
        "geometry": f"{longitude},{latitude}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4269",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_miles,
        "units": "esriSRUnit_StatuteMile",
        "returnIdsOnly": "true",
    }


async def query(layer: int, params: dict[str, Any]) -> dict[str, Any]:
    """Run one layer query, raising if the service reports an error."""
    payload = await fetch_json(f"{layer}/query", params={**params, "f": "json"})
    if not isinstance(payload, dict) or "error" in payload:
        raise UpstreamServiceError("The BLM service could not run the query.")
    return payload


async def fetch_records(layer: int, ids: list[int]) -> list[dict[str, Any]]:
    """Fetch the records for these IDs in batches, a few batches at a time."""
    limit = asyncio.Semaphore(MAX_PARALLEL_BATCHES)

    async def batch(chunk: list[int]) -> list[dict[str, Any]]:
        async with limit:
            payload = await query(
                layer,
                {
                    "objectIds": ",".join(str(i) for i in chunk),
                    "outFields": DETAIL_FIELDS,
                    "returnGeometry": "false",
                },
            )
        return [f.get("attributes", {}) for f in payload.get("features", [])]

    chunks = [ids[i : i + ID_BATCH_SIZE] for i in range(0, len(ids), ID_BATCH_SIZE)]
    batches = await asyncio.gather(*(batch(c) for c in chunks))
    return [record for records in batches for record in records]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts across every claim found, not just those shown."""
    levels = Counter(quality_level(r.get("QLTY"))[1] for r in records)
    types = Counter(
        describe_claim_type(r.get("CSE_TYPE_NR")) or "Not recorded" for r in records
    )
    statuses = Counter(r.get("CSE_DISP") or "Not recorded" for r in records)
    return {
        "mapping_quality": dict(levels.most_common()),
        "claim_types": dict(types.most_common()),
        "statuses": dict(statuses.most_common()),
    }


def describe_status(status: ClaimStatus, count: int) -> str:
    """'active claims', 'closed claim', 'claims (active and closed)'."""
    noun = "claim" if count == 1 else "claims"
    if status is ClaimStatus.ALL:
        return f"{noun} (active and closed)"
    return f"{status.value} {noun}"


def _service_error() -> dict[str, Any]:
    return {
        "error": "The BLM mining claims service did not respond or returned an error.",
        "hint": (
            "The service may be slow or down. Try again in a minute, or with a "
            "smaller radius."
        ),
    }


def register(mcp: FastMCP) -> None:
    """Register the claims-near-a-location tool with the MCP server."""

    @mcp.tool(
        name="mining_claims_find_near_location",
        annotations={
            "title": "Find Mining Claims Near a Location",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def mining_claims_find_near_location(
        latitude: Annotated[
            float, "Latitude in decimal degrees (NAD83 or WGS84), e.g. 46.75049."
        ],
        longitude: Annotated[
            float,
            "Longitude in decimal degrees, negative in the US, e.g. -112.30029.",
        ],
        radius_miles: Annotated[
            float,
            f"Search radius in miles, more than 0 and at most {MAX_RADIUS_MILES:g}. "
            f"Default {DEFAULT_RADIUS_MILES:g}.",
        ] = DEFAULT_RADIUS_MILES,
        status: Annotated[
            ClaimStatus,
            "active (default), closed, or all claims.",
        ] = ClaimStatus.ACTIVE,
    ) -> dict[str, Any]:
        """List mining claims within a distance of a point.

        Use the latitude and longitude from mining_claims_locate_place (or
        given by the user). Data source: BLM's public mining claims map
        service (nlsdbpubgis.blm.gov), queried live; layer 1 for active
        claims, 2 for closed, 0 for all. It reflects BLM's MLRS case records
        as mapped by BLM; BLM does not publish an update schedule.

        A claim is found when its map shape is within the radius. The map
        shape may not be the claim's true location: claims mapped only to a
        whole section or county can appear "near" when they are not, and
        claims with no map shape never appear. Pass `notes` on to the user.

        Returns a dict with `search` (the point, radius, and status
        searched), `total_claims`, a `summary` sentence, and, when there are
        at most 2,000 claims:
          - `claims`: up to 25, sorted by serial number, each with name,
            serial numbers, status, claim type, states, legal description
            parts, and mapping quality.
          - `counts`: mapping quality levels, claim types, and statuses
            across every claim found (not just those shown).
        Over 2,000 claims, only the total is returned with a `hint` to use a
        smaller radius or status. `notes` carries caveats to pass on; `hint`
        says what to do next. On failure, returns `error` and `hint`. If
        nothing is found while the service holds far fewer claims than usual
        (e.g. BLM is reloading), it returns an error instead of zero claims.
        """
        bad_point = check_point(latitude, longitude)
        if bad_point:
            return bad_point
        if not (math.isfinite(radius_miles) and 0 < radius_miles <= MAX_RADIUS_MILES):
            return {
                "error": "That radius is not allowed.",
                "hint": (
                    f"Use a radius more than 0 and at most {MAX_RADIUS_MILES:g} miles."
                ),
            }

        layer = LAYERS[status]
        search = {
            "latitude": round(latitude, 5),
            "longitude": round(longitude, 5),
            "radius_miles": radius_miles,
            "status": status.value,
        }
        base: dict[str, Any] = {"search": search, "source": SOURCE}
        where = f"within {radius_miles:g} miles of {search['latitude']}, " + (
            f"{search['longitude']}"
        )

        try:
            ids_payload = await query(
                layer, distance_params(latitude, longitude, radius_miles)
            )
        except UpstreamServiceError:
            return _service_error()
        ids = sorted(int(i) for i in ids_payload.get("objectIds") or [])
        total = len(ids)

        if total == 0:
            # D-036: an empty or reloading service also finds nothing.
            try:
                total_payload = await query(TOTAL_LAYER, TOTAL_COUNT_PARAMS)
                if service_looks_incomplete(total_payload):
                    return dict(INCOMPLETE_ERROR)
            except UpstreamServiceError:
                return _service_error()
            return {
                **base,
                "total_claims": 0,
                "summary": f"No {describe_status(status, 0)} found {where}.",
                "notes": [NONE_NOTE],
                "hint": (
                    "Try a larger radius or status all. A serial number search "
                    "(mining_claims_lookup_by_serial) finds claims with no map "
                    "shape."
                ),
            }

        if total > MAX_CLAIMS_DESCRIBED:
            return {
                **base,
                "total_claims": total,
                "summary": (
                    f"{total} {describe_status(status, total)} found {where}: "
                    "too many to list "
                    f"(the limit is {MAX_CLAIMS_DESCRIBED})."
                ),
                "notes": [MAPPING_NOTE],
                "hint": "Use a smaller radius, or search active claims only.",
            }

        try:
            records = await fetch_records(layer, ids)
        except UpstreamServiceError:
            return _service_error()
        records.sort(key=lambda r: r.get("CSE_NR") or "")
        counts = summarize(records)
        shown = [build_claim(r) for r in records[:MAX_CLAIMS_SHOWN]]

        summary = f"{total} {describe_status(status, total)} found {where}."
        if total > len(shown):
            summary += f" Showing the first {len(shown)}, sorted by serial number."
        coarse = sum(counts["mapping_quality"].get(level, 0) for level in COARSE_LEVELS)
        notes = [MAPPING_NOTE]
        if coarse:
            notes.append(
                f"{coarse} of the {len(records)} claims are mapped only to a whole "
                "section or county, so they may not really be within the radius."
            )
        return {
            **base,
            "total_claims": total,
            "summary": summary,
            "claims": shown,
            "counts": counts,
            "notes": notes,
            "hint": DETAILS_HINT,
        }
