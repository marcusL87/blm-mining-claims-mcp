"""Tests for the official pages tool, mining_claims_official_pages.

The tool reads a fixed table (official_pages.py) and makes no outside calls,
so nothing here touches the network (D-017); one test makes sure of it by
breaking the HTTP client. The tool is called through FastMCP's in-memory
client, as a real MCP client would. Link checks against the live pages are
in scripts/check_official_links.py, not here.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mining_claims_mcp import official_pages as op
from mining_claims_mcp.models import OfficialTopic
from mining_claims_mcp.official_pages import (
    FEE_PAGE_NOTES,
    FEES,
    FEES_CHECKED,
    FEES_NOTE,
    FEES_STALE_DAYS,
    LAST_CHECKED,
    NOT_COVERED_NOTE,
    OFFICE_CONTACT,
    PAGES_NOTE,
    TOPIC_FEES,
    TOPICS,
    fee_entry,
    fees_stale_note,
    say_date,
    topic_pages,
)
from mining_claims_mcp.tools import official_pages

TOOL = "mining_claims_official_pages"
ALLOWED_HOSTS = {"www.blm.gov", "mlrs.blm.gov", "www.ecfr.gov"}
FEE_TOPICS = {
    OfficialTopic.FEES,
    OfficialTopic.RECORDING,
    OfficialTopic.SMALL_MINER_WAIVER,
    OfficialTopic.TRANSFERS_AND_CHANGES,
}
PAGE_TOPICS = [t for t in OfficialTopic if t is not OfficialTopic.LIST]
NON_FEE_TOPICS = [t for t in PAGE_TOPICS if t not in FEE_TOPICS]
CHECKED_TEXT = "checked November 15, 2026"
TODAY = date(2026, 11, 15)
DOLLAR = re.compile(r"\$\s*\d")


async def call(**args: Any) -> dict[str, Any]:
    mcp = FastMCP("test")
    official_pages.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(TOOL, args)
    return result.data


def all_strings(value: Any) -> list[str]:
    """Every string anywhere inside a result (dict keys excluded)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in all_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in all_strings(v)]
    return []


def all_urls(result: dict[str, Any]) -> list[str]:
    urls = [page["url"] for page in result.get("pages", [])]
    urls.append(result["office_contact"]["url"])
    return urls


# ── Registration and wiring ──────────────────────────────────────────────────


async def test_tool_is_registered_read_only_and_closed_world():
    mcp = FastMCP("test")
    official_pages.register(mcp)
    async with Client(mcp) as client:
        (tool,) = await client.list_tools()
    assert tool.name == TOOL
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False


async def test_schema_offers_every_topic_and_requires_one():
    mcp = FastMCP("test")
    official_pages.register(mcp)
    async with Client(mcp) as client:
        (tool,) = await client.list_tools()
    schema = tool.input_schema
    assert schema["required"] == ["topic"]
    topic = schema["properties"]["topic"]
    enum = topic.get("enum") or schema["$defs"]["OfficialTopic"]["enum"]
    assert set(enum) == {t.value for t in OfficialTopic}


async def test_full_server_registers_the_tool():
    from mining_claims_mcp.app import mcp

    async with Client(mcp) as client:
        names = [t.name for t in await client.list_tools()]
    assert TOOL in names


def test_server_instructions_point_how_to_questions_here():
    from mining_claims_mcp.app import mcp

    assert TOOL in mcp.instructions
    assert "how-to" in mcp.instructions


@pytest.mark.parametrize("topic", ["bogus", "Fees", "FEES", "", "fee", "mlrs"])
async def test_unknown_topics_are_rejected_by_the_schema(topic):
    with pytest.raises(ToolError):
        await call(topic=topic)


async def test_missing_topic_is_rejected():
    with pytest.raises(ToolError):
        await call()


# ── The table matches the topic list ─────────────────────────────────────────


def test_every_topic_except_list_has_a_table_row():
    assert set(TOPICS) == set(PAGE_TOPICS)


def test_topic_fees_only_name_known_fees_and_fee_topics():
    assert set(TOPIC_FEES) == FEE_TOPICS
    for keys in TOPIC_FEES.values():
        assert keys, "a fee topic must list at least one fee"
        assert len(keys) == len(set(keys))
        assert set(keys) <= set(FEES)


def test_fees_topic_lists_every_fee():
    assert set(TOPIC_FEES[OfficialTopic.FEES]) == set(FEES)


# ── list ─────────────────────────────────────────────────────────────────────


