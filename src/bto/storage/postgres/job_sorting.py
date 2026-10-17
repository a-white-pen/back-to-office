"""Sorting-store reads and transactional writes; no processing or schedule policy."""

import hashlib
import json
import logging
from contextlib import contextmanager
from uuid import uuid4

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .setup_job_sorting import COLUMN_CONTRACT

log = logging.getLogger(__name__)
PULL_COLUMNS = [r[0] for r in COLUMN_CONTRACT["job_listings"]][1:26]
RESULT_COLUMNS = [r[0] for r in COLUMN_CONTRACT["job_listings"]][28:36]
OWNER_LOCK = 72403104


class SortingBusy(RuntimeError):
    """The sorting advisory lock is owned by another runner."""


JOIN_RESULTS = """
 SELECT j.*, to_jsonb(t) AS triage, to_jsonb(e) AS extraction
 FROM job_sorting.job_listings j
 LEFT JOIN job_sorting.triage_results t ON t.input_hash=j.triage_input_hash
 LEFT JOIN job_sorting.extraction_results e ON e.input_hash=j.extraction_input_hash
"""


def hash_input(parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_rows(conn, statement, parameters=()):
    with conn.cursor(row_factory=dict_row) as cursor:
        # Changing range predicates need a plan for their actual values.
        cursor.execute(statement, parameters, prepare=False)
        return cursor.fetchall()


def stream_rows(conn, statement, parameters=(), batch_size=500):
    with (
        conn.transaction(),
        conn.cursor(name="sorting_" + uuid4().hex, row_factory=dict_row) as cursor,
    ):
        # Consumers exhaust these streams; plan for the complete result.
        conn.execute("SET LOCAL cursor_tuple_fraction=1.0")
        cursor.itersize = batch_size
        cursor.execute(statement, parameters)
        yield from cursor


@contextmanager
def sorting_owner(conn):
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (OWNER_LOCK,)).fetchone()[0]:
        raise SortingBusy("Another job-sorting owner is active")
    try:
        yield conn
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (OWNER_LOCK,))


def watermark(conn):
    return conn.execute(
        "SELECT max(wording_last_seen_at) FROM job_sorting.job_listings"
    ).fetchone()[0]


def copy_rows(conn, table, columns, rows):
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns))
    )
    with conn.cursor().copy(statement) as copy:
        for row in rows:
            copy.write_row(
                tuple(Jsonb(row.get(c)) if c == "board_facts" else row.get(c) for c in columns)
            )


