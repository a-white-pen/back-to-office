"""Persist collection Bronze: raw files, observation tables, and scrape_runs.

Called by:
    fetch_job_listings.run (every board goes through here)

Calls:
    storage.databricks.connection

This module is persistence mechanics only. It receives rows already shaped by
a board's parse.py as {column: value} dicts and lands them; no board's field
names or parsing rules live here. The column lists and type DDL below are the
data dictionary's Bronze contract, restated once so rows can be inserted with
their nested structure intact.

Main functions:
    write_search_artifact()       one raw file into raw_search_results (gzip)
    write_payload()               one content-addressed file into raw_job_listings
    insert_observations()         batched INSERT of parsed observation rows
    copy_unchanged_observations() server-side copy of the prior observation row
                                  for unchanged jobs (same payload version, so
                                  its payload-derived columns are re-stated
                                  without re-downloading anything)
    insert_running_run() / finish_run()   the scrape_runs lifecycle

Batching: observation rows travel as one JSON-array statement parameter and
are expanded server-side with inline(from_json(...)), so a run writes a few
statements, not thousands — the SQL warehouse is never held open row by row.
"""

import gzip
import json
import logging

from .connection import (
    JOB_LISTINGS_VOLUME,
    SCRAPE_RUNS_TABLE,
    SEARCH_RESULTS_VOLUME,
    DatabricksError,
    observation_table,
    table_name,
    volume_path,
)

log = logging.getLogger(__name__)

# Rows per INSERT statement. JD-bearing rows run 10–50 KB of JSON each, and
# the statement API rejects combined parameter payloads over 1,048,576 UTF-8
# bytes, a separate and lower ceiling than the 16 MiB statement limit. Stay
# comfortably under it.
INSERT_BATCH_ROWS = 100
INSERT_BATCH_BYTES = 700_000

# ── Bronze observation schemas (data-dictionary contract, one place) ────────
# (column name, Spark type DDL). Order defines both the INSERT column list and
# the struct field order that inline(from_json(...)) expands into.

MCF_COLUMNS = [
    ("board", "STRING"), ("market", "STRING"), ("board_job_id", "STRING"),
    ("run_id", "STRING"), ("content_hash", "STRING"), ("change_signal", "STRING"),
    ("uuid", "STRING"), ("title", "STRING"), ("description", "STRING"),
    ("postedCompany", "STRUCT<uen: STRING, name: STRING, ssicCode2020: STRING, "
     "ssicDescription2020: STRING, description: STRING, companyUrl: STRING, "
     "logoUploadPath: STRING, employeeCount: INT, "
     "responsiveEmployer: STRUCT<isResponsive: BOOLEAN>>"),
    ("hiringCompany", "STRUCT<name: STRING, uen: STRING>"),
    ("metadata", "STRUCT<updatedAt: STRING, editCount: INT, jobPostId: STRING, "
     "jobDetailsUrl: STRING, newPostingDate: DATE, originalPostingDate: DATE, "
     "expiryDate: DATE, repostCount: INT, isPostedOnBehalf: BOOLEAN, "
     "isHideSalary: BOOLEAN, isHideEmployerName: BOOLEAN, "
     "isHideCompanyAddress: BOOLEAN, deletedAt: STRING, totalNumberOfView: INT, "
     "totalNumberJobApplication: INT>"),
    ("status", "STRUCT<jobStatus: STRING>"),
    ("salary", "STRUCT<minimum: DOUBLE, maximum: DOUBLE, "
     "type: STRUCT<id: INT, salaryType: STRING>>"),
    ("minimumYearsExperience", "INT"), ("numberOfVacancies", "INT"),
    ("ssocCode", "STRING"), ("ssocVersion", "STRING"), ("occupationId", "STRING"),
    ("ssecEqa", "STRING"), ("ssecFos", "STRING"),
    ("categories", "ARRAY<STRUCT<id: INT, category: STRING>>"),
    ("employmentTypes", "ARRAY<STRUCT<id: INT, employmentType: STRING>>"),
    ("positionLevels", "ARRAY<STRUCT<id: INT, position: STRING>>"),
    ("skills", "ARRAY<STRUCT<skill: STRING, uuid: STRING, isKeySkill: BOOLEAN, "
     "confidence: DOUBLE>>"),
    ("screeningQuestions", "ARRAY<STRUCT<question: STRING>>"),
    ("flexibleWorkArrangements",
     "ARRAY<STRUCT<id: INT, flexibleWorkArrangement: STRING>>"),
    ("address", "STRUCT<postalCode: STRING, block: STRING, street: STRING, "
     "building: STRING, lat: DOUBLE, lng: DOUBLE, "
     "districts: ARRAY<STRUCT<id: INT, location: STRING, region: STRING, "
     "regionId: STRING, sectors: ARRAY<STRING>>>, isOverseas: BOOLEAN>"),
]

