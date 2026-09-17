"""Databricks mechanics and canonical application object names.

This stdlib-only client uses three APIs:
    Files API             PUT /api/2.0/fs/files/Volumes/...    raw files
    Statement Execution   POST /api/2.0/sql/statements         tables
    Jobs 2.2              POST /api/2.2/jobs/...               wheel runs

Credentials and grants must permit the corresponding volume, warehouse, table
and job operations. Object names for collection, cleaning and run-scoped
staging live here and are always qualified by the configured catalog; code
never hardcodes a deployment catalog.

Write rule inherited from production: a WRITE statement is never blindly
retried (a lost reply may have committed). There are two exceptions, both
statements that provably never ran. A Delta write conflict is reported as
FAILED — not committed — and parallel board runs make those routine. A
warehouse refusal is rejected before the statement even exists: no statement
id comes back and the warehouse does not start.
"""

import http.client
import json
import logging
import random
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
TRANSPORT_ERRORS = (http.client.HTTPException, ConnectionError, TimeoutError)

DELTA_CONFLICT_MARKERS = (
    "DELTA_CONCURRENT_APPEND",
    "DELTA_CONCURRENT_WRITE",
    "DELTA_CONCURRENT_DELETE_READ",
    "DELTA_CONCURRENT_DELETE_DELETE",
    "DELTA_CONCURRENT_TRANSACTION",
    "ConcurrentAppendException",
    "ConcurrentWriteException",
    "ConcurrentDeleteReadException",
)

# ── Bronze collection object names (data-dictionary contract) ──────────────
SEARCH_RESULTS_VOLUME = "raw_search_results"
JOB_LISTINGS_VOLUME = "raw_job_listings"
SCRAPE_RUNS_TABLE = "scrape_runs"
OBSERVATION_TABLES = {
    "mcf": "mcf_job_listings",
    "jobstreet": "seek_job_listings",
    "jobsdb": "seek_job_listings",
    "seek": "seek_job_listings",
    "indeed": "indeed_job_listings",
    "linkedin": "linkedin_job_listings",
}


def volume_path(catalog, volume, filename=None):
    """Unity Catalog path for a Bronze volume, optionally one file in it.
    Both volumes are flat: every file sits at the volume root."""
    base = f"/Volumes/{catalog}/bronze/{volume}"
    return f"{base}/{filename}" if filename else base


def table_name(catalog, table):
    return f"{catalog}.bronze.{table}"


# The two persistent Silver tables.
STANDARDIZED_JOB_LISTINGS_TABLE = "standardized_job_listings"
JOB_CANONICAL_MAPPING_TABLE = "job_canonical_mapping"

# Run-scoped staging volume for the bulk standardized load: JSONL parts are
# uploaded here and bulk-read into the scratch table, then the volume is
# dropped alongside the scratch. Never a persistent store.
STANDARDIZED_STAGING_VOLUME = "_build_standardized_staging"
# The packaged clean_job_listings code (the bto wheel) that Databricks jobs
# run — uploaded through the Files API, so the repo package stays the only
# source of truth and no notebook copy of the implementation exists.
CLEAN_JOB_LISTINGS_CODE_VOLUME = "clean_job_listings_code"


def silver_table_name(catalog, table):
    return f"{catalog}.silver.{table}"


def silver_volume_name(catalog, volume):
    return f"{catalog}.silver.{volume}"


def silver_volume_path(catalog, volume, filename=None):
    """Unity Catalog path for a Silver staging volume, optionally one file."""
    base = f"/Volumes/{catalog}/silver/{volume}"
    return f"{base}/{filename}" if filename else base


def observation_table(catalog, board):
    return table_name(catalog, OBSERVATION_TABLES[board])


# A stopped serverless warehouse normally starts on the next statement within
# seconds and may stop between runs. Occasionally Databricks instead returns
# HTTP 400 "The request could not be processed by the warehouse" without
# beginning to start it, and the refusal can persist. Wait and retry twice,
# requesting an explicit start immediately before each retry because that may
# succeed when the statement's implicit start does not. Match the message, not
# HTTP 400 alone, so a genuinely malformed statement still fails immediately.
WAREHOUSE_REFUSAL_MARKER = "could not be processed by the warehouse"
WAREHOUSE_REFUSAL_RETRIES = 2
WAREHOUSE_REFUSAL_WAIT_SECONDS = 35 * 60
# a normal start takes seconds; this only bounds the wait before the retry
WAREHOUSE_START_WAIT_SECONDS = 5 * 60
WAREHOUSE_START_POLL_SECONDS = 10


def is_warehouse_refusal(message):
    text = str(message or "")
    return "HTTP 400" in text and WAREHOUSE_REFUSAL_MARKER in text


def is_delta_conflict(message):
    text = str(message or "")
    return any(marker in text for marker in DELTA_CONFLICT_MARKERS)


