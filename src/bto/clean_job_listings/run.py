"""Build the two Silver cleaning products from Bronze observations.

Standardization runs locally, preserves observation history and publishes
validated rows. Canonicalization runs locally for development or as a
Databricks wheel job, then publishes a validated mapping bound to the Delta
version of `standardized_job_listings` that it read. The nightly run offers
the published mapping to the job as an incremental baseline; the job proves
that safe or rebuilds the complete history itself, so what is validated and
published here is the same complete mapping either way. See README.md for
the workflow and docs/CONTRACT.md for exact guarantees.

The two Silver tables are published SEPARATELY. Each publication can be
atomic on its own — a full build replaces standardized with one Delta
`CREATE OR REPLACE`, a nightly build MERGEs into it — but no single
transaction covers both tables. A failure after the standardized publication
therefore leaves the new standardized state published while the mapping is
still bound to the standardized version it was built from. That mismatched
pair is exactly what the freshness guard in `storage.databricks.freshness`
detects, so no consumer trusts it; see also `--stage canonicalize` to
finish the pair. Scratch tables and staging volumes are cleaned on the
success path and deliberately left behind on failure, where they are
evidence.

`--stage promote` publishes an already-validated pair of tables exactly as
they are, never recomputing — into a suffixed target only: a validation pair
covers a fraction of Bronze and is never the production tables.

Rows come down through the SQL Statement API in hash buckets sized to
stay under the 26 MB INLINE result cap. Standardized rows go up as JSONL
part files through the Files API into a run-scoped staging volume and are
bulk-read into the scratch table with one INSERT — a handful of uploads
instead of hundreds of 1 MiB-capped parameter statements. The smaller
canonical mapping uses JSON-array parameter batches. Bucket counts are measured
from the selected corpus; nothing assumes the full history fits one call.
"""

import argparse
import gzip
import hashlib
import json
import logging
import math
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import settings as settings_module
from ..send_notifications import notify
from ..storage.databricks import freshness
from ..storage.databricks.connection import (
    CLEAN_JOB_LISTINGS_CODE_VOLUME,
    JOB_CANONICAL_MAPPING_TABLE,
    JOB_LISTINGS_VOLUME,
    SCRAPE_RUNS_TABLE,
    STANDARDIZED_JOB_LISTINGS_TABLE,
    STANDARDIZED_STAGING_VOLUME,
    Databricks,
    observation_table,
    silver_table_name,
    silver_volume_name,
    silver_volume_path,
    table_name,
    volume_path,
)
from ..storage.databricks.freshness import pinned
from . import build_canonical_mapping as canonical
from . import canonicalize_on_databricks as spark_job
from . import standardize_job_listings as standardize

log = logging.getLogger(__name__)

INSERT_BYTE_CAP = 700_000       # combined-parameter ceiling is 1 MiB; stay clear
# Standardized rows go up as JSONL part files through the Files API and are
# bulk-read into the scratch table — a handful of uploads plus one INSERT
# instead of hundreds of parameter-capped statements. ~32 MB per part keeps
# each upload well-behaved without creating many files.
STAGE_FILE_BYTE_CAP = 32_000_000
# INLINE reads are sized per pair from measured bytes (see _plan_read_buckets);
# the Statement API's hard ceiling is 26,214,400 bytes per result.
READ_BYTE_TARGET = 16_000_000
# Canonical document retrieval sizes its own batches from the usable
# fingerprint count — Bronze sampling buckets are a different concern and must
# not force hundreds of near-empty document queries on a small run. ~500 docs
# per read keeps each INLINE result a few MB, far under the 26 MB ceiling.
DOCS_PER_READ = 500

# How long the canonicalization job may run. The default matches the
# `connection.py` defaults it overrides; `--canonicalize-timeout-hours` raises
# it for a single run. There is no checkpoint, so a job killed at the deadline
# discards all work completed during that run.
CANONICALIZE_TIMEOUT_SECONDS = 3 * 3600
# The client keeps polling for this much longer than the deadline it gave
# Databricks. The two clocks do not start together — the server's covers
# queueing and setup that begin before `wait_run` is even called — so an equal
# client deadline could abandon a run the service is about to terminate or
# finish, orphaning its scratch. 10 minutes covers that skew and the 20-second
# poll interval, and matches the tolerance `wait_run` already allows for
# consecutive polling failures.
CLIENT_WAIT_MARGIN_SECONDS = 600

# The 45-column Silver schema as Delta DDL — types from the data dictionary.
_STANDARDIZED_DDL = """
run_id STRING, board STRING, market STRING, board_job_id STRING,
content_hash STRING,
title STRING, description_html STRING, description_text STRING,
posted_date DATE, expiry_date DATE, board_status STRING, views_count INT,
advertiser_name STRING, hiring_company_name STRING, company_registry_id STRING,
board_company_id STRING, is_agency_posting BOOLEAN, company_website STRING,
company_employee_count INT,
job_country_code STRING, job_location STRING,
salary_raw STRING, salary_min DOUBLE, salary_max DOUBLE,
salary_currency STRING, salary_period STRING,
categories ARRAY<STRING>, employment_types ARRAY<STRING>, job_function STRING,
position_levels ARRAY<STRING>, min_years_experience INT, skills ARRAY<STRING>,
flexible_work_arrangements ARRAY<STRING>, screening_questions ARRAY<STRING>,
board_attributes ARRAY<STRING>,
job_url STRING, apply_url STRING, apply_type STRING,
poster_name STRING, poster_profile_url STRING,
contacts ARRAY<STRUCT<type: STRING, value: STRING>>, job_source_name STRING,
fingerprint STRING, minhash_signature ARRAY<BIGINT>, standardized_at TIMESTAMP
""".strip().replace("\n", " ")

_MAPPING_DDL = ("board STRING, market STRING, board_job_id STRING, "
                "fingerprint STRING, canonical_job_id STRING, "
                "canonicalized_at TIMESTAMP")

_ARRAY_COLUMNS = ("categories", "employment_types", "position_levels", "skills",
                  "flexible_work_arrangements", "screening_questions",
                  "board_attributes", "contacts", "minhash_signature")


class BuildError(RuntimeError):
    """A publication-blocking invariant failed at this step."""


# --------------------------------------------------------------- plumbing

def _insert_rows(dbx, table, ddl, columns, rows):
    """Insert small row sets in parameter-sized JSON batches."""
    statement = (f"INSERT INTO {table} ({', '.join(columns)}) "
                 f"SELECT inline(from_json(:rows, 'ARRAY<STRUCT<{ddl}>>'))")
    batch, batch_bytes, written = [], 0, 0

    def flush():
        nonlocal batch, batch_bytes, written
        if not batch:
            return
        dbx.execute(statement,
                    parameters={"rows": json.dumps(batch, ensure_ascii=False)})
        written += len(batch)
        batch, batch_bytes = [], 0

    for row in rows:
        sparse = {k: v for k, v in row.items() if v is not None}
        encoded = len(json.dumps(sparse, ensure_ascii=False).encode("utf-8"))
        if batch and batch_bytes + encoded > INSERT_BYTE_CAP:
            flush()
        batch.append(sparse)
        batch_bytes += encoded
    flush()
    return written


def _encode_row(row):
    """One standardized row → one JSONL line, byte-identical serialization to
    the parameter path: None-valued fields dropped (from_json reads absent
    fields as NULL), real Unicode kept (`ensure_ascii=False`). json.dumps
    escapes every control character, so a line never contains a raw newline."""
    sparse = {key: value for key, value in row.items() if value is not None}
    return (json.dumps(sparse, ensure_ascii=False) + "\n").encode("utf-8")


def _stage_files(dbx, directory, prefix, encoded_rows,
                 byte_cap=STAGE_FILE_BYTE_CAP):
    """Buffer JSONL lines into part files and PUT each into the staging
    volume. Returns (files, rows, bytes)."""
    buffer, buffered, files, rows, total = [], 0, 0, 0, 0

    def flush():
        nonlocal buffer, buffered, files
        if not buffer:
            return
        dbx.upload(f"{directory}/{prefix}-part-{files:05d}.jsonl",
                   b"".join(buffer))
        files += 1
        buffer, buffered = [], 0

    for line in encoded_rows:
        if buffer and buffered + len(line) > byte_cap:
            flush()
        buffer.append(line)
        buffered += len(line)
        rows += 1
        total += len(line)
    flush()
    return files, rows, total


