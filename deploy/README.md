# Deployment

BTO runs on an Ubuntu AWS Lightsail host in Singapore. The reviewed checkout
lives at `/opt/bto`, its virtual environment at `/opt/bto/.venv`, and its single
deployed wheel at `/opt/bto/wheels/`. Secrets are outside the repository in the
root-owned `0600` file `/etc/bto/env`, loaded by systemd through
`EnvironmentFile=`. Source-controlled units live in [systemd/](systemd/) and
are installed in `/etc/systemd/system/`.

The host accepts SSH only. PostgreSQL is not exposed to the internet.

## Deployment flow

```text
fetch-job-listings.timer (03:30 SGT nightly)
    └──> fetch-job-listings.service
         │    a provider outcome of ok, partial or failed is not a
         │    non-zero exit; a failed board is not a failed service
         │
         ├── safe completion (exit 0)
         │   ├──> send-job-listings-summary-email.service
         │   └──> clean-job-listings.service
         │        └──> Databricks
         │
         └── unsafe termination (non-zero: crash, configuration, kill)
             ├──> send-job-listings-summary-email.service
             └──> fallback alert: cleaning skipped
```

The fetch timer has `Persistent=false`, so a firing missed while the host is
down is skipped rather than run late. The summary service runs after every
fetch execution and attempts email delivery. Cleaning runs only after exit 0
from the fetch and has no timer of its own.

Exit 0 means collection completed normally and wrote its finished handoff.
Provider outcomes remain `ok`, `partial`, or `failed`, but none blocks cleaning
of the Bronze observations that landed. A crash, configuration failure, or kill
exits non-zero and blocks cleaning. The fetch fallback then attempts an explicit
skipped-cleaning alert.

Both success targets are intentionally listed on one space-separated
`OnSuccess=` directive. systemd activates both; neither replaces the other.

## systemd units

| Unit | Purpose |
| :--- | :--- |
| `fetch-job-listings.timer` | Starts the nightly fetch at 03:30 SGT. |
| `fetch-job-listings.service` | Runs all configured board × market fetches. It does not restart automatically. Its fallback attempts to report unsafe termination and skipped cleaning. |
| `send-job-listings-summary-email.service` | Attempts to send one success/failure summary for the scheduled fetch execution. It has no timer. |
| `clean-job-listings.service` | Runs incremental standardization, reconciliation, and Databricks canonicalization after a successful fetch. It has no timer or automatic restart. |

Cleaning uses the production command:

```bash
/opt/bto/.venv/bin/python -m bto.clean_job_listings.run \
  --stage nightly --engine databricks --wheel /opt/bto/wheels
```

The initial production Silver tables require a deliberate, explicitly approved
full-history build before the nightly cleaning chain is activated. The rules
for that build belong to the
[cleaning README](../src/bto/clean_job_listings/README.md) and
[contract](../src/bto/clean_job_listings/docs/CONTRACT.md), not this deployment guide.

## Installing or redeploying

1. Sync the reviewed checkout to `/opt/bto`.
2. Install or update the application in `/opt/bto/.venv`:

   ```bash
   cd /opt/bto
   .venv/bin/python -m pip install -e .
   ```

3. Export the reviewed commit to a clean temporary tree and build one wheel
   there, so ignored private files from the live checkout cannot be packaged:

   ```bash
   build_dir="$(mktemp -d)"
   git archive --format=tar HEAD | tar -x -C "$build_dir"
   /opt/bto/.venv/bin/python -m pip wheel --no-deps \
     -w "$build_dir/dist" "$build_dir"
   ```

4. Inspect the wheel before deployment:

   ```bash
   /opt/bto/.venv/bin/python -m zipfile -l "$build_dir"/dist/bto-*.whl \
     | grep '/searches'
   ```

   Every match must end in `searches.example.py`; private `searches.py`
   implementations must be absent.
5. Replace the deployed wheel in `/opt/bto/wheels/`; that directory must hold
   exactly one `bto-*.whl`.
6. Copy the unit files from `deploy/systemd/` on an initial install, or only
   changed units on a redeploy, to
   `/etc/systemd/system/`, then reload systemd:

   ```bash
   sudo systemctl daemon-reload
   ```

7. On an initial install, after the approved full-history build is complete,
   enable and start the timer that activates the nightly chain:

   ```bash
   sudo systemctl enable --now fetch-job-listings.timer
   ```

   The cleaning and summary services are static; the fetch service activates
   them through `OnSuccess=` and `OnFailure=`.

Install local development and parity-test dependencies with
`python -m pip install -e '.[dev]'`.

## Canonicalization wheel

The one deployed wheel is built from the same reviewed checkout as the source
installation and is reused by nightly runs until the next deployment.
Databricks may cache an installed package version, so any deployment that
changes executable wheel code must first assign a new `version` in
`pyproject.toml`. Never deploy different wheel bytes under a version already
used. Wheels and `dist/` are not committed.

## Logs and inspection

```bash
systemctl status fetch-job-listings.service
systemctl status send-job-listings-summary-email.service
systemctl status clean-job-listings.service
systemctl list-timers fetch-job-listings.timer
journalctl -u fetch-job-listings.service
journalctl -u send-job-listings-summary-email.service
journalctl -u clean-job-listings.service
```

journald is persistent, so logs survive host restarts.

## Alerts and failures

The summary service attempts a fetch summary after success or failure.
Successful production cleaning attempts its own summary and reports handled
problems separately; a run with quarantined rows attempts both. Email delivery
does not change whether the underlying operation succeeded or failed. If a
handled cleaning failure's Python alert is not delivered, cleaning exits 6 so
systemd can attempt its fallback notification. The fetch and cleaning services
have `ExecStopPost=` fallbacks for failures Python could not report; the summary
service does not. The fetch fallback also attempts to report that cleaning was
skipped. If any email delivery fails, or if the interpreter, source tree, or
environment file is unavailable, rely on systemd status and journald.

No service automatically restarts. Diagnose the failure before a manual rerun;
otherwise the next successful nightly fetch starts cleaning again.

## PostgreSQL

PostgreSQL is BTO's operational store and currently records provider costs. It
listens only on loopback; port 5432 is never opened to the internet. Remote
administration uses an SSH tunnel, with the database client connecting to
`localhost:5432` through that tunnel. SSH identity and database credentials
remain local secrets; runtime database settings come from `/etc/bto/env`.

The [PostgreSQL data dictionary](../src/bto/storage/postgres/data-dictionary.md)
owns the data contract.

## Recovery rules

- Never edit, reset, or delete Bronze data to recover cleaning. Fix the cause
  and rebuild or rerun from the immutable source data.
- Do not overlap a manual fetch with the finalization of a full-history
  canonicalization build; new observations can invalidate its final
  reconciliation.
- Before any manual rerun, confirm no collection or cleaning process is active
  and read the relevant journal. Ordinary nightly cleaning work is atomic and
  safe to retry after the cause is fixed.
