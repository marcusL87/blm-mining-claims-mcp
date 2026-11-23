"""Tool 1: look up a mining claim by serial number.

Finds a claim by its current (MLRS) or legacy (LR2000) serial number, or by
part of one, in BLM's public mining claims map service, and returns the claim
described in plain terms. Design: section 9 of the project notes.

Field quirks this file handles (see section 7 of the project notes):
  - ADMIN_STATE is empty for most claims, so the office filter uses the serial
    number prefix instead (every Montana/Dakotas claim starts with "MT").
  - The service reports query errors inside a normal 200 response.
The claim description (build_claim, with its legal description, claim type,
and mapping quality helpers) is shared with other tools in
mining_claims_mcp/claims.py.
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
    build_claim,
    service_looks_incomplete,
)
from mining_claims_mcp.models import OfficeCode
from mining_claims_mcp.utils import (
    DEFAULT_TIMEOUT,
    UpstreamServiceError,
    fetch_json,
    sql_quote,
)

# Layer 0 holds all cases (active and closed).
QUERY_PATH = "0/query"
DETAIL_FIELDS = "CSE_NR,LEG_CSE_NR,CSE_NAME,CSE_DISP,CSE_TYPE_NR,CSE_META,QLTY"
LIST_FIELDS = "CSE_NR,LEG_CSE_NR,CSE_NAME,CSE_DISP"
MIN_PARTIAL_LENGTH = 5
MAX_PARTIAL_RESULTS = 10
MAX_SERIAL_LENGTH = 20
# A nationwide "contains" search reads all 4.4 million records; run alongside
# the count query it took about 33 s on Oct 17, past the 30 s default.
NATIONWIDE_TIMEOUT = 90.0


def clean_serial(raw: str) -> str:
    """Uppercase and drop spaces, dashes, and periods: 'mmc 114339' -> 'MMC114339'."""
    return re.sub(r"[\s.\-]", "", raw).upper()


def _list_entry(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "serial_number": attrs.get("CSE_NR"),
        "legacy_serial_number": attrs.get("LEG_CSE_NR") or None,
        "name": attrs.get("CSE_NAME") or None,
        "status": attrs.get("CSE_DISP"),
    }


async def query(
    params: dict[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Run a layer 0 query, raising if the service reports an error."""
    payload = await fetch_json(
        QUERY_PATH, params={**params, "f": "json"}, timeout=timeout
    )
    if not isinstance(payload, dict) or "error" in payload:
        raise UpstreamServiceError("The BLM service could not run the query.")
    return payload


async def _incomplete_check() -> dict[str, Any] | None:
    """Before saying "not found": an error if the service looks incomplete (D-036)."""
    try:
        if service_looks_incomplete(await query(TOTAL_COUNT_PARAMS)):
            return dict(INCOMPLETE_ERROR)
    except UpstreamServiceError:
        return _service_error()
    return None


def _service_error() -> dict[str, Any]:
    return {
        "error": "The BLM mining claims service did not respond or returned an error.",
        "hint": (
            "The service may be slow or down. Try again in a minute; if it keeps "
            "failing, search the serial number in MLRS (https://mlrs.blm.gov)."
        ),
    }