async def test_list_returns_every_topic_with_its_question():
    result = await call(topic="list")
    assert [t["topic"] for t in result["topics"]] == [t.value for t in PAGE_TOPICS]
    for entry in result["topics"]:
        assert entry["question"] == TOPICS[OfficialTopic(entry["topic"])][0]
        assert entry["question"].endswith("?")
    assert TOOL in result["hint"]
    assert result["office_contact"] == OFFICE_CONTACT
    assert result["last_checked"] == "November 15, 2026"
    assert "pages" not in result
    assert "fees" not in result


# ── Every topic: pages, contact, notes ───────────────────────────────────────


@pytest.mark.parametrize("topic", PAGE_TOPICS, ids=lambda t: t.value)
async def test_every_topic_returns_pages_summary_and_contact(topic):
    result = await call(topic=topic.value)
    question, pages, summary = TOPICS[topic]
    assert result["topic"] == topic.value
    assert result["question"] == question
    assert result["summary"] == summary
    assert len(result["pages"]) >= 1
    assert result["pages"] == [{"title": t, "url": u} for t, u in pages]
    for page in result["pages"]:
        assert page["title"].strip()
    assert result["office_contact"] == OFFICE_CONTACT
    assert result["last_checked"] == say_date(LAST_CHECKED)


@pytest.mark.parametrize("topic", PAGE_TOPICS, ids=lambda t: t.value)
async def test_every_url_is_https_on_an_allowed_host(topic):
    result = await call(topic=topic.value)
    for url in all_urls(result):
        parts = urlparse(url)
        assert parts.scheme == "https", url
        assert parts.hostname in ALLOWED_HOSTS, url
        assert not parts.query and not parts.fragment, url
        assert " " not in url, url


@pytest.mark.parametrize("topic", PAGE_TOPICS, ids=lambda t: t.value)
async def test_no_page_is_listed_twice_in_a_topic(topic):
    urls = [p["url"] for p in (await call(topic=topic.value))["pages"]]
    assert len(urls) == len(set(urls))


def test_left_out_pages_are_not_listed():
    """Plan section 3, 'Checked and not used': 404s and the 502 Reports page."""
    urls = [u for _q, pages, _s in TOPICS.values() for _t, u in pages]
    for bad in (
        "mining-claims/forms",
        "maintaining-a-claim",
        "recording-a-claim",
        "reports.blm.gov",
    ):
        assert not any(bad in u for u in urls), bad


@pytest.mark.parametrize("topic", PAGE_TOPICS, ids=lambda t: t.value)
async def test_every_topic_carries_the_page_and_contact_notes(topic):
    notes = (await call(topic=topic.value))["notes"]
    assert notes[0] == PAGES_NOTE
    assert notes[-1] == NOT_COVERED_NOTE


@pytest.mark.parametrize("topic", PAGE_TOPICS, ids=lambda t: t.value)
async def test_fee_notes_only_on_fee_topics(topic):
    notes = (await call(topic=topic.value))["notes"]
    assert (FEES_NOTE in notes) == (topic in FEE_TOPICS)
    has_page_notes = all(n in notes for n in FEE_PAGE_NOTES)
    assert has_page_notes == (topic in {OfficialTopic.FEES, OfficialTopic.RECORDING})
    if not has_page_notes:
        assert not any(n in notes for n in FEE_PAGE_NOTES)


async def test_office_contact_is_a_copy():
    result = await call(topic="staking")
    result["office_contact"]["phone"] = "changed"
    assert OFFICE_CONTACT["phone"] == "406-896-5004"
    direct = topic_pages(OfficialTopic.STAKING, TODAY)
    direct["office_contact"]["email"] = "changed"
    assert OFFICE_CONTACT["email"] == "BLM_MT_SO_Information@blm.gov"


def test_office_contact_is_the_montana_dakotas_state_office():
    assert OFFICE_CONTACT == {
        "name": "BLM Montana/Dakotas State Office",
        "address": "5001 Southgate Drive, Billings, MT 59101",
        "phone": "406-896-5004",
        "email": "BLM_MT_SO_Information@blm.gov",
        "url": "https://www.blm.gov/office/montanadakotas-state-office",
    }


# ── Dollar amounts only in fees (and fee-topic summaries) ────────────────────


@pytest.mark.parametrize("topic", NON_FEE_TOPICS, ids=lambda t: t.value)
async def test_non_fee_topics_have_no_dollar_amounts(topic):
    result = await call(topic=topic.value)
    assert "fees" not in result
    assert "fees_checked" not in result
    for text in all_strings(result):
        assert "$" not in text, text


async def test_list_has_no_dollar_amounts():
    for text in all_strings(await call(topic="list")):
        assert "$" not in text, text


