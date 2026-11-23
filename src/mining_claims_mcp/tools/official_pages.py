"""Official pages tool: BLM's own pages for procedure (how-to) questions.

Returns links, a short summary staff can say to a caller, the state office
contact, and (for fee topics) dated fee amounts, from the fixed table in
official_pages.py. Makes no outside calls. Design:
docs/official_pages_plan.md, decisions D-048 and D-049.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastmcp import FastMCP

from mining_claims_mcp.models import OfficialTopic
from mining_claims_mcp.official_pages import topic_pages


def register(mcp: FastMCP) -> None:
    """Register the official BLM pages tool."""

    @mcp.tool(
        name="mining_claims_official_pages",
        annotations={
            "title": "Official BLM Pages for Mining Claim Procedures",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def mining_claims_official_pages(
        topic: Annotated[
            OfficialTopic,
            "staking (how to stake), where_allowed (where claims can be "
            "located), recording (filing a new claim), fees (amounts and due "
            "dates), small_miner_waiver (getting out of the maintenance fee), "
            "forms, transfers_and_changes (sell, transfer, amend, abandon, "
            "change address), mlrs_account (MLRS accounts and online "
            "payment), rules (regulations, permission to work on a claim), or "
            "list (topic names only).",
        ],
    ) -> dict[str, Any]:
        """BLM's official pages for a mining claim procedure question, each
        with a link, plus a short summary staff can say to a caller.

        Use this for how-to questions: staking, filing (recording) a claim,
        fees and due dates, the small miner waiver, forms, transfers and
        amendments, MLRS accounts and online payments, and rules. For a
        specific claim or place use the data tools
        (mining_claims_lookup_by_serial, mining_claims_search,
        mining_claims_locate_place, mining_claims_find_near_location).

        Repeat only what the result says: say the summary, give the links,
        and don't add rules, deadlines or amounts that aren't in it. Quote a
        fee only from `fees`, using its `text`, which carries the date the
        BLM fees page was checked. For anything not covered, give
        `office_contact`. A question about whether a place is open to mining
        is `where_allowed`: "no active claims recorded" does not mean open.

        Data source: a fixed list kept in this server, written from BLM's
        own pages (blm.gov, mlrs.blm.gov, ecfr.gov) on `last_checked`; fee
        amounts from the BLM Mining Claim Fees page on `fees_checked`. No
        live calls, so it is instant and always available.

        Returns a dict with `topic`, `question`, `summary`, `pages` (each
        `title`, `url`), `office_contact` (Montana/Dakotas State Office:
        address, phone, email, url), `last_checked`, and `notes` to follow.
        Fee topics (fees, recording, small_miner_waiver,
        transfers_and_changes) also return `fees` (each `item`, `amount`,
        `per`, `due`, `text`) and `fees_checked`; if that date is over a year
        old, `notes` says to confirm the amounts first. topic `list` returns
        `topics` (each `topic`, `question`) instead.
        """
        return topic_pages(topic, date.today())
