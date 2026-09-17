# fetch_job_listings

This is the collection contract for the current build. The collector runs
B's searches across every enabled board and market and lands what comes
back in Bronze. It collects and preserves; it does not
clean, standardize or interpret.

The [data dictionary](../storage/databricks/data-dictionary.md) is authoritative
for schemas, field definitions, datatypes and the hashing and fingerprint
recipes. This document explains how collection works.

## Structure

These are the collection modules.

```text
back-to-office/
├── AGENTS.md
├── CLAUDE.md
├── LICENSE
├── README.md
├── Makefile
├── pyproject.toml
│
└── src/bto/
    ├── __init__.py
    ├── __main__.py                 # CLI: python -m bto collect --board X --market Y
    ├── settings.py                 # env/config; enabled board × market combinations
    │
    ├── fetch_job_listings/
    │   ├── __init__.py
    │   ├── README.md               # this document
    │   ├── run.py                  # one board × market run
    │   ├── http_client.py          # shared direct HTTP plumbing for MCF + SEEK
    │   ├── apify_client.py         # shared Apify plumbing for Indeed + LinkedIn
    │   │
    │   ├── mcf/
    │   │   ├── __init__.py
    │   │   ├── fetch.py            # call MCF search + detail endpoints
    │   │   ├── parse.py            # map MCF payloads → Bronze mcf_job_listings table
    │   │   ├── searches.example.py # committed safe search template
    │   │   └── searches.py         # ignored local search terms
    │   │
    │   ├── seek/
    │   │   ├── __init__.py
    │   │   ├── fetch.py            # call SEEK / JobStreet / JobsDB search + detail
    │   │   ├── parse.py            # map SEEK payloads → Bronze seek_job_listings table
    │   │   ├── markets.py          # AU/NZ/SG/HK/TH source configuration
    │   │   ├── searches.example.py # committed safe search template
    │   │   └── searches.py         # ignored local search terms by market
    │   │
    │   ├── indeed/
    │   │   ├── __init__.py
    │   │   ├── fetch.py            # run/read Indeed Apify actor
    │   │   ├── parse.py            # map Indeed actor rows → Bronze indeed_job_listings table
    │   │   ├── markets.py          # market + actor configuration
    │   │   ├── searches.example.py # committed safe search template
    │   │   └── searches.py         # ignored local search terms by market
    │   │
    │   └── linkedin/
    │       ├── __init__.py
    │       ├── fetch.py            # run/read LinkedIn Apify actor
    │       ├── parse.py            # map LinkedIn actor rows → Bronze linkedin_job_listings table
    │       ├── markets.py          # market/location + actor configuration
    │       ├── searches.example.py # committed safe search template
    │       └── searches.py         # ignored local search terms by market
    │
    ├── send_notifications/
    │   ├── __init__.py
    │   └── notify.py               # problem alerts and the daily summary, by email
    │
    └── storage/
        ├── databricks/
        │   ├── __init__.py
        │   ├── data-dictionary.md  # authoritative Databricks collection data contract
        │   ├── connection.py       # Databricks connection / SQL / volume mechanics
        │   ├── read.py             # load latest MCF + SEEK Bronze state into memory
        │   └── write.py            # write raw files, Bronze tables + scrape_runs
        │
        └── postgres/
            ├── __init__.py
            ├── data-dictionary.md  # PostgreSQL cost data contract
            ├── connection.py       # PostgreSQL connection mechanics
            └── write.py            # write collection Apify cost records
```

Two clients are shared, and only inside this folder: `http_client.py` for MCF and
SEEK, which make direct HTTP calls, and `apify_client.py` for Indeed and
LinkedIn. No source folder imports another.

**Three responsibilities, kept apart.** `fetch.py` obtains the source data.
`parse.py` understands that one source's payload and maps it into its Bronze
table representation. `storage/databricks/write.py` performs the persistence
mechanics. **Source-specific parsing never goes into `write.py`** — it is the
one module every board shares, and a board's field names have no business in it.

