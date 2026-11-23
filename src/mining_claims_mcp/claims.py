"""Shared helpers for describing mining claim records in plain terms.

Used by every tool that reads BLM's mining claims map service: claim type
from CSE_TYPE_NR, legal description parts from CSE_META, mapping quality
from QLTY, and the whole claim description (build_claim). Field quirks are
in section 7 of the project notes:
  - CSE_META (legal description) parts are split by "|" or " | " and repeat.
  - QLTY (mapping quality) starts with a score, then log text.
"""

from __future__ import annotations

import re
from typing import Any

from mining_claims_mcp.config import settings
from mining_claims_mcp.utils import UpstreamServiceError

# Partial-reload check (D-036). When a search finds nothing, each tool sends
# this count to layer 0 through its own query function and passes the reply
# to service_looks_incomplete.
TOTAL_LAYER = 0
TOTAL_COUNT_PARAMS = {"where": "1=1", "returnCountOnly": "true"}
INCOMPLETE_ERROR = {
    "error": (
        "The BLM mining claims service holds far fewer claims than usual right "
        "now, so a claim that exists may not be found. This is not a "
        "'not found' answer."
    ),
    "hint": (
        "BLM may be reloading its data. Try again later, or search MLRS "
        "(https://mlrs.blm.gov)."
    ),
}

# "Ask first, MLRS last" (D-045): results state what this data lacks without
# sending the user away; MLRS is suggested only once the tools have run out of
# things to try (and in the service error messages).
NOT_IN_DATA_NOTE = (
    "This data has no claimant names, certificates of location, or case documents."
)
MLRS_LAST_RESORT = (
    "If the details are right and the claim is still not found, the caller "
    "can search MLRS (https://mlrs.blm.gov)."
)

# First four digits of CSE_TYPE_NR. Preferred over BLM_PROD, whose text varies.
CLAIM_TYPES = {
    "3841": "Lode claim",
    "3842": "Placer claim",
    "3843": "Tunnel site",
    "3844": "Mill site",
}

STATE_NAMES = {
    "AK": "Alaska",
    "AZ": "Arizona",
    "CA": "California",
    "CO": "Colorado",
    "ID": "Idaho",
    "MT": "Montana",
    "ND": "North Dakota",
    "NM": "New Mexico",
    "NV": "Nevada",
    "OR": "Oregon",
    "SD": "South Dakota",
    "UT": "Utah",
    "WY": "Wyoming",
}

# BLM meridian codes seen in Montana/Dakotas data. Others are shown as codes.
MERIDIANS = {
    "05": "Fifth Principal Meridian",
    "07": "Black Hills Meridian",
    "20": "Principal Meridian, Montana",
}

# Last digit of a township or range code: 0 = whole, 1-3 = fractional.
FRACTIONS = {"0": "", "1": " 1/4", "2": " 1/2", "3": " 3/4"}

# One legal description part, e.g. "MT 20 0270N 0310W 016 A NW".
LEGAL_PART = re.compile(
    r"^(?P<state>[A-Z]{2})\s+(?P<meridian>\d{2})\s+"
    r"(?P<twp>\d{3})(?P<twp_frac>\d)(?P<twp_dir>[NS])\s+"
    r"(?P<rng>\d{3})(?P<rng_frac>\d)(?P<rng_dir>[EW])\s+"
    r"(?P<section>\d{3})"
    r"(?:\s+(?:A\s+)?(?P<subdivision>.+))?$"
)
LOT = re.compile(r"^(?:Government\s+)?Lot\s*(\d+)$", re.IGNORECASE)
QUARTERS = re.compile(r"^(?:NE|NW|SE|SW)+$")


def describe_claim_type(code: str | None) -> str | None:
    if not code:
        return None
    return CLAIM_TYPES.get(code[:4], f"Unknown type (code {code})")


def _describe_subdivision(text: str) -> str:
    text = text.strip()
    lot = LOT.match(text)
    if lot:
        return f"Lot {lot.group(1)}"
    if text.upper() == "ALL":
        return "all of section"
    if QUARTERS.match(text):
        return "".join(f"{text[i : i + 2]}1/4" for i in range(0, len(text), 2))
    return text


