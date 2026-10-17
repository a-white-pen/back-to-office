"""Populate cleaned wording history and catch up from represented PostgreSQL state."""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from bto.filter_and_bucket_job_listings import HISTORY_REASON, PROCESSING_START
from bto.filter_and_bucket_job_listings.extract_job_listing_details import advertiser_line
from bto.storage.databricks.connection import DatabricksError, silver_table_name, table_name
from bto.storage.databricks.freshness import StaleMapping, require_current_mapping
from bto.storage.postgres.job_sorting import PULL_COLUMNS, hash_input, upsert_listings, watermark

log = logging.getLogger(__name__)
BOARD_FACTS = (
    "job_location",
    "posted_date",
    "company_website",
    "company_employee_count",
    "categories",
    "job_function",
    "position_levels",
    "min_years_experience",
    "skills",
    "flexible_work_arrangements",
    "board_attributes",
    "screening_questions",
    "contacts",
    "poster_name",
    "poster_profile_url",
    "apply_url",
    "apply_type",
    "job_source_name",
)
READ_BYTES = 16_000_000


def wording_hash(row):
    return hash_input(
        [row.get("title") or "", (row.get("description_text") or "")[:16000], advertiser_line(row)]
    )


def prepare_wordings(records):
    merged = {}
    for record in records:
        row = dict(record)
        for prefix in ("listing_first_seen", "wording_first_seen", "wording_last_seen"):
            if prefix + "_us" in row:
                micros = int(row[prefix + "_us"])
                seconds, remainder = divmod(micros, 1_000_000)
                row[prefix + "_at"] = datetime.fromtimestamp(seconds, timezone.utc).replace(
                    microsecond=remainder
                )
        if not row.get("job_url"):
            raise ValueError("Cleaned listing is missing its board URL")
        if (
            not row["listing_first_seen_at"]
            <= row["wording_first_seen_at"]
            <= row["wording_last_seen_at"]
        ):
            raise ValueError("Invalid listing seen-time order")
        row["wording_hash"] = wording_hash(row)
        row["board_facts"] = {
            k: v
            for k, v in (row.get("board_facts") or {}).items()
            if k in BOARD_FACTS and v is not None
        }
        row.setdefault("canonical_job_id", None)
        row.setdefault("closed_on_board", None)
        key = (row["board"], row["market"], row["board_job_id"], row["wording_hash"])
        old = merged.get(key)
        if old:
            first = min(old["wording_first_seen_at"], row["wording_first_seen_at"])
            listing_first = min(old["listing_first_seen_at"], row["listing_first_seen_at"])
            newest = max(
                [old, row], key=lambda r: (r["wording_last_seen_at"], r.get("latest_run_id", ""))
            )
            fingerprint = max(
                [r for r in [old, row] if r.get("fingerprint")],
                key=lambda r: (r["wording_last_seen_at"], r.get("latest_run_id", "")),
                default={},
            ).get("fingerprint")
            row = dict(newest)
            row.update(
                wording_first_seen_at=first,
                listing_first_seen_at=listing_first,
                fingerprint=fingerprint,
            )
        merged[key] = row
    return list(merged.values())


def latest_version(dbx, table):
    return int(dbx.query(f"DESCRIBE HISTORY {table} LIMIT 1")[0][0])


def source_versions(dbx, catalog):
    return {
        "standardized_version": latest_version(dbx, silver_table_name(catalog, "standardized_job_listings")),
        "runs_version": latest_version(dbx, table_name(catalog, "scrape_runs")),
        "mapping_version": latest_version(dbx, silver_table_name(catalog, "job_canonical_mapping")),
    }


def source_names(catalog, standardized_version, runs_version):
    return (
        f"{silver_table_name(catalog, 'standardized_job_listings')} VERSION AS OF {int(standardized_version)}",
        f"{table_name(catalog, 'scrape_runs')} VERSION AS OF {int(runs_version)}",
    )


