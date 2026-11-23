"""Repeatable tool-level checks for the 24 evaluation questions.

Calls the server's tools through FastMCP's in-memory client (the same
code path an MCP client uses), against the LIVE public BLM and USGS
services, with the tool arguments an agent needs for each question in
eval/questions.md. Each tool call is timed, and each question's key
facts are checked automatically against the tool output.

This checks the tools, not a model's wording: questions graded by a
judge (12, 13, 17, 18, 20, 22, 24) are checked on the tool facts the
answer depends on. The agent-level run is in eval/results_2026-11-17.md.

Usage (from the repo root):
    uv run python eval/run_tool_checks.py            # 3 passes
    uv run python eval/run_tool_checks.py --runs 1

Light on the shared BLM service on purpose: one call at a time.
Writes eval/tool_checks_<date>.json and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mining_claims_mcp.app import mcp  # noqa: E402

Step = tuple[str, dict[str, Any]]
Check = Callable[[list[dict[str, Any]]], list[str]]


def _names(result: dict[str, Any]) -> list[str]:
    return [c["name"] for c in result.get("claims", [])]


def _claim(result: dict[str, Any]) -> dict[str, Any]:
    return result["claims"][0]


def expect(cond: bool, message: str) -> list[str]:
    return [] if cond else [message]


SERIAL = "mining_claims_lookup_by_serial"
LOCATE = "mining_claims_locate_place"
NEAR = "mining_claims_find_near_location"
SEARCH = "mining_claims_search"
PAGES = "mining_claims_official_pages"
ELKHORN = {"latitude": 46.30738, "longitude": -111.9206, "radius_miles": 1}


def q1(r):
    c = _claim(r[0])
    return expect(c["status"] == "Closed", "status not Closed") + expect(
        c["name"] == "DUTCHESS JAN" and c["serial_number"] == "MT103786089",
        "wrong claim",
    )


def q2(r):
    t = _claim(r[0])["legal_description"][0]["text"]
    return expect("T. 6 S., R. 3 W., sec. 27, NW1/4" in t, f"legal desc {t}")


def q3(r):
    return expect(_claim(r[0])["states"] == ["South Dakota"], "state not SD")


def q4(r):
    c = _claim(r[0])
    return expect(
        c["claim_type"] == "Placer claim" and c["name"] == "LINGO #3",
        "not placer LINGO #3",
    )


def q5(r):
    c = _claim(r[0])
    return expect(
        c["serial_number"] == "MT103741508" and c["name"] == "PRAIRIE", "wrong serial"
    )


def q6(r):
    c = _claim(r[0])
    return expect(r[0]["cleaned_serial"] == "MMC150000", "input not cleaned") + expect(
        c["name"] == "ROBINHOOD #1" and c["serial_number"] == "MT103818716",
        "wrong claim",
    )


def q7(r):
    return expect(r[0].get("total_count") == 30, f"count {r[0].get('total_count')}")


def q8(r):
    errs = expect(
        r[0]["match_type"] == "none" and "ALL" in r[0].get("hint", ""),
        "MT search did not hint ALL",
    )
    m = r[1].get("matches", [])
    return errs + expect(
        len(m) == 1
        and m[0]["name"] == "S P 600"
        and m[0]["serial_number"] == "AZ101907298",
        "nationwide search wrong",
    )


def q9(r):
    quarters = {}
    for res in r:
        c = _claim(res)
        quarters[c["name"]] = len(c["legal_description"])
        if c["status"] != "Closed":
            return [f"{c['name']} not Closed"]
    best = max(quarters, key=quarters.get)
    top = sorted(quarters.values())[-2:]
    return expect(
        best == "WILL 55" and quarters[best] == 4 and top[0] < 4, f"{quarters}"
    )


def q10(r):
    notes = " ".join(r[0].get("notes", []))
    return expect(
        "no claimant names" in notes and "certificates of location" in notes,
        "missing not-in-data note",
    )


def q11(r):
    c = r[0]["candidates"]
    errs = expect(
        r[0]["status"] == "single_match" and c[0]["latitude"] == 46.30738,
        "locate not unique",
    )
    return errs + expect(
        r[1]["total_claims"] == 48
        and r[1]["counts"]["claim_types"] == {"Lode claim": 48},
        f"closed count {r[1]['total_claims']}",
    )


def q12(r):
    counties = sorted(tuple(c["counties"]) for c in r[0]["candidates"])
    return expect(
        r[0]["status"] == "multiple_matches"
        and counties == [("Granite", "Powell"), ("Lincoln",), ("Rosebud",)]
        and any("Ask the user which one" in n for n in r[0].get("notes", [])),
        f"{counties}",
    )


def q13(r):
    h = r[0].get("hint", "")
    return expect(
        r[0]["status"] == "no_match" and "spelling" in h and "landmark" in h,
        "no_match/hint",
    )


def q14(r):
    errs = expect(
        r[0]["total_claims"] == 0 and "status all" in r[0].get("hint", ""),
        "active search/hint",
    )
    return errs + expect(r[1]["total_claims"] == 69, f"all {r[1]['total_claims']}")


def q15(r):
    claims = r[0]["claims"]
    expected = sorted(
        [
            "DUTCHESS DREAM",
            "DUTCHESS DREAM II",
            "DUTCHESS DREAM II",
            "DUTCHESS LANDING",
            "DUTCHESS LANDING",
            "DUTCHESS LANDING X",
            "DUTCHESS ANN",
            "DUTCHESS JOAN",
        ]
    )
    ok = (
        sorted(_names(r[0])) == expected
        and all(
            c["status"] == "Closed" and c["claim_type"] == "Lode claim" for c in claims
        )
        and all(
            p["subdivision"] == "SE1/4" for c in claims for p in c["legal_description"]
        )
    )
    return expect(ok, f"{sorted(_names(r[0]))}")


def q16(r):
    c = _claim(r[0])
    parts = c["legal_description"]
    ok = (
        c["serial_number"] == "MT102155937"
        and c["status"] == "Closed"
        and all(
            p["meridian"] == "Black Hills Meridian" and p["section"] == "sec. 10"
            for p in parts
        )
        and sorted(p["subdivision"] for p in parts)
        == sorted(["NW1/4NW1/4", "NE1/4NW1/4", "SW1/4NW1/4", "SE1/4NW1/4"])
    )
    return expect(ok, "GH440 facts")


def q17(r):
    h = r[0].get("hint", "")
    return expect(
        r[0]["total_claims"] == 0
        and "township and range" in h
        and "landmark" in h
        and "MLRS" not in h,
        "Drumlummon hint",
    )


def q18(r):
    errs = expect(r[1]["total_claims"] == 0, f"active {r[1]['total_claims']}")
    errs += expect(r[2]["total_claims"] == 48, f"closed {r[2]['total_claims']}")
    return errs + expect(
        "not whether land is open" in r[3]["summary"], "where_allowed caveat"
    )


def _fee(result, item_start):
    return next(f for f in result["fees"] if f["item"].startswith(item_start))


def _dated(result):
    return all("checked November 15, 2026" in f["text"] for f in result["fees"])


def q19(r):
    lode = _fee(r[0], "Annual maintenance fee, existing lode")
    placer = _fee(r[0], "Annual maintenance fee, existing placer")
    ok = (
        lode["amount"] == "$200"
        and lode["due"] == "on or before September 1"
        and placer["per"] == "for each 20 acres or portion"
        and _dated(r[0])
        and any(p["title"] == "Mining Claim Fees" for p in r[0]["pages"])
    )
    return expect(ok, "fee facts")


def q20(r):
    s = r[0]["summary"]
    ok = all(
        k in s
        for k in (
            "clearly marked",
            "corner monuments",
            "discovery",
            "citizens",
            "agent",
        )
    )
    return expect(
        ok and any(p["title"] == "Staking a Claim" for p in r[0]["pages"]),
        "staking summary",
    )


def q21(r):
    s = r[0]["summary"]
    ok = all(
        k in s
        for k in (
            "10 or fewer",
            "3830-2",
            "September 1",
            "can't be filed online",
            "3830-4",
            "December 30",
        )
    )
    aff = _fee(r[0], "Affidavit of assessment work")
    return expect(ok and aff["amount"] == "$15" and _dated(r[0]), "waiver facts")


def q22(r):
    titles = [p["title"] for p in r[0]["pages"]]
    ok = (
        "Pay.gov" in r[0]["summary"]
        and "MLRS: creating a new account" in titles
        and "MLRS Help Center" in titles
    )
    return expect(ok and "private" in r[0]["summary"], "MLRS account facts")


def q23(r):
    t = _fee(r[0], "Transfer of interest")
    ok = "quitclaim deed" in r[0]["summary"] and "county first" in r[0]["summary"]
    return expect(
        ok
        and t["amount"] == "$15"
        and t["per"] == "per claim per transferee"
        and _dated(r[0]),
        "transfer facts",
    )


def q24(r):
    errs = expect(
        r[0]["total_claims"] == 69 and r[1]["total_claims"] == 0, "Dutchess counts"
    )
    return errs + expect(
        "not whether land is open" in r[3]["summary"], "where_allowed caveat"
    )


QUESTIONS: list[tuple[int, list[Step], Check]] = [
    (1, [(SERIAL, {"serial_number": "mmc 154255"})], q1),
    (2, [(SERIAL, {"serial_number": "MMC200000"})], q2),
    (3, [(SERIAL, {"serial_number": "MT102154255"})], q3),
    (4, [(SERIAL, {"serial_number": "MMC100000"})], q4),
    (5, [(SERIAL, {"serial_number": "MMC1"})], q5),
    (6, [(SERIAL, {"serial_number": "MMC-150000"})], q6),
    (7, [(SERIAL, {"serial_number": "MT1021542"})], q7),
    (
        8,
        [
            (SERIAL, {"serial_number": "1907298"}),
            (SERIAL, {"serial_number": "1907298", "state": "ALL"}),
        ],
        q8,
    ),
    (9, [(SERIAL, {"serial_number": f"MMC{n}"}) for n in range(11428, 11438)], q9),
    (10, [(SERIAL, {"serial_number": "MT101334253"})], q10),
    (
        11,
        [
            (
                LOCATE,
                {
                    "name": "Elkhorn Peak",
                    "state": "MT",
                    "county": "Jefferson",
                    "feature_type": "summit",
                },
            ),
            (NEAR, {**ELKHORN, "status": "closed"}),
        ],
        q11,
    ),
    (12, [(LOCATE, {"name": "King Mountain"})], q12),
    (13, [(LOCATE, {"name": "Quarnstrom Peak"})], q13),
    (
        14,
        [
            (SEARCH, {"claim_name": "Dutchess"}),
            (SEARCH, {"claim_name": "Dutchess", "status": "all"}),
        ],
        q14,
    ),
    (
        15,
        [
            (
                SEARCH,
                {
                    "claim_name": "Dutchess",
                    "township": "29N",
                    "range": "27W",
                    "section": 17,
                    "status": "all",
                },
            )
        ],
        q15,
    ),
    (
        16,
        [
            (
                SEARCH,
                {
                    "claim_name": "GH440",
                    "township": "3S",
                    "range": "3E",
                    "land_state": "SD",
                    "status": "all",
                },
            )
        ],
        q16,
    ),
    (17, [(SEARCH, {"claim_name": "Drumlummon", "status": "all"})], q17),
    (
        18,
        [
            (LOCATE, {"name": "Elkhorn Peak", "state": "MT", "county": "Jefferson"}),
            (NEAR, {**ELKHORN, "status": "active"}),
            (NEAR, {**ELKHORN, "status": "closed"}),
            (PAGES, {"topic": "where_allowed"}),
        ],
        q18,
    ),
    (19, [(PAGES, {"topic": "fees"})], q19),
    (20, [(PAGES, {"topic": "staking"})], q20),
    (21, [(PAGES, {"topic": "small_miner_waiver"})], q21),
    (22, [(PAGES, {"topic": "mlrs_account"})], q22),
    (23, [(PAGES, {"topic": "transfers_and_changes"})], q23),
    (
        24,
        [
            (SEARCH, {"claim_name": "Dutchess", "status": "all"}),
            (SEARCH, {"claim_name": "Dutchess"}),
            (PAGES, {"topic": "staking"}),
            (PAGES, {"topic": "where_allowed"}),
        ],
        q24,
    ),
]


async def run_question(
    client: Client, steps: list[Step], check: Check
) -> dict[str, Any]:
    results, timings = [], []
    for tool, args in steps:
        start = time.perf_counter()
        reply = await client.call_tool(tool, args)
        timings.append(round(time.perf_counter() - start, 2))
        results.append(reply.structured_content or json.loads(reply.content[0].text))
    errors = [r["error"] for r in results if "error" in r]
    if errors:
        failures = [f"tool error: {e}" for e in errors]
    else:
        try:
            failures = check(results)
        except Exception as exc:  # a missing field counts as a failure
            failures = [f"check crashed: {exc!r}"]
    return {
        "passed": not failures,
        "failures": failures,
        "call_seconds": timings,
        "total_seconds": round(sum(timings), 2),
        "tool_calls": len(steps),
    }


async def main(runs: int) -> None:
    started = datetime.now().astimezone()
    record: dict[str, Any] = {
        "started": started.isoformat(timespec="seconds"),
        "runs": [],
    }
    async with Client(mcp) as client:
        for run in range(1, runs + 1):
            print(f"Run {run} of {runs}")
            rows = {}
            for number, steps, check in QUESTIONS:
                rows[number] = await run_question(client, steps, check)
                row = rows[number]
                verdict = "PASS" if row["passed"] else "FAIL"
                print(
                    f"  Q{number:>2} {verdict} {row['total_seconds']:>6.2f} s  "
                    f"{row['call_seconds']}  {row['failures'] or ''}"
                )
            record["runs"].append(rows)
    record["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
    out = Path(__file__).parent / f"tool_checks_{started:%Y-%m-%d}.json"
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print("\nSummary (seconds are total tool time per question)")
    print("Q  | passes | median | min | max")
    for number, _, _ in QUESTIONS:
        rows = [r[number] for r in record["runs"]]
        secs = [r["total_seconds"] for r in rows]
        print(
            f"{number:>2} | {sum(r['passed'] for r in rows)}/{len(rows)} | "
            f"{statistics.median(secs):.2f} | {min(secs):.2f} | {max(secs):.2f}"
        )
    total = sum(r["passed"] for run in record["runs"] for r in run.values())
    print(f"\n{total} of {len(QUESTIONS) * runs} question runs passed. Saved {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    asyncio.run(main(parser.parse_args().runs))
