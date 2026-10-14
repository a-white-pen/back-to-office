# Web

This folder contains the BTO web application: page presentation, HTTP routes,
browser interactions, authentication and sessions. The files are placeholders;
UI and authentication are not implemented.

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

Private `auth_config.py` will hold `SECRET_KEY` and `USERS`, including password
hashes and `admin`/`viewer` roles. The example contains dummy values only and is
not runtime configuration.

Both roles use the same login, pages and visible controls. Admin can use actions;
viewer controls communicate view-only access. Server-side permissions must
reject viewer mutations, and browsing must leave workflow state unchanged.

Business logic lives in the relevant BTO modules; `web/` presents it and handles
browser interaction.
