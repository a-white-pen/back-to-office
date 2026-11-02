# filter_and_bucket_job_listings

Filtering and bucketing prepares cleaned job listings for manual Review. It
filters unwanted work, assesses job requirements against the configured profile,
assigns buckets and suppresses repeated listings.

The [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md) owns
the stored listing, model-result and cost contracts. This document explains
the workflow and its modules.

## How filtering and bucketing work

```text
cleaned Databricks listings and canonical mapping
        ↓
listing wording history in PostgreSQL
        ↓
free title and language filters, reusable stored answers
        ↓
Gemini Batch triage: is the work in scope?
        ↓
Gemini Batch extraction for listings that pass triage
        ↓
requirement evaluation and bucketing
        ↓
repeat suppression and final validation
        ↓
Review publication and terminal nightly email
```

Only distinct inputs still needing an answer go to Gemini. Validated answers
are stored and reused across matching inputs. The results feed the Review
pages, which reuse the same requirement evaluation.

## Structure

```text
src/bto/filter_and_bucket_job_listings/
├── README.md
├── __init__.py                        # bucket labels and processing-night boundaries
├── config.py                          # load private preferences and build prompts
├── run.py                             # CLI, history loads, re-sorts and paid runs
│
├── pull_job_listings.py                # cleaned listing history → PostgreSQL
│
├── filter_job_titles.py                # exclude clearly unwanted job titles
├── filter_non_english_jds.py           # exclude clearly non-English JDs
│
├── triage_job_listings.py              # build triage input and validate scope answers
├── triage_policy.example.py           # private processing window and verdict labels
├── triage_prompt.example.py
│
├── extract_job_listing_details.py      # build extraction input and validate JD details
├── extraction_prompt.example.py
│
├── evaluate_job_requirements.py        # compare requirements with the configured profile
├── bucket_job_listings.py              # apply ordered bucket and reason rules
├── suppress_repeated_job_listings.py   # link qualifying repeats over 30 collection nights
│
├── prepare_batches.py                 # reuse answers and prepare distinct unpaid inputs
├── estimate_batches.py                # estimate token usage and cost
├── configure_gemini.py                 # shared model settings, input hashes and cost calculation
├── call_gemini_batch.py                # submit, reconnect to and retrieve remote Batch jobs
├── run_batches.py                     # coordinate the resumable paid Batch workflow
│
└── preferences/
    ├── years_of_experience.example.py
    ├── job_preferences.example.py
    ├── market_preferences.example.py
    ├── skills_and_qualifications.example.py
    ├── job_title_filter.example.py
    └── employer_classification.example.py
```

The structure shows public `.example.py` files. Their private equivalents
contain the runtime preferences and prompts and are gitignored.

## Processing steps

| Step | Files | What it does | Preferences used |
| :--- | :--- | :--- | :--- |
| 1. Pull job listings | `pull_job_listings.py` | Load cleaned wording history and refresh listing facts, board closure and proven-current canonical grouping. | None |
| 2. Filter job titles | `filter_job_titles.py` | Match fail phrases and protect words. A protect phrase overrides a fail phrase. Clear failures receive the Title filter bucket without a new Gemini request. | `job_title_filter.py` |
| 3. Filter non-English JDs | `filter_non_english_jds.py` | Apply fixed script and common-English-word checks before Gemini. | None |
| 4. Triage job listings | `triage_job_listings.py`, `triage_prompt.py` | Assess whether the work is in scope, independently of the candidate's suitability; validate the verdict and supporting quote. | `job_preferences.py`, `skills_and_qualifications.py` |
| 5. Extract job listing details | `extract_job_listing_details.py`, `extraction_prompt.py` | Extract kinds of work, requirement lines and recruitment-firm/public-sector signals; validate the returned fields and evidence. | `job_preferences.py`, `skills_and_qualifications.py` |
| 6. Evaluate job requirements | `evaluate_job_requirements.py` | Produce For, Against and Other asks, missing requirements, hard exclusions and experience comparisons. | `job_preferences.py`, `years_of_experience.py`, `skills_and_qualifications.py`, `market_preferences.py` |
| 7. Bucket job listings | `bucket_job_listings.py` | Apply the ordered bucket/reason rules and resolve employer classifications using configuration and extraction. | `job_preferences.py`, `market_preferences.py`, `employer_classification.py` |
| 8. Suppress repeated job listings | `suppress_repeated_job_listings.py` | Link an earlier different listing with matching canonical group, title, bucket, reason and lacks, within the preceding 30 collection nights. | None |
| 9. Finish the night | `run_batches.py`, `run.py` | Validate the full processing window and prepare the terminal report. The nightly launcher sends one completion/failure email after Review refresh. | None |