def aggregate_sql(silver, runs, bucket_count, bucket, start):
    # The SQL digest only refines raw groups. Python owns the official input hash.
    # A JSON array keeps field boundaries even when payloads contain control characters.
    refinement = "sha2(to_json(array(coalesce(title,''),coalesce(description_text,''),coalesce(advertiser_name,''),coalesce(hiring_company_name,''))),256)"
    facts = ",".join(f"'{key}',o.{key}" for key in BOARD_FACTS)
    content = [
        c
        for c in PULL_COLUMNS
        if c
        not in {
            "board",
            "market",
            "board_job_id",
            "wording_hash",
            "fingerprint",
            "canonical_job_id",
            "closed_on_board",
            "listing_first_seen_at",
            "wording_first_seen_at",
            "wording_last_seen_at",
            "board_facts",
        }
    ]
    fields = ",".join(f"'{c}',o.{c}" for c in content)
    selection = (
        "TRUE"
        if start is None
        else "(started_at>=CAST(:watermark AS TIMESTAMP) OR (started_at<CAST(:watermark AS TIMESTAMP) AND (finished_at IS NULL OR finished_at>=CAST(:watermark AS TIMESTAMP))))"
    )
    return f"""WITH all_obs AS (
        SELECT s.*,r.started_at,r.finished_at FROM {silver} s JOIN {runs} r ON r.run_id=s.run_id
        WHERE s.board=:board AND s.market=:market AND pmod(hash(s.board_job_id),{int(bucket_count)})={int(bucket)}),
        listing AS (SELECT board,market,board_job_id,min(started_at) first_seen FROM all_obs GROUP BY board,market,board_job_id),
        worded AS (SELECT *,{refinement} wkey FROM all_obs WHERE content_hash IS NOT NULL AND {selection}),
        wordings AS (SELECT board,market,board_job_id,wkey,min(started_at) first_seen,max(started_at) last_seen,
            max_by(run_id,struct(started_at,run_id)) latest_run_id,
            max_by(fingerprint,CASE WHEN fingerprint IS NOT NULL THEN struct(started_at,run_id) END) fingerprint
            FROM worded GROUP BY board,market,board_job_id,wkey)
        SELECT to_json(named_struct('board',w.board,'market',w.market,'board_job_id',w.board_job_id,
            'listing_first_seen_us',unix_micros(l.first_seen),'wording_first_seen_us',unix_micros(w.first_seen),
            'wording_last_seen_us',unix_micros(w.last_seen),'latest_run_id',w.latest_run_id,'fingerprint',w.fingerprint,
            {fields},'board_facts',named_struct({facts})))
        FROM wordings w JOIN listing l USING(board,market,board_job_id)
        JOIN worded o ON o.board=w.board AND o.market=w.market AND o.board_job_id=w.board_job_id
            AND o.wkey=w.wkey AND o.run_id=w.latest_run_id"""


def read_bucket(dbx, statement, parameters, bucket_count, bucket):
    try:
        records = [
            json.loads(row[0])
            for row in dbx.query(statement(bucket_count, bucket), parameters=parameters)
        ]
    except DatabricksError as error:
        # Split only a proven result-size refusal; all other source failures remain failures.
        if not any(
            word in str(error).casefold()
            for word in ["truncat", "result size", "response size", "26214400", "26,214,400"]
        ):
            raise
        if bucket_count >= 1_048_576:
            raise ValueError("A source listing exceeds safe inline transfer limits") from error
        log.warning("pull bucket split buckets=%d bucket=%d", bucket_count, bucket)
        yield from read_bucket(dbx, statement, parameters, bucket_count * 2, bucket)
        yield from read_bucket(dbx, statement, parameters, bucket_count * 2, bucket + bucket_count)
        return
    yield records


