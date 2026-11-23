"""Official BLM pages for procedure questions (staking, filing, fees, ...).

A fixed table, kept in the code so the official pages tool makes no outside
calls: it is instant and can't fail on a slow service. Every link was checked
(HTTP 200) and every summary was written from the page's own text on
LAST_CHECKED. Fee amounts come from the BLM Mining Claim Fees page, checked
on FEES_CHECKED. Design: docs/official_pages_plan.md, decisions D-048, D-049.

The summaries are drafts until front desk staff review them (open questions
A12). Changing them is only an edit to this table and a re-run of the tests.

To re-check: open every URL below, compare each summary and fee amount with
the page, then update LAST_CHECKED and FEES_CHECKED (and any changed amounts).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mining_claims_mcp.models import OfficialTopic

LAST_CHECKED = date(2026, 11, 15)  # links and summaries
FEES_CHECKED = date(2026, 11, 15)  # fee amounts on the fees page
FEES_STALE_DAYS = 365

# Base URLs are constants; nothing here is built from user input.
BLM = "https://www.blm.gov"
MINERALS = f"{BLM}/programs/energy-and-minerals/mining-and-minerals"
MC = f"{MINERALS}/locatable-minerals/mining-claims"
# These two pages sit under "locatable-materials" on blm.gov (checked Nov 15).
LOCATABLE = f"{MINERALS}/locatable-materials"
MLRS = "https://mlrs.blm.gov"
ECFR = "https://www.ecfr.gov/current/title-43/subtitle-B/chapter-II/subchapter-C"

FORM_3830_2 = f"{BLM}/sites/default/files/docs/2024-07/3830-002.pdf"
FORM_3830_4 = f"{BLM}/sites/default/files/docs/2024-06/3830-004.pdf"

# Staff to confirm whether a dedicated mining claims email should be used
# instead (open questions A12). From https://www.blm.gov/montana-dakotas.
OFFICE_CONTACT = {
    "name": "BLM Montana/Dakotas State Office",
    "address": "5001 Southgate Drive, Billings, MT 59101",
    "phone": "406-896-5004",
    "email": "BLM_MT_SO_Information@blm.gov",
    "url": f"{BLM}/office/montanadakotas-state-office",
}

PAGES_NOTE = (
    "These are BLM's official pages. Give the caller the link; don't add "
    "rules or amounts that aren't in the summary."
)
FEES_NOTE = (
    "Quote amounts only from `fees`, with their checked date. BLM changes "
    "fees from time to time; the fees page is the authority."
)
NOT_COVERED_NOTE = (
    "For anything these summaries don't cover, give the caller the office contact."
)
# Notes from the fees page itself, for topics that list new-claim fees.
FEE_PAGE_NOTES = (
    'The fees page lists these amounts under "New claims filed on or after '
    'September 1, 2024".',
    "Processing, location and first maintenance fees must be paid in full "
    "when the claim is filed with BLM.",
    "A claim located before September 1 but filed with BLM on or after "
    "September 1 owes maintenance fees for both assessment years, unless a "
    "valid waiver is filed.",
)

FEES_PAGE = "BLM fees page"
RECORDING_PAGE = "BLM recording page"
NEW_CLAIM_DUE = "when the claim is filed with BLM (within 90 days of location)"

# key: (item, amount, per, due, source page)
FEES: dict[str, tuple[str, str, str, str, str]] = {
    "new_lode_processing": (
        "Processing fee, new lode claim, mill site or tunnel site",
        "$25",
        "per claim",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "new_lode_location": (
        "Location fee, new lode claim, mill site or tunnel site",
        "$49",
        "per claim",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "new_lode_maintenance": (
        "First maintenance fee, new lode claim, mill site or tunnel site",
        "$200",
        "per claim",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "new_lode_total": (
        "Total for a new lode claim, mill site or tunnel site",
        "$274",
        "per claim",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "new_placer_processing_location": (
        "Processing and location fees, new placer claim",
        "$25 processing + $49 location",
        "per claim",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "new_placer_maintenance": (
        "First maintenance fee, new placer claim",
        "$200",
        "for each 20 acres or portion",
        NEW_CLAIM_DUE,
        FEES_PAGE,
    ),
    "annual_lode": (
        "Annual maintenance fee, existing lode claim, mill site or tunnel site",
        "$200",
        "per claim or site",
        "on or before September 1",
        FEES_PAGE,
    ),
    "annual_placer": (
        "Annual maintenance fee, existing placer claim",
        "$200",
        "for each 20 acres or portion",
        "on or before September 1",
        FEES_PAGE,
    ),
    "small_miner_waiver": (
        "Small miner waiver (Form 3830-2)",
        "No fee",
        "",
        "on or before September 1",
        FEES_PAGE,
    ),
    "affidavit": (
        "Affidavit of assessment work",
        "$15",
        "per claim",
        "on or before December 30",
        FEES_PAGE,
    ),
    "intent_to_hold": (
        "Notice of intent to hold",
        "$15",
        "per claim",
        "on or before December 30",
        FEES_PAGE,
    ),
    "transfer": (
        "Transfer of interest (quitclaim deed or other transfer document)",
        "$15",
        "per claim per transferee",
        "after recording with the county",
        FEES_PAGE,
    ),
    "amendment": (
        "Amended notice of location",
        "$15",
        "per claim per amendment",
        "within 90 days after recording the amendment with the county",
        FEES_PAGE,
    ),
    "abandonment": (
        "Abandonment or relinquishment",
        "No fee",
        "",
        "",
        RECORDING_PAGE,
    ),
    "address_change": ("Change of address", "No fee", "", "", RECORDING_PAGE),
}

NEW_CLAIM_FEES = (
    "new_lode_processing",
    "new_lode_location",
    "new_lode_maintenance",
    "new_lode_total",
    "new_placer_processing_location",
    "new_placer_maintenance",
)
# Fee keys returned with each topic; topics not listed return no fees.
TOPIC_FEES: dict[OfficialTopic, tuple[str, ...]] = {
    OfficialTopic.FEES: tuple(FEES),
    OfficialTopic.RECORDING: NEW_CLAIM_FEES,
    OfficialTopic.SMALL_MINER_WAIVER: (
        "small_miner_waiver",
        "annual_lode",
        "annual_placer",
        "affidavit",
        "intent_to_hold",
    ),
    OfficialTopic.TRANSFERS_AND_CHANGES: (
        "transfer",
        "amendment",
        "abandonment",
        "address_change",
    ),
}
TOPICS_WITH_FEE_PAGE_NOTES = (OfficialTopic.FEES, OfficialTopic.RECORDING)

# topic: (question, [(title, url)], summary)
TOPICS: dict[OfficialTopic, tuple[str, list[tuple[str, str]], str]] = {
    OfficialTopic.STAKING: (
        "How do I stake a claim?",
        [
            ("Staking a Claim", f"{MC}/staking-a-claim"),
            (
                "Locating a Mining Claim (where claims can and can't be located)",
                f"{MC}/locating-a-claim",
            ),
            (
                "Mining Claims and Sites on Federal Lands (brochure, PDF)",
                f"{BLM}/sites/default/files/PublicRoom_Mining_Claims_Brochure-2019.pdf",
            ),
        ],
        "Claim boundaries must be clearly marked on the ground, and each state "
        "has its own rules for corner monuments, so check Montana's (or South "
        "or North Dakota's) requirements too. Before staking, check BLM "
        "records for existing claims and look for existing claim markers on "
        "the ground. A claim needs a discovery of a valuable mineral. U.S. "
        "citizens, people who have declared their intention to become "
        "citizens, and corporations can stake claims, and an agent can stake "
        "for them.",
    ),
    OfficialTopic.WHERE_ALLOWED: (
        "Can I stake a claim here?",
        [("Locating a Mining Claim", f"{MC}/locating-a-claim")],
        "Claims can be located only on federal land open to mineral entry, "
        "mostly BLM and Forest Service land. Land withdrawn from mineral entry "
        "is closed, including national parks, national monuments, Indian "
        "reservations, military reservations, wildlife areas managed by the "
        "Fish and Wildlife Service, and designated wilderness. The mining "
        "claims map shows recorded claims, not whether land is open; for a "
        "specific parcel, contact the state office.",
    ),
    OfficialTopic.RECORDING: (
        "How do I file (record) a new claim?",
        [
            ("Recording a Mining Claim or Site", f"{MC}/recording"),
            (
                "Mineral & Land Records System (MLRS)",
                f"{BLM}/services/land-records/mlrs",
            ),
        ],
        "A new claim must be recorded in two places within 90 days of "
        "staking: with the county recorder or clerk where the claim is (some "
        "states require it sooner), and with the BLM state office. The notice "
        "includes the location date, the locators' names and addresses, the "
        "claim name and type, acres, the legal description to the quarter "
        "section, and a map. Processing, location and first-year maintenance "
        "fees are due when the claim is filed with BLM. New claims can be "
        "filed and paid online in MLRS. A claim not recorded on time can be "
        "considered abandoned.",
    ),
    OfficialTopic.FEES: (
        "What are the fees, and when are they due?",
        [
            ("Mining Claim Fees", f"{MC}/fees"),
            ("Annual Maintenance and Assessment", f"{LOCATABLE}/annual-maintenance"),
        ],
        "The annual maintenance fee is due on or before September 1 every "
        "year, for each claim or site (placer claims per 20 acres). New claims "
        "also pay processing and location fees when filed. Fees can be paid "
        "online in MLRS (which uses Pay.gov), in person at the state office, "
        "or by mail. Missing the September 1 deadline without filing a waiver "
        "subjects the claims to forfeiture by law.",
    ),
    OfficialTopic.SMALL_MINER_WAIVER: (
        "Can I get out of paying the maintenance fee?",
        [
            ("Mining Claim Fees (waiver section)", f"{MC}/fees"),
            ("Form 3830-2, Maintenance Fee Waiver Certification (PDF)", FORM_3830_2),
            ("Form 3830-4, Affidavit of Annual Assessment Work (PDF)", FORM_3830_4),
        ],
        "Claimants who, with all related parties, hold 10 or fewer claims "
        "nationwide can file a small miner's waiver (Form 3830-2) instead of "
        "paying, on or before September 1. The waiver can't be filed online; "
        "file it in person at the state office or by mail. Waiver holders "
        "then file an affidavit of assessment work (Form 3830-4), or a notice "
        "of intent to hold, on or before December 30, with a per-claim "
        "processing fee.",
    ),
    OfficialTopic.FORMS: (
        "Where are the forms?",
        [
            (
                "BLM Electronic Forms (mining forms are numbered 3809, 3830 and 3860)",
                f"{BLM}/services/electronic-forms",
            ),
            (
                "Form 3830-5, Maintenance Fee Payment Form, lode claims, mill "
                "and tunnel sites (PDF)",
                f"{BLM}/sites/default/files/docs/2024-07/3830-005_0.pdf",
            ),
            (
                "Form 3830-5a, Maintenance Fee Payment Form, placer claims (PDF)",
                f"{BLM}/sites/default/files/docs/2024-07/3830-005a.pdf",
            ),
            ("Form 3830-2, Maintenance Fee Waiver Certification (PDF)", FORM_3830_2),
            ("Form 3830-4, Affidavit of Annual Assessment Work (PDF)", FORM_3830_4),
        ],
        "BLM's mining claim forms are on the Electronic Forms page. Most "
        "filings and payments can be done online in MLRS instead. The "
        "location notice isn't on the Electronic Forms page; the Recording "
        "page says location notice forms are available from printing "
        "companies, office supply stores, state agencies and BLM offices.",
    ),
    OfficialTopic.TRANSFERS_AND_CHANGES: (
        "How do I transfer, amend or give up a claim, or change my address?",
        [
            (
                "Recording a Mining Claim or Site (amendments, transfers, abandonment)",
                f"{MC}/recording",
            ),
            ("Mining Claim Fees (transfer and amendment fees)", f"{MC}/fees"),
        ],
        "To transfer a claim, use a deed such as a quitclaim deed, record it "
        "with the county first, then file it with BLM. To change a claim's "
        "description, file an amended location notice with the county and "
        "then BLM within 90 days. Each has a per-claim processing fee. To give "
        "up a claim, send a letter to the county and BLM with the claim name "
        "and BLM serial number; there's no fee. Address changes go to the "
        "state office, also free.",
    ),
    OfficialTopic.MLRS_ACCOUNT: (
        "How do I create an MLRS account, pay online or check a payment?",
        [
            ("MLRS (sign in)", MLRS),
            ("MLRS: creating a new account", f"{MLRS}/s/creating-a-new-account"),
            ("MLRS Help Center", f"{MLRS}/s/help-center"),
            (
                "Getting Started with MLRS for Mining Claimants (PDF, 2021)",
                f"{BLM}/sites/default/files/docs/2021-07/"
                "Getting%20Started%20with%20MLRS%20for%20Mining%20Claimants_2021.pdf",
            ),
            ("About MLRS", f"{BLM}/services/land-records/mlrs"),
        ],
        "MLRS is BLM's online system for filing new claims, paying maintenance "
        "and processing fees, and filing documents such as transfers, "
        "amendments and affidavits. It's available 24 hours a day and sends "
        "payments through Pay.gov. An account is needed to file and pay; the "
        "'creating a new account' page and the Help Center explain how. "
        "Account and payment details are private, so staff can't look them up "
        "through this assistant.",
    ),
    OfficialTopic.RULES: (
        "What are the regulations? What do I need before working on my claim?",
        [
            (
                "43 CFR Group 3800 (mining claims regulations, parts 3830-3839)",
                f"{ECFR}/part-3830",
            ),
            ("43 CFR 3809 (surface management)", f"{ECFR}/part-3800/subpart-3809"),
            (
                "Surface Management of Locatable Minerals",
                f"{LOCATABLE}/surface-management",
            ),
            (
                "Explanation of Location (lode and placer claims, mill and "
                "tunnel sites)",
                f"{LOCATABLE}/explanation-of-location",
            ),
        ],
        "The mining claim rules are in Title 43 of the Code of Federal "
        "Regulations, parts 3830-3839, and work on the ground is covered by "
        "43 CFR 3809. A claim doesn't give exclusive rights to the surface; "
        "using or occupying the land for mining must be approved by the BLM "
        "field office. For what a specific project needs, contact the field "
        "office or state office.",
    ),
}


def say_date(d: date) -> str:
    """date(2026, 11, 15) -> 'November 15, 2026' (same on every platform)."""
    return f"{d:%B} {d.day}, {d.year}"


def fee_entry(key: str) -> dict[str, str]:
    """One fee as a dict, with a ready-to-say `text` carrying FEES_CHECKED."""
    item, amount, per, due, source = FEES[key]
    text = f"{item}: {amount}"
    if per:
        text += f" {per}"
    text += f" ({source}, checked {say_date(FEES_CHECKED)})."
    if due:
        text += f" Due {due}."
    return {"item": item, "amount": amount, "per": per, "due": due, "text": text}


def fees_stale_note(today: date) -> str | None:
    """A warning when FEES_CHECKED is over a year before `today`, else None."""
    if (today - FEES_CHECKED).days > FEES_STALE_DAYS:
        return (
            f"These fee amounts were last checked on {say_date(FEES_CHECKED)}, "
            "over a year ago. Confirm them on the fees page before quoting them."
        )
    return None


def topic_list() -> dict[str, Any]:
    """Every topic name with the question it answers."""
    return {
        "topics": [
            {"topic": topic.value, "question": question}
            for topic, (question, _pages, _summary) in TOPICS.items()
        ],
        "office_contact": dict(OFFICE_CONTACT),
        "last_checked": say_date(LAST_CHECKED),
        "hint": "Call mining_claims_official_pages again with one of these topics.",
    }


def topic_pages(topic: OfficialTopic, today: date) -> dict[str, Any]:
    """The pages, summary, office contact and (for fee topics) fees for a topic.

    `today` is used only to decide whether the fee amounts are over a year old.
    """
    if topic is OfficialTopic.LIST:
        return topic_list()
    question, pages, summary = TOPICS[topic]
    result: dict[str, Any] = {
        "topic": topic.value,
        "question": question,
        "summary": summary,
        "pages": [{"title": title, "url": url} for title, url in pages],
        "office_contact": dict(OFFICE_CONTACT),
        "last_checked": say_date(LAST_CHECKED),
    }
    notes = [PAGES_NOTE]
    fee_keys = TOPIC_FEES.get(topic)
    if fee_keys:
        result["fees"] = [fee_entry(key) for key in fee_keys]
        result["fees_checked"] = say_date(FEES_CHECKED)
        notes.append(FEES_NOTE)
        if topic in TOPICS_WITH_FEE_PAGE_NOTES:
            notes.extend(FEE_PAGE_NOTES)
        stale = fees_stale_note(today)
        if stale:
            notes.append(stale)
    notes.append(NOT_COVERED_NOTE)
    result["notes"] = notes
    return result
