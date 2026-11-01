"""Write and update BTO listings, model answers, sorting results and costs.

Related writes stay in one transaction. The shared input hash preserves the
exact encoding used for stored wording and reusable-answer identities.
"""

import hashlib
import json
import logging

from psycopg import sql
from psycopg.types.json import Jsonb

from bto.log_errors import log_failure

from . import read, setup
from .connection import connect
from .read import read_rows
from .setup import COLUMN_CONTRACT

log = logging.getLogger(__name__)
_LISTING_COLUMNS = [column for column, _, _ in COLUMN_CONTRACT["job_listings"]]
PULL_COLUMNS = _LISTING_COLUMNS[_LISTING_COLUMNS.index("board"):_LISTING_COLUMNS.index("triage_input_hash")]
RESULT_COLUMNS = _LISTING_COLUMNS[_LISTING_COLUMNS.index("bucket"):_LISTING_COLUMNS.index("repeat_of_id")]


def record_indeed_cost(record):
    """Store one Indeed Actor run's reported cost, updating its existing row."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.apify_indeed
                (apify_run_id, bto_run_id, market, search_term, started_at,
                 finished_at, status, result_count, cost_usd, recorded_at)
            VALUES (%(apify_run_id)s, %(bto_run_id)s, %(market)s,
                    %(search_term)s, %(started_at)s, %(finished_at)s,
                    %(status)s, %(result_count)s, %(cost_usd)s, now())
            ON CONFLICT (apify_run_id) DO UPDATE SET
                finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status,
                result_count = EXCLUDED.result_count,
                cost_usd = EXCLUDED.cost_usd,
                recorded_at = now()
            """,
            record,
        )


def record_linkedin_cost(record):
    """Store one LinkedIn Actor run's cost across its configured search terms."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.apify_linkedin
                (apify_run_id, bto_run_id, market, started_at, finished_at,
                 status, result_count, cost_usd, recorded_at)
            VALUES (%(apify_run_id)s, %(bto_run_id)s, %(market)s,
                    %(started_at)s, %(finished_at)s, %(status)s,
                    %(result_count)s, %(cost_usd)s, now())
            ON CONFLICT (apify_run_id) DO UPDATE SET
                finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status,
                result_count = EXCLUDED.result_count,
                cost_usd = EXCLUDED.cost_usd,
                recorded_at = now()
            """,
            record,
        )


def hash_input(parts):
    """Hash the exact JSON encoding shared by wording and answer identities."""
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def copy_rows(conn, table, columns, rows):
    """Bulk-load rows, adapting board facts to PostgreSQL JSON."""
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns))
    )
    with conn.cursor().copy(statement) as copy:
        for row in rows:
            copy.write_row(
                tuple(Jsonb(row.get(c)) if c == "board_facts" else row.get(c) for c in columns)
            )


def upsert_listings(conn, rows, processing_start, history_reason):
    """Merge observed wordings and timestamps without replacing processed history."""
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
    """Write changed sorting decisions in one transaction."""
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
        log_failure(log, "Gemini cost write failed step=%s", stage, error=error)
        return False


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


def change_review_state(conn, identifier, field, active, *, return_states=False):
    """Change one group's retained state without duplicating it across every copy.

    Clearing a field clears it on all retained anchors in the merged group."""
    if field not in read.REVIEW_TIMESTAMPS or not isinstance(active, bool):
        raise ValueError("Unknown Review state change")
    with conn.transaction():
        # One owner's writes are tiny; serialize them so two tabs cannot choose
        # different first anchors for the same previously untouched group.
        conn.execute("SELECT pg_advisory_xact_lock(72403105)")
        selected = read.current_review_listing(conn, identifier)
        key = read.review_group_key(selected)
        states = read.review_states(conn)
        state = states.get(key)
        anchors = state["anchors"] if state else []
        if active:
            if state and state[field] is not None:
                return (state, states) if return_states else state
            anchor = anchors[0] if anchors else selected
            conn.execute(
                sql.SQL("""INSERT INTO job_tracking.review_status
                (board,market,board_job_id,{field},updated_at) VALUES (%s,%s,%s,now(),now())
                ON CONFLICT (board,market,board_job_id) DO UPDATE
                SET {field}=now(),updated_at=now() WHERE review_status.{field} IS NULL""").format(
                    field=sql.Identifier(field)
                ),
                (anchor["board"], anchor["market"], anchor["board_job_id"]),
            )
        else:
            for anchor in anchors:
                if anchor[field] is not None:
                    conn.execute(
                        sql.SQL("""UPDATE job_tracking.review_status
                        SET {field}=NULL,updated_at=now()
                        WHERE (board,market,board_job_id)=(%s,%s,%s) AND {field} IS NOT NULL""").format(
                            field=sql.Identifier(field)
                        ),
                        (anchor["board"], anchor["market"], anchor["board_job_id"]),
                    )
        states = read.review_states(conn)
        state = states.get(key, dict.fromkeys(read.REVIEW_TIMESTAMPS))
        return (state, states) if return_states else state