SEEK_COLUMNS = [
    ("board", "STRING"), ("market", "STRING"), ("board_job_id", "STRING"),
    ("run_id", "STRING"), ("content_hash", "STRING"), ("change_signal", "STRING"),
    ("job", "STRUCT<id: STRING, title: STRING, content: STRING, abstract: STRING, "
     "status: STRING, isExpired: BOOLEAN, "
     "listedAt: STRUCT<dateTimeUtc: TIMESTAMP>, "
     "expiresAt: STRUCT<dateTimeUtc: TIMESTAMP>, "
     "advertiser: STRUCT<id: STRING, name: STRING>, "
     "location: STRUCT<label: STRING>, workTypes: STRUCT<label: STRING>, "
     "classifications: ARRAY<STRUCT<label: STRING>>, "
     "salary: STRUCT<label: STRING, currencyLabel: STRING>, "
     "products: STRUCT<questionnaire: STRUCT<questions: ARRAY<STRING>>, "
     "bullets: ARRAY<STRING>>, "
     "contactMatches: ARRAY<STRUCT<type: STRING, value: STRING>>, "
     "sourceZone: STRING, isVerified: BOOLEAN, phoneNumber: STRING>"),
    ("companyProfile", "STRUCT<id: STRING, name: STRING>"),
    ("gfjInfo", "STRUCT<location: STRUCT<countryCode: STRING>, "
     "workTypes: STRUCT<label: ARRAY<STRING>>>"),
]

INDEED_COLUMNS = [
    ("board", "STRING"), ("market", "STRING"), ("board_job_id", "STRING"),
    ("run_id", "STRING"), ("content_hash", "STRING"),
    ("id", "STRING"), ("title", "STRING"), ("jobDescription", "STRING"),
    ("jobDescriptionHTML", "STRING"),
    ("salary", "STRUCT<min: DOUBLE, max: DOUBLE, currencyCode: STRING, "
     "type: STRING>"),
    ("location", "STRUCT<countryCode: STRING, countryName: STRING, city: STRING, "
     "postalCode: STRING, streetAddress: STRING, fullAddress: STRING, "
     "formatted: STRUCT<long: STRING, short: STRING>, latitude: DOUBLE, "
     "longitude: DOUBLE, admin1Code: STRING>"),
    ("jobLocationCity", "STRING"), ("formattedLocation", "STRING"),
    ("companyDetails", "STRUCT<name: STRING, employeeRange: STRING, "
     "industry: STRING, sectorNames: ARRAY<STRING>, revenue: STRING, "
     "ceoName: STRING, websiteUrl: STRING, rating: DOUBLE, reviewCount: BIGINT, "
     "headquartersLocation: STRUCT<address: STRING>, logoUrl: STRING>"),
    ("companyOverviewLink", "STRING"),
    ("attributes", "ARRAY<STRUCT<key: STRING, label: STRING>>"),
    ("occupations", "ARRAY<STRUCT<key: STRING, label: STRING>>"),
    ("benefits", "ARRAY<STRUCT<key: STRING, label: STRING>>"),
    ("socialInsurance", "ARRAY<STRUCT<key: STRING, label: STRING>>"),
    ("jobTypes", "ARRAY<STRING>"),
    ("pubDate", "BIGINT"), ("expirationDate", "BIGINT"),
    ("jobSourceName", "STRING"), ("originalApplyUrl", "STRING"),
    ("viewJobLink", "STRING"), ("language", "STRING"), ("trackingKey", "STRING"),
    ("expired", "BOOLEAN"), ("isRepost", "BOOLEAN"), ("newJob", "BOOLEAN"),
    ("urgentlyHiring", "BOOLEAN"), ("highVolumeHiring", "BOOLEAN"),
]

