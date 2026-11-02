# Deployment

BTO runs on an Ubuntu AWS Lightsail host in Singapore. The nightly checkout is
`/opt/bto`, its virtual environment is `/opt/bto/.venv`, and `/opt/bto/wheels/`
holds one deployed wheel. systemd loads secrets from `/etc/bto/env`, owned by
root with mode `0600`.

Caddy serves the web front ends. PostgreSQL listens on loopback only; remote
administration uses an SSH tunnel. Its table contracts belong to the
[PostgreSQL data dictionary](../src/bto/storage/postgres/data-dictionary.md).

## What runs each night

```text
nightly-run.timer (03:30 SGT)
    → nightly-run.service
    → collect job listings
    → clean and canonicalize
    → load into PostgreSQL, filter and bucket (including Gemini Batch work)
    → refresh the live Review cache and verify it loaded
    → send one terminal email
```

The timer skips missed firings (`Persistent=false`). The next collection compares
then-current board results with saved state; listings removed during downtime
may no longer be available. Controlled board failures
do not stop the other board × market runs or block cleaning of usable results.
The email reports any lost coverage.

The workflow attempts a completion email only after Review publication is
confirmed. A reported stage failure produces a failure email; publication
failures appear under **Rules & Review**, with a reason. systemd also attempts a
fallback alert if the process stops before Python can report. Delivery failures
are logged; email delivery is not guaranteed.

The nightly Python launcher enforces these whole-stage timeouts:

| Stage | Time limit |
| :--- | :--- |
| Collection | Six hours. |
| Cleaning: standardization and canonicalization | Four hours. |
| Filtering/bucketing, including Gemini triage and extraction | No overall morning cutoff; individual retry and spending guards still apply. |

If collection or cleaning exceeds its limit, the launcher kills that local
command, stops the remaining workflow and reports the failure in the email.
Remote provider work has its own limits; inspect its state before rerunning an
interrupted stage. Cleaning does not include Gemini triage or extraction.

A failure does not trigger an immediate systemd restart loop. The timer still
starts the service at 03:30 SGT the next day, provided it is no longer running.
The next invocation either resumes unfinished sorting or starts a new night's
collection, as described under recovery below.

## Deployment files and private configuration

| Source file | Installed location | Purpose |
| :--- | :--- | :--- |
| [nightly-run.timer](systemd/nightly-run.timer) | `/etc/systemd/system/nightly-run.timer` | Starts the nightly workflow. |
| [nightly-run.service](systemd/nightly-run.service) | `/etc/systemd/system/nightly-run.service` | Runs the workflow and its terminal email. |

Use the keys in [.env.example](../.env.example) for `/etc/bto/env`. In addition to
collection, Databricks, PostgreSQL and SMTP settings, nightly processing needs
the two Gemini Batch API keys and the `BTO_REVIEW_REFRESH_*` settings. The refresh
URL is the live HTTPS origin; the credentials are the existing admin login.
Provision private searches, preferences, prompts and web authentication files
separately. They must not enter the public repository or wheel.

