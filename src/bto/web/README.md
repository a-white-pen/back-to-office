# Web

This folder contains the BTO web application: page presentation, HTTP routes,
browser interactions, authentication and sessions. Review reads the locked PostgreSQL sorting tables.

## Structure

```text
src/bto/web/
├── __init__.py                 # package marker
├── README.md                   # package orientation
├── app.py                      # create/configure Flask and register routes
├── auth.py                     # login, sessions and permissions
├── auth_config.py              # private authentication configuration; ignored
├── auth_config.example.py      # safe example configuration
├── routes/
│   ├── __init__.py             # package marker
│   ├── review.py               # Review HTTP routes
│   ├── to_apply.py             # To apply HTTP routes
│   ├── applied.py              # Applied HTTP routes
│   └── taxonomy.py             # Taxonomy HTTP route
├── templates/
│   ├── base.html               # shared authenticated shell/navigation
│   ├── login.html              # Front Door/login page
│   ├── review.html             # Review page
│   ├── to_apply.html           # To apply page
│   ├── applied.html            # Applied page
│   ├── taxonomy.html           # Taxonomy/info page
│   ├── _listing.html           # shared listing/JD presentation
│   └── _card.html              # shared job/card presentation
└── static/
    ├── app.css                 # shared styling
    └── app.js                  # browser interactions
```

## Pages

- **Login:** the Front Door for both roles.
- **Review:** browse job listings and review their details.
- **To apply:** present application preparation and its downstream results.
- **Applied:** present submitted applications and tracking.
- **Taxonomy:** definitions/info, linked from the top-right shared navigation.
  Its current design content is stale and will be rebuilt later.

## Authentication

Private `auth_config.py` holds `SECRET_KEY` and `USERS`, including password
hashes and `admin`/`viewer` roles. The example contains dummy values only and is
not runtime configuration.

Both roles use the same login, pages and visible controls. Connected actions require admin;
viewer controls communicate view-only access. Server-side permissions reject viewer mutations. Browsing does not mutate
owner state. Unconnected actions are unavailable to both roles.

Business logic lives in the relevant BTO modules; `web/` presents it and handles
browser interaction.

## Local run

Install the `web` extra and provide ignored `.env` PostgreSQL settings and
`auth_config.py`. Run `python -m bto.web.app --host 127.0.0.1 --port 5050`.
The local user LaunchAgents keep this server and its PostgreSQL SSH tunnel
available while the Mac is awake and logged in.

Review supports grouped card/table browsing, search, filters, listing history,
wording diffs, copy matches, advertiser windows and requirement evidence. It
uses Refresh to read newly committed data without writing user state. Empty tables remain
empty; no demo jobs are generated. HTML is sanitized before display; LinkedIn
uses retained text.

To apply uses the approved Submit / Prepare shell. Applied has its approved
layout and empty state. Their application backends remain unconnected.
Taxonomy keeps navigation and the basic shell while its definitions await an
update. Stars, read state and application actions have no temporary storage.
Query logic lives in `review_jds/review_job_listings.py`; sorting rules are
reused from `filter_and_bucket_job_listings/`.