def _ensure_silver_schema(dbx, catalog):
    """The Silver schema is created empty on first use; tables only ever
    appear through the staged builds below — never here."""
    dbx.execute(f"CREATE SCHEMA IF NOT EXISTS {catalog}.silver")


def _fresh_scratch(dbx, name, ddl):
    dbx.execute(f"DROP TABLE IF EXISTS {name}")
    dbx.execute(f"CREATE TABLE {name} ({ddl})")


def _single_value(dbx, statement, parameters=None):
    rows = dbx.query(statement, parameters=parameters)
    return rows[0][0] if rows else None


def _require(violations, context):
    if violations:
        for violation in violations[:20]:
            log.error("HARD INVARIANT FAILED (%s): %s", context, violation)
        raise BuildError(f"{context}: {len(violations)} hard invariant "
                         f"violation(s) — "
                         + "; ".join(violations[:5]))


# ----------------------------------------------------- standardize stage

# The exact observation identity — one Bronze row, one standardized row. The
# incremental selection, the disjointness proof, the insert-only MERGE and the
# full key reconciliation all use exactly these four columns and nothing else:
# never a date, a run-id watermark, a content hash or the listing id alone.
OBSERVATION_KEY = ("run_id", "board", "market", "board_job_id")
_SGT = timezone(timedelta(hours=8))


def observation_key_match(left, right):
    """The four-column observation identity as an equality predicate."""
    return " AND ".join(f"{left}.{column} = {right}.{column}"
                        for column in OBSERVATION_KEY)


