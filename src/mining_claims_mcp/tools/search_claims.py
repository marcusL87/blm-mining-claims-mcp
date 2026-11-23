"""Search tool: find Montana/Dakotas office claims by name or township/range.

Searches BLM's public mining claims map service for claims whose name
contains the given text, whose legal description lists a township and range
(optionally a section), or both. Design: docs/search_tool_plan.md and
decision D-042.

Service facts this file relies on (sizing, D-042):
  - CSE_NAME and CSE_META have no index, so "contains" searches are slow
    nationwide (11-19 s) but fast within the office filter CSE_NR LIKE 'MT%'
    (count 0.7-0.9 s, sorted list of 25 in 0.2-1.7 s). Searches are always
    limited to the office.
  - Office claim names are all uppercase, so the input is uppercased.
  - In CSE_META a township looks like "0120N": 3 digits, a fraction digit,
    and a direction. The one-character wildcard "_" covers the fraction.
    "Contains" is needed: some descriptions list the section second.
  - The service reports query errors inside a normal 200 response.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any

from fastmcp import FastMCP

from mining_claims_mcp.claims import (
    INCOMPLETE_ERROR,
    MLRS_LAST_RESORT,
    NOT_IN_DATA_NOTE,
    TOTAL_COUNT_PARAMS,
    TOTAL_LAYER,
    build_claim,
    service_looks_incomplete,
)
from mining_claims_mcp.models import ClaimStatus, LandState
from mining_claims_mcp.tools.find_near_location import (
    DETAIL_FIELDS,
    LAYERS,
    describe_status,
)
from mining_claims_mcp.utils import UpstreamServiceError, fetch_json, sql_quote

OFFICE_FILTER = "CSE_NR LIKE 'MT%'"
MAX_CLAIMS_SHOWN = 25
MIN_NAME_LENGTH = 3
MAX_NAME_LENGTH = 60
MAX_TOWNSHIP = 199  # North Dakota reaches T. 163 N.
MAX_RANGE = 99
MAX_SECTION = 36

# Meridian code in CSE_META for each state's land (open questions A7: still
# to be confirmed with staff).
MERIDIAN_CODES = {LandState.MT: "20", LandState.SD: "07", LandState.ND: "05"}
STATE_NAMES = {
    LandState.MT: "Montana",
    LandState.SD: "South Dakota",
    LandState.ND: "North Dakota",
}

# Letters, numbers, spaces and # - . ' & only. % and _ are SQL wildcards.
NAME_CHARS = re.compile(r"^[A-Z0-9 #\-.'&]+$")
TOWNSHIP_TEXT = re.compile(
    r"^(?:T|TWP|TOWNSHIP)?\.?\s*(\d{1,3})\s*(N|NORTH|S|SOUTH)\.?$"
)
RANGE_TEXT = re.compile(r"^(?:R|RNG|RANGE)?\.?\s*(\d{1,2})\s*(E|EAST|W|WEST)\.?$")

SOURCE = "BLM mining claims map service (nlsdbpubgis.blm.gov), queried live"
OFFICE_LABEL = "Montana/Dakotas (serial numbers starting with MT)"
NAME_NOTE = (
    "Name matches are 'contains': DUTCHESS also finds DUTCHESS OVEN I. Only "
    "claims with Montana/Dakotas office serial numbers (MT) are searched."
)
SECTION_NOTE = (
    "A section search finds every claim whose legal description lists that "
    "section, including claims that also cover other sections."
)
LEGAL_NOTE = "Legal descriptions are shown as BLM recorded them and can contain errors."
CLAIMANT_NOTE = NOT_IN_DATA_NOTE
LOCATE_HINT = (
    "ask the caller for a township and range, or a nearby landmark "
    "(mining_claims_locate_place, then mining_claims_find_near_location)"
)
DETAILS_HINT = (
    "For full details on a claim, use mining_claims_lookup_by_serial with its "
    "serial number."
)


def clean_name(text: str) -> tuple[str | None, dict[str, Any] | None]:
    """Return (cleaned uppercase name, None) or (None, error dict)."""
    name = " ".join(text.split()).upper()
    if "%" in name or "_" in name:
        return None, {
            "error": "The claim name can't contain % or _.",
            "hint": "Give part of the name in plain letters, e.g. DUTCHESS.",
        }
    if len(name) < MIN_NAME_LENGTH:
        return None, {
            "error": f"The claim name needs at least {MIN_NAME_LENGTH} characters.",
            "hint": "Give more of the name, or search by township and range.",
        }
    if len(name) > MAX_NAME_LENGTH:
        return None, {
            "error": f"The claim name is longer than {MAX_NAME_LENGTH} characters.",
            "hint": "Give a shorter part of the name; matches are 'contains'.",
        }
    if not NAME_CHARS.match(name):
        return None, {
            "error": "The claim name has characters that can't be searched.",
            "hint": "Use letters, numbers, spaces, and # - . ' & only.",
        }
    return name, None


def parse_township(text: str) -> tuple[int, str] | None:
    """'12N', 'T. 12 N.', '12 north' -> (12, 'N'); None if not usable."""
    m = TOWNSHIP_TEXT.match(" ".join(text.split()).upper())
    if not m or not 1 <= int(m[1]) <= MAX_TOWNSHIP:
        return None
    return int(m[1]), m[2][0]


def parse_range(text: str) -> tuple[int, str] | None:
    """'13W', 'R. 13 W.', '13 west' -> (13, 'W'); None if not usable."""
    m = RANGE_TEXT.match(" ".join(text.split()).upper())
    if not m or not 1 <= int(m[1]) <= MAX_RANGE:
        return None
    return int(m[1]), m[2][0]


def location_pattern(
    land_state: LandState,
    township: tuple[int, str],
    range_: tuple[int, str],
    section: int | None,
) -> str:
    """CSE_META 'contains' text, e.g. '20 012_N 013_W 026'."""
    text = (
        f"{MERIDIAN_CODES[land_state]} {township[0]:03d}_{township[1]} "
        f"{range_[0]:03d}_{range_[1]}"
    )
    if section is not None:
        text += f" {section:03d}"
    return text


def build_where(name: str | None, location: str | None) -> str:
    """The office filter plus each validated filter, joined with AND."""
    parts = [OFFICE_FILTER]
    if name:
        parts.append(f"CSE_NAME LIKE {sql_quote(f'%{name}%')}")
    if location:
        parts.append(f"CSE_META LIKE {sql_quote(f'%{location}%')}")
    return " AND ".join(parts)


def describe_search(search: dict[str, Any]) -> str:
    """'named like "DUTCHESS" in T. 12 N., R. 13 W., sec. 26 (Montana)'."""
    pieces = []
    if "claim_name" in search:
        pieces.append(f'with a name containing "{search["claim_name"]}"')
    if "township" in search:
        where = f"in {search['township']}, {search['range']}"
        if "section" in search:
            where += f", sec. {search['section']}"
        pieces.append(f"{where} ({search['land_state_name']})")
    return " ".join(pieces)


async def query(layer: int, params: dict[str, Any]) -> dict[str, Any]:
    """Run one layer query, raising if the service reports an error."""
    payload = await fetch_json(f"{layer}/query", params={**params, "f": "json"})
    if not isinstance(payload, dict) or "error" in payload:
        raise UpstreamServiceError("The BLM service could not run the query.")
    return payload


def _service_error() -> dict[str, Any]:
    return {
        "error": "The BLM mining claims service did not respond or returned an error.",
        "hint": "The service may be slow or down. Try again in a minute.",
    }


def _input_error(error: str, hint: str) -> dict[str, Any]:
    return {"error": error, "hint": hint}


def register(mcp: FastMCP) -> None:
    """Register the claim name and township/range search tool."""

    @mcp.tool(
        name="mining_claims_search",
        annotations={
            "title": "Search Mining Claims by Name or Township and Range",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def mining_claims_search(
        claim_name: Annotated[
            str | None,
            "Part of the claim name, at least 3 characters, e.g. DUTCHESS. "
            "Letters, numbers, spaces and # - . ' & only.",
        ] = None,
        township: Annotated[
            str | None,
            "Township number and direction, e.g. 12N, T. 12 N., or 12 north. "
            "Needs range too.",
        ] = None,
        range: Annotated[  # noqa: A002 - the name staff use
            str | None,
            "Range number and direction, e.g. 13W, R. 13 W., or 13 west. "
            "Needs township too.",
        ] = None,
        section: Annotated[
            int | None,
            "Section number, 1-36. Only with township and range.",
        ] = None,
        land_state: Annotated[
            LandState,
            "State where the land is: MT (default), SD, or ND. Picks the "
            "meridian; only used with township and range.",
        ] = LandState.MT,
        status: Annotated[
            ClaimStatus,
            "active (default), closed, or all claims.",
        ] = ClaimStatus.ACTIVE,
    ) -> dict[str, Any]:
        """Find Montana/Dakotas office mining claims by claim name, by
        township and range (optionally section), or both combined.

        Use this when the caller gives a claim or mine name, or a legal
        description (township, range, section). For a serial number use
        mining_claims_lookup_by_serial; for a town, peak, creek or other
        landmark use mining_claims_locate_place, then
        mining_claims_find_near_location. If a caller names a mine and the
        name search finds nothing or too much, ask for a township and range
        or a nearby landmark.

        Ask first, MLRS last: when a search finds nothing or too much,
        follow `hint` and ask the user for more detail rather than sending
        them to MLRS (https://mlrs.blm.gov). Mention MLRS only when the user
        wants claimant names or case documents (not in this data), or when
        `hint` says so after the other options are used up.

        Data source: BLM's public mining claims map service
        (nlsdbpubgis.blm.gov), queried live; layer 1 for active claims, 2
        for closed, 0 for all. It reflects BLM's MLRS case records; BLM does
        not publish an update schedule. Only claims with Montana/Dakotas
        office serial numbers (MT, which includes South and North Dakota)
        are searched. Township and range use the meridian for land_state
        (Montana: Principal Meridian; South Dakota: Black Hills Meridian;
        North Dakota: Fifth Principal Meridian). Fractional townships and
        ranges are included.

        Returns a dict with `search` (the filters as cleaned, and status),
        `total_claims`, a `summary` sentence, and `claims`: up to 25, sorted
        by serial number, each with name, serial numbers, status, claim
        type, states, legal description parts, and mapping quality. `notes`
        carries caveats to pass on; `hint` says what to do next (e.g. how
        to narrow more than 25). On failure, or when the input can't be
        used, returns `error` and `hint`. If nothing is found while the
        service holds far fewer claims than usual (e.g. BLM is reloading),
        it returns an error instead of zero claims.
        """
        has_location = township is not None or range is not None
        if claim_name is None and not has_location:
            return _input_error(
                "Give a claim name, or a township and range.",
                "e.g. claim_name DUTCHESS, or township 12N and range 13W. "
                "For a serial number use mining_claims_lookup_by_serial.",
            )
        if section is not None and (township is None or range is None):
            return _input_error(
                "A section needs a township and range too.",
                "Give township and range with the section, e.g. 12N, 13W, 26.",
            )
        if has_location and (township is None or range is None):
            return _input_error(
                "Township and range must be given together.",
                "Give both, e.g. township 12N and range 13W.",
            )

        search: dict[str, Any] = {"office": OFFICE_LABEL}
        name = None
        if claim_name is not None:
            name, error = clean_name(claim_name)
            if error:
                return error
            search["claim_name"] = name

        location = None
        if has_location:
            twp = parse_township(township)
            if twp is None:
                return _input_error(
                    "That township can't be read.",
                    f"Use a number 1-{MAX_TOWNSHIP} and N or S, e.g. 12N or "
                    "T. 12 N. Fractional townships are found without the "
                    "fraction.",
                )
            rng = parse_range(range)
            if rng is None:
                return _input_error(
                    "That range can't be read.",
                    f"Use a number 1-{MAX_RANGE} and E or W, e.g. 13W or "
                    "R. 13 W. Fractional ranges are found without the "
                    "fraction.",
                )
            if section is not None and not 1 <= section <= MAX_SECTION:
                return _input_error(
                    "That section number is not allowed.",
                    f"Use a section from 1 to {MAX_SECTION}.",
                )
            location = location_pattern(land_state, twp, rng, section)
            search["township"] = f"T. {twp[0]} {twp[1]}."
            search["range"] = f"R. {rng[0]} {rng[1]}."
            if section is not None:
                search["section"] = section
            search["land_state"] = land_state.value
            search["land_state_name"] = STATE_NAMES[land_state]
        search["status"] = status.value

        layer = LAYERS[status]
        where = build_where(name, location)
        base: dict[str, Any] = {"search": search, "source": SOURCE}
        notes = []
        if name:
            notes.append(NAME_NOTE)
        if section is not None:
            notes.append(SECTION_NOTE)
        notes += [LEGAL_NOTE, CLAIMANT_NOTE]

        try:
            count_payload, list_payload = await asyncio.gather(
                query(layer, {"where": where, "returnCountOnly": "true"}),
                query(
                    layer,
                    {
                        "where": where,
                        "outFields": DETAIL_FIELDS,
                        "orderByFields": "CSE_NR",
                        "resultRecordCount": MAX_CLAIMS_SHOWN,
                        "returnGeometry": "false",
                    },
                ),
            )
        except UpstreamServiceError:
            return _service_error()
        total = count_payload.get("count")
        if not isinstance(total, int) or isinstance(total, bool):
            return _service_error()
        records = [f.get("attributes", {}) for f in list_payload.get("features", [])]
        described = describe_search(search)

        if total == 0:
            # D-036: an empty or reloading service also finds nothing.
            try:
                total_payload = await query(TOTAL_LAYER, TOTAL_COUNT_PARAMS)
                if service_looks_incomplete(total_payload):
                    return dict(INCOMPLETE_ERROR)
            except UpstreamServiceError:
                return _service_error()
            hints = []
            if name:
                hints.append("check the spelling or give a shorter part of the name")
            if status is not ClaimStatus.ALL:
                hints.append("search with status all")
            if section is not None:
                hints.append("drop the section")
            if location:
                hints.append("check land_state (the meridian)")
            else:
                hints.append(LOCATE_HINT)
            hint = "Try to " + ", or ".join(hints) + "."
            # D-045: MLRS only once status all and a location have been tried.
            if location and status is ClaimStatus.ALL:
                hint += " " + MLRS_LAST_RESORT
            return {
                **base,
                "total_claims": 0,
                "summary": f"No {describe_status(status, 0)} found {described}.",
                "claims": [],
                "notes": notes,
                "hint": hint,
            }

        shown = [build_claim(r) for r in records[:MAX_CLAIMS_SHOWN]]
        summary = f"{total} {describe_status(status, total)} found {described}."
        if total > len(shown):
            summary += f" Showing the first {len(shown)}, sorted by serial number."
            narrow = []
            if not location:
                narrow.append("a township and range")
            elif section is None:
                narrow.append("a section")
            if not name:
                narrow.append("part of the claim name")
            if status is ClaimStatus.ALL:
                narrow.append("status active or closed")
            hint = (
                "To narrow it, add " + " or ".join(narrow) + ". "
                if narrow
                else "Only the first 25 are shown; there is no paging. "
            ) + DETAILS_HINT
        else:
            hint = DETAILS_HINT
        return {
            **base,
            "total_claims": total,
            "summary": summary,
            "claims": shown,
            "notes": notes,
            "hint": hint,
        }