Both model steps receive the title and the first 16,000 JD characters.
Extraction also receives the advertiser and hiring company. Supporting quotes
must match the supplied title or JD.

Bucketing uses the first matching rule. Unrecognised required named items count
as gaps: one blocks Top picks, while two distinct counting gaps make a listing
Ineligible. Configured names and aliases determine recognition. Ordinary travel
requirements appear under Other asks without changing the bucket.

Repeat windows are anchored to the wording's first-seen night. A qualifying
earlier listing has a different `(board, market, board_job_id)` identity; the
earliest qualifying listing is selected. A later wording cannot suppress
backwards. Collection nights begin at 03:00 Singapore time.

## Private preferences

Private files contain literal data assignments, not executable configuration.
The six preference examples and two prompt examples form one fictional setup.
For a new checkout, copy each example to the matching filename without
`.example`, then customise the values. Work-group IDs, experience keys, kind
names and aliases must agree across the files. Examples are not loaded as
runtime defaults.

| File in `preferences/` | What it contains | Where it is used |
| :--- | :--- | :--- |
| `years_of_experience.py` | Comfortable, stretch and limit boundaries for each work group and overall experience. | Requirement evaluation |
| `job_preferences.py` | Work groups, kinds of work, years groups and SG/HK/TH preferences; optional required-technology overrides and less-interested rules for main or present kinds. | Triage, extraction, requirement evaluation and bucketing |
| `market_preferences.py` | Work rights, monthly salary floors and minimum contract lengths. Each market also selects its short-contract bucket and whether to exclude public-sector work. | Requirement evaluation and bucketing |
| `skills_and_qualifications.py` | Languages, highest degree, qualifications, held/lacked skills and industries, packages, cannot-do groups, specific experience, aliases and optional contextual aliases. | Triage, extraction and requirement evaluation |
| `job_title_filter.py` | Fail phrases and protect words. | Title filtering |
| `employer_classification.py` | Recruitment firms and aliases, public/non-public organisations and self-hiring recruitment-licence holders. | Bucketing |

## Prompts

Private `triage_policy.py` supplies the processing start, screening labels and
normal-night spending ceiling.
Its engine roles are shared by request validation and PostgreSQL checks; the
labels remain private and preserve existing saved answers.

Private `triage_prompt.py` and `extraction_prompt.py` supply the fixed
instructions, listing template and output contract. Their public examples
show those fields and the required placeholders with illustrative instructions.

`config.py` injects kinds of work into both prompts, packages and platforms into
triage, and industries into extraction. These lists come from the private
preferences. Request builders and answer validators live in the triage,
extraction and Batch-preparation modules.

## Running

### Normal nightly run

`run.py` pulls a proven-current cleaned publication into PostgreSQL, then calls
`run_batches.py` for the two-stage paid workflow. Normal nights share one
privately configured ceiling across triage and extraction. Free rules and answer reuse run
before a fresh conservative estimate; the estimator uses recorded triage
outcomes and token usage and stops if required measurements are unavailable.
Finite request/output bounds reserve outstanding billing before submission.
Missing billing receipts stop further submissions until reconciled.

