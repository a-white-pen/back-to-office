# filter_and_bucket_job_listings

After cleaning, this workflow prepares job listings for manual review: it
filters, assesses requirements, assigns review buckets and suppresses repeats.
Processing, storage and Review share the implemented deterministic rules.

## How filtering and bucketing work

`run.py` calls the pull into PostgreSQL, then applies cheap filters before
Gemini. Distinct inputs go to triage Batch; after validation, persistence and
free triage-dependent logic, missing PASS inputs go to extraction Batch.
Evaluation, bucketing, repeat suppression and final validation follow.

Manual review reuses requirement evaluation. Review page queue construction,
grouping and ordering belong to manual review.

## Structure

```text
src/bto/filter_and_bucket_job_listings/
├── __init__.py                               # package marker
├── README.md                                 # package orientation
├── config.py                                 # validate private data and render prompts
├── run.py                                    # coordinate the night and re-sorts
├── call_gemini.py                            # model identity, hashes and usage pricing
├── prepare_batches.py                        # free rules and exact-result reuse
├── estimate_batches.py                       # fresh cohort/token billing estimate
├── call_gemini_batch.py                       # durable Batch transport and correlation
├── run_batches.py                            # dependent stages, ingestion and recovery
├── filter_job_titles.py                      # cheap title fail/protect filter
├── filter_non_english_jds.py                 # cheap non-English JD filter
├── triage_job_listings.py                    # run and validate job triage
├── triage_prompt.py                          # private triage prompt; ignored
├── triage_prompt.example.py                  # safe public prompt template
├── extract_job_listing_details.py            # run and validate structured extraction
├── extraction_prompt.py                      # private extraction prompt; ignored
├── extraction_prompt.example.py              # safe public prompt template
├── evaluate_job_requirements.py              # For, Against, Other asks, lacks, walls and years
├── bucket_job_listings.py                    # apply the ordered bucket rules
├── suppress_repeated_job_listings.py         # apply the 30-day repeat rule
└── preferences/
    ├── __init__.py                           # package marker
    ├── years_of_experience.py                # private years thresholds; ignored
    ├── years_of_experience.example.py        # safe public years template
    ├── job_preferences.py                    # private work preferences; ignored
    ├── job_preferences.example.py            # safe public work template
    ├── market_preferences.py                 # private market values; ignored
    ├── market_preferences.example.py         # safe public market template
    ├── skills_and_qualifications.py          # private background data; ignored
    ├── skills_and_qualifications.example.py  # safe public background template
    ├── job_title_filter.py                   # private fail/protect lists; ignored
    ├── job_title_filter.example.py           # safe public title template
    ├── employer_classification.py            # private employer lists; ignored
    └── employer_classification.example.py    # safe public employer template

src/bto/sync/
└── pull_job_listings.py                      # transfer cleaned listings into PostgreSQL
```

## Processing steps

