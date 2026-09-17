# Data Dictionary: `bto`

Databricks is the analytical lakehouse, organised in medallion layers:

- **Bronze** — what each job board returned and what each scrape run saw;
- **Silver** — those records reconciled into one common schema;
- **Gold** — aggregated analytical data products.

PostgreSQL is the operational store and currently records cost data.

## Contents

- **[Bronze](#bronze)**
    - Volumes — [`raw_search_results`](#volume-raw_search_results) · [`raw_job_listings`](#volume-raw_job_listings)
    - Tables — [`scrape_runs`](#table-scrape_runs) · [`mcf_job_listings`](#table-mcf_job_listings) · [`seek_job_listings`](#table-seek_job_listings) · [`indeed_job_listings`](#table-indeed_job_listings) · [`linkedin_job_listings`](#table-linkedin_job_listings)
- **[Silver](#silver)**
    - Tables — [`standardized_job_listings`](#table-standardized_job_listings) · [`job_canonical_mapping`](#table-job_canonical_mapping)
- **[Gold](#gold)**

---

## Bronze

Bronze preserves source data as received, with only the transformation needed
to make it queryable and metadata that records ingestion. The two volumes are
the raw landing zone: scraper output
exactly as produced, never edited. The four board tables are derived from them —
each run's search responses in `raw_search_results` say which jobs were seen, and
`raw_job_listings` holds the payload each observation points at. One row per job
observed per run, in that board's own field names, with only the transformations
needed to make the data queryable. A parsing defect is corrected by reprocessing
the raw files, never by editing them.

**Observation grain.** One Bronze row = one source job seen in one board × market run.
A job matching several search terms in the same run is one observation, not several.
The same job seen tomorrow is another observation. Cross-board duplicates stay separate.
Observation history is not repost history: the same job seen on three runs is one
listing seen three times.

**Guaranteed on every observation:** `run_id`, `board`, `market`, `board_job_id`.
Every `run_id` has exactly one `scrape_runs` parent, and that parent carries
`started_at` — observation time is read from the parent, never parsed out of the id.

**The payload invariant, both ways.** `content_hash` names the payload version behind
the observation. When it is NULL the correct payload was not captured, and the
payload-derived columns are NULL alongside it — never back-filled from an older payload
or from the search card. When it is **not** NULL, `raw_job_listings/{content_hash}.json.gz`
exists and the payload-derived columns carry that payload's own projection, whether the
payload was fetched during this run or the row restates an unchanged earlier one. A row
holding a `content_hash` with no projection is a defect, not a state: the run loop treats
such a prior row as fetch-required rather than copying the gap forward.

This invariant holds identically for backfilled history and for forward collection, so
nothing downstream needs to know which era a row came from. One exception is confined to
the raw evidence: LinkedIn observations captured by the retired actor hold that actor's
field names in `raw_job_listings`, and were projected into the current Bronze columns
through documented semantic equivalents (`id`→`jobId`, `title`→`jobTitle`,
`descriptionText`→`jobDescription`, `postedAt`→`publishedAt`, `link`→`jobUrl`,
`companyLinkedinUrl`→`companyUrl`, `applicantsCount`→`applicationsCount`,
`employmentType`→`contractType`, `seniorityLevel`→`experienceLevel`,
`industries`→`sector`, `jobFunction`→`workType`, plus `location`, `companyName`,
`companyLogo` and `applyUrl` unchanged). Current columns that actor never supplied are
NULL. The raw payload bytes were not altered.

**Finding the latest prior observation.** Observation chronology comes from the
parent `scrape_runs.started_at`: join the parent run and use that timestamp for
downstream ordering. `run_id` is an identifier, not a general observation
timestamp, and must not be ordered lexically to infer chronology — `_qNNN` and
`_backfill_*` run ids exist whose text order does not encode time. One scoped
exception: the collection-specific MCF/SEEK prior-state lookup reads the
fixed-format `YYYYMMDD_HHMMSS` embedded in its own tables' run ids for change
detection. That mechanism is specific to that reader, not a general chronology
rule or a downstream ordering rule. **The four-character suffix exists only to
make the id unique and carries no ordering meaning** — never sort or compare on
it.

An observation counts as prior state if it was written, whatever its run's `status`.
A `failed` run does not invalidate the rows it did land. MCF and SEEK change detection
reads that latest observation's `change_signal` and `content_hash`; a NULL
`content_hash` still means the job was seen without a valid payload for that version,
so it stays fetch-required.

### Volumes

#### Volume: `raw_search_results`

* **Unity Catalog Path:** `/Volumes/bto/bronze/raw_search_results`
* **Description:** Preserved search-run artifacts. Source result data is retained without cleaning. MCF and ordinary SEEK-family searches store the board's own responses, one file per term × page; an oversized SEEK AU search also writes a probe and state-partition pages under distinct filename slugs. Indeed and LinkedIn store one envelope per Apify actor run, wrapping the provider dataset with the actor input and run metadata needed to reproduce it.

| File Pattern | Holds |
| :--- | :--- |
| `/{run_id}_{board}_{market}_search_{term}_p{NNN}.json.gz` | **MCF, SEEK family.** One search query label × page. For oversized SEEK AU terms the label distinguishes the probe and each state partition, whose page numbers may repeat. The same job may appear in several files if it matches several searches |
| `/{run_id}_{board}_{market}_search_{term}_actor_run.json.gz` | **Indeed.** One actor run per term — actor input, run metadata and the complete `dataset[]`. Empty results are stored too |
| `/{run_id}_{board}_{market}_search_all_terms_actor_run.json.gz` | **LinkedIn.** One actor run covering all terms. `saveOnlyUniqueItems` requests actor-side uniqueness; observation selection still detects and removes duplicate job ids defensively |
| `/{run_id}_{board}_{market}_jd_fetches.jsonl.gz` | **MCF, SEEK family.** One JSONL line per distinct observed job, recording the run's detail-fetch decision |

Every filename ending in `.gz` is genuinely gzip-compressed.

**Sample File Shape**

| Board | Contents | Paging |
| :--- | :--- | :--- |
| **MCF** | `{ total, results: [ {uuid, title, postedCompany{…}, metadata{…}}, … ×100 ], _links }` | 100 rows per requested page, numbered from `p000`, with the page number only in the `_links` URLs |
| **SEEK family** | `{ totalCount, data: [ {id, title, companyName, salaryLabel, listingDate, teaser, …}, … ×100 ], searchParams, solMetadata, … }` | 100 rows per requested page, numbered from `p001`; `searchParams.page` states the page and `searchParams.pageSize` is null, so the size is the one requested |
| **Indeed** | `{ board, term, query_key, actor, input, run, provider, dataset: [ {id, title, jobDescription, …} ] }` | None — one file per term |
| **LinkedIn** | `{ board, terms[], input, actor, run, pricing, provider, dataset: [ {jobId, jobTitle, jobDescription, …} ] }` | None — one file per run |

Duplicates across files are expected for MCF, SEEK and Indeed, because one job matches
several searches and every response is kept. LinkedIn requests actor-side deduplication,
and observation selection still handles duplicate ids defensively.

**Sample `jd_fetches` Line**

`run_id`, `board` and `market` come from the filename.

| Field | Meaning |
| :--- | :--- |
| `board_job_id` | The job the decision was about |
| `change_signal` | The signal its search card carried in this run |
| `fetch_outcome` | `fetched` — a full JD was captured · `unchanged` — an existing payload version was reused · `deferred` — the full-JD fetch remains in the durable backlog · `failed` — the selected full-JD fetch did not land |
| `content_hash` | The payload version, or NULL when none was captured |

---

#### Volume: `raw_job_listings`

* **Unity Catalog Path:** `/Volumes/bto/bronze/raw_job_listings`
* **Description:** One file per distinct full-payload version, content-addressed by `content_hash`. MCF and SEEK payloads are the detail response the board returned; Indeed and LinkedIn payloads are the actor row from the search result, canonically reserialized. Nothing is cleaned or enriched.

| File Pattern | Holds |
| :--- | :--- |
| `/{content_hash}.json.gz` | One distinct payload version. Observations from many runs may point at the same file |

A refetch only creates a new file if the returned bytes hash to a previously unseen
`content_hash`. A changed change-signal does **not** by itself mean a new file.

Every filename ending in `.gz` is genuinely gzip-compressed.

**Sample File Shape**

| Board | Contents |
| :--- | :--- |
| **MCF** | `{ uuid, title, description, postedCompany{}, hiringCompany{}, salary{}, metadata{}, categories[], employmentTypes[], positionLevels[], skills[], address{}, status{} }` |
| **SEEK family** | `{ data: { jobDetails: { job: {id, title, content, advertiser{}, salary, listedAt{}, expiresAt{}, status, …}, companyProfile{}, gfjInfo{} } } }` |
| **Indeed** | `{ id, title, jobDescription, jobDescriptionHTML, companyDetails{}, salary{}, location{}, attributes[], occupations[], pubDate, trackingKey }` |
| **LinkedIn** | `{ jobId, jobTitle, jobDescription, companyName, companyId, sector, companyAddress{}, experienceLevel, yearsOfExperience[], publishedAt, searchString }` |

> **Stored value recipe — `content_hash`**
> SHA-256 of the **uncompressed** payload bytes, hex-encoded. Gzip happens after hashing.
> * MCF, SEEK family — the detail HTTP response body exactly as received, unaltered.
> * Indeed, LinkedIn — the selected actor row, serialized as
>   `json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')`.
>   Volatile keys such as Indeed's `trackingKey` are included.

---

### Tables

**Contract metadata.** “Logical Key” and required (`Nullable: NO`) values are
application-contract and build-validation invariants. Delta does not declare
them as `PRIMARY KEY` or `NOT NULL` constraints unless explicitly stated.

#### Table: `scrape_runs`

* **Fully Qualified Name:** `bto.bronze.scrape_runs`
* **Medallion Layer:** Bronze
* **Grain:** One row per board × market execution
* **Logical Key:** `run_id`
* **Description:** One row per board × market execution: what ran, when, how much it found and fetched, and how it ended. **Not append-only** — the row is inserted as `running` at the start and updated in place when the run finishes, so a row still marked `running` is durable evidence of a run that never completed.

##### Columns

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `run_id` | `STRING` | NO | Opaque unique identifier. Normal forward runs use `{board}_{market}_{YYYYMMDD_HHMMSS}_{suffix}` — e.g. `linkedin_sg_20260828_091530_a7f3`, with an SGT timestamp and four random hex characters — while historical `_qNNN` and `_backfill_*` variants also exist. Chronology comes from `started_at`, never lexical `run_id` order. |
| `board` | `STRING` | NO | Board slug: `mcf` · `jobstreet` · `jobsdb` · `seek` · `indeed` · `linkedin`. The market is not part of it — see `market`. |
| `market` | `STRING` | NO | Market being searched, e.g. `sg`, `hk`, `th`, `au`, `nz`, `uk`, `us`, `ca`. |
| `started_at` | `TIMESTAMP` | NO | UTC. Written when the `running` row is inserted. |
| `finished_at` | `TIMESTAMP` | YES | UTC. Written when the row reaches a terminal status. NULL while `running`, including permanently when a run crashed hard. |
| `status` | `STRING` | NO | `running` started, not yet finished · `ok` completed with the expected usable collection evidence · `partial` completed but collection coverage or evidence was incomplete or not fully satisfactory · `failed` did not achieve a usable successful completion, including on a fatal run-level failure. A row left at `running` with a NULL `finished_at` is a crashed run, and stays that way as evidence. |
| `terms_swept` | `INT` | YES | Search terms executed. |
| `terms_succeeded` | `INT` | YES | Number of search terms completed successfully. Succeeded means the term ran without error; a term that returned zero results is a success. |
| `unique_seen` | `INT` | YES | Distinct job ids seen across all terms. |
| `new_jobs` | `INT` | YES | Ids never seen before. |
| `changed_jobs` | `INT` | YES | Known ids whose board change signal moved. |
| `jds_intended` | `INT` | YES | Number of full-JD fetches selected for this run, bounded by the configured per-run cap (default 5,000). |
| `jds_fetched` | `INT` | YES | Selected full-JD fetches that landed. |
| `fetch_failures` | `INT` | YES | Selected full-JD fetches that did not land. |
| `backlog_remaining` | `INT` | YES | Fetches deferred past the configured per-run cap and carried forward. |
| `term_results` | `STRING` | YES | JSON: per-term total, pages and hits. Calibration data. |
| `full_sweep` | `BOOLEAN` | YES | TRUE when the run covered the complete intended board search space. Independent of `status` — a run can finish cleanly and still not have swept everything. |
| `error_reason` | `STRING` | YES | Short cause for a `partial` or `failed` run. |
| `run_trigger` | `STRING` | YES | `scheduled` · `manual` · `recovery` · `backfill`. |

#### Table: `mcf_job_listings`

* **Fully Qualified Name:** `bto.bronze.mcf_job_listings`
* **Medallion Layer:** Bronze
* **Grain:** One row per MCF job observed per scrape run
* **Logical Key:** `(run_id, board, market, board_job_id)`
* **Description:** MyCareersFuture job listings for Singapore. Except for the observation-level `change_signal`, columns after `content_hash` use MCF's own field names and come from the JD payload.

##### Columns

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `board` | `STRING` | NO | Always `mcf`. Logical-key part 2. |
| `market` | `STRING` | NO | Always `sg`. Logical-key part 3. `address.isOverseas` marks a role sited abroad. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 4. Same value as `uuid`. |
| `run_id` | `STRING` | NO | The scrape run this observation belongs to. FK to `scrape_runs.run_id`, which holds the run's `started_at` — join for observation time rather than storing it again. Logical-key part 1. |
| `content_hash` | `STRING` | YES | The payload version this observation points at, and the filename stem in `raw_job_listings`. Repeats across observations while the payload is unchanged. NULL when the correct payload was not captured — never a sign the job was absent, new or reposted. |
| `change_signal` | `STRING` | YES | The search card's change signal for this observation — MCF's `metadata.updatedAt`, opaque. Observation provenance, not payload: it is carried independently of the full payload and may remain populated when `content_hash` is NULL; it is what the next run compares against. |
| `uuid` | `STRING` | YES | MCF's job id inside the payload. Same value as `board_job_id`. |
| `title` | `STRING` | YES |  |
| `description` | `STRING` | YES | The JD, HTML as MCF serves it. |
| `postedCompany.uen` | `STRING` | YES | Singapore UEN of the advertiser — a company registry number. |
| `postedCompany.name` | `STRING` | YES | The advertiser — employer or agency, undistinguished. |
| `postedCompany.ssicCode2020` | `STRING` | YES | SSIC 2020 industry code for the advertiser. `78104` is *employment agencies*. |
| `postedCompany.ssicDescription2020` | `STRING` | YES | Industry name for `ssicCode2020`. Often absent; the code is the field of record. |
| `postedCompany.description` | `STRING` | YES | The company's own blurb, HTML as MCF serves it. |
| `postedCompany.companyUrl` | `STRING` | YES | The advertiser's own site, as typed by the poster. Not normalized. |
| `postedCompany.logoUploadPath` | `STRING` | YES | MCF's board-hosted employer logo URL. |
| `postedCompany.employeeCount` | `INT` | YES | Company headcount. Rarely populated. |
| `postedCompany.responsiveEmployer.isResponsive` | `BOOLEAN` | YES | MCF's badge for employers who reply to applicants. |
| `hiringCompany.name` | `STRING` | YES | The end employer when an agency posted. Rare. |
| `hiringCompany.uen` | `STRING` | YES | The end employer's UEN. Rare. |
| `metadata.updatedAt` | `STRING` | YES | MCF's last-updated stamp inside the payload. The observation-level copy used for change detection is `change_signal`. |
| `metadata.editCount` | `INT` | YES | How many times the employer has edited the posting. |
| `metadata.jobPostId` | `STRING` | YES | MCF's human-facing id, e.g. `MCF-2026-0878867`. |
| `metadata.jobDetailsUrl` | `STRING` | YES | The canonical posting URL. |
| `metadata.newPostingDate` | `DATE` | YES | The posted date MCF displays. |
| `metadata.originalPostingDate` | `DATE` | YES | When the ad first went up. Differs from `newPostingDate` on reposts. |
| `metadata.expiryDate` | `DATE` | YES | The board's stated closing date. |
| `metadata.repostCount` | `INT` | YES | How many times the ad has been recycled. |
| `metadata.isPostedOnBehalf` | `BOOLEAN` | YES | Agency flag. The boolean form of the `hiringCompany` test. |
| `metadata.isHideSalary` | `BOOLEAN` | YES | The employer withheld pay, which tells a withheld salary apart from an unstated one. |
| `metadata.isHideEmployerName` | `BOOLEAN` | YES | The employer posted anonymously, so `postedCompany.name` may not be the recognisable name. |
| `metadata.isHideCompanyAddress` | `BOOLEAN` | YES | The address block is suppressed, which is why the address fields can be empty. |
| `metadata.deletedAt` | `STRING` | YES | When the listing was last taken down. **Historical only — it does not mean the listing is closed now.** A live `Re-open` listing may carry one; read current state from `status.jobStatus`. |
| `metadata.totalNumberOfView` | `INT` | YES | Board-reported view count. |
| `metadata.totalNumberJobApplication` | `INT` | YES | Board-reported application count. |
| `status.jobStatus` | `STRING` | YES | MCF's current listing status: `Open`, `Re-open` or `Closed`. |
| `salary.minimum` | `DOUBLE` | YES |  |
| `salary.maximum` | `DOUBLE` | YES |  |
| `salary.type` | `STRUCT<id INT, salaryType STRING>` | YES | Salary period, e.g. `{"id": 4, "salaryType": "Monthly"}`. |
| `minimumYearsExperience` | `INT` | YES | A structured integer, not parsed from prose. |
| `numberOfVacancies` | `INT` | YES |  |
| `ssocCode` | `STRING` | YES | Singapore Standard Occupational Classification code for the role. Read it together with `ssocVersion`. |
| `ssocVersion` | `STRING` | YES | The SSOC version `ssocCode` belongs to, e.g. `2020v3`. A code means nothing without its version. |
| `occupationId` | `STRING` | YES | MCF's board-native occupation identifier. More granular than `ssocCode`. |
| `ssecEqa` | `STRING` | YES | SSEC code for the qualification the role asks for, e.g. `70`, `51`, or `NR` where none is coded. Decoding needs the SSEC 2020 reference. |
| `ssecFos` | `STRING` | YES | SSEC code for the field of study, e.g. `0919`. Pairs with `ssecEqa`. |
| `categories` | `ARRAY<STRUCT<id INT, category STRING>>` | YES | MCF's category taxonomy. The `id` survives a label rename. |
| `employmentTypes` | `ARRAY<STRUCT<id INT, employmentType STRING>>` | YES | e.g. `Full Time`, `Contract`. A listing may carry several. |
| `positionLevels` | `ARRAY<STRUCT<id INT, position STRING>>` | YES | e.g. `Executive`, `Manager`. A listing may carry several. |
| `skills` | `ARRAY<STRUCT<skill STRING, uuid STRING, isKeySkill BOOLEAN, confidence DOUBLE>>` | YES | SSG skills-framework tags. `isKeySkill` marks the ones the employer weights; `uuid` is the stable skill key. `confidence` is currently unpopulated. |
| `screeningQuestions` | `ARRAY<STRUCT<question STRING>>` | YES | The employer's own questions to the candidate. |
| `flexibleWorkArrangements` | `ARRAY<STRUCT<id INT, flexibleWorkArrangement STRING>>` | YES | MCF's flexible-work signal: `Telecommuting`, `Flexi-Hours`, `Compressed Work Schedule`, `Staggered Time` and similar. |
| `address.postalCode` | `STRING` | YES | Empty when `metadata.isHideCompanyAddress` is set. |
| `address.block` | `STRING` | YES | Street-address parts, empty when the address is hidden. |
| `address.street` | `STRING` | YES |  |
| `address.building` | `STRING` | YES |  |
| `address.lat` | `DOUBLE` | YES | Workplace latitude. |
| `address.lng` | `DOUBLE` | YES | Workplace longitude. |
| `address.districts` | `ARRAY<STRUCT<id INT, location STRING, region STRING, regionId STRING, sectors ARRAY<STRING>>>` | YES | MCF's area taxonomy, e.g. `D14 Geylang, Eunos` in region `Central`. `Islandwide` (id 998) means no area was named. |
| `address.isOverseas` | `BOOLEAN` | YES | Marks a role sited outside Singapore. |

#### Table: `seek_job_listings`

* **Fully Qualified Name:** `bto.bronze.seek_job_listings`
* **Medallion Layer:** Bronze
* **Grain:** One row per SEEK-family job observed per scrape run
* **Logical Key:** `(run_id, board, market, board_job_id)`
* **Description:** SEEK-family job listings from SEEK, JobStreet and JobsDB. All five markets share one JSON shape, so the board is a column rather than five tables. Except for the observation-level `change_signal`, columns after `content_hash` use SEEK's own field names and come from the JD payload.

##### Columns

Fields live under `data.jobDetails` in the source file; the paths below are given
relative to it.

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `board` | `STRING` | NO | `seek` · `jobstreet` · `jobsdb`. Logical-key part 2. |
| `market` | `STRING` | NO | `au` · `nz` · `sg` · `hk` · `th`. Logical-key part 3. Only five pairings are swept: SEEK in AU and NZ, JobStreet in SG, JobsDB in HK and TH. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 4. Same value as `job.id`. |
| `run_id` | `STRING` | NO | The scrape run this observation belongs to. FK to `scrape_runs.run_id`, which holds the run's `started_at` — join for observation time rather than storing it again. Logical-key part 1. |
| `content_hash` | `STRING` | YES | The payload version this observation points at, and the filename stem in `raw_job_listings`. Repeats across observations while the payload is unchanged. NULL when the correct payload was not captured — never a sign the job was absent, new or reposted. |
| `change_signal` | `STRING` | YES | The search card's change signal for this observation — the 14-field fingerprint. Observation provenance, not payload: it is present even when `content_hash` is NULL, and is what the next run compares against. |
| `job.id` | `STRING` | YES | SEEK's job id inside the payload. Same value as `board_job_id`. |
| `job.title` | `STRING` | YES |  |
| `job.content` | `STRING` | YES | The JD, HTML as SEEK serves it. |
| `job.abstract` | `STRING` | YES | SEEK's teaser line — roughly one sentence, not a plain-text rendering of `content`. |
| `job.status` | `STRING` | YES | SEEK's current listing status: `Active` or `Expired`. Preferred over `isExpired`, which is absent from stub records. |
| `job.isExpired` | `BOOLEAN` | YES | Boolean equivalent of `status`. |
| `job.listedAt.dateTimeUtc` | `TIMESTAMP` | YES | The board's posted date. |
| `job.expiresAt.dateTimeUtc` | `TIMESTAMP` | YES | The board's stated closing date. |
| `job.advertiser` | `STRUCT<id STRING, name STRING>` | YES | The poster — agency or employer, undistinguished. `id` is stable across name changes. |
| `companyProfile` | `STRUCT<id STRING, name STRING>` | YES | Present only where the advertiser maintains a SEEK company page. A different key space from `advertiser.id`. |
| `job.location` | `STRUCT<label STRING>` | YES | The job's location as displayed, e.g. `Hong Kong Island, HK`. SEEK gives no coordinates or postcode. |
| `gfjInfo.location.countryCode` | `STRING` | YES | The country as the row states it, rather than as `market` implies. Absent on stub records. |
| `job.workTypes` | `STRUCT<label STRING>` | YES | SEEK's display taxonomy, e.g. `Full time`, `Contract/Temp`. One string, which may carry several comma-separated values. |
| `gfjInfo.workTypes` | `STRUCT<label ARRAY<STRING>>` | YES | The Google-for-Jobs taxonomy, e.g. `["FULL_TIME"]`. A different vocabulary from `workTypes`; both are kept. |
| `job.classifications` | `ARRAY<STRUCT<label STRING>>` | YES | SEEK's category taxonomy. The source gives a label only. |
| `job.salary` | `STRUCT<label STRING, currencyLabel STRING>` | YES | Free text as SEEK gives it. Often prose rather than a range, so it is not a parsed amount. |
| `job.products.questionnaire` | `STRUCT<questions ARRAY<STRING>>` | YES | Employer screening questions asked of the candidate, e.g. *"How many years' experience do you have as a systems engineer?"*. Question text only — no ids, types or answer options. |
| `job.products.bullets` | `ARRAY<STRING>` | YES | The employer's selling points from the listing card. Marketing lines, not requirements. |
| `job.contactMatches` | `ARRAY<STRUCT<type STRING, value STRING>>` | YES | Contact details SEEK found in the JD text, each value paired with its `type` — `Email` or `Phone`. |
| `job.sourceZone` | `STRING` | YES | The SEEK platform region serving the listing, e.g. `asia-7`, `anz-1`. |
| `job.isVerified` | `BOOLEAN` | YES | SEEK's verified-advertiser flag. |
| `job.phoneNumber` | `STRING` | YES | Currently NULL; contact numbers are supplied in `contactMatches` instead. |

**A small share of records are minimal stubs**, carrying only `id`, `title`, `content`, `status`,
`listedAt` and `expiresAt`, with no `companyProfile` or `gfjInfo` block. Everything outside those six
is NULL on those rows — the source's own shape, not a parser failure.

> **Stored value recipe — SEEK `change_signal`**
> `stable = {field: hit.get(field) for field in CARD_SIGNAL_FIELDS}`, serialized as
> `json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')`,
> then SHA-256. The signal is `f"{listingDate}|{sha256_hexdigest}"`.
> `CARD_SIGNAL_FIELDS` = `listingDate`, `title`, `advertiser`, `companyName`, `salaryLabel`,
> `locations`, `classifications`, `teaser`, `workTypes`, `workArrangements`, `bulletPoints`,
> `tags`, `roleId`, `displayType`. An absent field participates as JSON `null`, so every
> fingerprint covers all fourteen keys.

#### Table: `indeed_job_listings`

* **Fully Qualified Name:** `bto.bronze.indeed_job_listings`
* **Medallion Layer:** Bronze
* **Grain:** One row per Indeed job observed per scrape run
* **Logical Key:** `(run_id, board, market, board_job_id)`
* **Description:** Indeed job listings collected via Apify. Columns below `content_hash` are Indeed's own field names, taken from the actor row.

##### Columns

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `board` | `STRING` | NO | Always `indeed`. Logical-key part 2. |
| `market` | `STRING` | NO | `sg` · `hk` · `th` · `au` · `nz` · `uk` · `us` · `ca`. Logical-key part 3. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 4. Same value as `id`. |
| `run_id` | `STRING` | NO | The scrape run this observation belongs to. FK to `scrape_runs.run_id`, which holds the run's `started_at` — join for observation time rather than storing it again. Logical-key part 1. |
| `content_hash` | `STRING` | YES | The payload version this observation points at, and the filename stem in `raw_job_listings`. Repeats across observations while the payload is unchanged. NULL when the correct payload was not captured — never a sign the job was absent, new or reposted. |
| `id` | `STRING` | YES | Indeed's job id inside the payload. Same value as `board_job_id`. |
| `title` | `STRING` | YES |  |
| `jobDescription` | `STRING` | YES | The JD as plain text. |
| `jobDescriptionHTML` | `STRING` | YES | The same JD with formatting kept. |
| `salary.min` | `DOUBLE` | YES |  |
| `salary.max` | `DOUBLE` | YES |  |
| `salary.currencyCode` | `STRING` | YES | The currency as the row states it: `SGD`, `HKD`, `THB`, `USD`, `CAD`, `GBP`, `AUD` or `NZD`. |
| `salary.type` | `STRING` | YES | The salary period: `MONTH`, `YEAR`, `HOUR`, `WEEK` or `DAY`. An amount is meaningless without it. |
| `location.countryCode` | `STRING` | YES | The job's country as the row states it, which may differ from `market`. |
| `location.countryName` | `STRING` | YES | The same country spelled out. |
| `location.city` | `STRING` | YES |  |
| `location.postalCode` | `STRING` | YES |  |
| `location.streetAddress` | `STRING` | YES |  |
| `location.fullAddress` | `STRING` | YES | The fullest address string Indeed gives. |
| `location.formatted.long` | `STRING` | YES | Human-readable location. |
| `location.formatted.short` | `STRING` | YES | The abbreviated form. |
| `location.latitude` | `DOUBLE` | YES |  |
| `location.longitude` | `DOUBLE` | YES |  |
| `location.admin1Code` | `STRING` | YES | State or region code. The matching source `admin*Name` fields are currently empty. |
| `jobLocationCity` | `STRING` | YES | A top-level copy of `location.city`. |
| `formattedLocation` | `STRING` | YES | A top-level display string, sometimes only the country. |
| `companyDetails.name` | `STRING` | YES | The employer. |
| `companyDetails.employeeRange` | `STRING` | YES | Company size band. |
| `companyDetails.industry` | `STRING` | YES | The employer's industry. Often absent. |
| `companyDetails.sectorNames` | `ARRAY<STRING>` | YES | Indeed's internal sector enum, e.g. `["Iv1_HEALTH_CARE"]`. |
| `companyDetails.revenue` | `STRING` | YES | Revenue band, e.g. `$500M to $1B (USD)`. `Decline to state` is a real value, not a null. |
| `companyDetails.ceoName` | `STRING` | YES |  |
| `companyDetails.websiteUrl` | `STRING` | YES | The employer's own site. |
| `companyDetails.rating` | `DOUBLE` | YES | Indeed's employer review score. |
| `companyDetails.reviewCount` | `BIGINT` | YES | How many reviews the rating rests on. |
| `companyDetails.headquartersLocation` | `STRUCT<address STRING>` | YES | A single free-text address line. |
| `companyDetails.logoUrl` | `STRING` | YES |  |
| `companyOverviewLink` | `STRING` | YES | Indeed's own company-profile and review page (`…/cmp/<slug>`). Not the employer's own website — that is `companyDetails.websiteUrl`. The slug is a stable Indeed company key. |
| `attributes` | `ARRAY<STRUCT<key STRING, label STRING>>` | YES | Indeed's tags — skills, benefits, shift patterns, requirements. `key` is the stable id behind a label that can be reworded or localised. |
| `occupations` | `ARRAY<STRUCT<key STRING, label STRING>>` | YES | Indeed's occupation taxonomy. `key` is stable, the label is not. |
| `benefits` | `ARRAY<STRUCT<key STRING, label STRING>>` | YES | Stated benefits. |
| `socialInsurance` | `ARRAY<STRUCT<key STRING, label STRING>>` | YES | Insurance and statutory-contribution tags, concentrated in the Asian markets. |
| `jobTypes` | `ARRAY<STRING>` | YES | Full-time, contract, and so on. Plain strings — the source attaches no key here. |
| `pubDate` | `BIGINT` | YES | Epoch milliseconds. |
| `expirationDate` | `BIGINT` | YES | Epoch milliseconds, like `pubDate`. |
| `jobSourceName` | `STRING` | YES | Where Indeed syndicated the listing from — often the employer or its ATS. |
| `originalApplyUrl` | `STRING` | YES | The external application destination, usually the employer's ATS. |
| `viewJobLink` | `STRING` | YES | The Indeed job page as a **relative path** (`/viewjob?jk=…`); it needs the market's Indeed domain in front to be usable. |
| `language` | `STRING` | YES | The JD's language, e.g. `en`, `th`, `zh`. |
| `trackingKey` | `STRING` | YES | Volatile provider tracking metadata — it changes between fetches without the job changing. |
| `expired` | `BOOLEAN` | YES | Indeed's expiry flag. Unreliable — see the note below. |
| `isRepost` | `BOOLEAN` | YES |  |
| `newJob` | `BOOLEAN` | YES |  |
| `urgentlyHiring` | `BOOLEAN` | YES |  |
| `highVolumeHiring` | `BOOLEAN` | YES |  |

**Indeed gives no reliable lifecycle signal.** `expired`, `isRepost`, `newJob`,
`urgentlyHiring` and `highVolumeHiring` are false on effectively every collected row, so unlike MCF's
`status.jobStatus` and SEEK's `job.status` they cannot say whether a listing is still live.

#### Table: `linkedin_job_listings`

* **Fully Qualified Name:** `bto.bronze.linkedin_job_listings`
* **Medallion Layer:** Bronze
* **Grain:** One row per LinkedIn job observed per scrape run
* **Logical Key:** `(run_id, board, market, board_job_id)`
* **Description:** LinkedIn job listings collected via Apify. Columns below `content_hash` use the current actor's field names. Historical observations from the retired actor were projected through the semantic equivalents documented in the Bronze introduction; their original payloads remain unchanged in `raw_job_listings`.

##### Columns

For the current actor, an absent scalar on the job block is an
**empty string, not a null**; the company block used nulls. Both read as "no value".

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `board` | `STRING` | NO | Always `linkedin`. Logical-key part 2. |
| `market` | `STRING` | NO | `sg` · `hk` · `th` · `au`. Logical-key part 3. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 4. Same value as `jobId`. |
| `run_id` | `STRING` | NO | The scrape run this observation belongs to. FK to `scrape_runs.run_id`, which holds the run's `started_at` — join for observation time rather than storing it again. Logical-key part 1. |
| `content_hash` | `STRING` | YES | The payload version this observation points at, and the filename stem in `raw_job_listings`. Repeats across observations while the payload is unchanged. NULL when the correct payload was not captured — never a sign the job was absent, new or reposted. |
| `jobId` | `STRING` | YES | LinkedIn's job id inside the payload. Same value as `board_job_id`. |
| `jobTitle` | `STRING` | YES |  |
| `jobDescription` | `STRING` | YES | The JD as plain text. The actor emits no HTML field. |
| `location` | `STRING` | YES | **The job's location**, e.g. `Manly, New South Wales, Australia`. Not `companyAddress`, which describes the company. |
| `sector` | `STRING` | YES | Industry tag(s) for the **listing**. Often equals `companyIndustry` for direct employers, but on agency postings it describes the job or client's industry, not the agency's. May hold several industry labels joined into one string — and LinkedIn industry names themselves contain commas (`Technology, Information and Internet`), so it must never be naively split on commas. |
| `contractType` | `STRING` | YES | **The employment type**: `Full-time`, `Contract`, `Part-time`, `Internship`, `Temporary`, `Volunteer`, `Other`. |
| `workType` | `STRING` | YES | **The job function**, e.g. `Engineering and Information Technology`. A category, not a second employment type. |
| `experienceLevel` | `STRING` | YES | A seniority band such as `Entry level` or `Mid-Senior level`. |
| `yearsOfExperience` | `ARRAY<STRUCT<years STRING, context STRING, lang STRING>>` | YES | Experience demands the actor lifted from the JD. `years` is source text (`7+`, `3-6`); `context` names what the years are of. |
| `salaryInfo` | `ARRAY<STRING>` | YES | Pay as the actor gives it, e.g. `["$111111", "$222222"]`. Carries a currency symbol but no pay period. |
| `applicationsCount` | `STRING` | YES | Prose, not a number — e.g. `Be among the first 25 applicants`. |
| `posterFullName` | `STRING` | YES | Who posted the role — hiring manager or recruiter. |
| `posterProfileUrl` | `STRING` | YES | Their LinkedIn profile. |
| `publishedAt` | `STRING` | YES | Source posting date or timestamp: the current actor uses an ISO-8601 instant; historical projected rows may contain a date-only value. |
| `postedTime` | `STRING` | YES | The posted date in relative form, e.g. `8 hours ago`. Recomputed at every fetch, so display only. |
| `jobUrl` | `STRING` | YES | The public LinkedIn job page. |
| `applyUrl` | `STRING` | YES | The current actor repeats `jobUrl`, including when `applyType` is `EXTERNAL`; it does not expose the employer's own application destination. |
| `applyType` | `STRING` | YES | How the application is made: `EXTERNAL` or `EASY_APPLY`. |
| `searchString` | `STRING` | YES | The keyword and location that surfaced this row, e.g. `example-role - Exampleland`. |
| `dynamicFilterMatch` | `BOOLEAN` | YES | The actor's flag for whether the row matched the requested filters. |
| `companyId` | `STRING` | YES | LinkedIn's company key. Stable across name changes. |
| `companyName` | `STRING` | YES |  |
| `companyUrl` | `STRING` | YES | The company's LinkedIn page. |
| `companyLogo` | `STRING` | YES |  |
| `companyWebsite` | `STRING` | YES | The company's own site. |
| `companyDescription` | `STRING` | YES | The company's own blurb. |
| `companyIndustry` | `STRING` | YES | Localised — see the note below. |
| `companyEmployeeCount` | `BIGINT` | YES | A number, so unaffected by localisation. |
| `companyEmployeeCountRange` | `STRING` | YES | Localised band string, e.g. `10,001+ employees`. |
| `companyOrganizationType` | `STRING` | YES | Localised, e.g. `Public Company`. Includes `Government Agency`. |
| `companyFoundedDate` | `STRING` | YES |  |
| `companyFollowersCount` | `BIGINT` | YES | LinkedIn followers. |
| `companySpecialties` | `ARRAY<STRING>` | YES | The company's self-declared specialisms. |
| `companyAffiliatedPages` | `ARRAY<STRING>` | YES | Names of the company's other LinkedIn pages — subsidiaries and divisions. |
| `companyOfficeLocations` | `ARRAY<STRING>` | YES | Full address lines for the company's offices. |
| `companyAddress` | `STRUCT<addressCountry STRING, addressLocality STRING, addressRegion STRING, postalCode STRING, streetAddress STRING>` | YES | **The company's registered or head office — not the job's location.** `addressCountry` is where the company is based and routinely differs from `market`. |
| `companyRecentPosts` | `ARRAY<STRUCT<datePublished STRING, text STRING, url STRING>>` | YES | The company's recent LinkedIn posts. |

**The current actor uses a consistent company-enrichment pattern:** `companyId`,
`companyName`, `companyUrl` and `companyLogo` are present; the other `company*`
fields are null together when the actor cannot resolve the company page.

**`companyIndustry`, `companyEmployeeCountRange` and `companyOrganizationType`
are localised** to the company page's own language. `companyEmployeeCount` is
numeric and language-free.

---

## Silver

Silver holds exactly two persistent tables. `standardized_job_listings` maps
the four Bronze board tables into one common
schema — one `advertiser_name`, one `salary_min`, whichever board a row came
from — cleans the description, and stores the matching inputs (`fingerprint`,
`minhash_signature`) that `job_canonical_mapping` reads. It keeps Bronze's
observation grain: standardizing changes the columns, never the number of rows.
`job_canonical_mapping` then records which canonical group every usable wording
of every source listing belongs to. There is no persistent canonical-jobs,
deduplicated-jobs, or pair-evidence table — a current grouped view is derived
from the mapping.

**`standardized_job_listings` does not deduplicate.** It stores the matching
inputs; grouping happens in `job_canonical_mapping`, as group membership rather
than row collapsing. In this table, a LinkedIn row and an MCF row for the same
real-world vacancy stay separate rows.

`first_seen`, `last_seen` and `delisted_at` are deliberately absent; lifecycle
is derived from Bronze observation history and `scrape_runs` when needed.

### Tables

#### Table: `standardized_job_listings`

* **Fully Qualified Name:** `bto.silver.standardized_job_listings`
* **Medallion Layer:** Silver
* **Grain:** One row per Bronze observation — one source job seen in one scrape run
* **Logical Key:** `(run_id, board, market, board_job_id)`
* **Description:** Every Bronze observation from every board, mapped into one common schema. One row per source job per run — repeated observations of the same listing stay separate rows, so this is standardized observation history, not a current-state table. Nothing is deduplicated across boards: two boards advertising the same real vacancy remain two rows, and matching them is the later canonical stage's job. A board that does not provide a concept leaves the column NULL — nothing is inferred to fill a gap. An observation Bronze recorded without a payload is standardized as a row carrying its identity and NULL content, so the sighting survives into Silver; `job_url` alone is identity-derived and is still populated on such rows. Rebuildable in full from Bronze.

**Missing-value normalization (payload-derived standardized values).** For these values, absence is NULL. Scalar strings are trimmed and become NULL when blank after trimming. Arrays drop NULL/blank elements, keep source order and duplicates, and become NULL when nothing usable remains — a standardized array is never stored as `[]`. Structs keep any object carrying meaningful information, with blank optional members normalized to NULL. No semantic missing-value guessing — wordings such as `n/a` or `-` survive unless a field's own rule says otherwise.

##### Columns

Grouped for readability; the groups are presentation only, and every group table has the same schema columns.

**Observation identity & payload**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `run_id` | `STRING` | NO | The scrape run this observation belongs to, copied verbatim from Bronze — suffix variants such as `_qNNN` and `_backfill_*` are never parsed or normalized. FK to `scrape_runs.run_id`, which holds the run's `started_at` — join for observation time rather than storing it again. Logical-key part 1. |
| `board` | `STRING` | NO | `mcf` · `seek` · `jobstreet` · `jobsdb` · `indeed` · `linkedin`. Logical-key part 2. |
| `market` | `STRING` | NO | The market the listing was swept from, e.g. `sg`, `au`. Logical-key part 3. Not the job's location — see `job_country_code`. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 4. |
| `content_hash` | `STRING` | YES | The payload version this observation standardizes. Repeats across observations while the payload is unchanged. **NULL when Bronze had no payload for the observation** — the job was seen but its detail fetch was deferred or failed. Every column derived from the payload is then NULL too; the row is kept so the sighting is not lost. For Indeed and LinkedIn the hash is capture identity — a changed hash does not necessarily mean a material JD change. |

**Job content**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `title` | `STRING` | YES | mcf `title` · seek `job.title` · indeed `title` · linkedin `jobTitle`. NULL when the observation has no payload — see `content_hash`. |
| `description_html` | `STRING` | YES | The richest formatting the board gave. Deliberately NULL for every LinkedIn observation, both actor eras — see the description note below. |
| `description_text` | `STRING` | YES | The JD as plain text — one shared HTML recipe on the HTML-bearing boards, LinkedIn's own plaintext rule otherwise; see the description note below. This is what matching and triage read, never the HTML. NULL when the observation has no payload — see `content_hash` — when normalization leaves no meaningful visible text, or when the description is quarantined (see the description note below). |

**Dates, listing state & board metrics**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `posted_date` | `DATE` | YES | The board's posted date, as a UTC calendar date. mcf `metadata.newPostingDate`, a DATE as supplied · seek the UTC calendar date of `job.listedAt.dateTimeUtc` · indeed `pubDate` epoch milliseconds interpreted in UTC · linkedin the first 10 characters of `publishedAt` only when they form a valid `YYYY-MM-DD` date, otherwise NULL — covering both ISO instants and historical date-only values. NULL when the observation has no payload — see `content_hash`. |
| `expiry_date` | `DATE` | YES | The board-reported expiry date of the listing — not necessarily the employer's application deadline, nor the end of the underlying vacancy. mcf `metadata.expiryDate` · seek the UTC calendar date of `job.expiresAt.dateTimeUtc` — no local-time conversion · indeed the UTC calendar date of `expirationDate` (epoch ms) only when `expirationDate >= pubDate`, otherwise NULL — internally consistent far-future values are preserved, with no sentinel list · linkedin none. |
| `board_status` | `STRING` | YES | The board's own current-status value, verbatim: mcf `Open` / `Re-open` / `Closed` · seek `Active` / `Expired`. NULL for Indeed and LinkedIn, which state nothing reliable — their lifecycle comes from sweep evidence later, not from this table. |
| `views_count` | `INT` | YES | mcf `metadata.totalNumberOfView` only. Board-reported and grows with age — normalize by days since `posted_date` before comparing. |

**Advertiser & hiring company**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `advertiser_name` | `STRING` | YES | The advertiser as the board shows it — employer or agency, undistinguished. mcf `postedCompany.name` · seek `job.advertiser.name`, not `companyProfile.name` · indeed `companyDetails.name` · linkedin `companyName`. |
| `hiring_company_name` | `STRING` | YES | The end employer, where the source explicitly supplies one apart from the advertiser. mcf `hiringCompany.name` only, and rare — see `is_agency_posting` for the reliable flag. Kept verbatim — never NULLed merely for equalling `advertiser_name`. |
| `company_registry_id` | `STRING` | YES | A government registry number for the advertiser. mcf `postedCompany.uen` only. No other board gives one — their ids are board-local keys, kept separately in `board_company_id`. |
| `board_company_id` | `STRING` | YES | The board's own stable company key: seek `job.advertiser.id` · linkedin `companyId` · indeed the `/cmp/` slug from `companyOverviewLink`. Board-local — only meaningful together with `board`. NULL for mcf, whose key is the registry id. |
| `is_agency_posting` | `BOOLEAN` | YES | mcf `metadata.isPostedOnBehalf`, the only explicit flag any board gives. NULL elsewhere — absence of the flag is not evidence of a direct employer. |
| `company_website` | `STRING` | YES | The employer's own site. mcf `postedCompany.companyUrl` · indeed `companyDetails.websiteUrl` · linkedin `companyWebsite`. SEEK gives none. |
| `company_employee_count` | `INT` | YES | Numeric headcount: mcf `postedCompany.employeeCount` · linkedin `companyEmployeeCount`, not the localised string band `companyEmployeeCountRange`. Indeed gives only a band string, left in Bronze. |

**Location**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `job_country_code` | `STRING` | YES | ISO 3166-1 alpha-2 for the **physical job location** — exactly two uppercase ASCII letters; NULL whenever it cannot be safely determined. mcf literal `SG`; for overseas listings (`address.isOverseas`) the country comes from `address.overseasCountry` via a **targeted raw-payload dereference** — the one field Silver reads from `raw_job_listings` — mapped with a pinned ISO name lookup (`pycountry == 26.2.16`, case-insensitive exact matching only — never fuzzy search; unknown or unresolvable names → NULL, never guessed) · seek `gfjInfo.location.countryCode`, validated and uppercased; the closed historical JobStreet SG stub shape (payload row, `job` present, `gfjInfo` NULL) → `SG`, a narrow historical exception and never a generic market fallback · indeed `location.countryCode`, trimmed and uppercased, then validated as ISO alpha-2 — invalid → NULL · linkedin a fixed map of the known markets (`sg→SG` · `hk→HK` · `th→TH` · `au→AU`), NULL on identity-only observations. `companyAddress.addressCountry` is the company's HQ, never the job's country. |
| `job_location` | `STRING` | YES | Human-readable job location. mcf `address.districts[].location` labels joined with `; ` — the literal `overseas` for MCF overseas listings · seek `job.location.label` · indeed `location.formatted.long` · linkedin `location`. |

**Salary**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `salary_raw` | `STRING` | YES | The board's own salary wording, kept for display and re-parsing: seek `job.salary.label` trimmed verbatim — placeholder wordings (`n/a`, `-`, `.`, …) are preserved, whitespace-only → NULL · linkedin non-empty `salaryInfo` elements joined with ` – ` in source order, duplicates kept, empty array → NULL. NULL for mcf and indeed, whose salary arrives structured. |
| `salary_min` | `DOUBLE` | YES | Numeric lower bound **in the source-reported currency and period** — never converted, annualised or repaired. mcf `salary.minimum`, preserved as supplied · indeed `salary.min`, preserved as supplied · seek parsed from the label only where the grammar below safely resolves the relevant information (`from X` → min only; `up to X` → max only; unresolved pieces stay NULL) · linkedin never (no period semantics). |
| `salary_max` | `DOUBLE` | YES | As `salary_min`. |
| `salary_currency` | `STRING` | YES | mcf literal `SGD` — a documented platform-level assumption; MCF has no per-row currency field · indeed trimmed `salary.currencyCode` as supplied, normally an ISO code but not validated here · seek explicit evidence only, `currencyLabel` first and an explicit label token otherwise — never inferred from the market · linkedin never. |
| `salary_period` | `STRING` | YES | `Hourly` · `Daily` · `Weekly` · `Monthly` · `Annual`. mcf trimmed and title-cased `salary.type.salaryType`, emitted only when it is one of those five values; unrecognized values become NULL · indeed `salary.type` mapped (`MONTH`→`Monthly`, `YEAR`→`Annual`, `HOUR`→`Hourly`, `WEEK`→`Weekly`, `DAY`→`Daily`) · seek only an explicit period token under the grammar below — where the grammar resolves no period, seek's normalized numeric fields stay NULL · linkedin never. |

> **SEEK salary label grammar (finite, deterministic).** Currency evidence, in precedence order: `currencyLabel`, else an explicit label token — `฿`/`บาท`→`THB`, `S$`→`SGD`, `HK$`→`HKD`, `A$`→`AUD`, `NZ$`→`NZD`, `US$`→`USD`, `RM`→`MYR`, `£`→`GBP`, `€`→`EUR`, or one of the accepted ISO codes `SGD` · `HKD` · `THB` · `AUD` · `NZD` · `USD` · `GBP` · `EUR` · `MYR` — no other code is accepted; a bare `$` resolves nothing. Period: explicit tokens only, including the `p.a.` / `p.m.` / `p.h.` variants, `per annum`, `per month/year/hour/day/week`, `annually`, `yearly`, `monthly`, `hourly`, `daily`, `weekly` and Thai `ต่อเดือน`. Ranges split on `-`, `–`, `—`, `~` or case-insensitive `to`. A `k` suffix scales its own endpoint by 1,000 and carries to the other endpoint only when that endpoint is bare and below 1,000; decimal `k` is supported. An explicit currency marker on either endpoint applies to the range. Whitespace-delimited trailing riders (` + bonus`, ` + super`, …) are ignored for numeric extraction and stay in `salary_raw`. Anything the grammar cannot resolve leaves the normalized fields NULL — no JD prose, no market inference, no FX conversion, no annualisation, no repair of suspicious source values.

**Classification & job attributes**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `categories` | `ARRAY<STRING>` | YES | The listing's own taxonomy labels, not remapped: mcf `categories[].category` · seek `job.classifications[].label` · indeed `occupations[].label` · linkedin `sector` **as a single element** — LinkedIn industry names contain commas, so the string is never split. |
| `employment_types` | `ARRAY<STRING>` | YES | mcf `employmentTypes[].employmentType` · seek `job.workTypes.label` split on commas, not `gfjInfo.workTypes.label`, which uses a different Google-for-Jobs vocabulary · indeed `jobTypes` · linkedin `contractType` as a single element. |
| `job_function` | `STRING` | YES | linkedin `workType` only, e.g. `Engineering and Information Technology`. A category of work — never an employment type, so it is kept apart from `employment_types`. |
| `position_levels` | `ARRAY<STRING>` | YES | mcf `positionLevels[].position` (may hold several) · linkedin `experienceLevel` as a single element. SEEK and Indeed give none. |
| `min_years_experience` | `INT` | YES | mcf `minimumYearsExperience` only — the one structured integer any board gives. LinkedIn's `5+` / `3-6` texts are not parsed here; triage reads them in the description. |
| `skills` | `ARRAY<STRING>` | YES | mcf `skills[].skill` labels only. The `uuid` and `isKeySkill` detail stays in Bronze. |
| `flexible_work_arrangements` | `ARRAY<STRING>` | YES | mcf `flexibleWorkArrangements[].flexibleWorkArrangement` labels — `Telecommuting`, `Flexi-Hours`, … NULL elsewhere. |
| `screening_questions` | `ARRAY<STRING>` | YES | The employer's questions to the candidate: seek `job.products.questionnaire.questions` · mcf `screeningQuestions[].question`. Question order and duplicates are preserved. Rich triage input — they state years, work authorisation and expected salary outright. |
| `board_attributes` | `ARRAY<STRING>` | YES | indeed `attributes[].label` only — Indeed's mixed tag vocabulary: skills, work modes, benefits, requirements. Duplicate labels are preserved — they come from distinct source keys. |

**Listing & application URLs**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `job_url` | `STRING` | YES | The stable URL of this listing on its board, **derived from observation identity alone** (`board`, `market`, `board_job_id`) — never the source's mutable slug or tracking URLs. The one payload-independent field: populated even on identity-only observations. Constructed per the recipe below. |
| `apply_url` | `STRING` | YES | A source-supplied application destination, where one exists — source-hosted or external, never manufactured from `job_url` and never a fallback to it. indeed trimmed `originalApplyUrl` · NULL for mcf and seek, which apply on-platform · NULL for linkedin — the current actor repeats `jobUrl`; the signal that an application leaves LinkedIn is `apply_type`. |
| `apply_type` | `STRING` | YES | linkedin `applyType` verbatim: `EXTERNAL` or `EASY_APPLY` — the only signal that an application leaves LinkedIn. |

> **Stored value recipe — `job_url`**
> ```text
> mcf            https://www.mycareersfuture.gov.sg/job/{board_job_id}
> jobstreet sg   https://sg.jobstreet.com/job/{board_job_id}
> jobsdb hk/th   https://{market}.jobsdb.com/job/{board_job_id}
> seek au/nz     https://{market}.seek.com/job/{board_job_id}
> indeed us      https://www.indeed.com/viewjob?jk={board_job_id}
> indeed other   https://{market}.indeed.com/viewjob?jk={board_job_id}
> linkedin       https://{market}.linkedin.com/jobs/view/{board_job_id}
> ```

**Poster, contacts & source**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `poster_name` | `STRING` | YES | linkedin `posterFullName` — the hiring manager or recruiter who posted. Empty string in the source reads as NULL here. |
| `poster_profile_url` | `STRING` | YES | linkedin `posterProfileUrl`. |
| `contacts` | `ARRAY<STRUCT<type STRING, value STRING>>` | YES | seek `job.contactMatches` — emails and phone numbers found in the JD. Members trimmed, blank → NULL; a struct is kept only while `value` is non-NULL — `type` may be NULL and is never inferred from the value. Source order and duplicates preserved, identical structs included; never sorted or deduplicated. No usable structs → NULL. |
| `job_source_name` | `STRING` | YES | indeed `jobSourceName` — where Indeed syndicated the listing from, often the employer or its ATS. |

**Matching inputs & lineage**

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `fingerprint` | `STRING` | YES | Exact-match key: SHA-256 of the normalized matching document — see the matching recipe below. The document is built from `title` and the **Latin-script part** of `description_text`; non-Latin-script description content is excluded from matching, while the stored `description_text` keeps it. NULL when the observation has no payload — see `content_hash` — on description-quarantined rows (see the description note below), when `title` and `description_text` are both NULL, and when a non-empty `description_text` contributes no Latin-script content at all (matching never falls back to the title alone). `sha256("")` is never stored. |
| `minhash_signature` | `ARRAY<BIGINT>` | YES | 128-value MinHash over the same normalized document — see the matching recipe below. Values are unsigned 32-bit, so the type is `BIGINT`; `INT` would overflow. NULL exactly when `fingerprint` is NULL — a zero-shingle sentinel signature is never stored. |
| `standardized_at` | `TIMESTAMP` | NO | When this Silver representation was generated or rebuilt. Lineage metadata, not a source observation timestamp. |

**Description recipes.** `description_html` comes from mcf `description`, seek
`job.content` and indeed `jobDescriptionHTML`; it is deliberately NULL for linkedin
in both actor eras. The HTML-bearing boards use the shared deterministic recipe in
[HTML_NORMALIZATION.md](../../clean_job_listings/docs/HTML_NORMALIZATION.md), and an empty
normalized result becomes NULL. Indeed's plaintext `jobDescription` is deliberately
not used. LinkedIn instead uses plaintext `jobDescription`, trimmed with exactly one
trailing `Show more` + whitespace + `Show less` sequence removed when present.
Matching reads `description_text`, never `description_html`.

**Unsupported-description quarantine.** For an isolated payload-bearing row with an
unsupported construct defined by
[HTML_NORMALIZATION.md](../../clean_job_listings/docs/HTML_NORMALIZATION.md), the row and
all independently derivable fields survive while `description_text`, `fingerprint`
and `minhash_signature` are NULL. The typed reason is reported, the board × market
result can be partial, and no quarantine state is persisted. If every payload-bearing
row for a selected board × market batch quarantines, the build fails before that
batch is merged or published. Identity-only rows are not quarantined payload rows.

**Matching recipe.** Derive the matching description by retaining only the
Latin-script portion of `description_text`, as defined in the cleaning contract.
Build `doc` from the non-NULL values among `title` and that matching description,
in that order; join them with one space, collapse Python-regex whitespace runs to
one ASCII space, trim, and apply Unicode `lower()`. When a nonempty description
contributes no Latin-script content, do not fall back to the title. With no usable
document, both matching fields are NULL. Otherwise `fingerprint` is lowercase-hex
SHA-256 of the UTF-8 document. `minhash_signature` is a 128-permutation MinHash
under `datasketch == 2.0.0` with its default seed, over the set of five-word
shingles. A document of five words or fewer is one whole-document shingle; each
shingle is fed as the big-endian integer of its BLAKE2b `digest_size=8` hash.

The stored signature is a candidate-generation input only; it never supplies
acceptance evidence. See
[CONTRACT.md §3](../../clean_job_listings/docs/CONTRACT.md#3-matching-inputs) for the
recipe and change policy and
[§4](../../clean_job_listings/docs/CONTRACT.md#4-candidate-and-match-contract) for exact
candidate and match semantics.

#### Table: `job_canonical_mapping`

* **Fully Qualified Name:** `bto.silver.job_canonical_mapping`
* **Medallion Layer:** Silver
* **Grain:** One row per source listing × distinct usable historical fingerprint, plus exactly one `fingerprint = NULL` sentinel row for each source listing that has never had a usable fingerprint
* **Logical Key:** `(board, market, board_job_id, fingerprint)` — a **build invariant**, not a Delta constraint: `fingerprint` is nullable, so no primary key can be declared; the build constructs rows uniquely and validation checks duplicates with a GROUP BY over all four columns (NULLs group together, so duplicate sentinels are caught too)
* **Description:** Which canonical group every usable wording of every source listing belongs to, under the **current complete standardized corpus** and the one locked matching recipe. Cross-board duplicates that `standardized_job_listings` keeps separate are reconciled here as group membership — rows are never collapsed. `run_id` is not part of the grain: repeated observations of a listing with the same wording add no rows, and a listing whose wording history is F1 → F2 → F1 has exactly two rows. Every source listing appears at least once; a listing that has never had a usable fingerprint gets one sentinel row and its deterministic self `canonical_job_id`, and never gains a sentinel later merely because an observation was identity-only or quarantined. The full-history algorithm defines the result. Construction may safely extend a validated existing mapping when the incremental rules prove equivalence; otherwise it reconstructs from `standardized_job_listings` + `scrape_runs` + the frozen matching recipe. Every successful publication atomically replaces the complete mapping.

##### Columns

| Column Name | Datatype | Nullable | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| `board` | `STRING` | NO | The mapped source listing's board. Logical-key part 1. |
| `market` | `STRING` | NO | The mapped source listing's market. Logical-key part 2. |
| `board_job_id` | `STRING` | NO | The board's own job id. Logical-key part 3. |
| `fingerprint` | `STRING` | YES | The usable wording this membership stands on — joins `standardized_job_listings.fingerprint`. Logical-key part 4. **NULL only on the never-usable sentinel**; NULL never equi-joins, so sentinel rows can never accidentally match an observation. |
| `canonical_job_id` | `STRING` | NO | A deterministic identifier for the canonical connected component — not a representative, current or best listing, and not the current wording. Winner = the component's earliest **listing × fingerprint membership**: first-seen = `min(scrape_runs.started_at)` over observations of that exact `(board, market, board_job_id, fingerprint)` — never lexical `run_id` order — with ties broken on `board`, `market`, `board_job_id`, then `fingerprint`. Serialized as `{board}:{market}:{board_job_id}:{fingerprint}` of the winning membership, full 64-hex fingerprint, no truncation or UUIDs. The serialization assumes its identity components contain no `:`; that assumption is not universally enforced. The winning fingerprint may be a historical wording, which is intentional. Sentinel rows keep the three-part `{board}:{market}:{board_job_id}`. Ids may change on component merges and on sentinel → usable transitions — ordinary canonicalization behaviour. |
| `canonicalized_at` | `TIMESTAMP` | NO | When the completed canonicalization candidate published to this table was produced — every row from the same whole-table publication carries the same value. Build/operational lineage, like `standardized_at`: not match provenance, not a row-level match time, not source observation time, not first/last-seen, not assignment-history time. Not part of the logical key. |

**Volume `bto.silver.clean_job_listings_code`** holds the packaged `bto` wheel the Databricks canonicalization job runs — a deployment artefact uploaded by the orchestrator, not data; the repository package is the only implementation.

**Table property `bto.standardized_version`** (Delta table metadata, not a column): the exact Delta version N of `standardized_job_listings` this mapping was built from, set by the same `CREATE OR REPLACE TABLE … TBLPROPERTIES` statement that publishes the rows. The mapping is current only while no data-changing standardized commit exists above N; timestamps never prove freshness. See [CONTRACT.md §8](../../clean_job_listings/docs/CONTRACT.md#8-generation-binding-and-freshness) and `storage/databricks/freshness.py`.

No other columns exist — no matched-by, score, confidence, component-type or diagnostic fields. Group shape is derived from the table itself: **unmatchable** = `fingerprint IS NULL` · **usable singleton** = group of one non-NULL row · **exact-shared** = one distinct fingerprint, several rows · **near-connected** = several distinct fingerprints · **mixed** = near-connected with a fingerprint carrying several rows.

> **Canonical graph (how groups form).** Nodes are distinct usable historical
> fingerprints; exact fingerprint equality is one node. Accepted evidence connects
> nodes, and connected components are canonical groups, with intentional
> transitivity. Historical wordings remain represented even when no current
> observation carries them. See
> [CONTRACT.md §4](../../clean_job_listings/docs/CONTRACT.md#4-candidate-and-match-contract)
> for exact candidate and match semantics.

> **Current assignment (how consumers read it).** A listing's current canonical
> assignment is derived, never stored: its latest usable fingerprint — from the
> most recent observation with a populated matching pair, selected by
> `ORDER BY scrape_runs.started_at DESC, run_id DESC` (bytewise string order;
> `run_id` is consulted only on an exact `started_at` tie, as a determinism
> device, never chronology) — joined to this table. A later identity-only
> observation does not erase the latest usable assignment; a never-usable
> listing's assignment is its sentinel row.

> **Construction and publication behaviour.** The table always contains the
> complete mapping for the standardized history at its bound version. Construction
> may safely extend a validated existing mapping or may perform the authoritative
> full-history reconstruction. New evidence may merge groups, change existing
> `canonical_job_id` values, move a listing's current assignment, and replace a
> sentinel when a usable fingerprint appears. Either construction path publishes
> the complete mapping atomically and writes `bto.standardized_version`. Failures
> before publication leave the previously published mapping and its
> `canonicalized_at` untouched; a data change detected after publication fails the
> run with the replacement still present but stale and rejected by freshness
> checks. See
> [CONTRACT.md §7](../../clean_job_listings/docs/CONTRACT.md#7-validation-guarantees)
> for validation guarantees and
> [§8](../../clean_job_listings/docs/CONTRACT.md#8-generation-binding-and-freshness)
> for freshness and version binding.


---

## Gold

No Gold data contract or object is defined.
