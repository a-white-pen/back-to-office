# Data Dictionary: PostgreSQL

PostgreSQL is the operational database for Back to Office. This file documents
only decided contracts; it does not imply that a table has been created. The
[deployment README](../../../../deploy/README.md) owns live server state.

Add schemas and tables only as the project needs them. Do not create placeholder
structures for future features.

## Contents

- **[Naming conventions](#naming-conventions)**
- **[Schemas](#schemas)**
  - [`costs`](#schema-costs)
    - [`apify_indeed`](#table-apify_indeed)
    - [`apify_linkedin`](#table-apify_linkedin)
    - [`apify_find_job_contacts`](#table-apify_find_job_contacts)
    - [`brave_find_direct_link`](#table-brave_find_direct_link)
    - [`brave_find_employer`](#table-brave_find_employer)
    - [`brave_find_people`](#table-brave_find_people)
    - [`brave_look_up_person`](#table-brave_look_up_person)
    - [`gemini_triage`](#table-gemini_triage)
    - [`gemini_extraction`](#table-gemini_extraction)
    - [`hunter_find_job_contacts`](#table-hunter_find_job_contacts)
  - [`job_sorting`](#schema-job_sorting)
    - [`job_listings`](#table-job_listings)
    - [`triage_results`](#table-triage_results)
    - [`extraction_results`](#table-extraction_results)
  - [`job_tracking`](#schema-job_tracking)
    - [`review_status`](#table-review_status)
    - [`direct_application_links`](#table-direct_application_links)
    - [`personnel_searches`](#table-personnel_searches)
    - [`relevant_personnel`](#table-relevant_personnel)
    - [`linkedin_posts`](#table-linkedin_posts)

---

## Naming conventions

Keep names plain and understandable.

| Object | Convention | Example |
| :--- | :--- | :--- |
| Schemas | lowercase `snake_case`, named for what the data represents | `costs` |
| Tables | lowercase `snake_case`; for provider-specific cost tables use `{provider}_{use_case}` | `apify_indeed` |
| Columns | lowercase `snake_case` | `apify_run_id` |
| Provider IDs | `{provider}_..._id` | `apify_run_id` |
| Back to Office run IDs | `bto_run_id` | `example_run_id` |
| Timestamps | end in `_at` | `started_at` |
| Money | include the currency in the name | `cost_usd` |

Do not repeat the schema name in the table name: use `costs.apify_indeed`, not
`costs.apify_indeed_costs`.

---

## Schemas

### Schema: `costs`

**Purpose:** Records the cost of external services used by Back to Office.

Providers and use cases may have different table shapes; they do not need one
universal cost table. A reporting view can combine them later.

Currently defined:

- `costs.apify_indeed`
- `costs.apify_linkedin`
- `costs.apify_find_job_contacts`
- `costs.brave_find_direct_link`
- `costs.brave_find_employer`
- `costs.brave_find_people`
- `costs.brave_look_up_person`
- `costs.gemini_triage`
- `costs.gemini_extraction`
- `costs.hunter_find_job_contacts`

---

#### Table: `apify_indeed`

**Full name:** `costs.apify_indeed`

**Actor:** [`curious_coder/indeed-scraper`](https://apify.com/curious_coder/indeed-scraper)  
**Actor ID:** `qA8rz8tR61HdkfTBL`

**Grain:** One row per Apify Actor run.

Indeed starts one Apify Actor run per search term, so several rows may belong to the same `bto_run_id`.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `apify_run_id` | `TEXT` | No | Apify's unique ID for the Actor run. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office board × market run that started this Actor run. |
| `market` | `TEXT` | No | Market being searched, such as `sg`, `hk` or `th`. |
| `search_term` | `TEXT` | No | Search term used for this Actor run. |
| `started_at` | `TIMESTAMPTZ` | No | Time the Apify Actor run started. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the Apify Actor run finished. NULL while unfinished. |
| `status` | `TEXT` | No | Apify run status. |
| `result_count` | `INTEGER` | Yes | Number of dataset rows returned by the Actor run. |
| `cost_usd` | `NUMERIC(12,6)` | Yes | Run cost in USD reported by Apify as `usageTotalUsd`. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this cost record was written or last refreshed. |

---

#### Table: `apify_linkedin`

**Full name:** `costs.apify_linkedin`

**Actor:** [`cheap_scraper/linkedin-job-scraper`](https://apify.com/cheap_scraper/linkedin-job-scraper)  
**Actor ID:** `2rJKkhh7vjpX7pvjg`

**Grain:** One row per Apify Actor run.

LinkedIn starts one Actor run for all configured search terms in that board × market run, so there is no `search_term` column here.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `apify_run_id` | `TEXT` | No | Apify's unique ID for the Actor run. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office board × market run that started this Actor run. |
| `market` | `TEXT` | No | Market being searched, such as `sg`, `hk` or `th`. |
| `started_at` | `TIMESTAMPTZ` | No | Time the Apify Actor run started. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the Apify Actor run finished. NULL while unfinished. |
| `status` | `TEXT` | No | Apify run status. |
| `result_count` | `INTEGER` | Yes | Number of dataset rows returned by the Actor run. |
| `cost_usd` | `NUMERIC(12,6)` | Yes | Run cost in USD reported by Apify as `usageTotalUsd`. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this cost record was written or last refreshed. |

##### Apify cost source

In both Apify tables, `cost_usd` is the completed run's `usageTotalUsd`.
Run-cost fields can take a few seconds to settle, so an in-progress value is not
final. These tables record run-level Actor cost only; fixed subscriptions and
other account-level charges are excluded.

---

#### Table: `apify_find_job_contacts`

**Full name:** `costs.apify_find_job_contacts`

**Actors:**

| Actor | Actor ID | Used for |
| :--- | :--- | :--- |
| [`harvestapi/linkedin-company-employees`](https://apify.com/harvestapi/linkedin-company-employees) | `Vb6LZkh4EqRlR0Ka9` | People at the company. |
| [`harvestapi/linkedin-profile-scraper`](https://apify.com/harvestapi/linkedin-profile-scraper) | `LpVuK3Zozwuipa5bp` | Profile details, such as the full surname when only its initial is shown. |
| [`harvestapi/linkedin-post-search`](https://apify.com/harvestapi/linkedin-post-search) | `buIWk2uOUzTmcLsuB` | LinkedIn posts about the listing by the company's staff. |
| [`harvestapi/linkedin-post-reactions`](https://apify.com/harvestapi/linkedin-post-reactions) | `S6mgSO5lezSZKi0zN` | Who reacted to a post. |
| [`harvestapi/linkedin-post-comments`](https://apify.com/harvestapi/linkedin-post-comments) | `ZI6ykbLlGS3APaPE8` | Who commented on a post. |

**Grain:** One row per Apify Actor run.

A people search starts several Actor runs, so several rows may belong to the same `bto_run_id`.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `apify_run_id` | `TEXT` | No | Apify's unique ID for the Actor run. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office people search that started this Actor run; matches `job_tracking.personnel_searches.bto_run_id`. |
| `apify_actor_id` | `TEXT` | No | Apify's ID for the Actor that ran, one of the Actor IDs above. |
| `started_at` | `TIMESTAMPTZ` | No | Time the Apify Actor run started. |
| `cost_usd` | `NUMERIC(12,6)` | Yes | Run cost in USD reported by Apify as `usageTotalUsd`. NULL until the finished run's cost has been read. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this cost record was written or last refreshed. |

##### Contact-finding Apify cost source

As in the collection Apify tables, `cost_usd` is the finished run's
`usageTotalUsd`. These Actors charge per event. Apify warns that costs
[may still be preliminary immediately after completion](https://docs.apify.com/api/v2/actor-run-get).
The row is written when the run starts, and `cost_usd` is filled in from a
later read of the finished run. Writes are UPSERT on `apify_run_id`.

---

#### Brave request costs

The four Brave tables below share this grain, column definition and cost source.

**Grain:** One row per Brave Search request whose successful response BTO
received and decoded.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `id` | `BIGINT` | No | Generated internal row identity. Primary key. |
| `cost_usd` | `NUMERIC(12,6)` | No | Calculated USD cost of this Brave request. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time BTO recorded the request's cost. Default `now()`. |

##### Brave cost source

Each Brave table records one API key, so each key's spend can be read on its
own. BTO calculates `cost_usd` at the Search plan's price per request. Prices
can change and are not fixed by this contract.

BTO writes a cost row only after receiving and decoding a successful response.
HTTP errors and answers from BTO's own search cache produce no row. Brave
documents [billing successful, non-error responses](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting).
`cost_usd` does not subtract Brave's free monthly credit. Like the Gemini
tables, these tables carry no run or listing key.

---

#### Table: `brave_find_direct_link`

**Full name:** `costs.brave_find_direct_link`

**Purpose:** Brave Search costs for job-listing research, on `BRAVE_FIND_DIRECT_LINK_API_KEY`.

Uses the [shared Brave request cost contract](#brave-request-costs).

---

#### Table: `brave_find_employer`

**Full name:** `costs.brave_find_employer`

**Purpose:** Brave Search costs for finding a missing company website in contact finding, on `BRAVE_FIND_EMPLOYER_API_KEY`.

Uses the [shared Brave request cost contract](#brave-request-costs).

---

#### Table: `brave_find_people`

**Full name:** `costs.brave_find_people`

**Purpose:** Brave Search costs for finding people at the company by web search in contact finding, on `BRAVE_FIND_PEOPLE_API_KEY`.

Uses the [shared Brave request cost contract](#brave-request-costs).

---

#### Table: `brave_look_up_person`

**Full name:** `costs.brave_look_up_person`

**Purpose:** Brave Search costs for finding a person's phone numbers and online accounts in a full lookup, on `BRAVE_LOOK_UP_PERSON_API_KEY`.

Uses the [shared Brave request cost contract](#brave-request-costs).

---

#### Table: `gemini_triage`

**Full name:** `costs.gemini_triage`

**Purpose:** Gemini call costs for triage.

**Grain:** One row per Gemini triage call for which Google returned a
response with billable usage.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `gemini_response_id` | `TEXT` | No | Google's unique `responseId` for the Gemini response. Primary key. |
| `service_tier` | `TEXT` | No | Gemini billing path: `batch` for a response retrieved from its verified Batch resource; otherwise the service tier Google reports, such as `flex` or `standard`. |
| `input_tokens` | `INTEGER` | No | Non-cached input tokens charged at the normal input price. |
| `cached_tokens` | `INTEGER` | No | Input tokens served from cache and charged at the cached-input price. Store zero when none are reported. |
| `output_tokens` | `INTEGER` | No | Billed output tokens, including thinking tokens where applicable. |
| `cost_usd` | `NUMERIC(12,6)` | No | Calculated USD cost of this Gemini call. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time BTO recorded the returned call's usage and cost. Default `now()`. |

---

#### Table: `gemini_extraction`

**Full name:** `costs.gemini_extraction`

**Purpose:** Gemini call costs for extraction.

**Grain:** One row per Gemini extraction call for which Google returned a
response with billable usage.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `gemini_response_id` | `TEXT` | No | Google's unique `responseId` for the Gemini response. Primary key. |
| `service_tier` | `TEXT` | No | Gemini billing path: `batch` for a response retrieved from its verified Batch resource; otherwise the service tier Google reports, such as `flex` or `standard`. |
| `input_tokens` | `INTEGER` | No | Non-cached input tokens charged at the normal input price. |
| `cached_tokens` | `INTEGER` | No | Input tokens served from cache and charged at the cached-input price. Store zero when none are reported. |
| `output_tokens` | `INTEGER` | No | Billed output tokens, including thinking tokens where applicable. |
| `cost_usd` | `NUMERIC(12,6)` | No | Calculated USD cost of this Gemini call. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time BTO recorded the returned call's usage and cost. Default `now()`. |

##### Gemini cost interpretation

In both Gemini tables, `input_tokens` counts non-cached input and
`cached_tokens` counts cached input. `output_tokens` includes candidate/output
tokens plus thinking tokens where Gemini bills thinking at the output price.
`cost_usd` is calculated from those three counts using the applicable Gemini
prices for the call's service tier. Prices can change and are not fixed by this
contract.

The nightly spend ceiling uses saved receipts plus reserved costs, so failed
database cost writes do not free budget. The morning email can derive cache
share from `input_tokens` and `cached_tokens`. Cache-storage charges are not
recorded in these per-call tables.

A call without a returned response/usage record cannot produce one of these
rows. `service_tier` has no CHECK: an unexpected provider value must not prevent
a real cost row from being recorded.

---

#### Table: `hunter_find_job_contacts`

**Full name:** `costs.hunter_find_job_contacts`

**Purpose:** Hunter credits used by contact finding.

**Grain:** One row per contact-finding request to a Hunter endpoint that can use
credits. Free calls, such as Account and Email Count, get no row.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `id` | `BIGINT` | No | Generated internal row identity. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office people search the request served; for a Look up, the search that found the person. Matches `job_tracking.personnel_searches.bto_run_id`. |
| `endpoint` | `TEXT` | No | Hunter API endpoint called, such as `domain-search`, `email-finder` or `email-verifier`. |
| `credits` | `NUMERIC(8,2)` | Yes | Credits Hunter took for the request. Zero when it took none. NULL when they could not be measured. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time BTO recorded the request's credits. Default `now()`. |

The usage column is `credits`, not `cost_usd`. Hunter is paid by a fixed monthly
plan of credits, so one request has no USD cost of its own. A USD share of the
plan would be an estimate, not a charge, and is not stored.

##### Hunter cost source

BTO measures `credits` from the fall in `requests.credits.remaining` between
free [Account reads](https://hunter.io/api-documentation) just before
and just after each credit-using request. BTO serializes these requests within
each process. Hunter's [credit rules](https://help.hunter.io/en/articles/1911617-how-do-credits-work-in-hunter)
allow repeat requests within the applicable period and searches that find no
address to use no credits; the measured change records that as zero.

---

### Schema: `job_sorting`

**Purpose:** Holds the job-listing wordings and filtering and bucketing results
needed downstream, plus reusable triage and extraction results.

The table contracts are defined below. Documenting them does not create the
tables.

---

#### Table: `job_listings`

**Full name:** `job_sorting.job_listings`

**Purpose:** The cleaned job-listing wordings pulled from Databricks, together
with listing/history information, reusable Gemini-result links, and the
filtering and bucketing result needed downstream.

**Grain:** One row per board listing × processing wording.

A board listing has multiple rows when its processing wording changes. If a
previously seen wording returns, update its existing row rather than create
another row for the same listing × wording.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `id` | `BIGINT` | No | Generated internal row identity. Primary key; `repeat_of_id` points here. |
| `board` | `TEXT` | No | Source board for the listing. |
| `market` | `TEXT` | No | BTO search market in which the listing was collected. |
| `board_job_id` | `TEXT` | No | The board's own listing identifier. |
| `wording_hash` | `TEXT` | No | SHA-256 of the exact listing-input title, first 16,000 JD characters and extraction advertiser line, using the deterministic encoding below. |
| `fingerprint` | `TEXT` | Yes | Cleaning's matching/text identifier. NULL when cleaning cannot produce one, such as for a JD with no usable Latin-script content. |
| `canonical_job_id` | `TEXT` | Yes | Current canonical job group published by cleaning. NULL when grouping was unavailable at pull; fill/refresh only from proven-current grouping. |
| `listing_first_seen_at` | `TIMESTAMPTZ` | No | First night BTO ever saw this board listing in any wording; the same value across its wording rows. |
| `wording_first_seen_at` | `TIMESTAMPTZ` | No | First night BTO saw this particular wording. |
| `wording_last_seen_at` | `TIMESTAMPTZ` | No | Most recent night BTO saw this particular wording. Current wording is determined from this history. |
| `closed_on_board` | `BOOLEAN` | Yes | TRUE means reliable evidence of closure; FALSE means reliable evidence it is open; NULL means closure is unknown. |
| `title` | `TEXT` | Yes | Job title, when available. |
| `description_text` | `TEXT` | Yes | Clean JD used by processing and search. |
| `description_html` | `TEXT` | Yes | JD HTML retained for formatted display in Review; NULL where the board does not supply HTML. |
| `advertiser_name` | `TEXT` | Yes | Advertiser name shown by the board, when available. |
| `hiring_company_name` | `TEXT` | Yes | Separately supplied hiring-company name, when available. |
| `is_agency_posting` | `BOOLEAN` | Yes | Source/board agency signal, distinct from the final `is_recruitment_firm` classification. |
| `job_country_code` | `TEXT` | Yes | Country code for the job's location, when available. |
| `employment_types` | `TEXT[]` | Yes | Board-supplied employment-type labels, when available. |
| `salary_raw` | `TEXT` | Yes | Source salary wording, when available. |
| `salary_min` | `DOUBLE PRECISION` | Yes | Lower salary amount in the supplied currency and period, where available. |
| `salary_max` | `DOUBLE PRECISION` | Yes | Upper salary amount in the supplied currency and period, where available. |
| `salary_currency` | `TEXT` | Yes | Currency of the salary amounts, when available. |
| `salary_period` | `TEXT` | Yes | Salary period, when available. |
| `job_url` | `TEXT` | No | Board listing URL used by Review and as a downstream fallback application link. |
| `board_facts` | `JSONB` | No | Additional board-supplied facts under the keys below. Defaults to `{}`; must be a JSON object. |
| `triage_input_hash` | `TEXT` | Yes | Foreign key to `job_sorting.triage_results(input_hash)`. NULL when there is no stored valid triage result. |
| `extraction_input_hash` | `TEXT` | Yes | Foreign key to `job_sorting.extraction_results(input_hash)`. NULL when there is no stored valid extraction result. |
| `bucket` | `TEXT` | Yes | Final substantive filtering/bucketing bucket; allowed values are listed below. NULL is allowed during processing. |
| `reason` | `TEXT` | Yes | Human-readable result reason. May be NULL when the bucket is sufficient or processing is incomplete. |
| `decided_by` | `TEXT` | Yes | `rules` or `triage`. NULL is valid for Not sorted/no successful decision and during processing. |
| `lacks` | `TEXT[]` | Yes | Required things the owner lacks under current preferences, in stable/sorted order. Empty array when evaluated with no lacks; NULL when requirements were not evaluated. |
| `years_over_comfortable` | `NUMERIC` | Yes | Difference between the relevant years ask and the owner's comfortable number, used by Review ordering. NULL when no applicable years result exists or requirements were not evaluated. |
| `preferred_held` | `SMALLINT` | Yes | Number of preferred requirement lines the owner meets, used by Review ordering. NULL when requirements were not evaluated. |
| `is_recruitment_firm` | `BOOLEAN` | Yes | Final recruitment-firm classification, distinct from `is_agency_posting`. NULL when classification cannot yet be completed. |
| `is_public_sector` | `BOOLEAN` | Yes | Final public-sector classification. NULL when classification cannot yet be completed. |
| `repeat_of_id` | `BIGINT` | Yes | Qualifying wording-row `id` of the earliest qualifying OTHER board listing under the 30-day rule. Another wording of this same `(board, market, board_job_id)` cannot be the target. NULL when not suppressed; the substantive bucket remains unchanged. |

##### Keys and constraints

**Primary key:** `id`

**Unique:** `(board, market, board_job_id, wording_hash)`

**Foreign keys:**

- `triage_input_hash` → `job_sorting.triage_results(input_hash)`
- `extraction_input_hash` → `job_sorting.extraction_results(input_hash)`
- `repeat_of_id` → `job_sorting.job_listings(id)`

**CHECKs:**

- `bucket`, when non-NULL, is one of `top_picks`, `in_lane`, `less_interested`,
  `ineligible`, `not_sorted`, `title_filter`, `non_tech` or `tech_not_for_me`.
- `decided_by`, when non-NULL, is `rules` or `triage`.
- `board_facts` is a JSON object.
- `repeat_of_id <> id`.

**Indexes:** Ordinary indexes on `canonical_job_id` and `advertiser_name`.

##### Board facts

A key is present only where the board supplies its value. These facts remain
inside `board_facts`; they are not separate columns.

| Key | Meaning |
| :--- | :--- |
| `job_location` | Human-readable job location. |
| `posted_date` | Board's posted date. |
| `company_website` | Advertiser's website. |
| `board_company_id` | Board's own ID for the advertiser's company, such as the LinkedIn company ID; only meaningful together with `board`. |
| `company_employee_count` | Advertiser's employee count. |
| `categories` | Board category labels. |
| `job_function` | Board job-function label. |
| `position_levels` | Board position-level labels. |
| `min_years_experience` | Board's structured minimum years of experience. |
| `skills` | Board skill labels. |
| `flexible_work_arrangements` | MCF flexible-work labels for the Flexible work row in Review; not a requirement/rule input. |
| `board_attributes` | Board-supplied attribute labels. |
| `screening_questions` | Employer's screening questions. |
| `contacts` | Contacts supplied in the listing. |
| `poster_name` | Name of the person who posted the listing. |
| `poster_profile_url` | Poster's profile URL. |
| `apply_url` | Source-supplied application destination. |
| `apply_type` | Board's application-type value. |
| `job_source_name` | Source from which the board syndicated the listing. |

##### History and nightly refresh

Initial population loads all available Silver listing/wording history. The private
processing-start setting is compared with `wording_first_seen_at`. Earlier rows have
`bucket = not_sorted` and `reason = "not read: before processing window"`.
They remain available for history/context, are excluded from the normal Review
queue, and are not sent to Gemini or re-sorted when preferences change.

Listing-level last seen is the latest `wording_last_seen_at` across that
listing's rows. Wording history retains distinct versions and their first/last
seen times; it does not retain a complete A → B → A transition-event history.

`wording_hash` is SHA-256 of a UTF-8 compact JSON array, in this order:
`[title_input, jd_input, advertiser_line]`, with `ensure_ascii=False` and
separators `(",", ":")`. These are exact strings used by triage/extraction;
JD input is the first 16,000 characters. The extraction advertiser line includes
", hiring for " and the hiring company only when the board names a different
company. Do not normalise case or whitespace. Neither cleaning's fingerprint/
content hash nor model, thinking level or prompt content/version defines this
wording identity. Gemini result fields stay in their reusable tables.

The normal pull starts from PostgreSQL's represented state/watermark and catches
up missed nights. Its cheap late-write guard checks runs that started before
the represented point but had not finished by it; there is no fixed multi-night
overlap. For listings seen again, refresh latest non-wording facts, including
last seen, salary, employment/job type and `board_facts`. Closure checks also
cover applicable open MCF/JobStreet/JobsDB listings not seen tonight.

Refresh every applicable stored row's `canonical_job_id` only when cleaning's
grouping is proven current, including filling previously NULL values. Do not
limit this refresh to recent rows or guess groups when freshness is unknown.

Changed rule-relevant facts rerun free deterministic rules for processing-window
rows without another Gemini read. Group changes recheck affected suppression
against the fixed `wording_first_seen_at` anchor. History-only rows stay Not
sorted. Rule 1 looks for a different board listing first seen in the previous
30 days before the evaluated wording's first-seen night, under today's same
bucket, reason and lacks. Same-title comparison ignores case and collapses
extra whitespace; it is not fuzzy. `repeat_of_id` targets the earliest
qualifying OTHER listing by `listing_first_seen_at`. Initially unjudged old
history limits suppression; it is not read merely to improve it.

##### Closed on board

For boards where closure can be established reliably, evidence may be the
board explicitly reporting Closed/Expired, or absence from two complete/full
sweeps where that inference is valid. Indeed and LinkedIn absence from search
results is not reliable closure evidence; closure remains unknown unless
another reliable signal exists. NULL must never be interpreted as open.

---

#### Table: `triage_results`

**Full name:** `job_sorting.triage_results`

**Purpose:** Reusable Gemini triage results for identical triage inputs across
listings, boards and nights.

**Grain:** One row per distinct reusable triage input.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `input_hash` | `TEXT` | No | SHA-256 identifying the reusable first-attempt triage input under the identity definition below. Primary key. |
| `profession` | `TEXT` | No | Short English phrase describing the work the person is being hired to do. |
| `quote` | `TEXT` | No | Exact stretch of the title or job description that best shows the profession and supports the verdict. |
| `verdict` | `TEXT` | No | Triage verdict. |

**CHECK:** `verdict` accepts the values configured by the private triage policy.
Setup and runtime validation use that same policy.

---

#### Table: `extraction_results`

**Full name:** `job_sorting.extraction_results`

**Purpose:** Reusable Gemini extraction results for identical extraction inputs
across listings, boards and nights.

**Grain:** One row per distinct reusable extraction input.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `input_hash` | `TEXT` | No | SHA-256 identifying the reusable first-attempt extraction input under the identity definition below. Primary key. |
| `kinds` | `TEXT[]` | No | One to three kinds of work in order, with the main kind first. |
| `lines` | `JSONB` | No | Ordered array of extracted requirement/condition lines using the nine-field extraction contract. Empty array when the listing has no extracted lines. |
| `recruitment_firm` | `BOOLEAN` | No | Gemini's recruitment-firm answer, used where the deterministic employer rules do not already settle it. |
| `public_sector` | `BOOLEAN` | No | Gemini's public-sector answer, used where the deterministic employer rules do not already settle it. |

**CHECK:** `lines` must have a top-level JSONB value that is an array.

`kinds` preserves order because its first value is the main kind. `lines` keeps
the complete checked extraction result together: a listing's requirements are
consumed together and must be reusable when preferences change.

##### Reusable Gemini input identity

Both result tables use SHA-256 of the UTF-8 compact JSON array
`[rendered_fixed_prompt, model, thinking_level, first_attempt_listing_input]`,
with `ensure_ascii=False` and separators `(",", ":")`. The rendered fixed
prompt includes injected preference content. The listing-specific first-attempt
input includes that step's listing block and closing instruction. Changes to
the fixed prompt, injected content, model, thinking level or first-attempt input
change `input_hash`; a retry's appended correction/problems do not. This
identity is separate from listing wording identity and needs no new column.


---

### Schema: `job_tracking`

**Purpose:** Stores my state and activity for jobs after automated sorting.

- `review_status` — current Review state.
- `direct_application_links` — latest job-listing research result.
- `personnel_searches` — each people search and its job-line state.
- `relevant_personnel` — people found by contact finding.
- `linkedin_posts` — LinkedIn posts about a listing, with who reacted and commented.

#### Table: `review_status`

**Full name:** `job_tracking.review_status`

**Purpose:** Remembers which jobs I have read, starred or saved to To Apply.

**Grain:** One current-state row per retained board-listing anchor. An action on
several currently grouped board copies reuses one anchor, not one row per copy.
Groups that were acted on separately may retain several rows after a merge.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `board` | `TEXT` | No | Retained anchor's board. Part of the primary key. |
| `market` | `TEXT` | No | Retained anchor's collection market. Part of the primary key. |
| `board_job_id` | `TEXT` | No | Retained anchor's source listing ID, across its wordings. Part of the primary key. |
| `reviewed_at` | `TIMESTAMPTZ` | Yes | Currently read when populated; NULL means unread. |
| `starred_at` | `TIMESTAMPTZ` | Yes | Currently starred when populated; NULL means unstarred. |
| `saved_to_apply_at` | `TIMESTAMPTZ` | Yes | Currently saved to To Apply when populated; NULL means not saved. |
| `updated_at` | `TIMESTAMPTZ` | No | Last actual state change. Defaults to `now()` and is set by write SQL. |

**Primary key:** `(board, market, board_job_id)`.

**Relationship:** The anchor must identify an existing board listing when state
is created. There is no foreign key to the wording table: its board-listing
identity repeats across wording rows and is not a unique referenced key.
The primary key is the only index. No trigger is required.

**Current state:** State changes use PostgreSQL `now()`. Clearing a state makes
its timestamp NULL; setting it again records a new time. A no-op leaves
`updated_at` unchanged. Keep the row even when all three state timestamps are
NULL, so subsequent actions can reuse the retained anchor. This is not action
history and does not record earlier toggles.

**Sharing and merges:** Resolve each retained anchor through its current board
listing, then its current canonical group. Display an active state if any
anchor in that group has it, with the earliest non-NULL timestamp. Reading a
merged group does not rewrite, delete or consolidate its stored rows. Subsequent
writes reuse the lexicographically first existing anchor; only the first action
on an untouched group chooses the selected board listing. Clearing a shared
state clears that field on every active anchor in the current group, so another
copy cannot keep the state active. Other fields and anchors remain intact.

**Splits:** Stored state follows the resulting group containing its retained
board listing. Do not copy state to every group after a split. A new wording
row does not change the anchor; a reassigned canonical ID does not orphan it.

**Serving:** Read current owner state separately from immutable Review snapshots
and detail caches. Viewer activity, GET requests, filtering and prefetch never
write it. Saved jobs remain available outside Review's date/filter window.

---

#### Table: `direct_application_links`

**Full name:** `job_tracking.direct_application_links`

**Purpose:** Remembers the latest job-listing research result for a prioritised
listing: the company's own job board and the page for this vacancy, shown in the
Links panel.

**Grain:** One row per prioritised listing. A new request updates the search
state. Previous links remain while queued or searching and after failure; a
non-failed result replaces them. This is not search history.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `board` | `TEXT` | No | Retained anchor's board. Part of the primary key. |
| `market` | `TEXT` | No | Retained anchor's collection market. Part of the primary key. |
| `board_job_id` | `TEXT` | No | Retained anchor's source listing ID. Part of the primary key. |
| `status` | `TEXT` | No | State or result of the latest search; allowed values are listed below. |
| `company_job_board_url` | `TEXT` | Yes | The company's own job board. NULL when not found. |
| `vacancy_url` | `TEXT` | Yes | The page for this vacancy on the company's own job board. NULL when not found. |
| `requested_at` | `TIMESTAMPTZ` | No | Time the latest search was asked for: the job was saved to To Apply, or Run now or Search again was pressed. Sets the queue order. |
| `started_at` | `TIMESTAMPTZ` | Yes | Time the latest search started. NULL while queued. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the latest search finished. NULL while queued or searching. |
| `failure_reason` | `TEXT` | Yes | Why the latest search failed, such as `timeout after 30 s`. NULL unless `status` is `failed`. |

**Primary key:** `(board, market, board_job_id)`.

**Foreign key:** `(board, market, board_job_id)` → `job_tracking.review_status(board, market, board_job_id)`

**CHECK:** `status` allows exactly `queued`, `searching`, `resolved`, `partial`,
`unresolved` and `failed`.

**Status:** No row means the listing has not been searched. `resolved` means
both links were found and shows as found. `partial` means one of the two was
found. `unresolved` means the search ran and found neither. `failed` means the
search could not complete, such as when Brave refused the request. A search cut
off by a restart is set to `failed`. Searches run one at a time, in
`requested_at` order.

**Relationship:** The listing key is a retained anchor in
`job_tracking.review_status`, chosen the same way as for a Review read, star or
save. After a merge, show the row with the most recent search. The primary key
is the only index.

---

#### Table: `personnel_searches`

**Full name:** `job_tracking.personnel_searches`

**Purpose:** Remembers each people search for a prioritised listing, for the job
line in the People panel.

**Grain:** One row per people search. Search again adds a row. The job line
shows the listing's latest search, the row with the latest `requested_at`.
Searches run one at a time, in `requested_at` order.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `bto_run_id` | `TEXT` | No | Back to Office's ID for this people search. Primary key; the contact-finding Apify and Hunter cost rows carry it. |
| `board` | `TEXT` | No | Retained anchor's board. |
| `market` | `TEXT` | No | Retained anchor's collection market. |
| `board_job_id` | `TEXT` | No | Retained anchor's source listing ID. |
| `status` | `TEXT` | No | State of the search; allowed values are listed below. |
| `requested_at` | `TIMESTAMPTZ` | No | Time the search was asked for: the job was starred, or Find people, Run now or Search again was pressed. Sets the queue order. |
| `started_at` | `TIMESTAMPTZ` | Yes | Time the search started. NULL while queued. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the search finished. NULL while queued or searching. |
| `failure_reason` | `TEXT` | Yes | Why the search failed, such as `timeout after 30 s`. NULL unless `status` is `failed`. |
| `email_status` | `TEXT` | Yes | Whether email checks could run; allowed values are listed below. NULL until the search reaches its email step. |
| `email_domain` | `TEXT` | Yes | Company email domain used for email checks, such as `example.com`. NULL when none was trusted or the email step has not run. |
| `email_domain_source` | `TEXT` | Yes | Where `email_domain` came from; allowed values are listed below. NULL when `email_domain` is NULL. |
| `email_domains_tried` | `TEXT[]` | Yes | Domains rejected because they carry no known addresses, in the order tried. NULL unless `email_status` is `no_trusted_domain`. |

**Primary key:** `bto_run_id`.

**Foreign key:** `(board, market, board_job_id)` → `job_tracking.review_status(board, market, board_job_id)`

**CHECKs:**

- `status` is one of `queued`, `searching`, `done`, `failed` or
  `recruitment_firm`. `recruitment_firm` means the search stopped because the
  employer is a recruitment firm and kept only the contacts printed on the
  listing. A search cut off by a restart is set to `failed`.
- `email_status`, when non-NULL, is one of `checked`, `no_trusted_domain`,
  `held_back` (too few Hunter credits were left) or `unavailable` (Hunter
  refused or could not be reached).
- `email_domain_source`, when non-NULL, is one of `linkedin_website` (the
  employer website on its LinkedIn listing), `company_job_page` (the company's
  own job board from job-listing research) or `hunter` (Hunter's domain for a
  matching company name).

**Job line:** No row means Not searched. `done` shows Found when the listing
has `relevant_personnel` rows that have a group, or None found when there are
none. "Email checks used" is the sum of `credits` in
`costs.hunter_find_job_contacts` for the row's `bto_run_id`; it is not stored
here.

**Relationship:** The listing key is chosen and read as in
`direct_application_links`. The primary key is the only index.

---

#### Table: `relevant_personnel`

**Full name:** `job_tracking.relevant_personnel`

**Purpose:** People found by contact finding for a prioritised listing, and what
their full lookup found, shown in the People panel.

**Grain:** One row per person found by the listing's latest people search that
ended `done` or `recruitment_firm`; such a search replaces the listing's rows.
Everyone found who works at the company has a row, including someone found only
through a LinkedIn post they wrote, reacted to or commented on.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `id` | `BIGINT` | No | Generated internal row identity. Primary key; Look up and `linkedin_posts` point here. |
| `board` | `TEXT` | No | Retained anchor's board. |
| `market` | `TEXT` | No | Retained anchor's collection market. |
| `board_job_id` | `TEXT` | No | Retained anchor's source listing ID. |
| `person_group` | `TEXT` | Yes | People panel group; allowed values are listed below. NULL for someone found only by reacting to or commenting on a LinkedIn post, who is shown only in the posts section. |
| `name` | `TEXT` | No | Name as found. A half name ends in the surname's initial; no separate flag is stored. A contact printed on the listing without a name uses the printed address or number. |
| `title` | `TEXT` | Yes | Job title or LinkedIn headline, when known. |
| `location` | `TEXT` | Yes | Location, when known. |
| `reason` | `TEXT` | Yes | One-line reason for the person's group, such as `Director-level product title in Singapore`. NULL when none is shown. |
| `provenance` | `TEXT` | No | How the person was found; allowed values are listed below. |
| `linkedin_url` | `TEXT` | Yes | LinkedIn profile link. NULL for a contact printed on the listing without one. |
| `email_address` | `TEXT` | Yes | Email address shown. NULL when none is known. |
| `email_kind` | `TEXT` | Yes | What is known about the address; allowed values are listed below. NULL when nothing is known yet, shown as not looked up. |
| `email_confidence` | `SMALLINT` | Yes | How sure the email check is that the address is right, 0 to 100. NULL when there is no score. |
| `channels` | `JSONB` | No | Ordered array of other ways to reach the person, under the keys below. Defaults to `[]`; empty array when none was found. |
| `looked_up_at` | `TIMESTAMPTZ` | Yes | Last recorded completion time for a full lookup. Automatic lookups record completion even if email checks fail; manual lookups only do so without an email-check failure. NULL until recorded. |
| `failure_reason` | `TEXT` | Yes | Why the latest manual full lookup failed, such as `email check timeout after 30 s`. NULL when no manual lookup failure is recorded. |

**Primary key:** `id`.

**Foreign key:** `(board, market, board_job_id)` → `job_tracking.review_status(board, market, board_job_id)`

**CHECKs:**

- `person_group`, when non-NULL, is one of `named_on_listing`, `recruiter`,
  `hiring_manager_or_team` or `also_at_company`, the People panel's four groups
  in order.
- `provenance` is one of `publicly_observed`, `provider_observed` or
  `provider_inferred`, shown as PUBLICLY OBSERVED, PROVIDER-OBSERVED or
  PROVIDER-INFERRED.
- `email_kind`, when non-NULL, is one of the values in the email-kind table
  below.
- `email_confidence`, when non-NULL, is from 0 to 100.
- `channels` must have a top-level JSONB value that is an array.

**Lookup state:** Failed when `failure_reason` is filled; otherwise done when
`looked_up_at` is filled; otherwise not looked up. The page shows Looking up
while the request runs; it is not stored. Look up runs the full lookup for one
row and fills that row in place.

**Relationship:** The listing key is chosen and read as in
`direct_application_links`. The primary key is the only index.

##### Email kinds

| Value | People panel chip |
| :--- | :--- |
| `valid_mailbox_found` | valid mailbox; a supplied or provider-found address verified by the email service |
| `valid_mailbox_built` | valid mailbox; BTO built the address from the company's pattern |
| `unchecked_seen_online` | mailbox unchecked · seen online |
| `unchecked_our_guess` | mailbox unchecked · our guess |
| `pattern_guess` | pattern guess |
| `no_mailbox` | no mailbox |
| `not_found` | not found |

##### Channel keys

| Key | Meaning |
| :--- | :--- |
| `kind` | `direct_line`, `mobile`, `github`, `x`, `instagram` or `personal_site`. |
| `value` | Number, handle or address shown. |
| `url` | Link to the account or site; null for a phone number, which the page dials from `value`. |
| `attributed` | `true` when evidence ties it to this person; `false` shows "same name, not confirmed". |
| `evidence` | Why it is tied to the person, such as `company @example`, or the page a phone number came from, such as `the company's contact page`; null when none. |

---

#### Table: `linkedin_posts`

**Full name:** `job_tracking.linkedin_posts`

**Purpose:** LinkedIn posts by the company's staff about a prioritised listing,
with who reacted and who commented.

**Grain:** One row per LinkedIn post found by the listing's latest people search
that ended `done` or `recruitment_firm`; a recruitment-firm search finds none.
The rows are replaced together with the listing's `relevant_personnel` rows.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `board` | `TEXT` | No | Retained anchor's board. Part of the primary key. |
| `market` | `TEXT` | No | Retained anchor's collection market. Part of the primary key. |
| `board_job_id` | `TEXT` | No | Retained anchor's source listing ID. Part of the primary key. |
| `post_url` | `TEXT` | No | Link to the post on LinkedIn. Part of the primary key. |
| `author_id` | `BIGINT` | No | Foreign key to `job_tracking.relevant_personnel(id)`: the person who posted it, whose row gives the name, title and profile link shown. |
| `repost_of_name` | `TEXT` | Yes | Original author's name when the post is a repost. NULL otherwise. |
| `posted_at` | `TIMESTAMPTZ` | Yes | Time LinkedIn gives for the post. NULL when it gives none. |
| `post_text` | `TEXT` | Yes | Text of the post. NULL when the post has none. |
| `reacted_by` | `JSONB` | No | Ordered array of the people who reacted, under the keys below. Empty array when none. |
| `commented_by` | `JSONB` | No | Ordered array of the people who commented, under the keys below. Empty array when none. |

**Primary key:** `(board, market, board_job_id, post_url)`.

**Foreign keys:**

- `(board, market, board_job_id)` → `job_tracking.review_status(board, market, board_job_id)`
- `author_id` → `job_tracking.relevant_personnel(id)`

**CHECK:** `reacted_by` and `commented_by` must each have a top-level JSONB value
that is an array.

The reaction and comment counts shown are the number of people in `reacted_by`
and `commented_by`, so they count only the people fetched.

**Relationship:** The listing key is chosen and read as in
`direct_application_links`. The primary key is the only index.

##### Reactor and commenter keys

| Key | Meaning |
| :--- | :--- |
| `name` | Name LinkedIn shows. |
| `headline` | LinkedIn headline. |
| `linkedin_url` | Profile link. |
| `person_id` | `relevant_personnel.id` when the person works at the company; null otherwise. Only these people can be looked up. |

Match people by `person_id`, not by link: the Actors return different link forms
for the same person. A person whose row has a group shows "listed above".