# ── job-listing research and contact finding ────────────────────────────────
# Research uses the retained (board, market, board_job_id) Review anchor.
# Cost writers use separate connections; callers log failures and continue.

# Columns a people search fills in; the listing key is added here and the
# database numbers each person's id.
PERSON_COLUMNS = [
    column
    for column, _, _ in setup.JOB_RESEARCH_COLUMNS["job_tracking.relevant_personnel"]
    if column not in {"id", "board", "market", "board_job_id"}
]
POST_COLUMNS = [
    column
    for column, _, _ in setup.JOB_RESEARCH_COLUMNS["job_tracking.linkedin_posts"]
    if column not in {"board", "market", "board_job_id"}
]
JSON_ARRAY_COLUMNS = {"channels", "reacted_by", "commented_by"}
FINISHED_RESEARCH_STATUSES = ("resolved", "partial", "unresolved", "failed")
FINISHED_PEOPLE_SEARCH_STATUSES = ("done", "failed", "recruitment_firm")
INTERRUPTED_SEARCH_REASON = "cut off by a restart"
BRAVE_COST_TABLES = (
    "brave_find_direct_link",
    "brave_find_employer",
    "brave_find_people",
    "brave_look_up_person",
)
# Serialize people-search requests so concurrent saves cannot queue duplicates.
PEOPLE_SEARCH_REQUEST_LOCK = 72403106


def _prepare_column_value(column, value):
    """Store absent JSON arrays as empty arrays; leave other values unchanged."""
    if column in JSON_ARRAY_COLUMNS:
        return Jsonb(value if value is not None else [])
    return value


def request_direct_application_link(conn, key):
    """Queue job-listing research for one listing; returns the row's status.

    A queued or searching row is kept as it is, so asking again keeps the
    listing's place in the queue. Any other row is queued again; the previous
    search's links stay until the new search finishes.
    """
    board, market, board_job_id = key
    queued = conn.execute(
        """
        INSERT INTO job_tracking.direct_application_links AS d
            (board, market, board_job_id, status, requested_at)
        VALUES (%s, %s, %s, 'queued', now())
        ON CONFLICT (board, market, board_job_id) DO UPDATE SET
            status = 'queued',
            requested_at = now(),
            started_at = NULL,
            finished_at = NULL,
            failure_reason = NULL
        WHERE d.status NOT IN ('queued', 'searching')
        RETURNING status
        """,
        (board, market, board_job_id),
    ).fetchone()
    if queued:
        return queued[0]
    return conn.execute(
        """
        SELECT status FROM job_tracking.direct_application_links
        WHERE (board, market, board_job_id) = (%s, %s, %s)
        """,
        (board, market, board_job_id),
    ).fetchone()[0]


def start_direct_application_link(conn, key):
    """Mark one listing's queued research as searching.

    Returns False when the row was not queued. Only a queued row moves, so a
    search that another worker has already started is never started twice.
    """
    board, market, board_job_id = key
    started = conn.execute(
        """
        UPDATE job_tracking.direct_application_links
        SET status = 'searching', started_at = now()
        WHERE (board, market, board_job_id) = (%s, %s, %s) AND status = 'queued'
        """,
        (board, market, board_job_id),
    )
    return started.rowcount == 1


def finish_direct_application_link(
    conn, key, *, status, company_job_board_url, vacancy_url, failure_reason=None
):
    """Record how one listing's research ended; its links replace the previous ones."""
    if status not in FINISHED_RESEARCH_STATUSES:
        raise ValueError(f"Unknown result for job-listing research: {status}")
    if failure_reason is not None and status != "failed":
        raise ValueError("Only a failed search has a failure reason")
    board, market, board_job_id = key
    finished = conn.execute(
        """
        UPDATE job_tracking.direct_application_links SET
            status = %s,
            company_job_board_url = %s,
            vacancy_url = %s,
            finished_at = now(),
            failure_reason = %s
        WHERE (board, market, board_job_id) = (%s, %s, %s)
        """,
        (status, company_job_board_url, vacancy_url, failure_reason, board, market, board_job_id),
    )
    if finished.rowcount != 1:
        raise ValueError(f"No job-listing research row for {board} {market} {board_job_id}")