```
SEEK source
    ↓
seek/fetch.py
    ↓
seek/parse.py
    ↓
storage/databricks/write.py
    ↓
Bronze seek_job_listings
```

The application itself runs on Lightsail:

```
LIGHTSAIL

source APIs / Apify
        ↓
fetch.py
        ↓
parse.py
        ↓
temporary/local processing
        ↓
storage writers
        │
        ├────────► Databricks
        └────────► PostgreSQL
```

Databricks is the authoritative analytical and raw data destination. PostgreSQL
stores the collection cost records its dictionary defines. Lightsail executes
the Python application and does the source-specific parsing and change-detection
work.

## Markets

**Supported** means the source and market configuration has been proven and is
part of the collection implementation. **Enabled** means it will run when the
collector is scheduled; `settings.py` controls this. A disabled market keeps
its configuration.

| Market | MCF | SEEK family | Indeed | LinkedIn |
| :--- | :---: | :--- | :---: | :---: |
| **SG** Singapore | ✓ | ✓ JobStreet | ✓ | ✓ |
| **HK** Hong Kong | — | ✓ JobsDB | ✓ | ✓ |
| **TH** Thailand | — | ✓ JobsDB | ✓ | ✓ |
| **AU** Australia | — | ✓ SEEK | ✓ | ✓ |
| **NZ** New Zealand | — | ✓ SEEK | ✓ | — |
| **UK** United Kingdom | — | — | ✓ | — |
| **US** United States | — | — | ✓ | — |
| **CA** Canada | — | — | ✓ | — |
| | **1** | **5** | **8** | **4** |

**18 supported.** MCF is Singapore only. The SEEK family stops at five — SEEK
Limited does not operate in the UK, US or Canada; its brands are SEEK (AU, NZ),
JobStreet (SG) and JobsDB (HK, TH). Indeed covers all eight. LinkedIn covers the
four markets with proven search configuration.

**10 enabled at launch:**

| Board | Enabled |
| :--- | :--- |
| MCF | SG |
| SEEK family | JobStreet SG · JobsDB HK · JobsDB TH |
| Indeed | SG · HK · TH |
| LinkedIn | SG · HK · TH |

SEEK AU/NZ, Indeed AU/NZ/UK/US/CA and LinkedIn AU stay supported but disabled.

## Search configuration

Each board directory needs a local `searches.py` holding the real search
terms. It is intentionally git-ignored — search strategy is private local
configuration, like `.env` — so a fresh clone fails loudly until it is
created. For each board, copy the committed template and replace the
example values:

```bash
for b in mcf seek indeed linkedin; do
  cp src/bto/fetch_job_listings/$b/searches.example.py \
     src/bto/fetch_job_listings/$b/searches.py
done
```

The example files carry only dummy terms; they are never imported by the
collector and there is no fallback to them at runtime.

## How each source is collected

| | Search | Raw artifact | Full JD |
| :--- | :--- | :--- | :--- |
| **MCF** | Unrestricted by date | One file per term × page, 100 a page | Second request |
| **SEEK family** | Unrestricted by date | One file per term × page, 100 a page | Second GraphQL request |
| **Indeed** | `postedWithinDays = 1` | One actor envelope per term, empty runs included | Already in the actor row |
| **LinkedIn** | Rolling 24h (`r86400`) | One actor envelope for the whole run | Already in the actor row |

MCF and the SEEK family return summary cards, so the full JD needs a second
request. A job matching several search terms collapses to one observation for
the run.

SEEK, JobStreet and JobsDB run on one platform and share one fetcher; only the
site config differs by market.

Indeed runs one actor per term, and the same job id often appears under several
terms. Within a run the **last encountered occurrence wins** — encounter order
is configured term order, then actor dataset order. Every variant stays in the
raw envelopes.

