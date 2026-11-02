# Back to Office

**A job search pipeline built to make finding a job less of a full-time job.**

Back to Office runs B's searches across multiple job boards every day,
collects new and updated listings, deduplicates the same jobs appearing across
different sources, and filters and buckets what remains.

Instead of reading thousands of JDs, B reviews a shortlist in a web interface,
with the system surfacing the key reasons for and against each job. B makes the
final decision on what is worth applying for.

For selected jobs, Back to Office then tailors a CV against B's evidence bank,
researches the original job opening and relevant hiring contacts, and helps with
the application and outreach process.

Finally, it tracks what happens after applying — from recruiter interest and CV
link clicks to recruiter calls, interviews, offers and rejections.

The aim isn't to automate the decision of where B should work. It's to automate
as much of the work around that decision as possible.

This checkout implements collection, cleaning, filtering, Review and job/contact
research. CV tailoring, application and CV-link tracking, and recruiter-call
reporting live in separate prototypes. Generated prototype CVs do not yet
include tracked links; automated form filling and outreach are planned.

## Demo

**BTO v1 demo** · 1 min 50 sec

https://github.com/user-attachments/assets/de711c5e-595d-4eb0-9992-683d26a399c5

<p align="center">
  <a href="https://www.python.org/"><img height="28" alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="https://www.postgresql.org/"><img height="28" alt="PostgreSQL" src="https://img.shields.io/badge/postgresql-4169E1?logo=postgresql&amp;logoColor=white"></a>
  <a href="https://www.databricks.com/"><img height="28" alt="Databricks" src="https://img.shields.io/badge/databricks-lakehouse-FF3621?logo=databricks&amp;logoColor=white"></a>
  <a href="https://spark.apache.org/"><img height="28" alt="Apache Spark" src="https://img.shields.io/badge/engine-Apache%20Spark-E25A1C?logo=apachespark&amp;logoColor=white"></a>
  <a href="https://delta.io/"><img height="28" alt="Delta Lake" src="https://img.shields.io/badge/storage-Delta%20Lake-003366"></a>
  <a href="https://aws.amazon.com/lightsail/"><img height="28" alt="AWS Lightsail" src="https://img.shields.io/badge/deploy-aws%20lightsail-FF9900?logo=amazonwebservices&amp;logoColor=white"></a>
  <a href="LICENSE"><img height="28" alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-lightgrey"></a>
</p>

## How it works

The volumes and review times below are illustrative; actual nights vary.

