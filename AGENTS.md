# Repository guidance

## Project Overview

Back to Office (BTO) collects job listings, preserves what boards returned,
cleans and deduplicates them, and is intended to support ranking, CV tailoring
and application tracking.

The implemented packages are `fetch_job_listings`, `clean_job_listings`,
`send_notifications`, `storage/databricks`, and `storage/postgres` (costs).
Databricks is the analytical lakehouse; PostgreSQL currently records
operational cost data. These namespaces are reserved and contain only
initializers:
`filter_and_rank`, `review_jds`, `tailor_cvs`, `research_job_listing`,
`apply_for_jobs`, `track_applied_jobs`, `web`, `analytics`, `record_costs`,
`storage/s3`, and `sync`. Their presence does not mean their behavior, schemas,
or architecture have been decided.

## Build and Run Commands

```bash
make test               # offline test suite
make lint               # ruff over src and tests
make scrape BOARD=mcf   # collect one board
```

Install with `python -m pip install -e '.[dev]'`; the Makefile calls
`.venv/bin/python`. The cleaning entrypoint is
`.venv/bin/python -m bto.clean_job_listings.run` (`--help` documents its local
interface). `deploy/README.md` and the systemd units own production arguments,
deployment and wheel builds.

## Testing Instructions

The owner's gitignored `tests/` tree uses recorded provider responses and
synthetic rows; it makes no live calls, does not touch Databricks and does not
spend money. Some fixtures are real provider captures. A public checkout
without tests is expected, not broken; never publish or force-add the suite.

## Code Style & Conventions

Name code for **what it does**, not the data it touches, and do not reuse terms.
A folder name must complete *"this folder contains code that ______."* —
`fetch_job_listings/` passes; `bronze/` does not.

There is one installable package, `src/bto/`; never manipulate `sys.path`.

- **Keep code at the narrowest useful scope**; share it only when several
  callers must agree on it. No `utils/`.
- **`storage/` holds storage mechanics and contracts, not jobs.** Each
  implemented store owns its names, connection and store-level contract guards;
  schedules and workflow failure policy belong elsewhere.
- **No board adapter may import another board adapter.**
- **One copy of the collection run loop**, driven by adapters. One run is one
  board × market execution.
- A module over 400 lines needs a reason in the commit message — a prompt to
  justify the size, not an instruction to split it.
- **Keep the nightly collection's import surface minimal.** Cleaning's runtime
  dependencies are declared in `pyproject.toml`; development/test tooling
  belongs in an extra.

Each fact has one home: the root README owns system purpose; package READMEs
own orientation; cleaning `CONTRACT.md` owns cleaning, matching and publication
guarantees; `HTML_NORMALIZATION.md` owns description/HTML behavior; data
dictionaries own schemas; comments and docstrings explain local why, source
quirks and safety boundaries.

## Boundaries & Guardrails

- No invented architecture, speculative abstractions, or cleanup nobody asked
  for. No new or heavy dependency without a concrete need and approval.
- **Bronze volumes are immutable.** Fix a parsing bug by re-reading the raw
  file, never by editing one.
- Current Silver products are rebuildable. PostgreSQL currently stores costs;
  future review/application state is unimplemented and has no decided schema.
- **Collection isolates each board × market run.** A controlled provider/run
  failure does not stop other pairs or make a normally completed collection
  command exit nonzero. Normal completion writes the finished handoff and
  permits configured downstream work. Startup/configuration failures, crashes,
  interruption and kills are process failures and may block downstream work.
- **Source health rules are adapter-specific.** Canary, response-shape, volume,
  budget and failure-rate guards differ by source. An isolated or recoverable
  zero result or provider problem is not automatically a broken board; preserve
  the source's `partial`/`failed` rules rather than inventing one global rule.
- Check rental and quota **before** spending. Validate provider output shape
  rather than pinning a build id. Never bypass login, CAPTCHA, access control or
  provider spending safeguards.
- **Cost writes fail soft**: a cost row that cannot be written is logged and
  reported, never a reason to fail the work that produced it. Record decided
  costs in the PostgreSQL `costs` schema, using the cost the provider reported
  for run-priced providers rather than inferring spend from result count.
- No production run, backfill, deployment or Databricks change unless the
  request explicitly authorizes it.

## Data & Storage Contracts

The [Databricks](src/bto/storage/databricks/data-dictionary.md) and
[PostgreSQL](src/bto/storage/postgres/data-dictionary.md) data dictionaries are
the storage contracts. Read and maintain them rather than restating schemas.

Canonical persistent Databricks names belong to `storage/databricks/`; do not
build a second registry. `DATABRICKS_CATALOG` in settings is authoritative and
must not be hardcoded. Run-scoped scratch SQL may name temporary objects.

Cleaning has exactly two persistent Silver products: `standardized_job_listings`
and `job_canonical_mapping`. Candidate pairs, evidence, diagnostics, scratch
outputs and current assignments stay non-persistent unless an explicit
architecture change approves another product. Consumers must satisfy the
freshness contract; canonical IDs are rebuildable historical evidence, not
immutable user state. The cleaning
[contract](src/bto/clean_job_listings/docs/CONTRACT.md) owns the exact rules.

## Private Search Configuration

Each board's `searches.py` is private and gitignored. Its tracked
`searches.example.py` has only safe dummy values, preserves the runtime
interface and is never imported. Tracked canaries stay with their adapters;
private production sweeps must include the applicable canary, although runtime
does not validate that relationship.

## Vocabulary

Use these words. Inventing synonyms makes the project unreadable in six months.

| Term | Means |
| :--- | :--- |
| **job listing** | One job on one board |
| **JD** | The job description text |
| **search results** | A board's list page — summary cards, no description |
| **payload** | The full raw response for one job listing, stored in `raw_job_listings` |
| **observation** | One source job seen in one board × market run |
| **evidence bank** | The owner's record of what they have actually done. Never a name for stored data |

Say **exact match** and **near match**, never "rung 1 / rung 2". `schema` means
a database schema, never the shape of a board's JSON.

## Security & Git Workflow

**This repository is PUBLIC.** Never `git add -f` private searches, tests,
fixtures/captures, validation evidence/corpora, notes, logs, databases,
snapshots, credentials, personal/CV/application data, bytecode, wheels or build
output. Check `.gitignore` before adding a new file kind.

`.env` is private. Committed `.env.example` must carry **the same keys in the
same order**, with secrets blank and only safe examples/defaults.

Keep diffs focused and preserve unrelated work; never reset, restore, stash or
clean to tidy up. Commit only what was asked for; do not amend, push or merge
unless told to.

## Agent Working Style

Be concise. Explain decisions, invariants, source quirks, safety boundaries and
material findings, not ordinary steps. Do not invent architecture, state or
cleanup; narrate commands; restate the task; or duplicate authoritative docs.
Report what changed, what was tested and remaining risk, then stop.

Never introduce internal phase-number terminology into committed code,
comments or documentation. Use functional names such as collection,
standardization, canonicalization, filtering and ranking, or notification.
