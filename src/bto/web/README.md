# web

The web application presents filtered job listings for manual Review and keeps
jobs saved to To Apply. It handles pages, browser interactions, authentication
and permissions. Job-listing research and contact finding supply the Links
and People panels.

The [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md) owns
stored listing, Review-state and research contracts. This document explains
the pages and how they are served.

## How the website works

```text
sign in as admin or viewer
        ↓
Review ← filtering and bucketing results in PostgreSQL
        browse JDs, requirements, copies and listing history
        admin can mark read, star and save
        ↓ save a job
To Apply
        ├── Links → job-listing research starts automatically
        └── People → contact finding starts automatically when also starred
        ↓
search results and status return to the saved job's panels
```

Admins can request searches and individual contact lookups from To Apply.
Application preparation, CV tailoring, submission and Applied tracking remain
unavailable. **How it works** explains the nightly workflow and interface.

## Structure

```text
src/bto/web/
├── README.md
├── __init__.py
├── app.py                            # create Flask, register routes and run locally
├── auth.py                           # login, sessions and admin/viewer permissions
├── auth_config.example.py            # safe authentication template
├── auth_config.py                    # private accounts and session secret; ignored
│
├── review_job_listings.py             # listing queries, grouping, details and requirements
├── review_table.py                    # table columns, values and sorting
├── review_empty.py                    # filter changes that can recover an empty queue
├── saved_job_controls.py              # filter and order saved jobs
├── review_cache.py                    # recent metadata, listing details and prefetch
├── review_cache_memory.py             # object budgets and memory-pressure checks
├── build_review_snapshot.py           # build, save and coordinate snapshot publication
│
├── routes/
│   ├── __init__.py
│   ├── review.py                     # Review pages, details, actions and cache endpoints
│   ├── to_apply.py                   # saved jobs, research requests and lookup actions
│   ├── applied.py                    # Applied page shell
│   └── how_it_works.py               # How it works page
│
├── templates/
│   ├── base.html                     # shared page shell and navigation
│   ├── login.html
│   ├── review.html
│   ├── to_apply.html
│   ├── applied.html
│   ├── how_it_works.html             # the nightly run, labels, emails, costs and keys
│   ├── _listing.html                 # selected JD and listing facts
│   ├── _review_table.html            # listing table
│   ├── _review_role.html             # role assessment and requirement sections
│   ├── _review_seen.html             # first-seen date menu and calendar
│   ├── _review_history.html          # owner history and other jobs from the advertiser
│   ├── _review_status.html           # filters and browsing status
│   └── _review_empty.html            # empty-queue alternatives
│
└── static/
    ├── app.css                       # shared page styling
    ├── app.js                        # shared interactions and To Apply research panels
    ├── review.css                    # Review styling
    └── review_table.js               # Review table, navigation and state actions
```

## Review and saved jobs

Review reads the automated
[filtering and bucketing results](../filter_and_bucket_job_listings/README.md).
The selected job shows its JD, copies, wording changes and requirements under
**For**, **Against** and **Other asks**, using the same requirement evaluator.
Private filtering preferences are needed for those assessments.

The table starts at 25 rows; the browser remembers the 25/50/75/100 choice.
Sorting applies to the complete filtered results before pagination. Read-state
changes keep the current browsing order and update the counts. **Rebuild
snapshot** resets that browsing selection; it does not run the nightly
metadata builder.

**I'll apply**, W or Enter saves the job, marks it read and advances. Shift
also stars it. A short-lived Undo restores the compound state change. Saved
jobs remain in To Apply outside Review's filters and can be filtered by
priority or advertiser type and ordered by priority or newest added.

Read, star and save state belongs to one shared owner, with the database
contract preserving it as canonical groups change. Merely browsing does not
mark a job read. **Your history** currently lists jobs saved to To Apply at the
same advertiser; submitted applications are not connected.

Collection-night dates come from stored listing history. The UI has no
separate collection-run ledger, so it cannot report exact fetch start/end
times or distinguish a missed run from a run that found no listings.

## Authentication

Both roles use the same login and pages. Admins can change Review state and
request research or contact lookups. Viewers can browse the shared results;
server-side checks reject their changes. Unimplemented actions remain
unavailable to both roles.

Copy `auth_config.example.py` to ignored `auth_config.py`, set a private random
`SECRET_KEY` and replace the dummy password hashes with real Werkzeug hashes.
`USERS` assigns each account an `admin` or `viewer` role. Account names and
password hashes stay in the private file; the public login page names no
account. The example is never loaded at runtime. Form submissions use CSRF
protection.

## Serving Review