def write_checkpoint(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(path)


def pull_job_listings(dbx, catalog, conn, *, history=False, checkpoint=None, on_batch=None):
    state = (
        json.loads(Path(checkpoint).read_text())
        if checkpoint and Path(checkpoint).exists()
        else None
    )
    if state and state.get("catalog", catalog) != catalog:
        raise ValueError("Pull checkpoint belongs to a different Databricks catalog")
    if history and state and state.get("status") == "complete":
        # A completed history load is not repeated; ordinary pulling drains subsequent arrivals.
        return {"inserted_ids": [], "rows": 0, "history_complete": True}
    observed = source_versions(dbx, catalog)
    if state and state.get("status") == "complete" and state.get("grouping_available") and all(
        state.get(key) == value for key, value in observed.items()
    ):
        return {"inserted_ids": [], "rows": 0, "grouping_available": True, "unchanged_completed_publication": True}
    # Stop before mutating or admitting paid reads against an unfinished publication.
    if require_current_mapping(dbx, catalog) != observed["standardized_version"] or source_versions(dbx, catalog) != observed:
        raise StaleMapping("Source publication changed before pull")
    if state and state.get("status") == "complete":
        if all(state.get(key) == observed[key] for key in ("standardized_version", "runs_version")):
            proof = {}
            if not refresh_canonical_groups(dbx, catalog, conn, None, proof=proof):
                raise StaleMapping("Canonical grouping is unavailable")
            state.update(proof, grouping_available=True)
            if checkpoint:
                write_checkpoint(checkpoint, state)
            if any(state.get(key) != value for key, value in source_versions(dbx, catalog).items()):
                return pull_job_listings(dbx, catalog, conn, checkpoint=checkpoint, on_batch=on_batch)
            return {"inserted_ids": [], "rows": 0, "grouping_available": True}
        state = None
    start = None if history else watermark(conn)
    if state is None:
        standardized = observed["standardized_version"]
        runs_version = observed["runs_version"]
        state = {
            "catalog": catalog,
            "standardized_version": standardized,
            "runs_version": runs_version,
            "start": start.isoformat() if start else None,
            "status": "loading",
            "done": [],
            "plan": [],
        }
    else:
        start = datetime.fromisoformat(state["start"]) if state.get("start") else None
    silver, runs = source_names(catalog, state["standardized_version"], state["runs_version"])
    parameters = {} if start is None else {"watermark": start.isoformat()}
    if not state["plan"]:
        pairs = dbx.query(f"SELECT DISTINCT board,market FROM {silver}")
        for board, market in pairs:
            projection = aggregate_sql(silver, runs, 1, 0, start)
            size = dbx.query(
                f"SELECT coalesce(sum(octet_length(value)),0) FROM ({projection}) q(value)",
                parameters={**parameters, "board": board, "market": market},
            )[0][0]
            state["plan"].append([board, market, max(1, math.ceil(int(size or 0) / READ_BYTES))])
    if checkpoint:
        write_checkpoint(checkpoint, state)
    inserted = []
    count = 0
    for board, market, buckets in state["plan"]:
        for bucket in range(buckets):
            batch_key = f"{board}:{market}:{buckets}:{bucket}"
            if batch_key in state["done"]:
                continue
            params = {**parameters, "board": board, "market": market}
            for records in read_bucket(
                dbx, lambda n, k: aggregate_sql(silver, runs, n, k, start), params, buckets, bucket
            ):
                wordings = prepare_wordings(records)
                # All of one listing remains in a leaf bucket, so merges stay complete.
                changes = upsert_listings(conn, wordings, PROCESSING_START, HISTORY_REASON)
                inserted.extend(
                    row["id"]
                    for row in changes
                    if row["inserted"] and row["wording_first_seen_at"] >= PROCESSING_START
                )
                count += len(wordings)
                log.info(
                    "pull batch finished board=%s market=%s bucket=%d/%d rows=%d",
                    board,
                    market,
                    bucket + 1,
                    buckets,
                    len(wordings),
                )
                if on_batch:
                    on_batch(changes)
            state["done"].append(batch_key)
            if checkpoint:
                write_checkpoint(checkpoint, state)
    refresh_closure(dbx, conn, silver, runs)
    proof = {}
    grouped = refresh_canonical_groups(dbx, catalog, conn, silver, proof=proof)
    if not grouped:
        raise StaleMapping("Canonical grouping is unavailable")
    state.update(proof, grouping_available=True)
    state["status"] = "complete"
    if checkpoint:
        write_checkpoint(checkpoint, state)
    if not history and any(state.get(key) != value for key, value in source_versions(dbx, catalog).items()):
        newer = pull_job_listings(dbx, catalog, conn, checkpoint=checkpoint, on_batch=on_batch)
        inserted.extend(newer["inserted_ids"])
        count += newer["rows"]
    return {
        "inserted_ids": inserted,
        "rows": count,
        "grouping_available": grouped,
        "history_complete": True,
    }


def refresh_closure(dbx, conn, silver, runs):
    # Only completed configured sweeps count; search-only boards never infer closure by absence.
    query = f"""WITH latest AS (SELECT s.board,s.market,s.board_job_id,max(r.started_at) last_seen,
        max_by(s.board_status,struct(r.started_at,s.run_id)) status
        FROM {silver} s JOIN {runs} r ON r.run_id=s.run_id GROUP BY s.board,s.market,s.board_job_id),
        absent AS (SELECT l.board,l.market,l.board_job_id,count(r.run_id) sweeps FROM latest l
        LEFT JOIN {runs} r ON r.board=l.board AND r.market=l.market AND r.started_at>l.last_seen
        AND r.finished_at IS NOT NULL AND r.full_sweep=TRUE AND r.status IN ('ok','partial')
        GROUP BY l.board,l.market,l.board_job_id)
        SELECT to_json(named_struct('board',l.board,'market',l.market,'board_job_id',l.board_job_id,
            'closed_on_board',CASE WHEN l.board NOT IN ('mcf','jobstreet','jobsdb','seek') THEN NULL
                WHEN a.sweeps>=2 OR lower(l.status) IN ('closed','expired') THEN TRUE
                WHEN lower(l.status) IN ('open','re-open','active') THEN FALSE ELSE NULL END))
        FROM latest l JOIN absent a USING(board,market,board_job_id)"""
    from bto.storage.postgres.job_sorting import copy_rows

    with conn.transaction():
        conn.execute(
            "CREATE TEMP TABLE closure(board text,market text,board_job_id text,closed_on_board boolean) ON COMMIT DROP"
        )
        # Narrow identity/status JSON fits bounded buckets even on the initial history.
        for records in narrow_buckets(dbx, query):
            copy_rows(
                conn, "closure", ["board", "market", "board_job_id", "closed_on_board"], records
            )
        conn.execute(
            "UPDATE job_sorting.job_listings j SET closed_on_board=c.closed_on_board FROM closure c WHERE (j.board,j.market,j.board_job_id)=(c.board,c.market,c.board_job_id) AND j.closed_on_board IS DISTINCT FROM c.closed_on_board"
        )


def narrow_buckets(dbx, query):
    # The subquery exposes JSON; hashing it makes an adaptive, bounded download without scratch writes.
    size = int(
        dbx.query(f"SELECT coalesce(sum(octet_length(value)),0) FROM ({query}) q(value)")[0][0] or 0
    )
    buckets = max(1, math.ceil(size / READ_BYTES))
    for bucket in range(buckets):
        statement = lambda n, k: (
            f"SELECT value FROM ({query}) q(value) WHERE pmod(hash(value),{n})={k}"
        )
        yield from read_bucket(dbx, statement, {}, buckets, bucket)


def refresh_canonical_groups(dbx, catalog, conn, silver, *, proof=None):
    from bto.storage.postgres.job_sorting import copy_rows

    try:
        standardized_version = require_current_mapping(dbx, catalog)
        # Current fingerprints must come from the publication the freshness proof names.
        silver = f"{silver_table_name(catalog, 'standardized_job_listings')} VERSION AS OF {standardized_version}"
        mapping = silver_table_name(catalog, "job_canonical_mapping")
        mapping_version = latest_version(dbx, mapping)
        runs_table = table_name(catalog, "scrape_runs")
        runs_version = latest_version(dbx, runs_table)
        runs = f"{runs_table} VERSION AS OF {runs_version}"
        query = f"""WITH current_fp AS (SELECT s.board,s.market,s.board_job_id,
            max_by(s.fingerprint,struct(r.started_at,s.run_id)) fingerprint FROM {silver} s
            JOIN {runs} r ON r.run_id=s.run_id WHERE s.fingerprint IS NOT NULL GROUP BY s.board,s.market,s.board_job_id)
            SELECT to_json(named_struct('board',m.board,'market',m.market,'board_job_id',m.board_job_id,
                'fingerprint',m.fingerprint,'canonical_job_id',m.canonical_job_id,
                'current_canonical_job_id',coalesce(c.canonical_job_id,CASE WHEN m.fingerprint IS NULL THEN m.canonical_job_id END)))
            FROM {mapping} VERSION AS OF {mapping_version} m LEFT JOIN current_fp f USING(board,market,board_job_id)
            LEFT JOIN {mapping} VERSION AS OF {mapping_version} c ON c.board=m.board AND c.market=m.market
                AND c.board_job_id=m.board_job_id AND c.fingerprint=f.fingerprint"""
        with conn.transaction():
            conn.execute(
                "CREATE TEMP TABLE grouping(board text,market text,board_job_id text,fingerprint text,canonical_job_id text,current_canonical_job_id text) ON COMMIT DROP"
            )
            for records in narrow_buckets(dbx, query):
                copy_rows(
                    conn,
                    "grouping",
                    [
                        "board",
                        "market",
                        "board_job_id",
                        "fingerprint",
                        "canonical_job_id",
                        "current_canonical_job_id",
                    ],
                    records,
                )
            if (
                require_current_mapping(dbx, catalog) != standardized_version
                or latest_version(dbx, mapping) != mapping_version
                or latest_version(dbx, runs_table) != runs_version
            ):
                raise StaleMapping("Grouping publication changed during download")
            conn.execute(
                "CREATE TEMP TABLE assignments ON COMMIT DROP AS SELECT DISTINCT board,market,board_job_id,NULL::text fingerprint,current_canonical_job_id canonical_job_id FROM grouping WHERE current_canonical_job_id IS NOT NULL UNION ALL SELECT board,market,board_job_id,fingerprint,canonical_job_id FROM grouping WHERE fingerprint IS NOT NULL"
            )
            missing = conn.execute(
                "SELECT 1 FROM job_sorting.job_listings j LEFT JOIN assignments a ON (j.board,j.market,j.board_job_id)=(a.board,a.market,a.board_job_id) AND j.fingerprint IS NOT DISTINCT FROM a.fingerprint WHERE a.canonical_job_id IS NULL LIMIT 1"
            ).fetchone()
            if missing:
                raise StaleMapping("Grouping does not cover every stored wording")
            conn.execute(
                "UPDATE job_sorting.job_listings j SET canonical_job_id=a.canonical_job_id FROM assignments a WHERE (j.board,j.market,j.board_job_id)=(a.board,a.market,a.board_job_id) AND j.fingerprint IS NOT DISTINCT FROM a.fingerprint AND j.canonical_job_id IS DISTINCT FROM a.canonical_job_id"
            )
        if proof is not None:
            proof.update(mapping_version=mapping_version)
        log.info("canonical grouping refreshed from one proven-current publication")
        return True
    except StaleMapping:
        log.warning("canonical grouping unavailable; previous assignments preserved")
        return False
