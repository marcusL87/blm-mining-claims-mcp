# Evaluation Questions: BLM Mining Claims MCP Server

Written test questions for the hackathon submission, with the expected answer for each. They show what the server's five tools can answer for BLM Montana/Dakotas State Office front desk staff, using only BLM's and USGS's public services.

- **Checked live** against the public services between Oct 17 and Nov 15, 2026.
- **Stable answers:** questions use closed claims or fixed facts (state, legal description, claim type), so the answers shouldn't change when claims open or close. Fee answers come from the BLM fees page as checked on November 15, 2026, and change only if BLM changes its fees.
- ***Judge*** marks an answer that describes behavior (for example, asking which place was meant) rather than one exact value; it needs a person or a model to grade it.
- Questions are independent and read-only. A few need several tool calls (for example 9, 11 and 24).
- Format: plain Markdown. They can be converted to the template's Phoenix `evaluation.xml` or the Databricks MLflow list if asked.

Source: the public questions BLM front desk staff answer; each question was checked live against the tools.

## Serial number lookup (`mining_claims_lookup_by_serial`)

| # | Question | Expected answer | What it tests |
|---|---|---|---|
| 1 | A caller asks whether mining claim mmc 154255 is still active. What is its status? | Closed (DUTCHESS JAN, MT103786089) | Status (question 2); messy legacy input |
| 2 | Where is mining claim MMC200000 located? Give the township, range, and section. | T. 6 S., R. 3 W., sec. 27 (NW1/4, Principal Meridian, Montana; CYANYDE #5) | Location (question 1) |
| 3 | Claim MT102154255 has an MT serial number. In which state is the claim actually located? | South Dakota (NW594, Black Hills Meridian) | MT prefix covers the Dakotas, not just Montana |
| 4 | Is the claim recorded under legacy number MMC100000 a lode claim or a placer claim? | Placer (LINGO #3) | Claim type |
| 5 | What is the current MLRS serial number for the claim with legacy number MMC1? | MT103741508 (PRAIRIE) | Legacy to current; short serial that is still an exact match |
| 6 | A caller reads their claim number over the phone as "M M C, dash, one five zero zero zero zero." What is the claim's name? | ROBINHOOD #1 (MMC150000, MT103818716) | Spoken or messy input (question 4) |
| 7 | A claimant remembers only that their serial number starts with MT1021542. How many claims in the Montana/Dakotas office match? | 30 | Partial search with a total count. Stable: the `MT1021542xx` range is fully assigned |
| 8 | A caller has a claim number containing 1907298 but doesn't know which state it's in. What is the claim's name? | S P 600 (AZ101907298, Arizona, Closed) | Following the tool's "search with ALL" hint after no MT match; a long (about 35 s) nationwide call |
| 9 | Legacy serials MMC11428 through MMC11437 are a block of ten consecutive lode claims. Which one's legal description covers the most quarter sections? Give the claim name. | WILL 55 (4 quarter sections; all ten are Closed) | Multi-step: 10 lookups and a comparison |
| 10 | Who is the claimant on MT101334253, and can you send me its Certificate of Location? | Not in this data; look the serial up in MLRS (mlrs.blm.gov) | Out of scope (questions 3 and 5): the model must not invent a name. Answer is a sentence, so it needs a model judge, not exact string matching |

## Places, nearby claims and search (`mining_claims_locate_place`, `mining_claims_find_near_location`, `mining_claims_search`)

| # | Question | Expected answer | What it tests |
|---|---|---|---|
| 11 | How many closed mining claims are within 1 mile of Elkhorn Peak in Jefferson County, Montana? | 48 (all lode claims). GNIS point 46.30738, -111.9206. No active claims within 1 mile, so the count only changes if a new claim is filed and closed there | Chained: locate place (state and county make it unique; there is also an Elkhorn Peak in Lawrence County, SD), then find near, status closed |
| 12 | Are there mining claims near King Mountain? | *Judge:* asks which King Mountain before searching, listing the 3 in the office states by county (Granite/Powell, Lincoln, Rosebud) | Ambiguous place name (from staff feedback) |
| 13 | What mining claims are near Quarnstrom Peak? (made-up name) | *Judge:* says the place wasn't found; suggests checking the spelling or giving a nearby landmark or coordinates. Must not say there are no claims there | No-match place name |
| 14 | Is there a mining claim called Dutchess? | 69 claims with DUTCHESS in the name, all closed, none active (T. 29 N., R. 27 W., Principal Meridian, Montana) | Claim name search (from staff feedback). The default (active) finds 0; the model must follow the hint to status all, not answer "no" |
| 15 | Which Dutchess claims are in section 17 of T. 29 N., R. 27 W.? | 8 closed lode claims, all in the SE1/4: DUTCHESS DREAM, DUTCHESS DREAM II (2 serials), DUTCHESS LANDING (2 serials), DUTCHESS LANDING X, DUTCHESS ANN, DUTCHESS JOAN | Name plus township, range and section (from staff feedback); repeated names told apart by serial |
| 16 | A caller has claim GH440 in T. 3 S., R. 3 E. in South Dakota. What is its status, and which meridian is it on? | Closed (MT102155937); Black Hills Meridian, sec. 10, NW1/4 (four quarter-quarters) | South Dakota search (`land_state` SD, meridian 07); an MT serial for SD land |
| 17 | A caller is asking about claims at the Drumlummon Mine. Can you help? | *Judge:* the name search finds no claim called Drumlummon; the model says a mine name is often not a claim name and asks for a township and range or a nearby landmark (e.g. Marysville). Must not say the mine doesn't exist, and shouldn't send the caller to MLRS first | Mine name (from staff feedback); "ask first, MLRS last" |
| 18 | Someone wants to stake a claim right at Elkhorn Peak in Jefferson County, Montana. Is that ground available? | *Judge:* no active claims with a map shape within 1 mile (48 closed ones), **but** this data can't say whether the land is open to mining (withdrawals, wilderness, private or patented land are not in it), and claims with no map shape don't show up. Must not say "yes, it's available" | Availability (from staff feedback) and the "no active claims is not open to mining" caveat |

## Official BLM pages for how-to questions (`mining_claims_official_pages`)

| # | Question | Expected answer | What it tests |
|---|---|---|---|
| 19 | When is the maintenance fee due and how much is it? | On or before September 1; $200 per lode claim, mill site or tunnel site; placer $200 for each 20 acres or portion; with "checked November 15, 2026" and the BLM Mining Claim Fees link | Topic `fees`; dated amounts |
| 20 | How do I stake a claim? | *Judge:* boundaries clearly marked on the ground, with the state's own corner-monument rules (Montana, or South or North Dakota); check BLM records and look for existing markers first; needs a discovery of a valuable mineral; U.S. citizens, declared intending citizens and corporations can stake, or an agent for them. Gives the Staking a Claim link. No made-up links or rules | Topic `staking`; repeats only the summary |
| 21 | Can I get out of paying the fee? | Small miner waiver: 10 or fewer claims nationwide (with related parties), Form 3830-2 on or before September 1, not online (in person or by mail); then an affidavit of assessment work (Form 3830-4) or notice of intent to hold by December 30, $15 per claim, with the checked date | Topic `small_miner_waiver` |
| 22 | How do I set up an MLRS account? | *Judge:* MLRS (mlrs.blm.gov) is BLM's online system for filing and paying (through Pay.gov); points to the "creating a new account" page and the Help Center; doesn't offer to look up the caller's account or payments | Topic `mlrs_account`; private data stays private |
| 23 | How do I sell my claim to my brother? | A deed such as a quitclaim deed; record it with the county first, then file it with BLM; $15 per claim per transferee, with the checked date | Topic `transfers_and_changes` |
| 24 | Is there a claim called Dutchess, and how would I stake one there? | *Judge:* 69 claims with DUTCHESS in the name, all closed, none active (search with status all); the staking summary; **and** that the claims map alone doesn't show whether the land is open to mining, so check with the state office. Must not say the ground is available | Mixed: search tool plus `staking` and `where_allowed`; the "not open to mining" caveat (from staff feedback) |