LinkedIn runs one actor for all terms with `saveOnlyUniqueItems`, so each job id
comes back at most once.

Indeed and LinkedIn are collected through Apify actors:

| Board | Actor | ID |
| :--- | :--- | :--- |
| Indeed | [curious_coder/indeed-scraper](https://apify.com/curious_coder/indeed-scraper) | `qA8rz8tR61HdkfTBL` |
| LinkedIn | [cheap_scraper/linkedin-job-scraper](https://apify.com/cheap_scraper/linkedin-job-scraper) | `2rJKkhh7vjpX7pvjg` |

Both run on the actor's current build. We **validate that the output still has
the fields we depend on** rather than pinning a build id — a pin stops
collection every time the provider publishes. If the shape has materially
changed, the run stops and reports rather than guessing.

For LinkedIn, every returned row is checked for a non-empty `jobId` matching
`[0-9]{6,24}`, plus `jobTitle`, `location`, `publishedAt` and
`jobDescription`; malformed rows are rejected individually, and collection
stops if rows were returned but none passes. The actor must still be
`PAY_PER_EVENT`, with start and result event prices at or below the expected
values.

## New or changed

MCF and SEEK only. A board cannot tell us what is new, so the comparison is
against **our own Bronze state**, after the search sweep and before any JD is
fetched.

```
1. load prior state    {board_job_id: (change_signal, content_hash)}
                       ← prior Bronze observations
2. sweep every search  {board_job_id: this run's change_signal}
3. diff                new        = id not seen before
                       changed    = signal moved
                       unresolved = signal matches, but no valid payload
                       unchanged  = signal matches and payload is valid
4. fetch JDs           new + changed + unresolved + carried deferred
5. write               raw payloads, jd_fetches, Bronze observations
```

**Observing a job is not a reason to request it again.** Where the signal is
unchanged and a valid payload exists, the observation reuses that `content_hash`
and no request is made. There is no periodic refresh of unchanged JDs.

| Source | Change signal |
| :--- | :--- |
| MCF | Search-card `metadata.updatedAt`, compared as an opaque string |
| SEEK family | Deterministic fingerprint of 14 stable search-card fields |

The exact SEEK fingerprint recipe is in the data dictionary.

Indeed and LinkedIn need no such gate — their JD already arrived with the search
result, so there is no second request to avoid.

### Prior state is read once, then compared locally

Step 1 is **one short batched read**, not a lookup per job. No job ever queries
Databricks to find out about itself.

```
Databricks Bronze
        │
        │ short/batched read of required prior state
        ▼
Lightsail in-memory prior state
        │
        ├── MCF SG comparisons
        ├── JobStreet SG comparisons
        ├── JobsDB HK comparisons
        └── JobsDB TH comparisons
        │
        ▼
collection run ends
        │
        ▼
discard the in-memory state
```

`storage/databricks/read.py` loads the latest MCF and SEEK prior observation
state into an in-memory mapping on the Lightsail box, and every per-job
comparison happens against that mapping. The process discards it when the
collection run ends.

**Databricks Bronze remains authoritative.** The Lightsail copy is temporary
working state — never a second database, never a replacement for Bronze
observation history, and never something a later run reads instead of Bronze.
It is not written to a local cache.

## What collection writes

The [Databricks data dictionary](../storage/databricks/data-dictionary.md) owns
all volume, table, column, filename and hashing contracts. Collection writes:

```text
one board × market run
    ├── raw search pages or actor envelopes
    ├── content-addressed full-JD payloads
    ├── one run record
    ├── one Bronze observation per source job seen
    └── MCF/SEEK detail-fetch decisions
```

- **Raw search artifacts:** every search page or actor envelope as collected.
  MCF and SEEK also store one `jd_fetches` decision per distinct observed job:
  `fetched`, `unchanged`, `deferred` or `failed`.
- **Full-JD payloads:** content-addressed files. MCF and SEEK hash the detail
  response body exactly as returned. Indeed and LinkedIn hash the canonical
  reserialization of the actor row. Hashing uses uncompressed bytes; gzip comes
  afterwards. Repeated hashes are skipped within one run; a later run may write
  the same content-addressed path again with identical bytes.
- **Run record:** one per board × market run, opened as `running` and updated at
  close.
- **Bronze observations:** one per distinct source job seen in the run. These
  hold the identity, change signal where available, the payload reference that
  the next run reads, and the payload's source-native columns as the data
  dictionary defines them. Cross-board standardization happens separately in
  `clean_job_listings`.
Databricks writes are **batched or grouped** where practical, so the SQL
warehouse is not held open across the whole scrape writing observations one at a
time. Nothing already decided weakens because of it: raw artifacts are still
preserved as the contract requires, useful work still survives failure under the
existing `partial` semantics, and nothing is deferred so late that a crash would
throw away work already done. The simplest safe batching is an implementation
choice, not a schema decision.

- **Apify cost records in PostgreSQL:** one per Actor run, using Apify's
  reported `usageTotalUsd`. MCF and SEEK make direct HTTP calls and have no cost
  table. Cost writes fail soft: an unavailable PostgreSQL database does not
  change the scrape status, and the accounting gap is logged and reported. The
  [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md) owns the
  cost-table contracts.

## Runs and failures

A run is exactly **one board × one market**. `jobstreet × sg` is one run,
`indeed × sg` another. They are independent, so a stuck board cannot hold up the
rest.

```
run_id = {board}_{market}_{YYYYMMDD_HHMMSS}_{suffix}

jobstreet_sg_20260828_033000_a7f3
```

Lowercase board and market, timestamp in SGT, four random hex characters.

Each run owns one Databricks run record, inserted as `running` at the start and
updated in place at the end:

| Status | Meaning |
| :--- | :--- |
| `running` | started, not yet finished |
| `ok` | everything intended landed |
| `partial` | usable work landed, but collection coverage, payloads, provider evidence, validation or volume checks were incomplete or unsatisfactory; `error_reason`, counters and term results explain why |
| `failed` | no usable successful completion, including a fatal run-level failure; counters may be incomplete |

A hard crash leaves the row at `running` with no `finished_at` — durable
evidence that a run started and never came back.

**A failed fetch must never look like a job that disappeared.**

| Situation | What happens |
| :--- | :--- |
| Detail request fails | Observation survives with its `change_signal`; `content_hash` NULL; `jd_fetches` records `failed` |
| Signal changed, fetch failed or deferred | `content_hash` stays NULL — the old hash describes the previous version and is not reused |
| Signal unchanged, valid payload | Reuse the existing `content_hash`, no request |
| Signal unchanged, no valid payload | Still fetch-required |

Payload-derived fields are never copied forward from an older payload. A NULL
`content_hash` means "we saw this job but do not hold its correct payload" — not
that it was absent, new or reposted.

If a board returns a volume wildly outside its normal history, the run stops or
fails safely rather than continuing, keeps the work already completed, and
notifies.

## Parallelism

Up to **4 board × market runs** run in parallel. There is no required order.

```text
                    MAX 4 RUNS AT ONCE

        ┌──────────────┬──────────────┬──────────────┬──────────────┐
        │    SLOT 1    │    SLOT 2    │    SLOT 3    │    SLOT 4    │
        ├──────────────┼──────────────┼──────────────┼──────────────┤
        │ JobStreet SG │  Indeed SG   │ LinkedIn HK  │  JobsDB TH   │
        │      │       │              │              │              │
        │      ▼       │              │              │              │
        │   FINISHED   │   RUNNING    │   RUNNING    │   RUNNING    │
        │      │       │              │              │              │
        │      ▼       │              │              │              │
        │  Indeed HK   │              │              │              │
        └──────────────┴──────────────┴──────────────┴──────────────┘
```

When `JobStreet SG` finishes, the next waiting run can take its slot while the
other three continue.

One run failing does not stop the others. Concurrency inside each run is
source-specific and kept conservative.

## The 5,000 fetch cap

MCF and SEEK process at most **5,000 full-JD fetch candidates per run** — new
jobs, changed jobs, unresolved jobs, and work carried forward.

It is a runaway guard, not a routine quota, and sits far above normal operation.
Anything past the cap is recorded as `deferred`, carried forward and picked up by
a later run; nothing is silently discarded. **Reaching it is abnormal and
notifies.**

## Emails

Three fetch-related message types. All subjects start with
**`[back-to-office]`**, so one Gmail filter on that tag catches everything this
stage sends.

### Problem alert

Delivery is attempted as soon as a run ends with a meaningful problem:

| | |
| :--- | :--- |
| `failed` | The run died |
| `partial` | The run finished with usable work but incomplete or unsatisfactory collection evidence; the alert gives the exact reason |
| Cap reached | 5,000 fetches in one run |
| Abnormal volume | A board returned far more than it ever has; the run stopped rather than continue |
| Cost not recorded | The scrape worked but its cost row could not be written |

Subject: **`[back-to-office] Fetch Job Listings Error`**.

The message contains the board, market, cause, counters, `run_id`, what landed
and what remains. A `partial` message must make clear that its counters and
outstanding work are known; a `failed` message must not imply that unreliable
counters are complete.

### Fetch-execution summary

One summary delivery is attempted per fetch execution, good or bad, when that
execution finishes. The scheduled 03:30 fetch is followed by its summary
through the systemd service chain; a manual or smoke fetch attempts its own
summary as it finishes, covering only its own run(s). The summary is scoped to
the exact runs the execution launched — never "everything from today".

Subject: **`[back-to-office] Fetch Job Listings Summary`**.

The message contains every board × market run the execution intended, status,
new and changed counts where available, totals, runtime, failures and
deferred work — as an HTML table with a plain-text fallback.

`Changed` is a dash for Indeed and LinkedIn — their job descriptions arrive with
the search result, so there is nothing to re-download and nothing to compare.
A run that failed shows `[FAILED]` in the table with a detail line under it.

### Summary unavailable

If Databricks does not return the run results needed to build a summary, a
summary-unavailable message is attempted instead.

Subject: **`[back-to-office] Fetch Job Listings Summary Unavailable`**.

Email uses `smtplib` and runtime environment settings: `.env` locally and
`/etc/bto/env` under the current systemd deployment. Sending is fail-soft:
missing configuration or delivery failure is logged and does not break
collection.

The daily email does not report spend; spend reporting is planned later.

## Logging

Ordinary Python logging to stdout, done in the modules where events happen.
Each run logs its `run_id`, per-term result counts, the diff summary, fetch
progress, any cap or anomaly trip, and its final status with counters.

```
Python logging → stdout / stderr → systemd → journald on Lightsail
```

There is no logging package of our own, and journald is an operating-system
facility on the box, so nothing in this repository represents it.
`send_notifications/` is a separate concern — problem alerts and the summary
email, not the primary system log.

The [deployment README](../../../deploy/README.md) owns server log persistence
and access.

## Deployment

Collection runs on AWS Lightsail through the source-controlled systemd units.
The [deployment README](../../../deploy/README.md) owns the schedule, current
server state and deployment mechanism.

## Outside collection

`clean_job_listings` · payload cleaning and standardization · Silver
transformations · fingerprint and MinHash · cross-board deduplication · canonical
jobs and lifecycle · filtering and ranking · CV tailoring · job research ·
applications and tracking · web · analytics · historical Silver migration.

`fetch_job_listings` uses PostgreSQL only for Apify cost rows. The separately
decided AWS monthly cost table remains in the PostgreSQL data dictionary.
Application state and synced listings come later.