def upsert_listings(conn, rows, processing_start, history_reason):
    if not rows:
        return []
    columns = sql.SQL(",").join(map(sql.Identifier, PULL_COLUMNS))
    key = "(board,market,board_job_id,wording_hash)"
    with conn.transaction():
        conn.execute(
            sql.SQL(
                "CREATE TEMP TABLE pulled ON COMMIT DROP AS SELECT {} FROM job_sorting.job_listings WITH NO DATA"
            ).format(columns)
        )
        copy_rows(conn, "pulled", PULL_COLUMNS, rows)
        crossed = conn.execute(
            f"""SELECT 1 FROM job_sorting.job_listings t JOIN pulled p
            USING {key} WHERE t.wording_first_seen_at >= %s AND p.wording_first_seen_at < %s
            AND (t.bucket IS NOT NULL OR t.triage_input_hash IS NOT NULL OR t.extraction_input_hash IS NOT NULL)
            LIMIT 1""",
            (processing_start, processing_start),
        ).fetchone()
        if crossed:
            raise ValueError("An already processed wording crossed the historical boundary")
        updates = []
        for name in PULL_COLUMNS:
            column = sql.Identifier(name)
            if name in {
                "board",
                "market",
                "board_job_id",
                "wording_hash",
                "canonical_job_id",
                "closed_on_board",
            }:
                continue
            if name.endswith("first_seen_at"):
                expression = sql.SQL("LEAST(t.{c},EXCLUDED.{c})").format(c=column)
            elif name == "wording_last_seen_at":
                expression = sql.SQL("GREATEST(t.{c},EXCLUDED.{c})").format(c=column)
            else:
                incoming = (
                    sql.SQL("COALESCE(EXCLUDED.{c},t.{c})").format(c=column)
                    if name == "fingerprint"
                    else sql.SQL("EXCLUDED.{}").format(column)
                )
                expression = sql.SQL(
                    "CASE WHEN EXCLUDED.wording_last_seen_at >= t.wording_last_seen_at THEN {} ELSE t.{} END"
                ).format(incoming, column)
            updates.append(sql.SQL("{}={}").format(column, expression))
        # A not-yet-processed late observation can safely turn an incomplete row into history.
        updates.extend(
            [
                sql.SQL(
                    "bucket=CASE WHEN LEAST(t.wording_first_seen_at,EXCLUDED.wording_first_seen_at)<%s THEN 'not_sorted' ELSE t.bucket END"
                ),
                sql.SQL(
                    "reason=CASE WHEN LEAST(t.wording_first_seen_at,EXCLUDED.wording_first_seen_at)<%s THEN %s ELSE t.reason END"
                ),
            ]
        )
        statement = sql.SQL("""INSERT INTO job_sorting.job_listings AS t ({cols},bucket,reason)
            SELECT {cols},CASE WHEN wording_first_seen_at<%s THEN 'not_sorted' END,
                   CASE WHEN wording_first_seen_at<%s THEN %s END FROM pulled
            ON CONFLICT (board,market,board_job_id,wording_hash) DO UPDATE SET {updates}
            RETURNING id,board,market,board_job_id,wording_first_seen_at,(xmax=0) AS inserted""").format(
            cols=columns, updates=sql.SQL(",").join(updates)
        )
        changed = read_rows(
            conn,
            statement,
            (
                processing_start,
                processing_start,
                history_reason,
                processing_start,
                processing_start,
                history_reason,
            ),
        )
        conn.execute("""UPDATE job_sorting.job_listings t SET listing_first_seen_at=m.first_seen
            FROM (SELECT p.board,p.market,p.board_job_id,min(j.listing_first_seen_at) first_seen
                  FROM (SELECT DISTINCT board,market,board_job_id FROM pulled) p
                  JOIN job_sorting.job_listings j USING(board,market,board_job_id)
                  GROUP BY p.board,p.market,p.board_job_id) m
            WHERE t.board=m.board AND t.market=m.market AND t.board_job_id=m.board_job_id
            AND t.listing_first_seen_at IS DISTINCT FROM m.first_seen""")
    log.info(
        "postgres pull rows=%d new_wordings=%d", len(rows), sum(r["inserted"] for r in changed)
    )
    return changed


def update_results(conn, rows):
    if not rows:
        return
    columns = ["id", *RESULT_COLUMNS]
    with conn.transaction():
        conn.execute(
            sql.SQL(
                "CREATE TEMP TABLE decisions ON COMMIT DROP AS SELECT {} FROM job_sorting.job_listings WITH NO DATA"
            ).format(sql.SQL(",").join(map(sql.Identifier, columns)))
        )
        copy_rows(conn, "decisions", columns, rows)
        assignment = sql.SQL(",").join(
            sql.SQL("{c}=d.{c}").format(c=sql.Identifier(c)) for c in RESULT_COLUMNS
        )
        updated = conn.execute(
            sql.SQL(
                "UPDATE job_sorting.job_listings j SET {} FROM decisions d WHERE j.id=d.id "
                "AND ({}) IS DISTINCT FROM ({})"
            ).format(
                assignment,
                sql.SQL(",").join(sql.Identifier("j", c) for c in RESULT_COLUMNS),
                sql.SQL(",").join(sql.Identifier("d", c) for c in RESULT_COLUMNS),
            )
        )
    log.info("postgres decisions rows=%d changed=%d", len(rows), updated.rowcount)