```
┌─ 1 ─ find new job listings ────────────────────────────────────────────────────────────────┐
│   ├─ run B's searches across job boards such as LinkedIn, Indeed, JobStreet and            │
│   │  MyCareersFuture, across various markets e.g. Singapore, Hong Kong, Thailand           │
│   ├─ MyCareersFuture and JobStreet return summary cards, so download the job               │
│   │  description only when the card is new or its change signal moved. Indeed and          │
│   │  LinkedIn return the description with the search result, so there is nothing           │
│   │  to re-request                                                                         │
│   ├─ save each payload as raw JSON in a Databricks Bronze volume                           │
│   ├─ write one Bronze observation per job seen — board job id, change signal and           │
│   │  content hash — the state the next run compares against                                │
│   └─ record each Apify run's reported cost in PostgreSQL; a cost write that fails          │
│      is logged and reported, never a reason to fail the scrape                             │
└──────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                               │  new or edited job listings saved each day
                                               ▼
┌─ 2 ─ standardize and canonicalize job listings ────────────────────────────────────────────┐
│   ├─ standardize listings from every job board into one common schema                      │
│   ├─ retain each observation's description and derive clean text for matching              │
│   ├─ build matching features such as MinHash and compare likely duplicates across boards   │
│   └─ group listings that represent the same job under one canonical job                    │
└──────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                               │  job listings to filter and bucket
                                               ▼
┌─ 3 ─ filter and bucket job listings ───────────────────────────────────────────────────────┐
│   ├─ pull the cleaned job listings needed for processing into PostgreSQL                   │
│   ├─ rule-based title and language filters remove clearly irrelevant or ineligible         │
│   │  listings before using the LLM                                                         │
│   ├─ LLM-based triage filters out non-technical roles and other technical work outside     │
│   │  the configured job preferences                                                        │
│   ├─ structured extraction pulls the facts needed from listings                            │
│   ├─ rule-based evaluation compares those facts with the configured preferences            │
│   │  and places each job listing into its review bucket                                    │
│   ├─ suppress recent repeats with the same results                                         │
│   └─ publish the Review snapshot, then send the nightly summary                            │
└──────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                               │  ~300 new JDs to review each day
                                               ▼
┌─ 4 ─ manually review JDs ──────────────────────────────────────────────────────────────────┐
│  PostgreSQL                                                                                │
│  B reviews each JD that made it past triage and filtering in a web interface               │
│   ├─ spend ~12 seconds per JD, ~1 hour for the ~300 JDs that arrive each day               │
│   └─ back-to-office system surfaces the key points for and against each job, including     │
│      the reasoning behind its assigned bucket                                              │
└──────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                               │  ~100 jobs saved to apply
                    ┌──────────────────────────┴───────────┬───────────────────────────────────┐
                    ▼                                      ▼                                   ▼
┌─ 5a ─ tailor custom CV ──────────────┐  ┌─ 5b ─ find the direct listing ─┐  ┌─ 5c ─ enrich the job listing ──┐
│   ├─ read the JD and work out the    │  │   ├─ find the original listing │  │   ├─ identify recruiters,      │
│   │  angle: what is really being     │  │   │  for the selected job      │  │   │  likely hiring managers    │
│   │  hired for, and where B fits     │  │   ├─ prefer the employer's     │  │   │  and relevant team members │
│   ├─ match that to B's evidence bank │  │   │  careers site or its       │  │   ├─ find useful contact,      │
│   │  — real projects, numbers, dates │  │   │  authorised application    │  │   │  profile and company       │
│   ├─ check generated claims against  │  │   │  portal                    │  │   │  information               │
│   │  the bank; repair failed bullets │  │   └─ identify where to apply,  │  │   └─ keep the findings with    │
│   │  within limits and exclude       │  │      or say when no direct     │  │      their sources and why     │
│   │  unresolved ones                 │  │      listing can be found      │  │      each person is relevant   │
│   └─ render HTML and PDF for review  │  │                                │  │                                │
└──────────────────────────────────────┘  └────────────────────────────────┘  └────────────────────────────────┘
                    └──────────────────────────┬───────────┴───────────────────────────────────┘
                                               ▼
┌─ 6 ─ apply for selected jobs ──────────────────────────────────────────────────────────────┐
│   ├─ B submits job applications manually initially                                         │
│   ├─ later, OpenClaw takes over repetitive form filling, leaving applications              │
│   │  open for B to review before submission                                                │
│   └─ use the job and contact data gathered earlier to send targeted outreach               │
│      to relevant hiring managers or recruiters for selected applications                   │
└──────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                               │                          incoming recruiter call
                           ┌───────────────────┘                                     │
                           │                                                         ▼
                           │                                                    ElevenLabs
                           │                                                         │
                           ▼                                                         ▼
┌─ 7a ─ track job applications ──────────────────────┐    ┌─ 7b ─ report recruiter calls ──────────────────────┐
│   ├─ keep each application, its CV and links       │    │   ├─ receive completed recruiter calls from        │
│   │  together                                      │    │   │  ElevenLabs                                    │
│   ├─ record activity on CV links as possible signs │    │   └─ email B the call details, transcript and      │
│   │  of interest                                   │    │      recording when available                      │
│   └─ track progress, notes and next steps after    │    │                                                    │
│      applying                                      │    │                                                    │
└────────────────────────────────────────────────────┘    └────────────────────────────────────────────────────┘
```

## Structure

```text
back-to-office/
├── AGENTS.md
├── README.md
├── bto-v1-demo.mp4                   # demo video
├── Makefile
├── pyproject.toml
├── deploy/
└── src/bto/
    ├── __main__.py                      # collection, summary and preflight commands
    ├── nightly_run.py                   # nightly workflow, checkpoints and recovery
    ├── settings.py                      # env, secrets, which boards are switched on
    ├── log_errors.py                    # failure locations without private exception data
    │
    ├── fetch_job_listings/              # 1 — find and ingest new job listings
    │                                    # http_client.py and apify_client.py live in here
    ├── clean_job_listings/              # 2 — standardize and canonicalize job listings
    ├── filter_and_bucket_job_listings/  # 3 — filter jobs into review buckets
    ├── review_jds/                      # request and confirm Review publication
    ├── tailor_cvs/                      # 5a — reserved; CV tailoring prototype is separate
    ├── research_job_listing/            # 5b — find the direct listing
    ├── find_job_contacts/               # 5c — find relevant hiring contacts and contact details
    ├── apply_for_jobs/                  # 6 — reserved for applications and outreach
    ├── track_applied_jobs/              # 7a — reserved; tracking prototype is separate
    │
    ├── web/                             # 4 — manual Review, saved jobs and API
    ├── analytics/                       # reserved for data analyses
    ├── record_costs/                    # reserved; current cost writes live in storage
    ├── sync/                            # reserved namespace
    ├── send_notifications/              # send system alerts and summaries
    │
    └── storage/
        ├── databricks/                  # medallion layers and raw volumes
        ├── postgres/                    # operational database (OLTP)
        └── s3/                          # reserved for S3 integration
```

## License

[MIT](LICENSE) © 2026 Belinda H. J. Wan
