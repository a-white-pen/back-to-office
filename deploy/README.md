# Deployment

BTO runs on an Ubuntu AWS Lightsail host in Singapore. The reviewed checkout
lives at `/opt/bto`, its virtual environment at `/opt/bto/.venv`, and its single
deployed wheel at `/opt/bto/wheels/`. Secrets are outside the repository in the
root-owned `0600` file `/etc/bto/env`, loaded by systemd through
`EnvironmentFile=`. Source-controlled units live in [systemd/](systemd/) and
are installed in `/etc/systemd/system/`.

The host accepts SSH and serves the existing web front ends behind Caddy.
PostgreSQL is not exposed to the internet.

## Nightly workflow

One service runs the complete workflow. Keep the source, wheel, private
configuration and notification-ownership changes together when deploying.

```text
fetch-job-listings.timer (03:30 SGT nightly)
    → fetch-job-listings.service → python -m bto.nightly
    → collection → cleaning / canonicalization → PostgreSQL reconciliation
    → free preparation → triage Batch → validated triage ingestion
    → free dependent logic → extraction Batch → validated extraction ingestion
    → deterministic rules → repeat suppression → full-window validation
    → connected Review publication → one terminal email
```

The timer keeps `Persistent=false`. The workflow invokes the existing collection and cleaning commands;
each stage retains its own bounded retry policy. Collection still isolates
board × market outcomes. The workflow verifies the finished collection handoff. Controlled board
outcomes retain their existing semantics: cleaning processes the observations
that landed, and the terminal report explains any lost coverage. A terminal failure sends one failure email;
a healthy asynchronous Batch has no morning cutoff or systemd start timeout.
No separate stage-success or arbitrary morning status emails are sent.

Private run state, submitted inputs, remote job IDs and downloaded results live
under ignored `data/job_sorting/`. Resume the saved logical run after process
or network loss. Never start a fresh directory to bypass an unfinished Batch.
Interrupted upstream collection/cleaning requires inspection before rerunning;
paid collection is not restarted automatically in the same collection night.
A later scheduled night can collect the missing observations through its normal
diff. Unfinished sorting resumes its saved logical run before another collection. PostgreSQL ownership is released
while Google processes a Batch. The serving snapshot refresh runs only after final deterministic validation.
`review_jds/refresh_review_cache.py` uses the existing admin login and CSRF token
to POST the existing `/review/cache/rebuild` action. It waits for a newer,
completed snapshot before success. The UI exposes minute precision, so the
request crosses a minute boundary before rebuilding. Reads have bounded
recovery; a lost rebuild receipt is checked by reads rather than another POST.
The existing builder permits 15 minutes per build. The handoff allows up to
35 minutes for an in-progress build, the requested fresh build and read recovery.
Failure to confirm freshness within that bound is a terminal publication
failure. The previous snapshot remains usable. No serving UI code is deployed.

Required private configuration adds `GEMINI_TRIAGE_JOBS_API_KEY`,
`GEMINI_EXTRACT_JOB_DETAILS_API_KEY`, `BTO_REVIEW_REFRESH_URL`,
`BTO_REVIEW_REFRESH_USERNAME` and `BTO_REVIEW_REFRESH_PASSWORD` to `/etc/bto/env`.
The last two are the existing admin login. The URL is the HTTPS serving origin.
Provision the six private preference modules and two prompt modules separately
with owner-only read permissions; none belongs in an archive or public wheel.
Existing PostgreSQL, Databricks, Apify and SMTP configuration remains in place.
Normal nights retain the shared USD 10 Batch ceiling. No Flex fallback or
morning processing cutoff is used.

## systemd units

| Unit | Purpose |
| :--- | :--- |
| `fetch-job-listings.timer` | Starts the nightly fetch at 03:30 SGT. |
| `fetch-job-listings.service` | Runs the complete reviewed nightly Python workflow. No automatic restart; a shared terminal-email checkpoint prevents duplicate fallback messages. |
| `send-job-listings-summary-email.service` | Standalone collection summary command; the full nightly workflow does not invoke it. |
| `clean-job-listings.service` | Standalone cleaning command. The full nightly workflow invokes cleaning directly; this unit has no timer or automatic restart. |

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

   Cleaning and summary services remain available as standalone commands. The
   nightly service invokes the commands directly; it has no `OnSuccess=` or
   `OnFailure=` targets. Do not also enable another sorting trigger.

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

## Alerts, resumption and rollback

The orchestrator sends one completion email only after sorting validation and
the serving cache refresh. A terminal failure sends one failure email after
that stage's bounded recovery; local/network polling loss keeps the same remote
Batch ID. There are no routine collection/cleaning-success emails or timed
morning status email in this workflow. Standalone manual commands retain their
existing notifications.

`data/job_sorting/nightly.json` records the logical run and attempted terminal
email. `runs/<run_id>/batch.json` holds immutable submission identities, remote
IDs and ingestion progress. Restarting an interrupted sorter resumes that run;
a completed same-night chain exits without collecting or emailing again.
Upstream execution keeps its original six-hour collection and four-hour cleaning
bounds; Batch and the overall service have no processing cutoff.

Before deployment, back up source, virtual environment, wheel, units and private
environment on the host in a root-only rollback directory. Preserve unrelated
deployed packages/services; copy only the reviewed application files. Export the
reviewed commit for the wheel, never the live directory containing private files.

To roll back while no nightly chain is active, restore the saved source, venv,
single wheel, environment and units, then run `systemctl daemon-reload` and
confirm the original 03:30 timer. Keep PostgreSQL data, all new runtime ledgers,
remote Batch IDs and downloaded results; a rollback does not cancel or erase
paid work. Never run two orchestrators, or a legacy fetch/clean chain alongside
an unfinished Batch run. Inspect active remote work before any rollback during
execution. The serving UI and its application state are outside this deployment.

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