def new_build_id(now_utc=None):
    """Return a unique build identifier for run-scoped scratch resources.

    The timestamp uses SGT for consistency with collection run identifiers;
    the random suffix prevents overlapping builds from sharing scratch.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    return f"{now_utc.astimezone(_SGT).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


def _bronze_source(catalog, board, exclude=None):
    """The Bronze table expression this board's observations are read from.

    Validation drivers narrow it (e.g. a join to a selection table). The
    reader, the bucket planner AND the source reconciliation all go through
    it, so whatever population is read is exactly the population reconciled.

    With `exclude` — the standardized table being maintained — the expression
    is the incremental population: Bronze observations whose exact four-column
    key is absent from that table (an anti-join; safe because Bronze
    observation rows are append-only).
    """
    table = observation_table(catalog, board)
    if exclude is None:
        return table
    return (f"(SELECT o.* FROM {table} o LEFT ANTI JOIN {exclude} t "
            f"ON {observation_key_match('t', 'o')})")


def _plan_read_buckets(dbx, catalog, board, market, exclude=None):
    """Hash-bucket count sized from the pair's measured payload bytes.

    The Statement API refuses an INLINE result over 26,214,400 bytes with an
    error rather than truncating it. READ_BYTE_TARGET leaves headroom for
    uneven hash buckets.
    Pure I/O partitioning: the union read is identical for any bucket count.
    """
    nbytes = _single_value(
        dbx, f"SELECT coalesce(sum(length(to_json(struct(o.*)))), 0) "
             f"FROM {_bronze_source(catalog, board, exclude)} o "
             f"WHERE o.board = :board AND o.market = :market",
        parameters={"board": board, "market": market})
    return max(1, -(-int(nbytes or 0) // READ_BYTE_TARGET))


def _iter_bronze_rows(dbx, catalog, board, market, buckets, bucket_limit,
                      exclude=None):
    source = _bronze_source(catalog, board, exclude)
    for bucket in range(min(buckets, bucket_limit or buckets)):
        rows = dbx.query(
            f"SELECT to_json(struct(o.*)) FROM {source} o "
            f"WHERE o.board = :board AND o.market = :market "
            f"AND pmod(hash(o.board_job_id), {buckets}) = {bucket}",
            parameters={"board": board, "market": market})
        for (encoded,) in rows:
            yield json.loads(encoded)


def _overseas_country(dbx, catalog, content_hash, cache):
    """Read the one contract-approved raw field, cached by payload hash."""
    if content_hash in cache:
        return cache[content_hash]
    path = volume_path(catalog, JOB_LISTINGS_VOLUME, f"{content_hash}.json.gz")
    payload = json.loads(gzip.decompress(dbx.download(path)))
    value = ((payload.get("address") or {}).get("overseasCountry"))
    cache[content_hash] = value
    return value


def run_standardize(dbx, catalog, pairs, buckets, bucket_limit, target_suffix,
                    standardized_at, incremental_target=None, build_id=None):
    """Build and validate the standardized CANDIDATE. Publishes nothing.

    Returns (scratch, report): the run-scoped scratch table holding the
    validated candidate, and the per-pair monitoring tallies. The caller
    promotes the candidate (wholesale) or merges it (incremental, see
    `publish_incremental`), or drops it. `buckets=None` sizes each pair's
    read from its measured bytes. With `incremental_target`, only Bronze
    observations whose exact key is absent from that table are read.
    """
    build_id = build_id or new_build_id()
    name = f"{target_suffix}_{build_id}"
    scratch = silver_table_name(
        catalog, "_build_" + STANDARDIZED_JOB_LISTINGS_TABLE + name)
    staging_volume = silver_volume_name(
        catalog, STANDARDIZED_STAGING_VOLUME + name)
    staging_dir = silver_volume_path(
        catalog, STANDARDIZED_STAGING_VOLUME + name)
    _fresh_scratch(dbx, scratch, _STANDARDIZED_DDL)
    dbx.execute(f"DROP VOLUME IF EXISTS {staging_volume}")
    dbx.execute(f"CREATE VOLUME {staging_volume}")

    plan = []
    for board, market in pairs:
        n = buckets or _plan_read_buckets(dbx, catalog, board, market,
                                          incremental_target)
        plan.append((board, market, n, bucket_limit))
        log.info("read plan %s × %s: %d bucket(s)%s", board, market, n,
                 f" (limit {bucket_limit})" if bucket_limit else "")

    overseas_cache = {}
    report = {"build_id": build_id, "scratch": scratch, "pairs": {}}
    expected = 0
    staged_rows = staged_files = staged_bytes = 0
    for board, market, n, limit in plan:
        tally = {"rows": 0, "identity_only": 0, "payload": 0,
                 "quarantined": {}, "payload_without_text": 0,
                 "title_only_fingerprints": 0, "status": "ok"}

        def build_rows(board=board, market=market, n=n, limit=limit,
                       tally=tally):
            nonlocal expected
            for row in _iter_bronze_rows(dbx, catalog, board, market, n,
                                         limit, incremental_target):
                expected += 1
                tally["rows"] += 1
                overseas = None
                if board == "mcf" and row.get("content_hash") \
                        and (row.get("address") or {}).get("isOverseas"):
                    overseas = _overseas_country(
                        dbx, catalog, row["content_hash"], overseas_cache)
                out, reason = standardize.standardize_observation(
                    board, row, overseas_country=overseas,
                    standardized_at=standardized_at)
                if out["content_hash"] is None:
                    tally["identity_only"] += 1      # not a quarantine
                else:
                    tally["payload"] += 1
                    if reason:
                        tally["quarantined"][reason] = \
                            tally["quarantined"].get(reason, 0) + 1
                    elif out["description_text"] is None:
                        tally["payload_without_text"] += 1
                    if out["fingerprint"] and out["description_text"] is None:
                        tally["title_only_fingerprints"] += 1
                yield out

        files, rows, nbytes = _stage_files(
            dbx, staging_dir, f"{board}-{market}",
            (_encode_row(row) for row in build_rows()))
        staged_files += files
        staged_rows += rows
        staged_bytes += nbytes
        quarantined = sum(tally["quarantined"].values())
        if tally["payload"] and quarantined == tally["payload"]:
            # every payload-bearing row of the pair quarantined: that is a
            # broken source or parser, not isolated bad data — refuse before
            # anything is merged (the locked structural rule; no percentages)
            _require([f"{board} × {market}: all {quarantined} payload-bearing "
                      f"row(s) quarantined ({tally['quarantined']})"],
                     "standardized candidate")
        if quarantined:
            tally["status"] = "partial"
        report["pairs"][f"{board}_{market}"] = tally
        log.info("standardized %s × %s: %d rows staged (%d part file(s)); "
                 "identity-only %d, quarantined %s, payload without text %d, "
                 "title-only fingerprints %d", board, market, rows, files,
                 tally["identity_only"], tally["quarantined"] or 0,
                 tally["payload_without_text"], tally["title_only_fingerprints"])

    if staged_rows:
        # one bulk load: the staged lines parse through the exact struct the
        # parameter path used, so row semantics are identical by construction
        # FAILFAST: a staged value the schema cannot hold fails the whole
        # load loudly instead of becoming a silent NULL (PERMISSIVE would
        # null just that field and keep the row)
        dbx.execute(
            f"INSERT INTO {scratch} ({', '.join(standardize.COLUMNS)}) "
            f"SELECT parsed.* FROM (SELECT from_json(value, "
            f"'STRUCT<{_STANDARDIZED_DDL}>', map('mode', 'FAILFAST')) AS parsed "
            f"FROM text.`{staging_dir}`)")
    log.info("bulk load: %d part file(s), %d rows, %d bytes staged",
             staged_files, staged_rows, staged_bytes)

    _validate_standardized(dbx, catalog, scratch, plan, expected,
                           exclude=incremental_target)
    dbx.execute(f"DROP VOLUME IF EXISTS {staging_volume}")
    report["staged_rows"] = staged_rows
    report["partial_pairs"] = sorted(
        pair for pair, tally in report["pairs"].items()
        if tally["status"] == "partial")
    log.info("standardized candidate %s validated (%d rows; partial pairs: %s)",
             scratch, staged_rows, report["partial_pairs"] or "none")
    return scratch, report


def publish_incremental(dbx, target, scratch, staged_rows, standardized_at):
    """Insert-only publication of a validated incremental candidate.

    MERGE on the exact observation key with WHEN NOT MATCHED THEN INSERT and
    no WHEN MATCHED action, so existing rows are immutable and a rerun is
    idempotent. A zero-row candidate issues no MERGE at all (no Delta
    version for nothing). Post-merge accounting: the target grew by exactly
    the staged rows and exactly those rows carry this batch's standardized_at.
    Returns the number of rows inserted.
    """
    if not staged_rows:
        log.info("incremental publish: zero new observations, no MERGE issued")
        return 0
    before = int(_single_value(dbx, f"SELECT count(*) FROM {target}") or 0)
    dbx.execute(f"MERGE INTO {target} t USING {scratch} s "
                f"ON {observation_key_match('t', 's')} "
                f"WHEN NOT MATCHED THEN INSERT *")
    after = int(_single_value(dbx, f"SELECT count(*) FROM {target}") or 0)
    batch = int(_single_value(
        dbx, f"SELECT count(*) FROM {target} "
             f"WHERE standardized_at = cast(:ts AS TIMESTAMP)",
        parameters={"ts": standardized_at}) or 0)
    violations = []
    if after != before + staged_rows:
        violations.append(f"target grew by {after - before}, staged {staged_rows}")
    if batch != staged_rows:
        violations.append(f"{batch} rows carry this batch's standardized_at, "
                          f"staged {staged_rows}")
    _require(violations, "incremental publication")
    log.info("incremental publish: %d rows merged into %s (%d → %d)",
             staged_rows, target, before, after)
    return staged_rows


def reconcile_full(dbx, catalog, target, board=None, market=None):
    """The locked nightly check, AFTER publication: every Bronze observation
    key exists in the standardized table and vice versa, over the complete
    Bronze history — both directions must be empty. Narrowed to one board or
    market only when the run itself was; the nightly run is never narrowed.
    Returns (missing, extra); raises BuildError on any mismatch."""
    predicate = " AND ".join(
        f"{column} = :{column}" for column, value in
        (("board", board), ("market", market)) if value) or "TRUE"
    parameters = {k: v for k, v in (("board", board), ("market", market)) if v}
    keys = ", ".join(OBSERVATION_KEY)
    bronze = " UNION ALL ".join(
        f"SELECT {keys} FROM {_bronze_source(catalog, family)} o "
        f"WHERE {predicate}"
        for family in ("mcf", "seek", "indeed", "linkedin"))
    standardized = f"SELECT {keys} FROM {target} WHERE {predicate}"
    missing = int(_single_value(
        dbx, f"SELECT count(*) FROM (({bronze}) EXCEPT ({standardized}))",
        parameters=parameters) or 0)
    extra = int(_single_value(
        dbx, f"SELECT count(*) FROM (({standardized}) EXCEPT ({bronze}))",
        parameters=parameters) or 0)
    violations = []
    if missing:
        violations.append(f"{missing} Bronze observation key(s) missing from "
                          f"{target}")
    if extra:
        violations.append(f"{extra} {target} key(s) with no Bronze observation")
    _require(violations, "full Bronze ↔ standardized reconciliation")
    log.info("full reconciliation exact: Bronze ↔ %s (%s)", target,
             parameters or "all boards and markets")
    return missing, extra


def _reconcile_with_bronze(dbx, catalog, scratch, board, market, buckets,
                           bucket_limit, exclude=None):
    """Independent source reconciliation on the exact observation keys —
    against Bronze itself, never against what the reader happened to
    return. Returns (missing, extra) key counts."""
    source = _bronze_source(catalog, board, exclude)
    limit = min(buckets, bucket_limit or buckets)
    params = {"board": board, "market": market}
    expected = (f"SELECT run_id, board, market, board_job_id "
                f"FROM {source} o WHERE o.board = :board "
                f"AND o.market = :market "
                f"AND pmod(hash(o.board_job_id), {buckets}) < {limit}")
    staged = (f"SELECT run_id, board, market, board_job_id FROM {scratch} "
              f"WHERE board = :board AND market = :market")
    missing = _single_value(
        dbx, f"SELECT count(*) FROM ({expected} EXCEPT {staged})",
        parameters=params)
    extra = _single_value(
        dbx, f"SELECT count(*) FROM ({staged} EXCEPT {expected})",
        parameters=params)
    return int(missing or 0), int(extra or 0)


def _validate_standardized(dbx, catalog, scratch, plan, expected,
                           exclude=None):
    """Publication-blocking invariants on the standardized candidate.
    With `exclude` (incremental mode) the candidate must also be disjoint
    from the table it will be merged into."""
    violations = []
    if exclude is not None:
        overlap = _single_value(dbx, f"""
            SELECT count(*) FROM {scratch} s JOIN {exclude} t
            ON {observation_key_match('t', 's')}""")
        if int(overlap or 0):
            violations.append(f"staged keys already present in {exclude}: "
                              f"{overlap}")
    staged = _single_value(dbx, f"SELECT count(*) FROM {scratch}")
    if int(staged) != expected:
        violations.append(f"row accounting: staged {staged} != read {expected}")
    null_identity = _single_value(dbx, f"""
        SELECT count(*) FROM {scratch}
        WHERE run_id IS NULL OR board IS NULL OR market IS NULL
           OR board_job_id IS NULL""")
    if int(null_identity):
        violations.append(f"rows with NULL identity (parse failure?): "
                          f"{null_identity}")
    duplicates = _single_value(dbx, f"""
        SELECT count(*) FROM (SELECT 1 FROM {scratch}
        GROUP BY run_id, board, market, board_job_id HAVING count(*) > 1)""")
    if int(duplicates):
        violations.append(f"logical-key duplicates: {duplicates}")
    mixed_pair = _single_value(dbx, f"""
        SELECT count(*) FROM {scratch}
        WHERE (fingerprint IS NULL) != (minhash_signature IS NULL)""")
    if int(mixed_pair):
        violations.append(f"matching pair not all-or-nothing: {mixed_pair}")
    empty_arrays = _single_value(dbx, f"""
        SELECT count(*) FROM {scratch} WHERE """ + " OR ".join(
        f"size({column}) = 0" for column in _ARRAY_COLUMNS))
    if int(empty_arrays):
        violations.append(f"empty arrays stored: {empty_arrays}")
    for board, market, buckets, bucket_limit in plan:
        missing, extra = _reconcile_with_bronze(
            dbx, catalog, scratch, board, market, buckets, bucket_limit,
            exclude)
        if missing or extra:
            violations.append(
                f"Bronze key reconciliation {board} × {market}: "
                f"{missing} missing, {extra} extra observation key(s)")
    _require(violations, "standardized candidate")


# ---------------------------------------------------- canonicalize stage

# The source query text lives in canonicalize_on_databricks (shared with the
# Spark job so both engines execute identical definitions); this side adds
# the listing-hash bucket predicate per INLINE read.


def _silver_read_buckets(dbx, source, projection, predicate="TRUE"):
    """Listing-hash bucket count for a Silver read, sized from the measured
    serialized bytes of exactly the rows it returns — the same READ_BYTE_TARGET
    discipline as the Bronze reader, so no single INLINE result can approach
    the Statement API limit at any corpus size. Buckets are whole listings
    (hash of board_job_id), so per-listing groups and windows stay intact and
    the union read is identical for any bucket count."""
    nbytes = _single_value(
        dbx, f"SELECT coalesce(sum(length(to_json(struct({projection})))), 0) "
             f"FROM {source} s WHERE {predicate}")
    return max(1, -(-int(nbytes or 0) // READ_BYTE_TARGET))


def _listing_bucket(buckets, bucket):
    return f"pmod(hash(s.board_job_id), {buckets}) = {bucket}"


def _read_memberships(dbx, catalog, source):
    runs = table_name(catalog, SCRAPE_RUNS_TABLE)
    buckets = _silver_read_buckets(
        dbx, source, "s.board, s.market, s.board_job_id, s.fingerprint, "
        "s.advertiser_name, s.run_id", "s.fingerprint IS NOT NULL")
    memberships = []
    for bucket in range(buckets):
        rows = dbx.query(spark_job.memberships_sql(
            source, runs, _listing_bucket(buckets, bucket)))
        memberships += [
            {"board": b, "market": m, "board_job_id": j, "fingerprint": fp,
             "first_seen_membership_at": seen, "advertiser": advertiser}
            for b, m, j, fp, seen, advertiser in rows]
    return memberships


def _read_transitions(dbx, catalog, source):
    """Observed consecutive usable-wording transitions per listing — the
    SQL twin of build_candidate_pairs.transitions_from_observations."""
    runs = table_name(catalog, SCRAPE_RUNS_TABLE)
    buckets = _silver_read_buckets(
        dbx, source, "s.board, s.market, s.board_job_id, s.fingerprint, "
        "s.run_id", "s.fingerprint IS NOT NULL")
    transitions = set()
    for bucket in range(buckets):
        rows = dbx.query(spark_job.transitions_sql(
            source, runs, _listing_bucket(buckets, bucket)))
        transitions |= {tuple(row) for row in rows}
    return transitions


def _document_read_buckets(dbx, source):
    """Batch count for document retrieval: enough to keep every INLINE result
    small, never more than the data warrants. Pure I/O partitioning — the
    union read is identical for any batch count, so canonical results cannot
    depend on it."""
    fingerprints = _single_value(dbx, spark_job.document_count_sql(source))
    return max(1, -(-int(fingerprints or 0) // DOCS_PER_READ))


def _require_determinate_representatives(dbx, source, runs):
    """The development engine's copy of the packaged job's gate: the same
    statement and the same pure decision, so both engines refuse the same
    data. `max_by` does not specify which of several maximum rows it returns,
    so a wording whose tied observations read differently has no defined
    representative and no build is defined on it. Observations BELOW the
    maximum are not consulted: the representative is the unique winner and
    older rows cannot make it ambiguous."""
    tied = [tuple(row) for row in
            dbx.query(spark_job.tied_representatives_sql(source, runs))]
    violations = spark_job.representative_violations(tied)
    if violations:
        raise canonical.features.RepresentativeNotDetermined(
            f"{source} does not determine its representatives: "
            + "; ".join(violations))


def _read_documents(dbx, catalog, source, buckets):
    """One representative observation per wording — title, description and
    signature taken together from the LATEST observation carrying that
    fingerprint, so a rebuild over identical data reads identical inputs."""
    runs = table_name(catalog, SCRAPE_RUNS_TABLE)
    docs, signatures, titles, bodies = {}, {}, {}, {}
    for bucket in range(buckets):
        rows = dbx.query(spark_job.documents_sql(
            source, runs, f"pmod(hash(s.fingerprint), {buckets}) = {bucket}"))
        for fp, title, text, signature in rows:
            docs[fp] = canonical.features.matching_document(title, text)
            signatures[fp] = (json.loads(signature)
                              if isinstance(signature, str) else signature)
            titles[fp] = title
            # the description BODY under the same matching normalization,
            # title excluded — the identical-body rule compares exactly this
            bodies[fp] = canonical.features.matching_document(None, text)
    return docs, signatures, titles, bodies


def _read_never_usable(dbx, source):
    buckets = _silver_read_buckets(
        dbx, source, "s.board, s.market, s.board_job_id", "s.fingerprint IS NULL")
    listings = []
    for bucket in range(buckets):
        rows = dbx.query(spark_job.never_usable_sql(
            source, _listing_bucket(buckets, bucket)))
        listings += [tuple(row) for row in rows]
    return listings


def deployed_wheel(path):
    """The deployment-provided bto wheel, resolved only when a Databricks
    canonicalization is about to run — never built here. `path` is the wheel
    file itself or the deployment-owned directory holding exactly ONE wheel
    (the unit file names the directory, so a version bump never edits it, and
    stale wheels accumulating there are a refusal, never a lottery). Changed
    code always arrives as a NEW package version: the identity check refuses
    a wheel whose name or version differs from the installed source, because
    Databricks serverless reuses a cached environment for an unchanged
    version (deploy/README.md). Raises FileNotFoundError carrying the
    operator-facing reason."""
    if path is None:
        raise FileNotFoundError("the databricks engine needs --wheel: the "
                                "deployed bto wheel (file or directory)")
    path = Path(path)
    if path.is_dir():
        wheels = sorted(path.glob("*.whl"))
        if len(wheels) != 1:
            raise FileNotFoundError(
                f"deployed wheel: {path} holds {len(wheels)} wheel(s), expected "
                f"exactly one — deployment places the wheel built from the "
                f"deployed checkout there and removes the previous one")
        path = wheels[0]
    if path.suffix != ".whl" or not path.is_file():
        raise FileNotFoundError(
            f"deployed wheel not found: {path} — deployment places the bto "
            f"wheel built from the deployed checkout there")
    return path


def _run_on_databricks(dbx, catalog, source, version, scratch, build_id, wheel,
                       baseline=None,
                       timeout_seconds=CANONICALIZE_TIMEOUT_SECONDS):
    """Submit the packaged canonicalization job for snapshot N and wait: the
    deployed wheel is uploaded to the code volume and becomes the task
    environment (with datasketch pinned), so driver and workers run exactly
    the packaged matching code. The job writes the scratch; every later step
    (stamp, joint validation, publication) stays here.

    With `baseline` — the published mapping — the job may extend it instead
    of rebuilding the whole history, but only when it can prove that safe;
    it decides that itself and falls back to the full rebuild otherwise, so
    the result here is the same complete candidate either way.

    `timeout_seconds` is the deadline given to Databricks; this process waits
    `CLIENT_WAIT_MARGIN_SECONDS` longer, so the service always decides a run's
    fate first."""
    wheel = deployed_wheel(wheel)
    _require(spark_job.wheel_mismatch(wheel), "deployed wheel identity")
    data = wheel.read_bytes()
    volume = silver_volume_name(catalog, CLEAN_JOB_LISTINGS_CODE_VOLUME)
    dbx.execute(f"CREATE VOLUME IF NOT EXISTS {volume}")
    wheel_path = silver_volume_path(catalog, CLEAN_JOB_LISTINGS_CODE_VOLUME,
                                    wheel.name)
    log.info("uploading %s (%d bytes, sha256 %s) to %s", wheel.name, len(data),
             hashlib.sha256(data).hexdigest(), wheel_path)
    dbx.upload(wheel_path, data)
    parameters = ["--source", source, "--version", str(version),
                  "--runs", table_name(catalog, SCRAPE_RUNS_TABLE),
                  "--scratch", scratch]
    if baseline:
        parameters += ["--mapping", baseline]
    run_id = dbx.submit_wheel_run(
        f"bto canonicalize {build_id}", wheel_path, spark_job.ENTRY_POINT_NAME,
        parameters, timeout_seconds=timeout_seconds)
    log.info("submitted Databricks run %s for %s at version %d "
             "(job deadline %ds, waiting up to %ds)", run_id, source, version,
             timeout_seconds, timeout_seconds + CLIENT_WAIT_MARGIN_SECONDS)
    result, message, tasks = dbx.wait_run(
        run_id, timeout_seconds=timeout_seconds + CLIENT_WAIT_MARGIN_SECONDS)
    logs = dbx.run_logs(tasks[0]) if tasks else ""
    diagnostics = {}
    for line in logs.splitlines():
        if line.startswith(spark_job.DIAGNOSTICS_MARKER):
            diagnostics = json.loads(line[len(spark_job.DIAGNOSTICS_MARKER):])
    if result != "SUCCESS":
        _require([f"Databricks run {run_id} ended {result}: {message} — "
                  f"{logs[-1500:]}"], "mapping candidate")
    for key, value in sorted(diagnostics.items()):
        log.info("graph diagnostic %s = %s", key, value)
    if diagnostics.get("mode") == "incremental":
        log.info("canonicalization mode: INCREMENTAL from version %s to %s "
                 "(%s new wording(s), %s seeded known wording(s), %s wording(s) "
                 "in the closed region after %s round(s), %s of history)",
                 diagnostics.get("baseline_version"),
                 diagnostics.get("version"),
                 diagnostics.get("new_fingerprints"),
                 diagnostics.get("seeded_known_fingerprints"),
                 diagnostics.get("region_wordings"),
                 diagnostics.get("closure_rounds"),
                 diagnostics.get("region_fraction"))
    else:
        log.info("canonicalization mode: FULL REBUILD at version %s%s",
                 diagnostics.get("version"),
                 f" — incremental refused: {diagnostics['fallback_detail']}"
                 if diagnostics.get("fallback_detail") else "")
    return diagnostics


def run_canonicalize(dbx, catalog, source, target_suffix, build_id=None,
                     engine="python", wheel=None, baseline=None,
                     timeout_seconds=CANONICALIZE_TIMEOUT_SECONDS):
    """Build, stamp and validate the mapping CANDIDATE from `source`.

    Captures the source's Delta version N BEFORE the first read and pins every
    read of this canonicalization — memberships, transitions, documents,
    never-usable listings and the joint validation — to that one immutable
    snapshot. Publishes nothing; returns (scratch, N, diagnostics): the
    run-scoped scratch table holding the candidate, the version it must be
    published against (`publish_mapping`), and the build's graph diagnostics,
    whose ``mode`` says whether it extended the baseline or rebuilt the whole
    history (with ``fallback_reason`` when an extension was refused).

    `baseline` offers a published mapping as an incremental starting point on
    the nightly Databricks path. The job proves that safe or rebuilds the whole
    history itself, so the candidate validated and published here is the same
    complete mapping either way. Without a baseline, the authoritative
    full-history reconstruction runs.

    `timeout_seconds` bounds the Databricks job only; the in-process Python
    engine has no deadline of its own.
    """
    build_id = build_id or new_build_id()
    scratch = silver_table_name(
        catalog, "_build_" + JOB_CANONICAL_MAPPING_TABLE
        + f"{target_suffix}_{build_id}")
    version = freshness.current_version(dbx, source)
    snapshot = pinned(source, version)
    log.info("canonicalizing %s at Delta version %d (scratch %s)", source,
             version, scratch)

    if engine == "databricks":
        diagnostics = _run_on_databricks(
            dbx, catalog, source, version, scratch, build_id, wheel,
            baseline=baseline, timeout_seconds=timeout_seconds)
        rows = None
    else:
        _require_determinate_representatives(
            dbx, snapshot, table_name(catalog, SCRAPE_RUNS_TABLE))
        memberships = _read_memberships(dbx, catalog, snapshot)
        transitions = _read_transitions(dbx, catalog, snapshot)
        doc_buckets = _document_read_buckets(dbx, snapshot)
        docs, signatures, titles, bodies = _read_documents(
            dbx, catalog, snapshot, doc_buckets)
        never_usable = _read_never_usable(dbx, snapshot)
        log.info("canonicalizing %d memberships, %d wordings (%d document "
                 "read(s)), %d transitions, %d never-usable listings",
                 len(memberships), len(docs), doc_buckets, len(transitions),
                 len(never_usable))

        rows, diagnostics = canonical.build_mapping_rows(
            memberships, docs, signatures=signatures, never_usable=never_usable,
            titles=titles, bodies=bodies, transitions=transitions)
        diagnostics["mode"] = "full_rebuild"     # this engine never extends
        for key, value in sorted(diagnostics.items()):
            log.info("graph diagnostic %s = %s", key, value)

        # substantive invariants against the candidate, before stamping.
        expected_listings = {(m["board"], m["market"], m["board_job_id"])
                             for m in memberships} | set(never_usable)
        _require(canonical.validate_candidate(
            rows, expected_listings=expected_listings,
            standardized_fingerprints=set(docs), titles=titles),
            "mapping candidate")

        _fresh_scratch(dbx, scratch, _MAPPING_DDL)
        _insert_rows(dbx, scratch, _MAPPING_DDL, canonical.MAPPING_COLUMNS,
                     (dict(row, canonicalized_at=None) for row in rows))
    duplicates = _single_value(dbx, f"""
        SELECT count(*) FROM (SELECT 1 FROM {scratch}
        GROUP BY board, market, board_job_id, fingerprint
        HAVING count(*) > 1)""")
    if int(duplicates):
        _require([f"staged logical-key duplicates: {duplicates}"],
                 "mapping candidate")

    # capture ONE timestamp, stamp it, verify a single value landed.
    canonicalized_at = datetime.now(timezone.utc) \
        .isoformat(sep=" ", timespec="seconds").replace("+00:00", "")
    dbx.execute(f"UPDATE {scratch} SET canonicalized_at = cast(:ts AS TIMESTAMP)",
                parameters={"ts": canonicalized_at})
    _require(_validate_joint(dbx, snapshot, scratch), "mapping candidate")
    staged = int(_single_value(dbx, f"SELECT count(*) FROM {scratch}") or 0)
    log.info("mapping candidate %s validated against version %d: %d rows "
             "(engine %s)", scratch, version, staged, engine)
    return scratch, version, diagnostics


# ---------------------------------------------- joint validation + promotion

def _stage_mix_rows(mapping, standardized):
    """One row per (group, usable wording): the group id and the wording's
    title — the input of the career-stage group guard."""
    return (f"(SELECT m.canonical_job_id, min(s.title) AS title "
            f"FROM {mapping} m JOIN {standardized} s "
            f"ON s.fingerprint = m.fingerprint "
            f"WHERE m.fingerprint IS NOT NULL "
            f"GROUP BY m.canonical_job_id, m.fingerprint)")


def _stage_mix_count(dbx, standardized, mapping, buckets=None):
    """FINAL groups holding both a clearly intern- and a clearly
    graduate-stage wording — must be zero.

    The career-stage rule is matching code, so one row per usable wording
    comes down to run it, and the full-history result can exceed the Statement
    API's 26 MB INLINE ceiling in one response. The rows are therefore read in
    buckets sized from their measured bytes (the same READ_BYTE_TARGET
    discipline as every other Silver read), keyed by the hash of the GROUP id
    so every canonical group sits wholly inside one bucket and the per-bucket
    counts add up to exactly the unpartitioned answer. `pmod` keeps the key in
    range for any hash value; row accounting across the buckets guards the
    partition itself. Pure I/O partitioning — the rule, the groups and the
    verdict are untouched.
    """
    rows_sql = _stage_mix_rows(mapping, standardized)
    buckets = buckets or _silver_read_buckets(
        dbx, rows_sql, "s.canonical_job_id, s.title")
    expected = int(_single_value(
        dbx, f"SELECT count(*) FROM {rows_sql} s") or 0)
    mixed = read = 0
    for bucket in range(buckets):
        rows = dbx.query(
            f"SELECT s.canonical_job_id, s.title FROM {rows_sql} s "
            f"WHERE pmod(hash(s.canonical_job_id), {buckets}) = {bucket}")
        stages = {}
        for group, title in rows:
            read += 1
            stage = canonical.features.career_stage(title)
            if stage:
                stages.setdefault(group, set()).add(stage)
        mixed += sum(1 for s in stages.values() if {"intern", "graduate"} <= s)
    if read != expected:
        raise BuildError(f"stage-mix read incomplete: {read} of {expected} "
                         f"group rows across {buckets} bucket(s)")
    return mixed


def _validate_joint(dbx, standardized, mapping):
    """The two tables as ONE generation: the shared standardized ↔ mapping
    contract (`freshness.mapping_invariants`, also used by consumers) plus
    the build-time checks that need matching code or are
    diagnostics only — the intern/graduate group guard and the
    canonicalized_at ≥ standardized_at ordering (a sanity check, never the
    freshness proof)."""
    violations = freshness.mapping_invariants(dbx, standardized, mapping)
    rows = dbx.query(f"""
        SELECT CASE WHEN (SELECT max(standardized_at) FROM {standardized})
                         <= (SELECT min(canonicalized_at) FROM {mapping})
                    THEN 0 ELSE 1 END""")
    if rows and int(rows[0][0] or 0):
        violations.append("mapping generation older than the standardized "
                          "generation: 1")
    mixed = _stage_mix_count(dbx, standardized, mapping)
    if mixed:
        violations.append(f"groups mixing intern and graduate stages: {mixed}")
    return violations


def publish_mapping(dbx, catalog, scratch, target, source, version):
    """Publish a validated mapping candidate bound to standardized version N.

    1. pre-publication race check: any DATA-CHANGING commit on `source`
       strictly above N refuses publication — the existing complete mapping
       stays untouched;
    2. ONE atomic Delta statement replaces the rows AND sets the property
       `bto.standardized_version = N` (a create-or-replace without the clause
       would reset it, so the rows and the binding can only appear together);
    3. post-publication race check: a data-changing commit that landed after
       step 1 makes the just-published mapping stale — the run fails, the
       mapping stays (its property truthfully says N) and the freshness guard
       rejects it until canonicalization reruns.
    Maintenance-only commits above N never block.
    """
    if scratch == target:
        raise BuildError(f"publication source and target coincide: {target}")
    changed = freshness.data_changing_commits_above(dbx, source, version)
    if changed:
        _require(["standardized changed during canonicalization: "
                  + ", ".join(f"v{v} {op}" for v, op, *_ in changed[:5])
                  + f" above snapshot {version}"], "mapping publication")
    dbx.execute(f"CREATE OR REPLACE TABLE {target} "
                f"TBLPROPERTIES ('{freshness.STANDARDIZED_VERSION_PROPERTY}' "
                f"= '{int(version)}') AS SELECT * FROM {scratch}")
    log.info("published %s bound to %s version %d", target, source, version)
    changed = freshness.data_changing_commits_above(dbx, source, version)
    if changed:
        _require([f"standardized changed after publication — {target} is "
                  f"bound to version {version} and is now stale: "
                  + ", ".join(f"v{v} {op}" for v, op, *_ in changed[:5])
                  + "; rerun clean job listings"], "mapping publication")
    return target


def should_canonicalize(inserted_rows, freshness_violations):
    """The locked skip rule: skip ONLY when the incremental batch merged
    zero rows AND the existing mapping is provably current."""
    return bool(inserted_rows) or bool(freshness_violations)


def run_nightly(dbx, config, catalog, pairs, args, build_id, standardized_at):
    """Run incremental standardization and canonicalize only when required.

    Both the canonicalization and skip paths finish with the same fail-closed
    freshness proof. The deployed wheel is inspected only when canonicalization
    runs. Returns ``inserted``, ``canonicalized``, bound ``version`` and how
    long each stage took (``canonicalization_elapsed`` is None when skipped),
    and how canonicalization ran (``canonicalization_mode``, None when
    skipped, with ``fallback_reason`` when an extension was refused).
    """
    started = datetime.now(timezone.utc)
    standardized = silver_table_name(
        catalog, STANDARDIZED_JOB_LISTINGS_TABLE + args.target_suffix)
    mapping = silver_table_name(
        catalog, JOB_CANONICAL_MAPPING_TABLE + args.target_suffix)
    std_scratch, report = run_standardize(
        dbx, catalog, pairs, args.buckets, args.bucket_limit,
        args.target_suffix, standardized_at, incremental_target=standardized,
        build_id=build_id)
    report["inserted"] = publish_incremental(
        dbx, standardized, std_scratch, report["staged_rows"], standardized_at)
    reconcile_full(dbx, catalog, standardized, board=args.board,
                   market=args.market)
    _drop(dbx, std_scratch)
    _report_standardization(config, report, args)
    standardization_elapsed = datetime.now(timezone.utc) - started

    violations = freshness.freshness_violations(dbx, catalog, args.target_suffix)
    canonicalize = should_canonicalize(report["inserted"], violations)
    log.info("canonicalization %s: %d new observation(s) inserted; mapping "
             "freshness: %s", "required" if canonicalize else "skipped",
             report["inserted"], "; ".join(violations) or "current")
    canonicalization_elapsed = None
    diagnostics = {}
    if canonicalize:
        canonicalization_started = datetime.now(timezone.utc)
        # Only the nightly Databricks path offers the published mapping as an
        # incremental baseline; the packaged job decides whether extension is
        # provably safe.
        map_scratch, version, diagnostics = run_canonicalize(
            dbx, catalog, standardized, args.target_suffix, build_id,
            engine=args.engine, wheel=args.wheel,
            baseline=mapping if args.engine == "databricks" else None,
            timeout_seconds=args.canonicalize_timeout_seconds)
        publish_mapping(dbx, catalog, map_scratch, mapping, standardized,
                        version)
        _drop(dbx, map_scratch)
        canonicalization_elapsed = (datetime.now(timezone.utc)
                                    - canonicalization_started)
    version = freshness.require_current_mapping(dbx, catalog, args.target_suffix)
    if canonicalize:
        # the reconciliation above ran BEFORE a canonicalization that may have
        # taken an hour: prove Bronze ↔ standardized once more, right before
        # success. The skip path reconciled seconds ago and touched nothing.
        reconcile_full(dbx, catalog, standardized, board=args.board,
                       market=args.market)
    log.info("cleaning complete: %s current against %s version %d", mapping,
             standardized, version)
    return {"inserted": report["inserted"], "canonicalized": canonicalize,
            "version": version,
            "standardization_elapsed": standardization_elapsed,
            "canonicalization_elapsed": canonicalization_elapsed,
            "canonicalization_mode": diagnostics.get("mode"),
            "fallback_reason": diagnostics.get("fallback_reason")}


def promote(dbx, catalog, standardized_source, mapping_source, target_suffix):
    """Publish an EXACT validated pair of tables — never a recomputation.

    Re-runs the joint invariants on the sources, then replaces each target
    with one atomic Delta `CREATE OR REPLACE TABLE … AS SELECT *`; readers
    see the previous complete table until each commit. The two replacements
    are separate commits, standardized first, so a failure between them
    leaves the targets from different generations — the mapping's bound
    standardized version is what a consumer verifies before reading.
    """
    _require(_validate_joint(dbx, standardized_source, mapping_source),
             "promotion candidate")
    standardized_target = silver_table_name(
        catalog, STANDARDIZED_JOB_LISTINGS_TABLE + target_suffix)
    mapping_target = silver_table_name(
        catalog, JOB_CANONICAL_MAPPING_TABLE + target_suffix)
    for source, target in ((standardized_source, standardized_target),
                           (mapping_source, mapping_target)):
        if source == target:
            raise BuildError(f"promotion source and target coincide: {target}")
    dbx.execute(f"CREATE OR REPLACE TABLE {standardized_target} "
                f"AS SELECT * FROM {standardized_source}")
    log.info("promoted %s → %s", standardized_source, standardized_target)
    # the pair was validated together and the standardized copy is exact, so
    # the mapping is bound to the version the copy just created
    version = freshness.current_version(dbx, standardized_target)
    publish_mapping(dbx, catalog, mapping_source, mapping_target,
                    standardized_target, version)
    return [standardized_target, mapping_target]


def _drop(dbx, *tables):
    for table in tables:
        dbx.execute(f"DROP TABLE IF EXISTS {table}")


# ----------------------------------------------------------------- CLI

# Table and volume names are composed UNQUOTED from these option values, so
# their shape is a usage rule checked before configuration is even loaded: a
# suffix is "_" plus letters, digits or "_" (`--target-suffix " "` names the
# production table with a trailing space; quotes, dots, ";", "--" or
# backticks alias another name or break the statement), a build id is
# letters, digits or "_" (what `new_build_id` generates), and a bucket count
# or limit is 1 or more (`range(-1)` reads nothing, and an empty candidate
# must never reach a publication).
_SUFFIX_SHAPE = re.compile(r"_[A-Za-z0-9_]+")
_BUILD_ID_SHAPE = re.compile(r"[A-Za-z0-9_]+")


def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is not 1 or more")
    return value


_positive_int.__name__ = "positive integer"      # argparse's "invalid … value"


def _positive_hours(text):
    """A finite, strictly positive number of hours. `float` accepts "inf" and
    "nan", and a deadline of either is not a deadline, so both are refused."""
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text} is not a finite number")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not greater than zero")
    return value


_positive_hours.__name__ = "positive hours"


def _check_name_fragments(parser, args):
    """Refuse any suffix or build id that is not a plain identifier
    fragment. "" is the production (unsuffixed) name and is judged by the
    stage rules, never here."""
    for option, value in (("--target-suffix", args.target_suffix),
                          ("--source-suffix", args.source_suffix)):
        if value and not _SUFFIX_SHAPE.fullmatch(value):
            parser.error(f"{option} {value!r}: a suffix is '_' followed by "
                         "letters, digits or '_' only")
    if args.build_id is not None and not _BUILD_ID_SHAPE.fullmatch(args.build_id):
        parser.error(f"--build-id {args.build_id!r}: letters, digits or '_' only")


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 4, not argparse's 2. Every exit status has one
    owner: 2, 3 and 5 are returned only after run.py DELIVERED its alert,
    so the systemd fallback (ExecStopPost= in clean-job-listings.service)
    stays silent on them; 6 is the same handled failures when the alert did
    NOT reach SMTP; 4 (usage, configuration) and 1 (Python itself — an
    import error, a missing dependency — before main() ran) carry no alert.
    The fallback reports 6, 4 and 1."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(4, f"{self.prog}: error: {message}\n")


def main(argv=None):
    parser = _Parser(
        description="Clean job listings: standardize Bronze, canonicalize, promote.")
    parser.add_argument("--stage",
                        choices=("nightly", "standardize", "canonicalize", "all",
                                 "promote"),
                        default="all",
                        help="nightly: the unattended production run — "
                             "incremental standardization, full "
                             "reconciliation, canonicalization only when "
                             "required, final freshness proof")
    parser.add_argument("--mode", choices=("wholesale", "incremental"),
                        default="wholesale",
                        help="standardize: rebuild every observation "
                             "(wholesale) or merge only the observations "
                             "missing from the target (incremental)")
    parser.add_argument("--engine", choices=("python", "databricks"),
                        default="python",
                        help="canonicalize in this process (python) or as a "
                             "Databricks job running the packaged wheel")
    parser.add_argument("--wheel", default=None,
                        help="databricks engine: the deployed bto wheel — the "
                             "file, or the directory holding exactly one; "
                             "read only when canonicalization runs")
    parser.add_argument("--canonicalize-timeout-hours", type=_positive_hours,
                        default=CANONICALIZE_TIMEOUT_SECONDS / 3600,
                        help="how long the Databricks canonicalization job may "
                             "run before the service terminates it (default: "
                             "3). Headroom, not a target: a job that finishes "
                             "early still finishes early. Raise it only for a "
                             "build with no measured runtime, such as the "
                             "first full-history one")
    parser.add_argument("--build-id", default=None,
                        help="run identifier for the scratch names "
                             "(default: generated like collection run ids)")
    parser.add_argument("--board")
    parser.add_argument("--market")
    parser.add_argument("--buckets", type=_positive_int, default=None,
                        help="override the per-pair BRONZE read bucket count "
                             "(default: sized from measured payload bytes)")
    parser.add_argument("--bucket-limit", type=_positive_int, default=None,
                        help="process only the first N buckets (smoke runs)")
    parser.add_argument("--target-suffix", default="",
                        help="suffix for the Silver table names — use a "
                             "clearly-temporary suffix for validation runs")
    parser.add_argument("--source-suffix", default=None,
                        help="promote: suffix of the validated tables to "
                             "publish exactly as they are")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout, force=True)
    _check_name_fragments(parser, args)
    # Hours are the operator's unit; seconds are every API's. Convert once.
    # Never let a sub-second request round to 0: the Jobs API reads 0 as NO
    # timeout, the exact opposite of what a small value asks for.
    args.canonicalize_timeout_seconds = max(
        1, int(args.canonicalize_timeout_hours * 3600))

    try:
        config = settings_module.load()
    except SystemExit as failure:
        # without configuration there is no way to email: exit 4 marks "not
        # alerted" for the systemd fallback (see _Parser)
        log.error("CONFIGURATION: %s", failure)
        return 4
    catalog = config["databricks_catalog"]
    pairs = [(board, market) for board, market
             in settings_module.supported_runs()
             if (not args.board or board == args.board)
             and (not args.market or market == args.market)]
    if args.stage in ("standardize", "canonicalize") and not args.target_suffix:
        parser.error("production targets are only built by --stage all and "
                     "maintained by --stage nightly (both tables as one "
                     "generation); give --target-suffix for a partial build")
    if args.stage == "promote" and args.source_suffix is None:
        parser.error("--stage promote needs --source-suffix")
    if args.stage == "promote" and not args.target_suffix:
        parser.error("--stage promote publishes a validated pair into a "
                     "suffixed target only: a validation pair covers a "
                     "fraction of Bronze and can never be the production "
                     "tables, which --stage all builds from complete Bronze")
    if args.mode == "incremental" and args.stage != "standardize":
        parser.error("--mode incremental applies to --stage standardize only")
    if args.engine == "databricks" and not args.wheel:
        parser.error("--engine databricks needs --wheel <path to the bto wheel>")
    if args.stage in ("nightly", "all") and args.engine != "databricks" \
            and not args.target_suffix:
        parser.error(f"--stage {args.stage} on the production tables "
                     "canonicalizes on Databricks; --engine python is a "
                     "development option that needs --target-suffix")
    if args.stage in ("nightly", "all") and not args.target_suffix \
            and (args.board or args.market or args.bucket_limit):
        parser.error(f"--stage {args.stage} on the production tables always "
                     "covers the complete supported board × market population "
                     "and reconciles all of Bronze; --board, --market and "
                     "--bucket-limit are development options that need "
                     "--target-suffix")
    if args.dry_run:
        log.info("dry run: stage=%s mode=%s catalog=%s pairs=%s buckets=%s "
                 "bucket_limit=%s suffix=%r source_suffix=%r build_id=%r "
                 "canonicalize_timeout=%ds",
                 args.stage, args.mode, catalog, pairs, args.buckets,
                 args.bucket_limit, args.target_suffix, args.source_suffix,
                 args.build_id, args.canonicalize_timeout_seconds)
        return 0

    build_id = args.build_id or new_build_id()
    args.build_id = build_id
    standardized = silver_table_name(
        catalog, STANDARDIZED_JOB_LISTINGS_TABLE + args.target_suffix)
    mapping = silver_table_name(
        catalog, JOB_CANONICAL_MAPPING_TABLE + args.target_suffix)
    # what a successful production run reports in its heartbeat; None until a
    # stage that owns one completes, so no failure path can send it
    summary, began = None, None
    try:
        # everything that talks to Databricks — the client, the schema
        # preflight and every stage — sits inside this boundary, so an outage
        # at start is reported exactly like one mid-run (one alert, exit 5)
        dbx = Databricks(config["databricks_host"], config["databricks_token"],
                         config.get("databricks_warehouse_id"), timeout=300)
        _ensure_silver_schema(dbx, catalog)
        if args.stage == "promote":
            promote(dbx, catalog,
                    silver_table_name(catalog, STANDARDIZED_JOB_LISTINGS_TABLE
                                      + args.source_suffix),
                    silver_table_name(catalog, JOB_CANONICAL_MAPPING_TABLE
                                      + args.source_suffix),
                    args.target_suffix)
            return 0
        began = datetime.now(timezone.utc)
        standardized_at = began.isoformat(sep=" ", timespec="seconds") \
            .replace("+00:00", "")
        if args.stage == "nightly":
            summary = run_nightly(dbx, config, catalog, pairs, args, build_id,
                                  standardized_at)
        elif args.stage == "all":
            # wholesale rebuild (the first production build): publish the
            # standardized candidate, prove COMPLETE Bronze ↔ the ACTUAL
            # published table, and only then canonicalize FROM that table at
            # its version N and publish the mapping bound to N
            std_scratch, report = run_standardize(
                dbx, catalog, pairs, args.buckets, args.bucket_limit,
                args.target_suffix, standardized_at, build_id=build_id)
            dbx.execute(f"CREATE OR REPLACE TABLE {standardized} "
                        f"AS SELECT * FROM {std_scratch}")
            _drop(dbx, std_scratch)
            reconcile_full(dbx, catalog, standardized,
                           board=args.board, market=args.market)
            _report_standardization(config, report, args)
            map_scratch, version, diagnostics = run_canonicalize(
                dbx, catalog, standardized, args.target_suffix, build_id,
                engine=args.engine, wheel=Path(args.wheel) if args.wheel else None,
                timeout_seconds=args.canonicalize_timeout_seconds)
            publish_mapping(dbx, catalog, map_scratch, mapping, standardized,
                            version)
            _drop(dbx, map_scratch)
            freshness.require_current_mapping(dbx, catalog, args.target_suffix)
            # canonicalization may have taken an hour: prove complete Bronze ↔
            # the published table once more, so exit 0 never claims coverage
            # that Bronze has outgrown meanwhile
            reconcile_full(dbx, catalog, standardized,
                           board=args.board, market=args.market)
            log.info("cleaning complete: %s current against %s version %d",
                     mapping, standardized, version)
            # the shape run_nightly returns, without stage timings: this build
            # published every staged row and always canonicalized
            summary = {"inserted": report["staged_rows"],
                       "canonicalized": True, "version": version,
                       "canonicalization_mode": diagnostics.get("mode")}
        elif args.stage == "standardize" and args.mode == "incremental":
            std_scratch, report = run_standardize(
                dbx, catalog, pairs, args.buckets, args.bucket_limit,
                args.target_suffix, standardized_at,
                incremental_target=standardized, build_id=args.build_id)
            report["inserted"] = publish_incremental(
                dbx, standardized, std_scratch, report["staged_rows"],
                standardized_at)
            reconcile_full(dbx, catalog, standardized,
                           board=args.board, market=args.market)
            _drop(dbx, std_scratch)
            _report_standardization(config, report, args)
            log.info("incremental standardization of %s complete: %d new "
                     "observation(s) inserted", standardized, report["inserted"])
        elif args.stage == "standardize":
            std_scratch, report = run_standardize(
                dbx, catalog, pairs, args.buckets, args.bucket_limit,
                args.target_suffix, standardized_at, build_id=args.build_id)
            dbx.execute(f"CREATE OR REPLACE TABLE {standardized} "
                        f"AS SELECT * FROM {std_scratch}")
            _drop(dbx, std_scratch)
            _report_standardization(config, report, args)
            log.info("published %s alone (partial build; mapping not "
                     "refreshed)", standardized)
        else:
            map_scratch, version, _diagnostics = run_canonicalize(
                dbx, catalog, standardized, args.target_suffix, build_id,
                engine=args.engine, wheel=Path(args.wheel) if args.wheel else None,
                timeout_seconds=args.canonicalize_timeout_seconds)
            publish_mapping(dbx, catalog, map_scratch, mapping, standardized,
                            version)
            _drop(dbx, map_scratch)
            freshness.require_current_mapping(dbx, catalog, args.target_suffix)
            log.info("published %s from %s version %d", mapping, standardized,
                     version)
    except BuildError as failure:
        log.error("BUILD FAILED — %s", failure)
        # 6 when the alert never reached SMTP. The ExecStopPost fallback
        # suppresses itself for 2, 3 and 5, so an undelivered alert has to
        # exit with a status it does not suppress or the failure is silent.
        return 2 if _alert(config, args, "cleaning build failed",
                           str(failure)) else 6
    except freshness.StaleMapping as stale:
        log.error("CLEANING INCOMPLETE — %s", stale)
        return 3 if _alert(config, args, "cleaning mapping not current",
                           str(stale)) else 6
    except Exception as failure:
        # Operational failures that are not build verdicts — a Databricks API
        # or network error, the job wait deadline, a missing deployed wheel,
        # anything unexpected. The traceback stays in the journal, one alert
        # is attempted, and the exit is 5 — never 1, which Python uses when it
        # dies BEFORE main() (an import error) and nobody alerted.
        # Nothing here bypasses validation: each publication statement is
        # atomic and its candidate is validated first.
        log.exception("CLEANING FAILED — %s", type(failure).__name__)
        return 5 if _alert(config, args, "cleaning failed",
                           f"{type(failure).__name__}: {failure}") else 6
    # OUTSIDE the try: the heartbeat reports work that is already finished and
    # published, so nothing it does may turn a successful run into a failure
    _summary(config, args, summary, began)
    return 0


def _report_standardization(config, report, args):
    """Log the monitoring tallies; alert through the existing path when any
    pair is partial. Alerting never fails the work that produced it."""
    for pair, tally in sorted(report["pairs"].items()):
        log.info("monitoring %s: %s", pair, json.dumps(tally, sort_keys=True))
    if report.get("partial_pairs"):
        _alert(config, args, "standardization partial: quarantined rows",
               json.dumps({pair: report["pairs"][pair]
                           for pair in report["partial_pairs"]},
                          indent=1, sort_keys=True))


def _summary(config, args, result, began):
    """The nightly heartbeat: one email per SUCCESSFUL production run.

    Only the two stages that maintain the production tables send it, and only
    when they are unsuffixed — a suffixed validation pair, a partial stage and
    a dry run stay silent. Problem alerts are independent, so
    a run with quarantined rows sends its alert AND this.

    Fail-soft like every alert here: an undelivered summary is logged and the
    run still succeeds. Its absence is an operational signal to investigate.
    """
    if result is None or args.target_suffix or args.stage not in ("nightly", "all"):
        return
    elapsed = None
    if began is not None:
        elapsed = timedelta(seconds=int(
            (datetime.now(timezone.utc) - began).total_seconds()))
    try:
        notify.send_clean_summary(
            config, run_id=getattr(args, "build_id", None) or "clean_job_listings",
            stage=args.stage, inserted=result.get("inserted"),
            canonicalized=result.get("canonicalized"),
            version=result.get("version"), elapsed=elapsed,
            standardization_elapsed=result.get("standardization_elapsed"),
            canonicalization_elapsed=result.get("canonicalization_elapsed"),
            canonicalization_mode=result.get("canonicalization_mode"),
            fallback_reason=result.get("fallback_reason"))
    except Exception as failure:
        log.warning("summary not sent: %s", failure)


def _alert(config, args, cause, detail):
    """True only when the alert reached SMTP; False when it did not."""
    try:
        return notify.send_problem_alert(
            config, run_id=getattr(args, "build_id", None) or "clean_job_listings",
            board=args.board or "all", market=args.market or "all",
            cause=cause, detail=detail, subject=notify.CLEAN_ALERT_SUBJECT)
    except Exception as failure:
        log.warning("alert not sent (%s): %s", cause, failure)
        return False


if __name__ == "__main__":
    sys.exit(main())