@pytest.mark.parametrize("topic", sorted(FEE_TOPICS), ids=lambda t: t.value)
async def test_fee_topics_have_dollars_only_in_fees_and_summary(topic):
    result = await call(topic=topic.value)
    outside = {k: v for k, v in result.items() if k not in ("fees", "summary")}
    for text in all_strings(outside):
        assert "$" not in text, text


def test_summaries_name_no_amount_missing_from_their_fees():
    """A fee-topic summary may name an amount only if its own fees list has it."""
    for topic, (_q, _p, summary) in TOPICS.items():
        amounts = set(re.findall(r"\$\d[\d,]*", summary))
        if not amounts:
            continue
        assert topic in FEE_TOPICS, topic
        listed = " ".join(FEES[k][1] for k in TOPIC_FEES[topic])
        for amount in amounts:
            assert amount in listed, (topic, amount)


# ── Fees: shape, amounts, checked date ───────────────────────────────────────


@pytest.mark.parametrize("topic", sorted(FEE_TOPICS), ids=lambda t: t.value)
async def test_fee_topics_return_dated_fee_lines(topic):
    result = await call(topic=topic.value)
    assert result["fees_checked"] == "November 15, 2026"
    assert [f["item"] for f in result["fees"]] == [
        FEES[k][0] for k in TOPIC_FEES[topic]
    ]
    for fee in result["fees"]:
        assert set(fee) == {"item", "amount", "per", "due", "text"}
        assert CHECKED_TEXT in fee["text"]
        assert fee["text"].startswith(f"{fee['item']}: {fee['amount']}")
        if fee["per"]:
            assert f"{fee['amount']} {fee['per']} (" in fee["text"]
        if fee["due"]:
            assert fee["text"].endswith(f"Due {fee['due']}.")
        else:
            assert "Due" not in fee["text"]
        assert "  " not in fee["text"]


def test_every_fee_line_carries_the_checked_date():
    for key in FEES:
        assert f"checked {say_date(FEES_CHECKED)}" in fee_entry(key)["text"]


def test_fee_lines_say_which_page_they_come_from():
    for key in FEES:
        text = fee_entry(key)["text"]
        source = (
            "BLM recording page"
            if key in ("abandonment", "address_change")
            else "BLM fees page"
        )
        assert f"({source}, checked" in text, key


def test_fee_amounts_match_the_fees_page_checked_nov_15():
    """Plan section 2a. Change these only after re-reading the fees page."""
    amounts = {key: (row[1], row[2]) for key, row in FEES.items()}
    assert amounts == {
        "new_lode_processing": ("$25", "per claim"),
        "new_lode_location": ("$49", "per claim"),
        "new_lode_maintenance": ("$200", "per claim"),
        "new_lode_total": ("$274", "per claim"),
        "new_placer_processing_location": (
            "$25 processing + $49 location",
            "per claim",
        ),
        "new_placer_maintenance": ("$200", "for each 20 acres or portion"),
        "annual_lode": ("$200", "per claim or site"),
        "annual_placer": ("$200", "for each 20 acres or portion"),
        "small_miner_waiver": ("No fee", ""),
        "affidavit": ("$15", "per claim"),
        "intent_to_hold": ("$15", "per claim"),
        "transfer": ("$15", "per claim per transferee"),
        "amendment": ("$15", "per claim per amendment"),
        "abandonment": ("No fee", ""),
        "address_change": ("No fee", ""),
    }


def test_lode_total_is_the_sum_of_its_parts():
    parts = ("new_lode_processing", "new_lode_location", "new_lode_maintenance")
    total = sum(int(FEES[k][1].lstrip("$")) for k in parts)
    assert f"${total}" == FEES["new_lode_total"][1]


def test_fee_due_dates_match_the_fees_page():
    assert FEES["annual_lode"][3] == "on or before September 1"
    assert FEES["annual_placer"][3] == "on or before September 1"
    assert FEES["small_miner_waiver"][3] == "on or before September 1"
    assert FEES["affidavit"][3] == "on or before December 30"
    assert FEES["intent_to_hold"][3] == "on or before December 30"
    assert "90 days" in FEES["new_lode_total"][3]
    assert "90 days" in FEES["amendment"][3]


def test_maintenance_fee_line_reads_as_planned():
    """The P3 question: September 1 and $200, with the checked date."""
    text = fee_entry("annual_lode")["text"]
    assert text == (
        "Annual maintenance fee, existing lode claim, mill site or tunnel "
        "site: $200 per claim or site (BLM fees page, checked November 15, "
        "2026). Due on or before September 1."
    )


def test_no_fee_line_has_no_dangling_per():
    assert fee_entry("abandonment")["text"] == (
        "Abandonment or relinquishment: No fee (BLM recording page, checked "
        "November 15, 2026)."
    )