LINKEDIN_COLUMNS = [
    ("board", "STRING"), ("market", "STRING"), ("board_job_id", "STRING"),
    ("run_id", "STRING"), ("content_hash", "STRING"),
    ("jobId", "STRING"), ("jobTitle", "STRING"), ("jobDescription", "STRING"),
    ("location", "STRING"), ("sector", "STRING"), ("contractType", "STRING"),
    ("workType", "STRING"), ("experienceLevel", "STRING"),
    ("yearsOfExperience",
     "ARRAY<STRUCT<years: STRING, context: STRING, lang: STRING>>"),
    ("salaryInfo", "ARRAY<STRING>"), ("applicationsCount", "STRING"),
    ("posterFullName", "STRING"), ("posterProfileUrl", "STRING"),
    ("publishedAt", "STRING"), ("postedTime", "STRING"), ("jobUrl", "STRING"),
    ("applyUrl", "STRING"), ("applyType", "STRING"), ("searchString", "STRING"),
    ("dynamicFilterMatch", "BOOLEAN"),
    ("companyId", "STRING"), ("companyName", "STRING"), ("companyUrl", "STRING"),
    ("companyLogo", "STRING"), ("companyWebsite", "STRING"),
    ("companyDescription", "STRING"), ("companyIndustry", "STRING"),
    ("companyEmployeeCount", "BIGINT"), ("companyEmployeeCountRange", "STRING"),
    ("companyOrganizationType", "STRING"), ("companyFoundedDate", "STRING"),
    ("companyFollowersCount", "BIGINT"),
    ("companySpecialties", "ARRAY<STRING>"),
    ("companyAffiliatedPages", "ARRAY<STRING>"),
    ("companyOfficeLocations", "ARRAY<STRING>"),
    ("companyAddress", "STRUCT<addressCountry: STRING, addressLocality: STRING, "
     "addressRegion: STRING, postalCode: STRING, streetAddress: STRING>"),
    ("companyRecentPosts",
     "ARRAY<STRUCT<datePublished: STRING, text: STRING, url: STRING>>"),
]

TABLE_COLUMNS = {
    "mcf_job_listings": MCF_COLUMNS,
    "seek_job_listings": SEEK_COLUMNS,
    "indeed_job_listings": INDEED_COLUMNS,
    "linkedin_job_listings": LINKEDIN_COLUMNS,
}


def _row_schema_ddl(columns):
    return "ARRAY<STRUCT<" + ", ".join(f"`{n}`: {t}" for n, t in columns) + ">>"


def _column_list(columns):
    return ", ".join(f"`{name}`" for name, _ in columns)


# ── raw files ───────────────────────────────────────────────────────────────
def write_search_artifact(dbx, catalog, filename, raw_bytes):
    """Land one raw search artifact (page, actor envelope, or jd_fetches
    JSONL) in raw_search_results, gzip-compressed as the .gz name promises."""
    dbx.upload(volume_path(catalog, SEARCH_RESULTS_VOLUME, filename),
               gzip.compress(raw_bytes))