| Step | Files | What it does | Preferences Used |
| :--- | :--- | :--- | :--- |
| 1. Pull job listings | `../sync/pull_job_listings.py` | Load all Silver history; pull from represented state with the unfinished-run guard; refresh listing facts, closure and every applicable row's proven-current grouping. | None |
| 2. Filter job titles | `filter_job_titles.py` | Use fail phrases and protect words. Clear failures go to Out of scope · Title filter without reading the JD. | `preferences/job_title_filter.py` |
| 3. Filter non-English JDs | `filter_non_english_jds.py` | Apply fixed deterministic language rules before Gemini, independent of private preferences. | None |
| 4. Triage job listings | `triage_job_listings.py`, `triage_prompt.py`, `call_gemini.py` | Run and validate Gemini's assessment of whether the work is in scope, not whether Belinda meets its requirements. | `preferences/job_preferences.py`, `preferences/skills_and_qualifications.py` |
| 5. Extract job listing details | `extract_job_listing_details.py`, `extraction_prompt.py`, `call_gemini.py` | Run and validate Gemini extraction of kinds of work, requirement lines, recruitment-firm signal and public-sector signal. | `preferences/job_preferences.py`, `preferences/skills_and_qualifications.py` |
| 6. Evaluate job requirements | `evaluate_job_requirements.py` | Compare requirements with configured experience, qualifications and market rules; produce For, Against, Other asks, lacks, walls and years position. | `preferences/years_of_experience.py`, `preferences/skills_and_qualifications.py`, `preferences/market_preferences.py` |
| 7. Bucket job listings | `bucket_job_listings.py` | Apply ordered bucket/reason rules; finalise recruitment-firm and public-sector status using configuration and extraction where appropriate. | `preferences/job_preferences.py`, `preferences/market_preferences.py`, `preferences/employer_classification.py` |
| 8. Suppress repeated job listings | `suppress_repeated_job_listings.py` | Compare a different board listing in the 30 days before this wording's first-seen night, under today's matching bucket/reason/lacks. Titles ignore case/extra spaces; target the earliest qualifying other listing. | None |
| 9. Finish the night | `run.py`, `../send_notifications/notify.py` | Finish deterministic work and send one terminal nightly email through existing notification machinery. | None |

## Private preferences

Real files are private, gitignored and contain data only: no functions or
business logic. The public `.example.py` files
show safe dummy structures; they are not runtime imports or working defaults.

| File | What it contains | Where it is used |
| :--- | :--- | :--- |
| `years_of_experience.py` | Comfortable, stretch and limit for each work group and Overall. | Requirement evaluation |
| `job_preferences.py` | Work groups, kinds/meanings, years groups and defaults for SG, HK and TH; the SG/HK Data Engineering preference activated by a required named technology. | Triage, extraction and bucketing |
| `market_preferences.py` | Boolean work rights, salary floors and minimum contract values. Home-versus-overseas behaviour stays in rule code. | Requirement evaluation and bucketing |
| `skills_and_qualifications.py` | Languages/levels, highest degree, things held, industries held/lacked, not-strong items, packages, grouped cannot-do items including Platforms, years with tools/industries/managing people, and aliases for name matching. | Triage, extraction and requirement evaluation |
| `job_title_filter.py` | Fail phrases and protect words. | Title filtering |
| `employer_classification.py` | Recruitment-firm names/aliases, public-sector organisations, known non-public-sector organisations and self-hiring recruitment-licence holders. | Bucketing |

## Prompts

The private `triage_prompt.py` and `extraction_prompt.py` supply the production prompts.
Their public `triage_prompt.example.py` and `extraction_prompt.example.py`
templates show the listing-specific closing instruction and output-contract
positions. Triage injects `kinds_of_work` and `packages`; extraction injects
`kinds_of_work` and `industries`. Values come from
`preferences/job_preferences.py` and `preferences/skills_and_qualifications.py`;
prompt files do not duplicate the lists.

## Running

### Normal nightly run

`run.py` reconciles a proven-current cleaned publication into PostgreSQL, then
runs triage and extraction through Gemini Batch with one shared USD 10 ceiling.
There is no morning model-processing cutoff. Stable item IDs, immutable private
submission records and saved remote job IDs allow safe resumption. Network loss
reconnects to the same job. PostgreSQL ownership is released while Google works.

Exact results and free filters run before a fresh estimate. Conservative
expected billing must fit the authorised ceiling; finite request/output bounds
also protect outstanding asynchronous billing. Invalid visible answers retain
the two-attempt rule. Provider errors, submission uncertainty, polling loss and
database failures have separate bounded recovery policies.

### Initial history and catch-up

Load all Silver history. Processing begins at **3:00 AM Singapore time on
9 Oct 2026**; 9 Oct is read afresh under the new contracts. Earlier wordings
are history-only Not sorted, with reason `not read: before processing window`,
excluded from the normal queue and preference re-sorts. Initially incomplete
suppression against unjudged old history is accepted.