Cache publication is called directly by nightly after sorting validation.
Provision its serving paths, account, service, readiness address and extra job
checks in private host configuration. Deploy the matching serving helper with
the nightly caller, and grant that caller non-interactive sudo only for the
configured helper command. Memory limits, handover and rollback are documented
in the [web README](../src/bto/web/README.md#nightly-refresh).

## Installing or redeploying

1. Confirm no nightly run or cache publication is active. Back up the deployed
   source, virtual environments, wheel, units and private configuration in a
   root-only rollback directory. Preserve unrelated applications and services.

2. Sync the reviewed checkout to `/opt/bto` and provision its private files.
   On a new host, create the virtual environment with Python 3.12 or newer:
   `python3 -m venv /opt/bto/.venv`.
   Any executable wheel-code change requires a new `version` in `pyproject.toml`
   before building: Databricks may cache an installed version. Never deploy
   different wheel bytes under a version already used.

   ```bash
   cd /opt/bto
   .venv/bin/python -m pip install -e .
   .venv/bin/python -m bto.nightly_run --check-config
   ```

   The configuration check validates private preferences and both prompts;
   it starts no jobs and makes no provider calls. Run it as the nightly service
   account before enabling or restarting the workflow. Nightly repeats this
   check before Collection. On the serving checkout, validate preferences as
   its service account and check an assessed JD; a working login page alone
   does not verify Review's configuration.

3. Build the wheel from the reviewed commit exported to a clean temporary tree.
   This keeps ignored private files from the live checkout out of the package.

   ```bash
   build_dir="$(mktemp -d)"
   git archive --format=tar HEAD | tar -x -C "$build_dir"
   /opt/bto/.venv/bin/python -m pip wheel --no-deps \
     -w "$build_dir/dist" "$build_dir"
   /opt/bto/.venv/bin/python -m zipfile -l "$build_dir"/dist/bto-*.whl
   ```

   Inspect the contents: searches, preferences, prompts and authentication
   configuration must contain only their safe examples. Replace the wheel in
   `/opt/bto/wheels/` so it contains exactly one `bto-*.whl`. Source and wheel
   must come from the same reviewed commit. Do not commit wheels or build output.

4. Deploy the matching Review publication helper in its privately configured
   serving checkout. When migrating an existing host, retire the old collection/cleaning/email chain
   and remove `publish-review-cache.path` and `publish-review-cache.service`.
   Disable their triggers before removing installed unit files. Do this only
   while the old workflows are idle; never run both nightly chains together.

5. Install the two files from `deploy/systemd/` in `/etc/systemd/system/`,
   then reload systemd:

   ```bash
   sudo systemctl daemon-reload
   ```

6. On a new installation, complete the explicitly approved storage setup and
   initial loads before activating nightly processing:

   - Build full-history Silver following the
     [cleaning README](../src/bto/clean_job_listings/README.md) and
     [cleaning contract](../src/bto/clean_job_listings/docs/CONTRACT.md).
   - Initialize and verify PostgreSQL's sorting and Review contracts, then load
     the initial Silver history into PostgreSQL. Follow the
     [filtering/bucketing entry points](../src/bto/filter_and_bucket_job_listings/README.md#local-entry-points-and-execution-gates)
     and [Review setup](../src/bto/web/README.md#localhost).

   On an existing installation, verify the required contracts and initial loads
   rather than recreating them. Then enable the timer:

   ```bash
   sudo systemctl enable --now nightly-run.timer
   ```

Individual stages retain their Python CLI commands. Do not enable a second
sorting trigger alongside the nightly workflow.

## Logs and recovery

```bash
systemctl status nightly-run.service
systemctl list-timers nightly-run.timer
journalctl -u nightly-run
# Inspect the privately configured serving service journal as well.
```

Logs go to journald and identify the run, stage, attempt, remote Batch job
and publication request. Unexpected failures include error types and call
locations. Exception messages, source lines and local variables are omitted
from these traces to protect private inputs and provider data. Provider bodies and submitted inputs remain in private
checkpoints under ignored `data/job_sorting/`.

- `nightly.json` records the logical run and terminal-email progress.
- `runs/<run_id>/batch.json` records submissions, remote IDs and saved results.
- At the next scheduled start, unfinished filtering/bucketing or cache
  publication resumes the saved run, skipping completed collection and cleaning.
  That invocation finishes the resumed run and exits; it does not then collect
  a fresh night's listings. Never create a fresh directory to bypass an
  unfinished Batch. A completed same-night invocation does not collect or email again.
- After a collection or cleaning failure, a later night's scheduled start creates
  a new nightly run. Same-night reruns of those interrupted stages require inspection;
  the launcher does not automatically repeat them.
- A publication-only failure can resume publication using completed paid work.
  The previous Review snapshot is preserved or restored on failure.
- Inspect interrupted collection or cleaning before rerunning. Do not overlap
  manual collection with full-history canonicalization finalization, and never
  edit Bronze data to recover cleaning.

Rollback also requires an idle workflow. Restore the backed-up source, virtual
environments, single wheel, units and configuration, reload systemd, and confirm
the 03:30 SGT timer. Retain PostgreSQL data, runtime checkpoints, remote Batch IDs
and downloaded results: rolling back code does not cancel paid work.