def register(mcp: FastMCP) -> None:
    """Register the serial number lookup tool with the MCP server."""

    @mcp.tool(
        name="mining_claims_lookup_by_serial",
        annotations={
            "title": "Look Up Mining Claim by Serial Number",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def mining_claims_lookup_by_serial(
        serial_number: Annotated[
            str,
            "Full or partial serial number, current (e.g. MT101334253) or legacy "
            "(e.g. MMC114339). Spaces, dashes, and periods are ignored.",
        ],
        state: Annotated[
            OfficeCode,
            "BLM office to search for partial matches, by serial prefix. Default "
            "MT (Montana/Dakotas office, which includes South and North Dakota "
            "claims). Use ALL for nationwide (slower: a partial search can take "
            "up to about a minute). Exact matches always "
            "search nationwide.",
        ] = OfficeCode.MT,
    ) -> dict[str, Any]:
        """Find a mining claim by current, legacy, or partial serial number.

        Data source: BLM's public mining claims map service
        (nlsdbpubgis.blm.gov, layer 0, all active and closed cases), queried
        live. It reflects BLM's MLRS case records as mapped by BLM; BLM does
        not publish an update schedule.

        Matching:
          1. Exact match on the current serial (CSE_NR) or legacy serial
             (LEG_CSE_NR), nationwide.
          2. If nothing is exact and the cleaned input has at least 5
             characters, a partial ("contains") match within the chosen
             office, returning up to 10 claims plus the total count.

        Returns a dict with `match_type` ("exact", "partial", or "none") and
        a `summary` sentence to relay to the user.
          - exact: `matched_on` and `claims`, each with name, serial numbers,
            status, claim type, states, legal description parts, and mapping
            quality (how exact the map shape is; say so when it is not exact).
          - partial: `total_count` and `matches` (serial, legacy serial, name,
            status). If total_count exceeds the matches shown, ask the user for
            more of the number.
          - none: `hint` with what to try next.
        Every result has `notes`: this data has no claimant names,
        certificates of location, or case documents. Those are in MLRS
        (https://mlrs.blm.gov); mention MLRS when the user asks for them, or
        when the claim can't be found after checking the number with them.
        Otherwise prefer asking the user for more detail over sending them
        to MLRS.
        On failure, returns `error` and `hint`. If nothing is found while the
        service holds far fewer claims than usual (e.g. BLM is reloading), it
        returns an error instead of "none"; never tell the user the claim
        doesn't exist in that case.
        """
        serial = clean_serial(serial_number)
        if not serial:
            return {
                "error": "No serial number was given.",
                "hint": "Provide a full or partial serial number, e.g. MMC114339.",
            }
        if not re.fullmatch(r"[A-Z0-9]+", serial) or len(serial) > MAX_SERIAL_LENGTH:
            return {
                "error": "That is not a valid serial number.",
                "hint": (
                    "Serial numbers are letters and digits only, e.g. MT101334253 "
                    "or MMC114339."
                ),
            }

        base = {
            "search_input": serial_number,
            "cleaned_serial": serial,
            "notes": [NOT_IN_DATA_NOTE],
        }
        quoted = sql_quote(serial)

        try:
            exact = await query(
                {
                    "where": f"CSE_NR = {quoted} OR LEG_CSE_NR = {quoted}",
                    "outFields": DETAIL_FIELDS,
                    "returnGeometry": "false",
                }
            )
        except UpstreamServiceError:
            return _service_error()

        records = [f.get("attributes", {}) for f in exact.get("features", [])]
        if records:
            on_legacy = any(r.get("LEG_CSE_NR") == serial for r in records)
            matched_on = "legacy serial" if on_legacy else "current serial"
            summary = f"Exact match on {matched_on} {serial}."
            if len(records) > 1:
                summary += f" {len(records)} claims share this number."
            return {
                **base,
                "match_type": "exact",
                "matched_on": matched_on,
                "summary": summary,
                "claims": [build_claim(r) for r in records],
            }

        if len(serial) < MIN_PARTIAL_LENGTH:
            if problem := await _incomplete_check():
                return problem
            return {
                **base,
                "match_type": "none",
                "summary": f"No claim has the exact serial number {serial}.",
                "hint": (
                    f"For a partial search, give at least {MIN_PARTIAL_LENGTH} "
                    "characters of the serial number."
                ),
            }

        # Office filter by serial prefix: ADMIN_STATE is empty for most claims.
        contains = sql_quote(f"%{serial}%")
        where = f"(CSE_NR LIKE {contains} OR LEG_CSE_NR LIKE {contains})"
        area = "nationwide"
        timeout = NATIONWIDE_TIMEOUT
        if state is not OfficeCode.ALL:
            where = f"CSE_NR LIKE {sql_quote(state.value + '%')} AND {where}"
            area = f"in the {state.label} office ({state.value})"
            timeout = DEFAULT_TIMEOUT

        try:
            count_payload, list_payload = await asyncio.gather(
                query({"where": where, "returnCountOnly": "true"}, timeout),
                query(
                    {
                        "where": where,
                        "outFields": LIST_FIELDS,
                        "returnGeometry": "false",
                        "orderByFields": "CSE_NR",
                        "resultRecordCount": MAX_PARTIAL_RESULTS,
                    },
                    timeout,
                ),
            )
        except UpstreamServiceError:
            return _service_error()

        matches = [
            _list_entry(f.get("attributes", {}))
            for f in list_payload.get("features", [])
        ]
        total = int(count_payload.get("count", len(matches)))
        if not matches:
            if problem := await _incomplete_check():
                return problem
            hint = (
                "Check the number with the caller: a digit may be missing or swapped."
            )
            # D-045: MLRS only once the nationwide search has been tried.
            if state is not OfficeCode.ALL:
                hint += " If the claim may be in another state, search with state ALL."
            else:
                hint += " " + MLRS_LAST_RESORT
            return {
                **base,
                "match_type": "none",
                "summary": f"No claims found matching {serial} {area}.",
                "hint": hint,
            }

        noun, verb = ("claim", "contains") if total == 1 else ("claims", "contain")
        summary = f"Partial match: {total} {noun} {area} {verb} {serial}."
        result = {
            **base,
            "match_type": "partial",
            "total_count": total,
            "matches": matches,
        }
        if total > len(matches):
            summary += f" Showing the first {len(matches)}, sorted by serial number."
            result["hint"] = "Ask for more of the serial number to narrow the search."
        result["summary"] = summary
        return result