def test_checked_dates():
    assert LAST_CHECKED == date(2026, 11, 15)
    assert FEES_CHECKED == date(2026, 11, 15)
    assert FEES_STALE_DAYS == 365


@pytest.mark.parametrize(
    ("d", "text"),
    [
        (date(2026, 11, 15), "November 15, 2026"),
        (date(2027, 1, 5), "January 5, 2027"),
        (date(2026, 9, 1), "September 1, 2026"),
    ],
)
def test_say_date(d, text):
    assert say_date(d) == text


# ── Over-a-year fee note ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "today",
    [
        FEES_CHECKED - timedelta(days=1),  # clock behind the check date
        FEES_CHECKED,
        FEES_CHECKED + timedelta(days=200),
        FEES_CHECKED + timedelta(days=FEES_STALE_DAYS),
    ],
)
def test_no_stale_note_within_a_year(today):
    assert fees_stale_note(today) is None
    notes = topic_pages(OfficialTopic.FEES, today)["notes"]
    assert not any("over a year ago" in n for n in notes)


@pytest.mark.parametrize(
    "today",
    [
        FEES_CHECKED + timedelta(days=FEES_STALE_DAYS + 1),
        date(2028, 1, 1),
        date(2035, 6, 30),
    ],
)
def test_stale_note_after_a_year(today):
    expected = (
        "These fee amounts were last checked on November 15, 2026, over a "
        "year ago. Confirm them on the fees page before quoting them."
    )
    assert fees_stale_note(today) == expected
    for topic in FEE_TOPICS:
        notes = topic_pages(topic, today)["notes"]
        assert expected in notes
        assert notes[-1] == NOT_COVERED_NOTE


@pytest.mark.parametrize("topic", NON_FEE_TOPICS, ids=lambda t: t.value)
def test_non_fee_topics_never_get_the_stale_note(topic):
    notes = topic_pages(topic, date(2035, 1, 1))["notes"]
    assert not any("over a year ago" in n for n in notes)


def _fixed_today(day: date) -> type[date]:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return day

    return FixedDate


async def test_tool_uses_todays_date_for_the_stale_note(monkeypatch):
    monkeypatch.setattr(official_pages, "date", _fixed_today(date(2028, 1, 1)))
    notes = (await call(topic="fees"))["notes"]
    assert any("over a year ago" in n for n in notes)


async def test_tool_has_no_stale_note_today(monkeypatch):
    monkeypatch.setattr(official_pages, "date", _fixed_today(TODAY))
    notes = (await call(topic="fees"))["notes"]
    assert not any("over a year ago" in n for n in notes)


async def test_stale_note_never_changes_the_fee_lines(monkeypatch):
    monkeypatch.setattr(official_pages, "date", _fixed_today(date(2030, 1, 1)))
    fees = (await call(topic="fees"))["fees"]
    assert all(CHECKED_TEXT in f["text"] for f in fees)
    assert all("2030" not in f["text"] for f in fees)


# ── Content the plan relies on ───────────────────────────────────────────────


async def test_where_allowed_says_claims_map_is_not_land_status():
    summary = (await call(topic="where_allowed"))["summary"]
    assert "not whether land is open" in summary
    assert "contact the state office" in summary


async def test_mlrs_account_links_to_mlrs_and_keeps_accounts_private():
    result = await call(topic="mlrs_account")
    assert "https://mlrs.blm.gov" in [p["url"] for p in result["pages"]]
    assert "can't look them up" in result["summary"]


async def test_waiver_topic_links_the_waiver_and_affidavit_forms():
    urls = [p["url"] for p in (await call(topic="small_miner_waiver"))["pages"]]
    assert op.FORM_3830_2 in urls
    assert op.FORM_3830_4 in urls
    assert all(u.endswith(".pdf") for u in urls[1:])


async def test_rules_topic_links_ecfr():
    urls = [p["url"] for p in (await call(topic="rules"))["pages"]]
    assert any(u.endswith("/part-3830") for u in urls)
    assert any(u.endswith("/subpart-3809") for u in urls)


def test_summaries_are_short_enough_to_say():
    for topic, (_q, _p, summary) in TOPICS.items():
        assert 100 < len(summary) < 700, (topic, len(summary))
        assert summary.endswith(".")


# ── No outside calls ─────────────────────────────────────────────────────────


async def test_no_topic_makes_an_http_call(monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the official pages tool must not call out")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    for topic in OfficialTopic:
        result = await call(topic=topic.value)
        assert "error" not in result


async def test_same_topic_gives_the_same_answer():
    assert await call(topic="fees") == await call(topic="fees")