Stable input identities, saved submissions and remote job IDs support
resumption. An ambiguous submission is reconciled before another POST. Invalid
answers have at most two visible attempts; provider failures, interrupted
polling and database failures have separate bounded recovery policies. An
input Gemini refuses gets one more try; up to 10 still refused in a stage are
left `not read: Google refused` for a later night, while more, or a spending
limit refusal, stop the night. Cost
writes fail soft, with pending writes retried. There is no overall morning
cutoff or Flex fallback.

Checkpoints and email previews live under ignored `data/job_sorting/`. A shared
host lock prevents competing paid runners. Failure logs include the stage,
checkpoint and call locations; exception text and provider bodies remain private.
Short PostgreSQL advisory ownership
protects preparation and ingestion; no database connection is held while
waiting for remote Batch processing. Gemini prompt caches cover asynchronous
processing and recovery and are separate from the website's Review cache.

### Initial history and catch-up

The initial pull loads all Silver history. Processing begins at the private
processing start in `triage_policy.py`. Earlier wordings remain history-only Not
sorted, with reason `not read: before processing window`; normal processing
and re-sorts exclude them. Repeat suppression cannot use unjudged old history.
An unfinished initial load blocks ordinary nightly pulling.

Paid catch-up uses the same Batch workflow and a fresh estimate with an explicit
spending ceiling. Resume unfinished work using its existing state directory
and matching prompt, model and budget settings.

### Re-sort and notification

Free re-sorts reapply deterministic rules to linked stored answers. They do
not load model prompts, change paid input identities or queue model work.
Validated answers retain their original links after prompt edits. Failed-twice
wordings do not receive fresh paid attempts on a later run. Older stored
deferral reasons remain recognised for reprocessing.

The scheduled `bto.nightly_run` chain runs collection, cleaning, sorting,
validation and confirmed live Review publication, then sends one terminal
completion/failure email. Publication failures are saved and reported; they can
resume without repeating completed model work. Manual paid runs publish Review
with `--refresh-review` and send email with `--send-email`; otherwise they write
private email previews. Re-sorts write a preview only. Service timing,
installation and recovery belong to the [deployment guide](../../../deploy/README.md).

## Storage

| Schema | Tables |
| :--- | :--- |
| `job_sorting` | `job_listings`, `triage_results`, `extraction_results` |
| `costs` | `gemini_triage`, `gemini_extraction` |

`job_sorting.job_listings` retains cleaned wording history, model-answer links
and filtering/bucketing results. The result tables hold reusable validated
answers; the cost tables record Gemini usage and costs. Exact table contracts
belong to the [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md).
The [cleaning contract](../clean_job_listings/docs/CONTRACT.md) owns source
freshness guarantees.

## Local entry points and execution gates

Install local Review and development dependencies with
`python -m pip install -e '.[web,dev]'`. Provision private configuration
separately; [`.env.example`](../../../.env.example) documents environment keys.

Verify existing tables with `python -m bto.storage.postgres.setup`. Creation
requires explicit `--create`; setup never alters an existing contract.

Use `python -m bto.filter_and_bucket_job_listings.run` with these arguments:

| Mode | Arguments |
| :--- | :--- |
| Initial history load | `--mode history-load --execute-load` |
| Nightly processing | `--mode night --allow-gemini --send-email` |
| Paid catch-up | `--mode catch-up --allow-gemini --ceiling-usd AMOUNT` |
| Free deterministic re-sort | `--mode re-sort` |
| Bucket validation report | `--mode report` |

`--state-dir PATH` selects the checkpoint directory. History loading and
reporting do not load prompts or call Gemini. Re-sorting uses PostgreSQL and
private preferences only.

Catch-up requires an agreed ceiling from a fresh estimate. The catch-up-only
`--estimate-basis expected` option admits work by expected cost when explicitly
authorised; conservative estimates are still reported and finite submission
bounds remain. Normal nights use conservative admission. Verify current
provider pricing and quotas before paid work.

The owner's gitignored tests use mocked provider responses. PostgreSQL
integration tests require an isolated `BTO_SORTING_TEST_DSN` and reject the
production tunnel; tests make no paid calls or Databricks changes.