def request_personnel_search(conn, key, bto_run_id):
    """Queue one people search unless this listing already has unfinished work."""
    board, market, board_job_id = key
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (PEOPLE_SEARCH_REQUEST_LOCK,))
        unfinished = conn.execute(
            """
            SELECT status FROM job_tracking.personnel_searches
            WHERE (board, market, board_job_id) = (%s, %s, %s)
            AND status IN ('queued', 'searching')
            """,
            (board, market, board_job_id),
        ).fetchone()
        if unfinished:
            return unfinished[0]
        conn.execute(
            """
            INSERT INTO job_tracking.personnel_searches
                (bto_run_id, board, market, board_job_id, status, requested_at)
            VALUES (%s, %s, %s, %s, 'queued', now())
            """,
            (bto_run_id, board, market, board_job_id),
        )
    return "queued"


def start_personnel_search(conn, bto_run_id):
    """Claim a queued search; return False if another worker already claimed it."""
    started = conn.execute(
        """
        UPDATE job_tracking.personnel_searches
        SET status = 'searching', started_at = now()
        WHERE bto_run_id = %s AND status = 'queued'
        """,
        (bto_run_id,),
    )
    return started.rowcount == 1


def set_personnel_search_email(
    conn,
    bto_run_id,
    *,
    email_status,
    email_domain=None,
    email_domain_source=None,
    email_domains_tried=None,
):
    """Record whether the search's email checks could run, and on which domain."""
    updated = conn.execute(
        """
        UPDATE job_tracking.personnel_searches SET
            email_status = %s,
            email_domain = %s,
            email_domain_source = %s,
            email_domains_tried = %s
        WHERE bto_run_id = %s
        """,
        (email_status, email_domain, email_domain_source, email_domains_tried, bto_run_id),
    )
    if updated.rowcount != 1:
        raise ValueError(f"No people search {bto_run_id}")


def finish_personnel_search(conn, bto_run_id, *, status, failure_reason=None):
    """Record how one people search ended."""
    if status not in FINISHED_PEOPLE_SEARCH_STATUSES:
        raise ValueError(f"Unknown result for a people search: {status}")
    if failure_reason is not None and status != "failed":
        raise ValueError("Only a failed search has a failure reason")
    finished = conn.execute(
        """
        UPDATE job_tracking.personnel_searches
        SET status = %s, finished_at = now(), failure_reason = %s
        WHERE bto_run_id = %s
        """,
        (status, failure_reason, bto_run_id),
    )
    if finished.rowcount != 1:
        raise ValueError(f"No people search {bto_run_id}")


def fail_interrupted_searches(conn, table):
    """Fail abandoned searches after taking the table's exclusive worker lock.

    Call once on worker startup; the other queue may still have an active worker."""
    if table not in read.SEARCH_TABLES:
        raise ValueError(f"Unknown search table: {table}")
    failed = conn.execute(
        sql.SQL(
            """
            UPDATE job_tracking.{} SET
                status = 'failed',
                finished_at = now(),
                failure_reason = %s
            WHERE status = 'searching'
            """
        ).format(sql.Identifier(table)),
        (INTERRUPTED_SEARCH_REASON,),
    ).rowcount
    if failed:
        log.warning(
            "postgres searches cut off by a restart set to failed table=%s count=%d",
            table,
            failed,
        )
    return failed


def replace_relevant_personnel(conn, key, people):
    """Replace one listing's people with those a finished search found.

    The listing's LinkedIn posts point at its people, so they are deleted too;
    store the new posts afterwards with replace_linkedin_posts. People are
    inserted in the order given and their new ids are returned in that order.
    Each person's keys mirror the columns; other keys are ignored.
    """
    board, market, board_job_id = key
    listing = (board, market, board_job_id)
    columns = ["board", "market", "board_job_id", *PERSON_COLUMNS]
    insert = sql.SQL(
        "INSERT INTO job_tracking.relevant_personnel ({}) VALUES ({}) RETURNING id"
    ).format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    person_ids = []
    with conn.transaction():
        conn.execute(
            """
            DELETE FROM job_tracking.linkedin_posts
            WHERE (board, market, board_job_id) = (%s, %s, %s)
            """,
            listing,
        )
        conn.execute(
            """
            DELETE FROM job_tracking.relevant_personnel
            WHERE (board, market, board_job_id) = (%s, %s, %s)
            """,
            listing,
        )
        for person in people:
            values = [*listing, *(_prepare_column_value(c, person.get(c)) for c in PERSON_COLUMNS)]
            person_ids.append(conn.execute(insert, values).fetchone()[0])
    log.info(
        "postgres relevant_personnel board=%s market=%s board_job_id=%s people=%d",
        board,
        market,
        board_job_id,
        len(person_ids),
    )
    return person_ids


