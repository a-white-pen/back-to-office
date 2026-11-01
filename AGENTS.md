# Repository guidance

## Project Overview

Back to Office (BTO) collects, cleans, deduplicates and buckets job listings
for manual Review. Saved jobs can request direct-listing and contact research.

The implemented packages are `fetch_job_listings`, `clean_job_listings`,
`filter_and_bucket_job_listings`, `review_jds`, `research_job_listing`,
`find_job_contacts`, `send_notifications`, `web`, `storage/databricks` and
`storage/postgres`. Databricks holds raw and cleaned listings; PostgreSQL holds
sorting history, Review state, research results and operational costs.
`nightly_run.py` coordinates collection, cleaning, Gemini Batch sorting, Review
publication and the terminal email.

`tailor_cvs`, `apply_for_jobs`, `track_applied_jobs`, `analytics`, `record_costs`,
`sync` and `storage/s3` contain only initializers.

## Build and Run Commands

```bash
make test               # offline test suite
make lint               # ruff over src and tests
make scrape BOARD=mcf   # collect one board
```

Create `.venv` with `python -m venv .venv`, then install with
`.venv/bin/python -m pip install -e '.[dev,web]'`. The Makefile uses that Python.
The cleaning entrypoint is
`.venv/bin/python -m bto.clean_job_listings.run` (`--help` documents its local
interface). `deploy/README.md` and the systemd units own production arguments,
deployment and wheel builds.

## Testing Instructions

`make test` and `make lint` include `private/tests/` when present. Run one
test with `.venv/bin/python -m pytest -q tests/path/test_file.py::test_name`.

Tests are offline against synthetic rows and recorded provider responses.
They make no live provider calls, touch no Databricks data and spend no money.
PostgreSQL integration tests require a separate local test database, never
production; they skip when it is not configured.

All tests, including synthetic tests, stay gitignored and must never be
committed. Tests needing absent private fixtures must skip with a reason.

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

Keep system purpose in the root README and package orientation in package
READMEs. Comments and docstrings explain local why, source quirks and safety
boundaries; do not duplicate authoritative documentation.

## Boundaries & Guardrails

- No invented architecture, speculative abstractions, or cleanup nobody asked
  for. No new or heavy dependency without a concrete need and approval.
- **Bronze volumes are immutable.** Fix a parsing bug by re-reading the raw
  file, never by editing one.
- Preserve Review read, star and save state as canonical groups change.
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

Cleaning persists only `standardized_job_listings` and `job_canonical_mapping`
in Silver. Intermediate pairs, evidence, diagnostics and assignments stay
non-persistent unless an architecture change is explicitly approved. These
products are rebuildable; canonical IDs are historical evidence, not immutable
user state. Follow the cleaning
[contract](src/bto/clean_job_listings/docs/CONTRACT.md) for matching, publication
and consumer freshness, and
[HTML_NORMALIZATION.md](src/bto/clean_job_listings/docs/HTML_NORMALIZATION.md)
for description/HTML behavior.

## Private Configuration

Each board's `searches.py`, filtering preferences, production prompts and
web authentication stay private and gitignored. Tracked `*.example.*` files
must preserve their runtime interfaces, contain only safe dummy values and
never load implicitly.

Keep canaries with their adapters. Production searches must include the
applicable canary; runtime does not validate that relationship.

**`private/` is a separate, never-published Git repository**, optionally
checked out inside this working copy and ignored here. It holds private policy
files and tests. Locate it through `bto.settings.private_dir()`, handle its
absence explicitly and never copy its contents into tracked public files.

## Vocabulary

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

Be concise. Explain decisions, invariants, source quirks, safety boundaries
and material findings. Do not narrate routine commands or restate the task.
Report what changed, what was tested and remaining risk, then stop.

Never introduce internal phase-number terminology into committed code,
comments or documentation. Use functional names such as collection,
standardization, canonicalization, filtering and bucketing, or notification.