PostgreSQL remains the source of listing history and owner state. With
`BTO_REVIEW_HOT_CACHE=1`, the server keeps a saved metadata snapshot for recent
listings and loads JD details as needed. The browser receives its current
table page and selected job.

| Data | How it is read |
| :--- | :--- |
| Recent listing metadata | Up to seven complete first-seen collection nights, all buckets, within 96 MiB. |
| Selected JDs and context | A shared cache of recently used details, up to 24 MiB and 500 entries; one worker can prepare the next jobs. |
| Older or uncovered searches | PostgreSQL queries, with an optional results cache bounded to 8 MiB. |
| Read, star and save state | Read from PostgreSQL for each request, separately from the metadata snapshot. |

The metadata snapshot covers whole nights. If the full requested date range
is not covered, PostgreSQL supplies the complete results and counts. Cache
budgets measure retained objects; the process also needs memory for Python,
requests and database buffers. Optional caches and prefetch pause under memory
pressure.

A loaded snapshot can keep metadata browsing available during a database
outage. Live state is then unknown, and changes are disabled until PostgreSQL
reconnects.

Unexpected route, search and cache failures log their exception type and call
frames. Exception messages and local values are omitted because they may
contain private data. Logs retain the module, timestamp and relevant listing
or publication identifiers.

### Nightly refresh

After sorting, suppression and validation, the nightly workflow calls
[`review_jds/refresh_review_cache.py`](../review_jds/refresh_review_cache.py).
It authenticates, requests a new publication and waits for that exact version
to become available. Manual Review's pages and actions live here in `web/`.

With external publication configured on the serving host:

```text
nightly requests publication
        ↓
pause new changes and wait for current requests and research work
        ↓
build and save a candidate in a separate, memory-limited process
        ↓
briefly pause remaining requests, install the candidate and restart the web service
        ↓
confirm the loaded version and HTTP readiness
        ↓
nightly sends its terminal email
```

The nightly caller runs `build_review_snapshot.py` synchronously. The old
snapshot serves reads during the build; the previous saved version is kept
until the replacement is verified. Failed publication preserves or restores
it and is reported in the nightly email without repeating completed model
work. The temporary systemd scope limits the helper; it adds no publication
service, watcher or timer.

External publication requires `BTO_REVIEW_EXTERNAL_PUBLICATION=1`, a private
state directory and a serving launcher that starts Review warming and calls
`build_review_snapshot.install_handover(app)`. The handover hook lets the helper
wait for active work and confirm the loaded version; `create_app()` does not
install it automatically. The serving host also needs the helper's service
permissions and paths. These are deployment requirements, not part of the
local development command.

Serving-host details belong in ignored `private/review_publication.json`, or
a file selected by `BTO_REVIEW_PUBLICATION_CONFIG`. Copy the generic
[host configuration example](publication_host.example.json) and configure the
service, interpreter, checkout, state directory, account and loopback health
address for that host. Configure any additional job tables and worker-thread
selectors so publication waits for all paid work, including work outside this
package. Empty selector lists must be an explicit choice for that deployment.

Before enabling publication, provision the configuration as a root-owned
regular file that is not writable by group or others. It must be readable by
the nightly and serving accounts. The privileged helper refuses missing or
untrusted configuration; it does not fall back to a different service or skip
the host's work checks. Keep the sudo policy restricted to the configured
helper command. Host configuration and private environment values are deployed
separately and are never included in the public package.

### Local snapshot

Set `BTO_REVIEW_HOT_CACHE=1` and a private `BTO_REVIEW_SNAPSHOT_FILE` to enable
saved snapshots locally. A service can instead supply `STATE_DIRECTORY`.
Startup uses the last compatible saved snapshot while checking for updates;
without one, it builds in the background.

Private `BTO_REVIEW_REFRESH_URL`, `BTO_REVIEW_REFRESH_USERNAME` and
`BTO_REVIEW_REFRESH_PASSWORD` let localhost follow completed live publications.
It checks their status, then rebuilds from its configured PostgreSQL database.
Snapshot files are not copied from the live site.

## Running locally

Provide private authentication, PostgreSQL settings in the environment or
ignored `.env`, and the private filtering preferences used by Review.
The sorting, Review and research tables must already exist;
[`storage/postgres/setup.py`](../storage/postgres/setup.py) owns their setup
and checks. Website startup does not create them.

```bash
python -m pip install -e '.[web]'
python -m bto.web.app --host 127.0.0.1 --port 5050
```

The command binds to localhost. Research and contact finding also need the
provider settings described in their
[research](../research_job_listing/README.md) and
[contact-finding](../find_job_contacts/README.md) READMEs.