def update_relevant_person(conn, person_id, **columns):
    """Fill in one person's row in place, as a full lookup does."""
    unknown = sorted(set(columns) - set(PERSON_COLUMNS))
    if unknown:
        raise ValueError(f"Not relevant_personnel columns: {', '.join(unknown)}")
    if not columns:
        return
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns
    )
    updated = conn.execute(
        sql.SQL("UPDATE job_tracking.relevant_personnel SET {} WHERE id = %s").format(assignments),
        [*(_prepare_column_value(column, value) for column, value in columns.items()), person_id],
    )
    if updated.rowcount != 1:
        raise ValueError(f"No relevant person with id {person_id}")


def replace_linkedin_posts(conn, key, posts):
    """Replace one listing's LinkedIn posts with those a finished search found.

    Each post's `author_id` is the poster's relevant_personnel id, so store the
    people first. Each post's keys mirror the columns; other keys are ignored.
    """
    board, market, board_job_id = key
    listing = (board, market, board_job_id)
    columns = ["board", "market", "board_job_id", *POST_COLUMNS]
    insert = sql.SQL("INSERT INTO job_tracking.linkedin_posts ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    with conn.transaction():
        conn.execute(
            """
            DELETE FROM job_tracking.linkedin_posts
            WHERE (board, market, board_job_id) = (%s, %s, %s)
            """,
            listing,
        )
        for post in posts:
            values = [*listing, *(_prepare_column_value(c, post.get(c)) for c in POST_COLUMNS)]
            conn.execute(insert, values)


def record_apify_find_job_contacts_run(record):
    """UPSERT one costs.apify_find_job_contacts row when its Actor run starts.

    `record` holds apify_run_id, bto_run_id, apify_actor_id and started_at.
    cost_usd stays NULL until record_apify_find_job_contacts_cost fills it in,
    and writing the start again never clears a cost already read.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.apify_find_job_contacts
                (apify_run_id, bto_run_id, apify_actor_id, started_at,
                 cost_usd, recorded_at)
            VALUES (%(apify_run_id)s, %(bto_run_id)s, %(apify_actor_id)s,
                    %(started_at)s, NULL, now())
            ON CONFLICT (apify_run_id) DO UPDATE SET
                bto_run_id = EXCLUDED.bto_run_id,
                apify_actor_id = EXCLUDED.apify_actor_id,
                started_at = EXCLUDED.started_at,
                recorded_at = now()
            """,
            record,
        )


def record_apify_find_job_contacts_cost(apify_run_id, cost_usd):
    """Fill in a finished Actor run's cost: Apify's reported usageTotalUsd.

    Raises when the run's start was never recorded, so the caller can log the
    lost cost row and carry on.
    """
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE costs.apify_find_job_contacts
            SET cost_usd = %s, recorded_at = now()
            WHERE apify_run_id = %s
            """,
            (cost_usd, apify_run_id),
        )
        if updated.rowcount != 1:
            raise ValueError(f"No costs.apify_find_job_contacts row for Apify run {apify_run_id}")


def record_hunter_find_job_contacts_credits(record):
    """Record measured Hunter credits, or None when the charge is unknown."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO costs.hunter_find_job_contacts (bto_run_id, endpoint, credits)
            VALUES (%(bto_run_id)s, %(endpoint)s, %(credits)s)
            """,
            record,
        )


def record_brave_cost(table, cost_usd):
    """Record one successful Brave request in the requesting key's cost table."""
    if table not in BRAVE_COST_TABLES:
        raise ValueError(f"Unknown Brave cost table: {table}")
    with connect() as conn:
        conn.execute(
            sql.SQL("INSERT INTO costs.{} (cost_usd) VALUES (%s)").format(sql.Identifier(table)),
            (cost_usd,),
        )
