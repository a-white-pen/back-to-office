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
    - [`aws_monthly`](#table-aws_monthly)
    - [`gemini_triage`](#table-gemini_triage)
    - [`gemini_extraction`](#table-gemini_extraction)
  - [`job_sorting`](#schema-job_sorting)
    - [`job_listings`](#table-job_listings)
    - [`triage_results`](#table-triage_results)
    - [`extraction_results`](#table-extraction_results)

---

## Naming conventions

Keep names plain and understandable.

| Object | Convention | Example |
| :--- | :--- | :--- |
| Schemas | lowercase `snake_case`, named for what the data represents | `costs` |
| Tables | lowercase `snake_case`; for provider-specific cost tables use `{provider}_{use_case}` | `apify_indeed` |
| Columns | lowercase `snake_case` | `apify_run_id` |
| Provider IDs | `{provider}_..._id` | `apify_run_id` |
| Back to Office run IDs | `bto_run_id` | `indeed_sg_20260828_033000_a7f3` |
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
- `costs.aws_monthly`
- `costs.gemini_triage`
- `costs.gemini_extraction`

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

#### Table: `aws_monthly`

**Full name:** `costs.aws_monthly`

**Source:** AWS Cost Explorer, `GetCostAndUsage`

**Grain:** One row per billing month × AWS service × AWS billing record type.

Unlike the Apify tables, this one has no `bto_run_id`. AWS bills by month and by
service, not by collection run, and no run can be attributed a share of it.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `billing_month` | `DATE` | No | First day of the AWS billing month, such as `2026-08-01`. Part of the primary key. |
| `service` | `TEXT` | No | AWS service name exactly as Cost Explorer returns it for the `SERVICE` dimension, such as `Amazon Simple Storage Service`. Part of the primary key. |
| `record_type` | `TEXT` | No | AWS billing record type exactly as Cost Explorer returns it for the `RECORD_TYPE` dimension, such as `Usage`, `Credit`, `Refund` or `Tax`. Part of the primary key. |
| `amount_usd` | `NUMERIC(12,6)` | No | `UnblendedCost` in USD as reported by AWS. Negative for credits and refunds. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this row was written or last refreshed. |

**Primary key:** `(billing_month, service, record_type)`

The money column is `amount_usd`, not `cost_usd` as in the Apify tables. A row
here can be a credit or a refund, which is not a cost. The name still carries the
currency, as the naming conventions require.

##### Cost source

Cost Explorer `GetCostAndUsage` with `Granularity=MONTHLY`,
`Metrics=["UnblendedCost"]`, grouped by the `SERVICE` and `RECORD_TYPE`
dimensions. One request returns every row for one month.

The metric is `UnblendedCost`, not `NetUnblendedCost`. `NetUnblendedCost`
already has credits deducted; storing it alongside the `Credit` rows would
subtract the same credits twice. `UnblendedCost` keeps gross usage and credits
as separate rows, which is what lets this table answer both questions asked of
it.

No price is ever written by hand. If the Lightsail plan changes price, the next
refresh records whatever AWS reports.

##### Sign convention

`amount_usd` keeps the sign AWS reports. `Usage` and `Tax` rows are positive.
`Credit` and `Refund` rows are negative.

Every query against this table depends on that:

- **What is costing money** — filter `record_type = 'Usage'`.
- **What was actually payable** — sum `amount_usd` across all record types.

Never `ABS()` the column, and never sum only the `Usage` rows and call it the
bill.

##### Deriving what was payable

```sql
SELECT billing_month, SUM(amount_usd) AS payable_usd
FROM costs.aws_monthly
GROUP BY billing_month
ORDER BY billing_month;
```

Nothing stores this figure. It is always derived, so it cannot drift from the
components it is derived from.

##### Collection behaviour

- Cost Explorer is the source of truth. Nothing in this table is entered by hand.
- Writes are UPSERT on `(billing_month, service, record_type)`. AWS billing data
  is delayed and is revised after a month closes, so a refresh must overwrite
  rather than insert a second row.
- Each refresh covers the current month and the previous month, so late AWS
  adjustments are picked up. A month closed longer than that is treated as
  settled.
- Cost Explorer charges per request. Those charges arrive on a later AWS bill as
  ordinary usage and are recorded by this table like any other service. They are
  never calculated or inserted separately.
- Cost Explorer can return a group with an empty service key. The collector must
  map that to a non-empty placeholder before writing, because all three primary
  key columns are `NOT NULL`.
- `recorded_at` records when a row was last refreshed, not whether its value
  changed. This table keeps no revision history.

##### Account scope

This table records the whole AWS account, not only Back to Office.
`SERVICE` × `RECORD_TYPE` has no project-allocation dimension, so read a figure
from this table as *the account's* cost, not the project's.

##### Not in scope

Per-bucket S3 attribution, usage-type breakdowns, reporting views, dashboards,
and any further AWS cost table.

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

The nightly spend ceiling includes actual `cost_usd` across both Gemini cost tables; durable private Batch checkpoints also retain outstanding finite billing bounds and accounting gaps. The
morning email can derive cache share from the stored `input_tokens` and
`cached_tokens` counts. Cache-storage charges are not recorded in these
per-call tables.

A call without a returned response/usage record cannot produce one of these
rows. `service_tier` has no CHECK: an unexpected provider value must not prevent
a real cost row from being recorded.

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
| `lacks` | `TEXT[]` | Yes | Required things Belinda lacks under current preferences, in stable/sorted order. Empty array when evaluated with no lacks; NULL when requirements were not evaluated. |
| `years_over_comfortable` | `NUMERIC` | Yes | Difference between the relevant years ask and Belinda's comfortable number, used by Review ordering. NULL when no applicable years result exists or requirements were not evaluated. |
| `preferred_held` | `SMALLINT` | Yes | Number of preferred requirement lines Belinda has, used by Review ordering. NULL when requirements were not evaluated. |
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

Initial population loads all available Silver listing/wording history. Processing
begins at **3:00 AM Singapore time on 9 Oct 2026**
(`2026-10-08T19:00:00Z`), using `wording_first_seen_at`. Earlier rows have
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

**CHECK:** `verdict` allows exactly `not_english`, `one_package`,
`chip_or_electronics_design`, `research_publication`, `pass` and `non_tech`.

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