def parse_legal_description(meta: str | None) -> list[dict[str, Any]]:
    """Split CSE_META into cleaned, deduplicated legal description parts.

    A part naming only a section is dropped when another part gives a
    subdivision of that same section. Parts that don't match the usual
    pattern are kept with only their raw text.
    """
    raw_parts: list[str] = []
    for piece in (meta or "").split("|"):
        piece = " ".join(piece.split())
        if piece and piece not in raw_parts:
            raw_parts.append(piece)

    parsed = []
    for raw in raw_parts:
        m = LEGAL_PART.match(raw)
        if not m:
            # A bare state code ("SD") carries no location; skip it.
            if not re.fullmatch(r"[A-Z]{2}", raw):
                parsed.append({"raw": raw, "text": raw})
            continue
        meridian = MERIDIANS.get(m["meridian"], f"meridian code {m['meridian']}")
        twp_frac = FRACTIONS.get(m["twp_frac"], "")
        rng_frac = FRACTIONS.get(m["rng_frac"], "")
        township = f"T. {int(m['twp'])}{twp_frac} {m['twp_dir']}."
        range_ = f"R. {int(m['rng'])}{rng_frac} {m['rng_dir']}."
        section = f"sec. {int(m['section'])}"
        subdivision = (
            _describe_subdivision(m["subdivision"]) if m["subdivision"] else None
        )
        text = ", ".join(
            p for p in (meridian, township, range_, section, subdivision) if p
        )
        parsed.append(
            {
                "state": m["state"],
                "meridian": meridian,
                "township": township,
                "range": range_,
                "section": section,
                "subdivision": subdivision,
                "text": text,
                "raw": raw,
                "_section_key": raw[: m.end("section")],
            }
        )

    subdivided = {
        p["_section_key"] for p in parsed if p.get("_section_key") and p["subdivision"]
    }
    result = []
    for p in parsed:
        key = p.pop("_section_key", None)
        if key and not p["subdivision"] and key in subdivided:
            continue
        result.append(p)
    return result


QUALITY_MEANINGS = {
    "exact": "Mapped from a direct match to the legal description.",
    "best_guess": (
        "Mapped as a calculated best guess from the legal description; not exact."
    ),
    "section": "Mapped to the whole section, not the claim's exact location.",
    "no_shape": "No map shape; attribute information only.",
    "partial": "Only partly mapped.",
    "county": "Mapped to the whole county, not the claim's exact location.",
    "improved": "Map shape improved by BLM staff.",
    "unrecognized": "Mapping quality score not recognized.",
    "not_recorded": "Mapping quality is not recorded.",
}


def quality_level(qlty: str | None) -> tuple[str | None, str]:
    """Read the QLTY score: (score text, level key in QUALITY_MEANINGS)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)", qlty or "")
    if not m:
        return None, "not_recorded"
    score = float(m.group(1))
    if score <= 3:
        level = "exact"
    elif score <= 8:
        level = "best_guess"
    elif score <= 10:
        level = "section"
    elif score in (11, 12) or 20 <= score <= 22:
        level = "no_shape"
    elif score == 15:
        level = "partial"
    elif score == 25:
        level = "county"
    elif score == 100:
        level = "improved"
    else:
        level = "unrecognized"
    return m.group(1), level


def describe_quality(qlty: str | None) -> dict[str, Any]:
    """Turn the QLTY score into a sentence about how exact the map shape is."""
    score, level = quality_level(qlty)
    return {"score": score, "meaning": QUALITY_MEANINGS[level]}


def build_claim(attrs: dict[str, Any]) -> dict[str, Any]:
    """Shape one service record into a plain-terms claim description."""
    legal = parse_legal_description(attrs.get("CSE_META"))
    states = []
    for part in legal:
        code = part.get("state")
        if code and code not in states:
            states.append(code)
    return {
        "name": attrs.get("CSE_NAME") or None,
        "serial_number": attrs.get("CSE_NR"),
        "legacy_serial_number": attrs.get("LEG_CSE_NR") or None,
        "status": attrs.get("CSE_DISP"),
        "claim_type": describe_claim_type(attrs.get("CSE_TYPE_NR")),
        "states": [STATE_NAMES.get(s, s) for s in states],
        "legal_description": legal,
        "mapping_quality": describe_quality(attrs.get("QLTY")),
    }


def service_looks_incomplete(count_payload: dict[str, Any]) -> bool:
    """True if layer 0's total (a TOTAL_COUNT_PARAMS reply) is below the minimum.

    Raises UpstreamServiceError if the reply has no usable count.
    """
    count = count_payload.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise UpstreamServiceError("The BLM service returned no record count.")
    return count < settings.blm_min_total_records
