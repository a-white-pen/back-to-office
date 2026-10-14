# filter_and_bucket_job_listings

After cleaning, this workflow prepares job listings for manual review: it
filters, assesses requirements, assigns review buckets and suppresses repeats.
This is a scaffold; processing will follow the agreed specification.

## How filtering and bucketing work

`run.py` calls the pull into PostgreSQL, then applies cheap filters before
Gemini. Extraction starts as individual listings pass triage, so triage and
extraction overlap. Evaluation, bucketing and repeat suppression follow, then
the morning email.

Manual review reuses requirement evaluation. Review page queue construction,
grouping and ordering belong to manual review.

## Structure

```text
src/bto/filter_and_bucket_job_listings/
├── __init__.py                               # package marker
├── README.md                                 # package orientation
├── run.py                                    # coordinate the night and re-sorts
├── call_gemini.py                            # shared cache, pricing, retries and spend ceiling
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
| 9. Finish the night | `run.py`, `../send_notifications/notify.py` | Finish deterministic work and send the morning email through existing notification machinery. | None |

## Private preferences

Real files are private, gitignored and contain data only: no functions or
business logic. Values remain unconfigured. The public `.example.py` files
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

The private `triage_prompt.py` and `extraction_prompt.py` remain unpopulated.
Their public `triage_prompt.example.py` and `extraction_prompt.example.py`
templates show the listing-specific closing instruction and output-contract
positions. Triage injects `kinds_of_work` and `packages`; extraction injects
`kinds_of_work` and `industries`. Values come from
`preferences/job_preferences.py` and `preferences/skills_and_qualifications.py`;
prompt files do not duplicate the lists.

## Running

### Normal nightly run

`run.py` calls the state-based pull after cleaning. Triage/extraction use
**Flex throughout**, with **one shared USD 10 ceiling**. At **8:30 AM Singapore
time**, stop starting new reads; sent calls finish before free processing and
the normal morning summary. Deferred work carries forward; failed-twice
wordings are terminal.

### Initial history and catch-up

Load all Silver history. Processing begins at **3:00 AM Singapore time on
9 Oct 2026**; 9 Oct is read afresh under the new contracts. Earlier wordings
are history-only Not sorted, with reason `not read: before processing window`,
excluded from the normal queue and preference re-sorts. Initially incomplete
suppression against unjudged old history is accepted.

The paid catch-up is Flex only without the ordinary cutoff. Recalculate calls,
cost and runtime immediately beforehand and obtain Belinda's explicit go;
the cache covers the whole run.

### Re-sort and notification

Free preference re-sorts reuse stored results; repeat windows stay anchored to
`wording_first_seen_at`. Existing notification machinery uses the approved
[template sources](../send_notifications/templates/). Bucket totals count
new/changed listing wordings first pulled tonight; carried-forward work counts
in read/cost totals. Failed runs or partials with lost search coverage get the
collection-failed email; other partials are morning notes.

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