def write_payload(dbx, catalog, content_hash, raw_bytes, _seen=None):
    """Land one content-addressed payload version in raw_job_listings.

    The hash was taken over these exact uncompressed bytes before this call;
    gzip happens here, after hashing. Content addressing makes the write
    idempotent — re-uploading a hash the volume already holds replaces the
    file with identical bytes. `_seen` (a per-run set) skips repeat uploads
    of a hash within one run.
    """
    if _seen is not None:
        if content_hash in _seen:
            return
        _seen.add(content_hash)
    dbx.upload(volume_path(catalog, JOB_LISTINGS_VOLUME, f"{content_hash}.json.gz"),
               gzip.compress(raw_bytes))


# ── observation tables ──────────────────────────────────────────────────────
def insert_observations(dbx, catalog, board, rows):
    """INSERT parsed observation rows, batched.

    Rows are {column: value} dicts from the board's parse.py. Each batch is
    one statement: the batch travels as a JSON-array parameter and
    inline(from_json(...)) expands it into typed columns server-side, so
    nested structs land as structs. A field a payload does not carry is
    simply absent from the JSON and lands as NULL.
    """
    table = observation_table(catalog, board)
    columns = TABLE_COLUMNS[table.rsplit(".", 1)[-1]]
    schema = _row_schema_ddl(columns)
    statement = (f"INSERT INTO {table} ({_column_list(columns)})\n"
                 f"SELECT inline(from_json(:rows, '{schema}'))")

    written = 0
    batch, batch_bytes = [], 2       # the serialized JSON array's brackets
    def flush():
        nonlocal written, batch, batch_bytes
        if not batch:
            return
        payload = json.dumps(batch, ensure_ascii=False)
        result = dbx.execute(statement, parameters={"rows": payload})
        inserted = _affected_rows(result)
        if inserted is not None and inserted != len(batch):
            raise DatabricksError(
                f"{table}: batch of {len(batch)} rows inserted {inserted}")
        written += len(batch)
        log.debug("%s: +%d rows (total %d)", table, len(batch), written)
        batch, batch_bytes = [], 2

    for row in rows:
        encoded = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        added_bytes = encoded + (2 if batch else 0)  # comma and space
        if batch and (len(batch) >= INSERT_BATCH_ROWS
                      or batch_bytes + added_bytes > INSERT_BATCH_BYTES):
            flush()
            added_bytes = encoded
        batch.append(row)
        batch_bytes += added_bytes
    flush()
    return written


def copy_unchanged_observations(dbx, catalog, board, market, run_id, pairs):
    """Write the current run's observation rows for unchanged jobs by copying each
    job's latest prior row server-side, with only run_id replaced.

    An unchanged job points at the same payload version (same content_hash),
    so its payload-derived columns are identical by definition — copying the
    prior row re-states them without downloading a single payload. `pairs`
    is [(board_job_id, prior_run_id)] taken from the prior-state read.
    """
    if not pairs:
        return 0
    table = observation_table(catalog, board)
    columns = TABLE_COLUMNS[table.rsplit(".", 1)[-1]]
    select_exprs = ", ".join(
        ":run_id" if name == "run_id" else f"t.`{name}`" for name, _ in columns)
    statement = (
        f"INSERT INTO {table} ({_column_list(columns)})\n"
        f"SELECT {select_exprs}\n"
        f"FROM {table} t\n"
        f"JOIN (SELECT p.board_job_id, p.prior_run_id FROM (\n"
        f"        SELECT explode(from_json(:pairs,\n"
        f"          'ARRAY<STRUCT<board_job_id: STRING, prior_run_id: STRING>>')) AS p\n"
        f"      )) sel\n"
        f"  ON t.board_job_id = sel.board_job_id AND t.run_id = sel.prior_run_id\n"
        f"WHERE t.board = :board AND t.market = :market")

    copied = 0
    for start in range(0, len(pairs), 2000):
        chunk = pairs[start:start + 2000]
        payload = json.dumps(
            [{"board_job_id": job_id, "prior_run_id": prior_run_id}
             for job_id, prior_run_id in chunk])
        result = dbx.execute(statement, parameters={
            "run_id": run_id, "pairs": payload, "board": board, "market": market,
        })
        inserted = _affected_rows(result)
        if inserted is not None and inserted != len(chunk):
            # The prior row must exist — Bronze is append-only. A mismatch is
            # integrity evidence, not something to paper over.
            raise DatabricksError(
                f"{table}: unchanged copy expected {len(chunk)} rows, "
                f"inserted {inserted}")
        copied += len(chunk)
    log.info("%s_%s: copied %d unchanged observation rows server-side",
             board, market, copied)
    return copied