Paid catch-up uses the same two-stage Batch path and a fresh estimate, under
explicit spend authority. Historical Flex ledgers remain separate retained
records. Any explicit prompt cache covers asynchronous processing and recovery.

### Re-sort and notification

Free preference re-sorts reuse stored results; repeat windows stay anchored to
`wording_first_seen_at`. Existing notification machinery uses the approved
[template sources](../send_notifications/templates/). Bucket totals count
new/changed listing wordings first pulled tonight; carried-forward work counts
in read/cost totals. The complete nightly chain sends one terminal completion/failure email at its
actual finish. Failed/partial collection and routine notes are included there;
there are no per-stage success messages or arbitrary morning status messages.

## Storage

| Schema | Tables |
| :--- | :--- |
| `job_sorting` | `job_sorting.job_listings`, `job_sorting.triage_results`, `job_sorting.extraction_results` |
| `costs` | `costs.gemini_triage`, `costs.gemini_extraction` |

`job_sorting.triage_results` and `job_sorting.extraction_results` store reusable
triage and extraction results. `costs.gemini_triage` and
`costs.gemini_extraction` store Gemini call costs. Their contracts are defined in
the [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md).
`job_sorting.job_listings` stores cleaned listing wordings, history, reusable
Gemini-result links and filtering/bucketing results. Its contract is also
defined there.

## Local entry points and execution gates

Install with `python -m pip install -e '.[web,dev]'`. Private files are literal
assignments, validated before processing. The safe examples show their shapes.

- Verify the existing locked tables: `python -m bto.storage.postgres.setup_job_sorting`.
  Setup creates missing tables only with the explicit `--create` flag; it never
  repairs or alters an existing contract.
- History load, after approval: `python -m bto.filter_and_bucket_job_listings.run
  --mode history-load --execute-load`. This mode never loads Gemini keys or calls it.
- Approved nightly execution: `python -m bto.filter_and_bucket_job_listings.run
  --mode night --allow-gemini --send-email`.
- Approved catch-up: use `--mode catch-up --allow-gemini --ceiling-usd AMOUNT`;
  the ceiling must be explicitly agreed from a fresh estimate. If the owner explicitly
  authorises admission by expected cost, add `--estimate-basis expected`; the
  conservative estimate is still reported and finite submission bounds still
  protect the remaining ceiling. Normal nights keep conservative admission.
- Free deterministic re-sort: `python -m bto.filter_and_bucket_job_listings.run
  --mode re-sort`.
- Bucket validation: `python -m bto.filter_and_bucket_job_listings.run --mode report`.

Runtime checkpoints and email previews live in ignored `data/job_sorting/`.
A partial pull retains its source versions, original watermark and completed
buckets. Resume with the same mode and state directory. An incomplete history
load blocks ordinary pulling. Results and costs commit as calls complete;
validated identical inputs share results across rows. A canonical host-level guard prevents competing Batch submissions. Short
PostgreSQL advisory ownership protects preparation and idempotent ingestion;
no database ownership is held during remote processing.

The fetch timer invokes `bto.nightly`, which runs collection → cleaning
→ Batch sorting → final validation → serving snapshot refresh → one terminal
email. Standalone collection/cleaning commands retain their manual alerts.
Scheduled sorting confirms the existing authenticated cache rebuild before
completion. Manual runs refresh it only with explicit `--refresh-review`.

Tests require an explicitly isolated `BTO_SORTING_TEST_DSN`; integration tests
reject the production tunnel. Provider responses are mocked. Initial history,
paid verification/catch-up, real recipient emails and public deployment require
separate approval. Before any paid run, verify reported tiers/usage, current
pricing, caching, eligible inputs, retry allowance and runtime.

Scheduled sorting uses `--refresh-review` to confirm the existing serving
snapshot refresh after final validation. Private serving credentials and
rollback/recovery steps belong to the [deployment guide](../../../deploy/README.md).
