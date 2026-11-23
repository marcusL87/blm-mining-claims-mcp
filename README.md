# BLM Mining Claims MCP Server

An MCP server that helps Bureau of Land Management (BLM) staff at the **Montana/Dakotas State Office** answer **public questions about mining claims**: callers who read out a claim number, name a mountain or a claim, give a township and range, or ask how to stake a claim or pay a fee.

Built for the GSA MCP Server and SI Agent Government Hackathon from the GSA MCP server template (Python, [FastMCP](https://github.com/jlowin/fastmcp), [uv](https://docs.astral.sh/uv/)). It uses **public data only**, needs **no API keys**, and every tool is **read-only**.

The evaluation report, slide deck and demo video were submitted separately.

---

## Start here

| If you want to see... | Look at |
|---|---|
| The five tools | [`src/mining_claims_mcp/tools/`](src/mining_claims_mcp/tools/) (one file per tool) |
| Typed, validated tool inputs | [`src/mining_claims_mcp/models.py`](src/mining_claims_mcp/models.py) |
| Server setup and the instructions sent to the model | [`src/mining_claims_mcp/app.py`](src/mining_claims_mcp/app.py) |
| Data source settings | [`src/mining_claims_mcp/config.py`](src/mining_claims_mcp/config.py) |
| The evaluation questions | [`eval/questions.md`](eval/questions.md) (24 questions with expected answers) |
| Evaluation results: same model with and without the server | [`eval/agent_luna_2026-11-22.md`](eval/agent_luna_2026-11-22.md) (with the server: 19 of 24 correct, 24 of 24 within scope) and [`eval/baseline_2026-11-22.md`](eval/baseline_2026-11-22.md) (without: 1 of 24, 23 of 24) |
| Earlier results and tool checks | [`eval/results_2026-11-17.md`](eval/results_2026-11-17.md) |
| The tests | [`tests/`](tests/) (598 tests, no network calls) |

---

## The problem

The state office front desk gets many calls about mining claims. Callers often lack the exact serial number. They read old-style serials aloud ("M M C, dash, one five zero zero zero zero"), name a peak or a gulch, name a mine (which often isn't a claim name), or ask how to stake a claim or what the fees are. Answering means switching between BLM's map service, place name lookups and BLM web pages, and it's easy to quote an old fee or a wrong link.

## The solution

Five tools that an AI assistant combines to answer those questions in plain words. Staff just ask; the model chooses the tools.

| Tool | What it answers | Data |
|---|---|---|
| `mining_claims_lookup_by_serial` | "What can you tell me about claim MMC-150000?" Finds a claim by current, legacy (MMC) or partial serial number, however it was typed | BLM mining claims map service (live) |
| `mining_claims_locate_place` | "Where is King Mountain?" Finds a named place and its coordinates, and lists same-named places by county so the model can ask which one | USGS Geographic Names (GNIS) (live) |
| `mining_claims_find_near_location` | "What active claims are within 2 miles of it?" Claims near a point, with counts by status, claim type and mapping quality | BLM mining claims map service (live) |
| `mining_claims_search` | "Is there a claim called Dutchess?" or "What's in T. 29 N., R. 27 W., section 17?" Search by claim name, township/range/section, or both | BLM mining claims map service (live) |
| `mining_claims_official_pages` | "When is the maintenance fee due?" "How do I sell my claim?" Official BLM pages and short summaries for how-to questions, with dated fee amounts | Fixed table in the code, checked against BLM's pages on Nov 15, 2026; no outside calls |

Each claim result gives the claim name, current and legacy serials, status, claim type, a readable legal description (township, range, section, meridian) and how exactly the claim is mapped.

**What the tools won't do, by design:**
- **No claimant names or case documents.** They aren't in the public map service. The tools say so, and point to BLM's MLRS system only for what this data lacks or when a claim can't be found ("ask first, MLRS last": first ask the caller for a serial number, name, township and range, or landmark).
- **"No active claims" never becomes "the land is open."** The claims map doesn't show withdrawals, wilderness or other closed land, so the tools say so and give the state office contact.
- **No made-up links or fees.** How-to answers come only from the official pages table, and every fee amount carries the date the BLM fees page was checked.

## Data sources

| Source | Used by | Freshness |
|---|---|---|
| [BLM Mining Claims map service](https://nlsdbpubgis.blm.gov/arcgis/rest/services/Mining_Claims/MiningClaims/MapServer) (ArcGIS REST, public, no key) | serial lookup, find near, search | Queried live on every call; reflects BLM's current data |
| [USGS Geographic Names Information System](https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer) (public, no key) | locate place | Queried live on every call |
| Official BLM web pages (summarized in [`official_pages.py`](src/mining_claims_mcp/official_pages.py)) | official pages | Links and summaries checked Nov 15, 2026; `scripts/check_official_links.py` re-checks the links |

If BLM's service is in the middle of a reload (it was briefly empty, then partly loaded, during development), a search that finds nothing reports that the service looks incomplete instead of saying "not found". The threshold is `BLM_MIN_TOTAL_RECORDS` in `config.py`.

---

## How it's built

```
src/mining_claims_mcp/
├── app.py              # Builds the FastMCP server and its instructions; picks stdio or HTTP
├── config.py           # Data source URLs and limits (environment variables or .env)
├── models.py           # Typed inputs (enums and validated values) shared by the tools
├── claims.py           # Shared helpers: legal descriptions, claim types, mapping quality
├── official_pages.py   # The official BLM pages table and dated fee lines
├── utils.py            # HTTP helpers with timeouts; the only place outbound calls are made
├── routes.py           # /health and /version for HTTP hosts
└── tools/              # One file per tool, each with register(mcp)
    ├── __init__.py
    ├── serial_lookup.py
    ├── locate_place.py
    ├── find_near_location.py
    ├── search_claims.py
    └── official_pages.py
```

Design choices worth noting:
- **Tools built around staff questions, not around the API.** For example, "claims near a place" is two tools the model chains (locate, then find near), and find-near uses an IDs-only distance query, fetches records by ID and sorts locally, because sorted distance queries on BLM's 4.4 million records time out. The nationwide partial serial search sorts its matches itself for the same reason.
- **Inputs are cleaned and validated before any query.** Serials are normalized (`mmc 114339` becomes `MMC114339`), townships and ranges accept the usual written forms, and coordinates outside the United States are rejected with a hint. Single quotes are escaped in every ArcGIS `where` clause.
- **Tool descriptions are written for the model:** data source, freshness, what each result means, and what to ask next when a search finds nothing or too much.
- **Errors are structured:** every failure returns a dict with an `error` and a `hint`, never a stack trace.
- **Platform-neutral code.** Nothing in `src/` is specific to a hosting platform; host settings live in `app.yaml` and `deploy/`.

---

## Run it

A standard Python MCP server, managed with [uv](https://docs.astral.sh/uv/). **No API keys or configuration are needed.**

```
uv sync
uv run mining-claims-mcp
```

This serves MCP over **stdio**, for any MCP client. When a host sets `PORT` (or `MCP_TRANSPORT=streamable-http`), it serves streamable HTTP at `/mcp` instead, with `/health` and `/version`.

The demo video uses a small local browser page to show each tool call as it happens. It's a development aid and isn't part of this repository.

---

## Evaluation

- **24 questions** in [`eval/questions.md`](eval/questions.md), modeled on the public questions front desk staff answer: messy and legacy serials, ambiguous place names, claim names, township/range/section, mine names that aren't claim names, "can I stake there?", fees, waivers, transfers and MLRS accounts. Each has an expected answer or, for judgment questions, written criteria.
- **Same model with and without the server (Nov 22, 2026).** OpenAI's gpt-6-luna answered all 24 questions twice, with the same staff prompt, one question at a time, never seeing the expected answers:

  | | Without the server | With the server |
  |---|---|---|
  | Correct (has every expected fact) | 1 of 24 (6 partly right) | **19 of 24** (5 partly right) |
  | Within scope (nothing made up) | 23 of 24 | **24 of 24** |
  | Median time per answer | 4.0 s | 5.4 s |

  - **With the server:** [`eval/run_agent_luna.py`](eval/run_agent_luna.py) adds the server's instructions and its five tools (the server runs in the same process, against the live BLM and USGS services). Results: [`eval/agent_luna_2026-11-22.md`](eval/agent_luna_2026-11-22.md). The 5 partial answers had no wrong facts; they left out facts the tools returned (for example that the Dutchess claims are all closed, or the $15 fee for the small miner waiver), and the same model included some of these in earlier checks, so answers vary from run to run. A BLM outage hit questions 17, 18 and 24 during the full run (the model said it couldn't check and made nothing up); they were rerun once the service answered ([`eval/agent_luna_2026-11-22_q8-17-18-24.md`](eval/agent_luna_2026-11-22_q8-17-18-24.md)). The run also found a tool problem: BLM could no longer finish the nationwide serial search sorted (question 8), so the tool now sorts those matches itself; question 8 then passed ([`eval/agent_luna_2026-11-22_q8.md`](eval/agent_luna_2026-11-22_q8.md)).
  - **Without the server:** [`eval/run_baseline.py`](eval/run_baseline.py), the same questions and prompt with no tools. The model was honest but couldn't answer any claim-record question, sent callers to MLRS for facts the tools return, and quoted an outdated maintenance fee ($165; $200 when checked Nov 15, 2026). Results: [`eval/baseline_2026-11-22.md`](eval/baseline_2026-11-22.md).
  - Both runs were graded by Claude against the same written criteria and reviewed by the project author (BLM staff).
- **Second agent run (Nov 17, 2026):** Claude Opus 5.5 in Claude Code, connected to the local server. 24 of 24 answers had the expected facts and stayed within scope; median 6.7 s per answer. Caveat: this agent had seen the expected answers and graded itself, so it is a second result, not the main comparison.
- **Scripted tool checks:** [`eval/run_tool_checks.py`](eval/run_tool_checks.py) sends the tool calls each question needs to the live services and checks the key facts in code. 72 of 72 passed over 3 passes on Nov 17, 2026 (`uv run python eval/run_tool_checks.py --runs 3`).
- **Guardrail cases:** SQL-style input, wildcards, a point outside the U.S., and instruction text as a place name were all rejected or handled safely.
- **Unit tests:** 598 tests in `tests/`, using stand-in service replies (no network), run by CI on every push.
- **Link check:** `scripts/check_official_links.py` opens each link in the official pages table and reports any that don't answer 200 (not part of the tests, which never go online).

Full tables: [`eval/agent_luna_2026-11-22.md`](eval/agent_luna_2026-11-22.md), [`eval/baseline_2026-11-22.md`](eval/baseline_2026-11-22.md) and [`eval/results_2026-11-17.md`](eval/results_2026-11-17.md).

To rerun:

```
uv sync --group dev
uv run pytest tests/ -v
uv run python eval/run_tool_checks.py --runs 3
uv run python eval/run_baseline.py     # needs an OpenAI API key
uv run python eval/run_agent_luna.py   # needs an OpenAI API key
```

---

## Security

- **Public data only.** BLM and USGS public services; no login, no API keys, no internal or controlled data.
- **Read-only.** Every tool is marked read-only and only reads data.
- **No user-supplied URLs.** Service addresses are set only by the operator (`config.py` or environment variables), never by a tool argument.
- **Validated input.** Inputs are checked and cleaned before any query; quotes are escaped in ArcGIS queries; wildcards are rejected in claim names.
- **Certificates always checked** on outbound HTTPS calls.
- **Local HTTP mode** listens on `127.0.0.1` by default, with a Host/Origin check and CORS limited to local addresses.
- **No secrets in the repo.** `.env` is ignored by Git; `.env.example` holds placeholders only.

See [SECURITY.md](SECURITY.md) for GSA's vulnerability disclosure policy.

---

## Deployment

The same code runs anywhere. When a host sets `PORT`, `app.py` serves MCP over streamable HTTP at `/mcp`, with `/health` and `/version`.

- **Databricks Apps:** `app.yaml` (repo root, for Git-based deploys) and [`deploy/databricks/`](deploy/databricks/). An earlier version of the server ran as a Databricks App during development.
- **Containers:** `Dockerfile` (port 8080).
- **cloud.gov:** `manifest.yaml`.

---

## License

[MIT](LICENSE).