def _retrying(fn, attempts=4, base_delay=2.0, label=""):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == attempts:
                raise
            log.warning("retry %d/%d after HTTP %s on %s",
                        attempt, attempts - 1, e.code, label)
        except urllib.error.URLError as e:
            if attempt == attempts:
                raise
            log.warning("retry %d/%d after network error on %s: %s",
                        attempt, attempts - 1, label, e.reason)
        except TRANSPORT_ERRORS as e:
            if attempt == attempts:
                raise
            log.warning("retry %d/%d after a dropped response on %s: %s",
                        attempt, attempts - 1, label, e.__class__.__name__)
        time.sleep(base_delay * (2 ** (attempt - 1)))


class DatabricksError(RuntimeError):
    pass


class Databricks:
    def __init__(self, host, token, warehouse_id=None, timeout=120,
                 refusal_retries=WAREHOUSE_REFUSAL_RETRIES):
        self.host = host.rstrip("/")
        self.token = token
        self.warehouse_id = warehouse_id
        self.timeout = timeout
        self.refusal_retries = refusal_retries

    # ------------------------------------------------------------- plumbing
    def _call(self, method, path, *, json_body=None, raw_body=None, label="",
              idempotent=True):
        url = self.host + path
        if raw_body is not None:
            data, content_type = raw_body, "application/octet-stream"
        elif json_body is not None:
            data, content_type = json.dumps(json_body).encode(), "application/json"
        else:
            data, content_type = None, "application/json"

        def once():
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": "Bearer " + self.token,
                         "Content-Type": content_type})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
            if not payload:
                return None                # file uploads reply with an empty body
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                raise DatabricksError(
                    f"{method} {path}: HTTP success but the body is not JSON: "
                    f"{payload[:200]!r}") from None

        try:
            if idempotent:
                return _retrying(once, label=label or path)
            return once()
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            hint = ""
            if e.code in (401, 403):
                hint = (" — auth problem: the credentials need Unity "
                        "Catalog access to the target files and tables, a "
                        "SQL warehouse, and the API in use")
            elif e.code == 404:
                hint = " — check DATABRICKS_HOST, the volume path, or the warehouse id"
            raise DatabricksError(
                f"{method} {path} failed: HTTP {e.code}: {detail}{hint}") from None
        except urllib.error.URLError as e:
            raise DatabricksError(
                f"{method} {path} failed: network error: {e.reason}") from None
        except TRANSPORT_ERRORS as e:
            raise DatabricksError(
                f"{method} {path} failed: the response was cut short "
                f"({e.__class__.__name__})") from None

    # ---------------------------------------------------------------- files
    def upload(self, path, data_bytes):
        """PUT one file into a volume. Overwrite is on, which makes the call
        idempotent: re-uploading identical bytes is a no-op in effect."""
        self._call("PUT", f"/api/2.0/fs/files{path}?overwrite=true",
                   raw_body=data_bytes, label="files.upload")

    def volume_exists(self, path):
        self._call("GET", f"/api/2.0/fs/directories{path}", label="files.dir")
        return True

    def download(self, path):
        """GET one volume file's raw bytes.

        This bypasses ``_call`` because file bodies are not JSON, while keeping
        the same retry and error conventions as other Files API calls.
        """
        url = self.host + f"/api/2.0/fs/files{path}"

        def once():
            req = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + self.token})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()

        try:
            return _retrying(once, label="files.download")
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            raise DatabricksError(
                f"GET {path} failed: HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise DatabricksError(
                f"GET {path} failed: network error: {e.reason}") from None
        except TRANSPORT_ERRORS as e:
            raise DatabricksError(
                f"GET {path} failed: the response was cut short "
                f"({e.__class__.__name__})") from None

    # ------------------------------------------------------------------ sql
    def submit_wheel_run(self, run_name, wheel_path, entry_point, parameters,
                         environment_version="4",
                         dependencies=("datasketch==2.0.0",),
                         timeout_seconds=3 * 3600):
        """One serverless job run of a wheel entry point (Jobs API 2.2
        runs/submit): the bto wheel from a volume plus its pinned matching
        dependency form the task environment, so driver and workers run
        exactly the packaged code. Returns the run id.

        The environment version decides the preinstalled Python packages, so
        it is part of the dependency contract, not a detail. Version 3 ships
        NumPy 1.26.4, which the wheel's `numpy>=2.0` would replace — pip
        upgrading a Databricks core package kills the Python kernel
        (ERROR_CORE_PACKAGE_VERSION_CHANGE). Version 4
        ships NumPy 2.1.3 and SciPy 1.15.1, satisfying both the wheel and
        `datasketch==2.0.0` (numpy>=1.11, scipy>=1.0.0) with nothing core
        replaced. Versions 5 and 6 also satisfy the versions but turn the
        Py4J gateway off and make Arrow-optimized Python UDFs the default,
        which changes how this job's UDFs execute; moving to them is a
        separate, tested step.
        """
        body = {
            "run_name": run_name,
            "timeout_seconds": timeout_seconds,
            "tasks": [{
                "task_key": "canonicalize",
                "python_wheel_task": {"package_name": "bto",
                                      "entry_point": entry_point,
                                      "parameters": list(parameters)},
                "environment_key": "bto",
            }],
            # `environment_version` is the current field; `client` is
            # deprecated in the Jobs API environment spec.
            "environments": [{
                "environment_key": "bto",
                "spec": {"environment_version": environment_version,
                         "dependencies": [wheel_path, *dependencies]},
            }],
        }
        payload = self._call("POST", "/api/2.2/jobs/runs/submit",
                             json_body=body, label="jobs.runs.submit",
                             idempotent=False)
        return payload["run_id"]

    def run_state(self, run_id):
        """(life_cycle_state, result_state, state_message, task_run_ids)."""
        payload = self._call("GET", f"/api/2.2/jobs/runs/get?run_id={run_id}",
                             label="jobs.runs.get")
        state = payload.get("state", {})
        tasks = [t.get("run_id") for t in payload.get("tasks", [])]
        return (state.get("life_cycle_state"), state.get("result_state"),
                state.get("state_message", ""), tasks)

    def wait_run(self, run_id, poll_seconds=20, timeout_seconds=3 * 3600,
                 error_allowance_seconds=600):
        """Poll until the run is terminal; returns (result_state, message,
        task_run_ids). A remote job is not abandoned because the control
        plane or the network blinked: polling errors are tolerated for
        `error_allowance_seconds` of CONSECUTIVE failures (a successful
        poll resets the window), inside the overall `timeout_seconds`.
        Authentication failures are never transient and raise at once."""
        deadline = time.monotonic() + timeout_seconds
        failing_since = None
        while True:
            try:
                life, result, message, tasks = self.run_state(run_id)
            except DatabricksError as error:
                now = time.monotonic()
                text = str(error)
                failing_since = now if failing_since is None else failing_since
                if ("HTTP 401" in text or "HTTP 403" in text
                        or now - failing_since > error_allowance_seconds
                        or now > deadline):
                    raise DatabricksError(
                        f"run {run_id}: polling failed for "
                        f"{int(now - failing_since)}s — {text}") from error
                log.warning("run %s: poll failed (%s); retrying for up to %ds",
                            run_id, text[:160], error_allowance_seconds)
                time.sleep(poll_seconds)
                continue
            failing_since = None
            if life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
                # an INTERNAL_ERROR life cycle still has task output worth
                # reading, so it is reported as a failed result, never raised
                return result or life, message, tasks
            if time.monotonic() > deadline:
                raise DatabricksError(f"run {run_id} still {life} after "
                                      f"{timeout_seconds}s")
            time.sleep(poll_seconds)

    def run_logs(self, task_run_id):
        """The task's captured stdout/stderr tail (runs/get-output), or ''."""
        payload = self._call("GET",
                             f"/api/2.2/jobs/runs/get-output?run_id={task_run_id}",
                             label="jobs.runs.get-output")
        return payload.get("logs") or ""

    def resolve_warehouse(self):
        if self.warehouse_id:
            return self.warehouse_id
        payload = self._call("GET", "/api/2.0/sql/warehouses",
                             label="sql.warehouses") or {}
        warehouses = payload.get("warehouses") or []
        if not warehouses:
            raise DatabricksError(
                "no SQL warehouse found — create one, or set DATABRICKS_WAREHOUSE_ID")
        running = [w for w in warehouses if w.get("state") == "RUNNING"]
        chosen = (running or warehouses)[0]
        self.warehouse_id = chosen["id"]
        log.info("warehouse: %s (%s, state=%s)",
                 chosen.get("name"), chosen["id"], chosen.get("state"))
        return self.warehouse_id

    def start_warehouse(self, wait_seconds=WAREHOUSE_START_WAIT_SECONDS,
                        poll_seconds=WAREHOUSE_START_POLL_SECONDS):
        """Ask Databricks to start the warehouse and wait until it reports
        RUNNING or `wait_seconds` pass. Best effort: returns the last state
        seen (None when even the request failed) and never raises — the
        statement retried afterwards is what succeeds or fails. Starting a
        running warehouse is a no-op, so repeating the request is harmless."""
        try:
            warehouse_id = self.resolve_warehouse()
            path = f"/api/2.0/sql/warehouses/{warehouse_id}"
            self._call("POST", path + "/start", json_body={},
                       label="sql.warehouse_start")
        except DatabricksError as error:
            log.warning("start request for the SQL warehouse failed: %s", error)
            return None
        deadline = time.monotonic() + wait_seconds
        state = None
        while True:
            try:
                state = (self._call("GET", path, label="sql.warehouse")
                         or {}).get("state")
            except DatabricksError as error:
                log.warning("could not read warehouse %s state: %s",
                            warehouse_id, error)
            if state == "RUNNING" or time.monotonic() >= deadline:
                return state
            time.sleep(poll_seconds)

    def query(self, statement, parameters=None):
        """One READ statement; auto-retried on transient failures."""
        return self._sql(statement, parameters=parameters, idempotent=True)

    def execute(self, statement, parameters=None):
        """One WRITE statement; never blindly retried (see module docstring)."""
        return self._sql(statement, parameters=parameters, idempotent=False)

    def _sql(self, statement, *, parameters=None, idempotent,
             conflict_attempts=4):
        delay = 2.0
        attempt = refusals = 0
        while True:
            try:
                return self._sql_once(statement, parameters=parameters,
                                      idempotent=idempotent)
            except DatabricksError as error:
                # a refusal never ran the statement, reads and writes alike,
                # and does not use up the Delta-conflict attempts
                if (is_warehouse_refusal(error)
                        and refusals < self.refusal_retries):
                    refusals += 1
                    log.warning("the SQL warehouse refused the statement — "
                                "retry %d/%d in %d minutes: %s", refusals,
                                self.refusal_retries,
                                WAREHOUSE_REFUSAL_WAIT_SECONDS // 60, error)
                    time.sleep(WAREHOUSE_REFUSAL_WAIT_SECONDS)
                    state = self.start_warehouse()
                    log.info("explicit start requested before retry %d/%d: "
                             "warehouse %s", refusals, self.refusal_retries,
                             state or "state unknown")
                    continue
                attempt += 1
                if attempt == conflict_attempts or not is_delta_conflict(error):
                    raise
                log.warning("retry %d/%d after a Delta write conflict — "
                            "another writer held the table",
                            attempt, conflict_attempts - 1)
                time.sleep(delay + random.uniform(0, delay))
                delay *= 2

    def _sql_once(self, statement, *, parameters=None, idempotent,
                  wait_seconds=50, poll_seconds=3, max_polls=120):
        warehouse_id = self.resolve_warehouse()
        body = {
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": f"{wait_seconds}s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        }
        if parameters:
            # Named parameter markers (:name). Values travel outside the SQL
            # text, so board-provided strings can never break the statement.
            body["parameters"] = [
                {"name": name, "value": value, "type": "STRING"}
                for name, value in parameters.items()
            ]
        payload = self._call("POST", "/api/2.0/sql/statements",
                             idempotent=idempotent, json_body=body,
                             label="sql.statements")
        if not isinstance(payload, dict):
            raise DatabricksError("sql.statements returned an empty response")

        statement_id = payload.get("statement_id")
        state = (payload.get("status") or {}).get("state")
        polls = 0
        while state in ("PENDING", "RUNNING"):
            if polls >= max_polls:
                try:    # best effort: never leave a zombie statement running
                    self._call("POST",
                               f"/api/2.0/sql/statements/{statement_id}/cancel",
                               label="sql.cancel")
                except DatabricksError:
                    pass
                raise DatabricksError(
                    f"statement {statement_id} still {state} after "
                    f"{wait_seconds + polls * poll_seconds}s — cancel requested")
            time.sleep(poll_seconds)
            polls += 1
            payload = self._call("GET", f"/api/2.0/sql/statements/{statement_id}",
                                 label="sql.poll")
            if not isinstance(payload, dict):
                raise DatabricksError(
                    f"sql.poll returned an empty response for {statement_id}")
            state = (payload.get("status") or {}).get("state")

        if state != "SUCCEEDED":
            error = (payload.get("status") or {}).get("error") or {}
            raise DatabricksError(
                f"statement {state}: {error.get('message', '(no message)')}")

        rows = list((payload.get("result") or {}).get("data_array") or [])
        next_link = (payload.get("result") or {}).get("next_chunk_internal_link")
        while next_link:
            chunk = self._call("GET", next_link, label="sql.chunk") or {}
            rows.extend(chunk.get("data_array") or [])
            next_link = chunk.get("next_chunk_internal_link")
        # A partial result must never masquerade as a complete one: the
        # service marks truncation in the manifest, and declares the total
        # row count — both are checked, so a short read is a loud failure.
        manifest = payload.get("manifest") or {}
        if manifest.get("truncated"):
            raise DatabricksError(
                f"statement {statement_id} result was truncated by the "
                f"service; refusing a partial result set")
        declared = manifest.get("total_row_count")
        if declared is not None and int(declared) != len(rows):
            raise DatabricksError(
                f"statement {statement_id} returned {len(rows)} rows but "
                f"the manifest declares {declared}")
        return rows