def _affected_rows(result):
    """num_affected_rows from an INSERT result, or None if the shape moved."""
    try:
        return int(result[0][0])
    except (IndexError, TypeError, ValueError):
        return None


# ── scrape_runs lifecycle ───────────────────────────────────────────────────
def insert_running_run(dbx, catalog, run_id, board, market, started_at_utc,
                       run_trigger):
    """Open the run's scrape_runs row as `running`. One INSERT; the same row
    is UPDATEd at completion — never a second row for the finish."""
    dbx.execute(
        f"INSERT INTO {table_name(catalog, SCRAPE_RUNS_TABLE)}\n"
        f"  (run_id, board, market, started_at, status, run_trigger)\n"
        f"SELECT :run_id, :board, :market, CAST(:started_at AS TIMESTAMP),\n"
        f"       'running', :run_trigger",
        parameters={
            "run_id": run_id, "board": board, "market": market,
            "started_at": started_at_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "run_trigger": run_trigger,
        })


def finish_run(dbx, catalog, run_id, *, finished_at_utc, status, counters,
               term_results, full_sweep, error_reason):
    """Close the run by updating its own row: terminal status, counters,
    per-term calibration JSON, sweep scope and cause. A hard crash before
    this call legitimately leaves the row at `running`."""
    dbx.execute(
        f"UPDATE {table_name(catalog, SCRAPE_RUNS_TABLE)} SET\n"
        f"  finished_at = CAST(:finished_at AS TIMESTAMP),\n"
        f"  status = :status,\n"
        f"  terms_swept = CAST(:terms_swept AS INT),\n"
        f"  terms_succeeded = CAST(:terms_succeeded AS INT),\n"
        f"  unique_seen = CAST(:unique_seen AS INT),\n"
        f"  new_jobs = CAST(nullif(:new_jobs, '') AS INT),\n"
        f"  changed_jobs = CAST(nullif(:changed_jobs, '') AS INT),\n"
        f"  jds_intended = CAST(:jds_intended AS INT),\n"
        f"  jds_fetched = CAST(:jds_fetched AS INT),\n"
        f"  fetch_failures = CAST(:fetch_failures AS INT),\n"
        f"  backlog_remaining = CAST(:backlog_remaining AS INT),\n"
        f"  term_results = :term_results,\n"
        f"  full_sweep = CAST(:full_sweep AS BOOLEAN),\n"
        f"  error_reason = nullif(:error_reason, '')\n"
        f"WHERE run_id = :run_id",
        parameters={
            "run_id": run_id,
            "finished_at": finished_at_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "terms_swept": str(counters.get("terms_swept", 0)),
            "terms_succeeded": str(counters.get("terms_succeeded", 0)),
            "unique_seen": str(counters.get("unique_seen", 0)),
            # Indeed and LinkedIn have no prior-state diff, so new/changed are
            # NULL there rather than a misleading zero.
            "new_jobs": ("" if counters.get("new_jobs") is None
                         else str(counters["new_jobs"])),
            "changed_jobs": ("" if counters.get("changed_jobs") is None
                             else str(counters["changed_jobs"])),
            "jds_intended": str(counters.get("jds_intended", 0)),
            "jds_fetched": str(counters.get("jds_fetched", 0)),
            "fetch_failures": str(counters.get("fetch_failures", 0)),
            "backlog_remaining": str(counters.get("backlog_remaining", 0)),
            "term_results": json.dumps(term_results, sort_keys=True,
                                       ensure_ascii=False),
            "full_sweep": "true" if full_sweep else "false",
            "error_reason": (error_reason or "")[:500],
        })