def store_answer(conn, stage, input_hash, answer, listing_ids):
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown result stage")
    table = f"{stage}_results"
    columns = [r[0] for r in COLUMN_CONTRACT[table]]
    values = [input_hash, *[Jsonb(answer[c]) if c == "lines" else answer[c] for c in columns[1:]]]
    statement = sql.SQL(
        "INSERT INTO job_sorting.{} ({}) VALUES ({}) ON CONFLICT(input_hash) DO NOTHING"
    ).format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    listing_ids = list(listing_ids)
    if not listing_ids:
        # One statement is already atomic; there are no links to commit with it.
        conn.execute(statement, values)
        return
    with conn.transaction():
        conn.execute(statement, values)
        conn.execute(
            sql.SQL("UPDATE job_sorting.job_listings SET {}=%s WHERE id=ANY(%s)").format(
                sql.Identifier(f"{stage}_input_hash")
            ),
            (input_hash, listing_ids),
        )


def find_answer(conn, stage, input_hash):
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown result stage")
    rows = read_rows(
        conn,
        sql.SQL("SELECT * FROM job_sorting.{} WHERE input_hash=%s").format(
            sql.Identifier(f"{stage}_results")
        ),
        (input_hash,),
    )
    return rows[0] if rows else None


def record_gemini_cost(conn, stage, record):
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown cost stage")
    columns = [r[0] for r in COLUMN_CONTRACT[f"gemini_{stage}"]][:-1]
    try:
        with conn.transaction():
            conn.execute(
                sql.SQL(
                    "INSERT INTO costs.{} ({}) VALUES ({}) ON CONFLICT(gemini_response_id) DO NOTHING"
                ).format(
                    sql.Identifier(f"gemini_{stage}"),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                ),
                [record[c] for c in columns],
            )
        return True
    except Exception as error:
        log.error("Gemini cost write failed step=%s error_type=%s", stage, type(error).__name__)
        return False


def cost_totals(conn, since):
    return read_rows(
        conn,
        """SELECT stage,service_tier,sum(input_tokens) input_tokens,
        sum(cached_tokens) cached_tokens,sum(output_tokens) output_tokens,sum(cost_usd) cost_usd
        FROM (SELECT 'triage' stage,* FROM costs.gemini_triage UNION ALL
              SELECT 'extraction' stage,* FROM costs.gemini_extraction) calls
        WHERE recorded_at >= %s GROUP BY stage,service_tier""",
        (since,),
    )


def update_repeats(conn, changes):
    if not changes:
        return
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE repeats(id bigint,repeat_of_id bigint) ON COMMIT DROP")
        copy_rows(conn, "repeats", ["id", "repeat_of_id"], changes)
        conn.execute(
            "UPDATE job_sorting.job_listings j SET repeat_of_id=r.repeat_of_id FROM repeats r WHERE j.id=r.id AND j.repeat_of_id IS DISTINCT FROM r.repeat_of_id"
        )
        conn.execute("DROP TABLE repeats")


def find_answers(conn, stage, hashes):
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown result stage")
    rows = read_rows(
        conn,
        sql.SQL("SELECT * FROM job_sorting.{} WHERE input_hash=ANY(%s)").format(
            sql.Identifier(f"{stage}_results")
        ),
        (list(set(hashes)),),
    )
    return {r["input_hash"]: r for r in rows}


def link_answers(conn, stage, pairs):
    if not pairs:
        return
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown result stage")
    if len(pairs) == 1:
        # Model answers arrive individually; avoid a COPY round trip for one link.
        conn.execute(
            sql.SQL("UPDATE job_sorting.job_listings SET {}=%s WHERE id=%s").format(
                sql.Identifier(f"{stage}_input_hash")
            ),
            (pairs[0]["input_hash"], pairs[0]["id"]),
        )
        return
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE links(id bigint,input_hash text) ON COMMIT DROP")
        copy_rows(conn, "links", ["id", "input_hash"], pairs)
        conn.execute(
            sql.SQL(
                "UPDATE job_sorting.job_listings j SET {}=l.input_hash FROM links l WHERE j.id=l.id"
            ).format(sql.Identifier(f"{stage}_input_hash"))
        )
