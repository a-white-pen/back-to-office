"""Read BTO job listings, saved model answers and costs from PostgreSQL.

Queries use the caller's connection. Large reads are streamed; write callers
can also fetch their RETURNING rows without leaving the write transaction.
"""

import json
from uuid import uuid4

from psycopg import sql
from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row

JOIN_RESULTS = """
 SELECT j.*, to_jsonb(t) AS triage, to_jsonb(e) AS extraction
 FROM job_sorting.job_listings j
 LEFT JOIN job_sorting.triage_results t ON t.input_hash=j.triage_input_hash
 LEFT JOIN job_sorting.extraction_results e ON e.input_hash=j.extraction_input_hash
"""


def read_rows(conn, statement, parameters=()):
    """Read rows as dictionaries without caching a value-dependent query plan."""
    with conn.cursor(row_factory=dict_row) as cursor:
        # Changing range predicates need a plan for their actual values.
        cursor.execute(statement, parameters, prepare=False)
        return cursor.fetchall()


def stream_rows(conn, statement, parameters=(), batch_size=500):
    """Stream a complete result inside the caller's transaction."""
    with (
        conn.transaction(),
        conn.cursor(name="sorting_" + uuid4().hex, row_factory=dict_row) as cursor,
    ):
        # Consumers exhaust these streams; plan for the complete result.
        conn.execute("SET LOCAL cursor_tuple_fraction=1.0")
        cursor.itersize = batch_size
        cursor.execute(statement, parameters)
        yield from cursor


def watermark(conn):
    return conn.execute(
        "SELECT max(wording_last_seen_at) FROM job_sorting.job_listings"
    ).fetchone()[0]


def find_answer(conn, stage, input_hash):
    """Read one saved answer using the same stage checks as a batch lookup."""
    return find_answers(conn, stage, [input_hash]).get(input_hash)


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


REVIEW_TIMESTAMPS = ("reviewed_at", "starred_at", "saved_to_apply_at")


def review_group_key(row):
    """Use current grouping, or the existing board-listing fallback when absent."""
    return row.get("canonical_job_id") or "listing:" + json.dumps(
        [row["board"], row["market"], row["board_job_id"]], ensure_ascii=False
    )


def review_anchors(conn):
    """Resolve only retained state anchors through their latest listing wording."""
    return read_rows(
        conn,
        """SELECT s.*,j.id current_listing_id,j.canonical_job_id
        FROM job_tracking.review_status s LEFT JOIN LATERAL (
            SELECT id,canonical_job_id FROM job_sorting.job_listings j
            WHERE (j.board,j.market,j.board_job_id)=(s.board,s.market,s.board_job_id)
            ORDER BY wording_last_seen_at DESC,wording_first_seen_at DESC,id DESC LIMIT 1
        ) j ON TRUE ORDER BY s.board,s.market,s.board_job_id""",
    )


def review_states(conn, *, allow_missing=False):
    """Combine retained read, star and save anchors under each current job group."""
    try:
        # A savepoint keeps the caller's transaction usable after this one
        # expected missing relation. Other missing tables/errors still fail.
        with conn.transaction():
            anchors = review_anchors(conn)
    except UndefinedTable as error:
        if not allow_missing or error.diag.message_primary != (
            'relation "job_tracking.review_status" does not exist'
        ):
            raise
        return None
    result = {}
    for row in anchors:
        key = review_group_key(row)
        state = result.setdefault(key, dict.fromkeys(REVIEW_TIMESTAMPS))
        state.setdefault("anchors", []).append(row)
        for field in REVIEW_TIMESTAMPS:
            if row[field] is not None and (state[field] is None or row[field] < state[field]):
                state[field] = row[field]
    groups = sorted({
        anchor["canonical_job_id"]
        for value in result.values()
        for anchor in value["anchors"]
        if anchor["canonical_job_id"] is not None
    })
    for value in result.values():
        value["members"] = {(a["board"], a["market"], a["board_job_id"]) for a in value["anchors"]}
    if groups:
        members = read_rows(
            conn,
            """SELECT j.board,j.market,j.board_job_id,j.canonical_job_id
            FROM job_sorting.job_listings j WHERE j.canonical_job_id=ANY(%s)
            AND NOT EXISTS(SELECT 1 FROM job_sorting.job_listings newer
                WHERE (newer.board,newer.market,newer.board_job_id)=(j.board,j.market,j.board_job_id)
                AND (newer.wording_last_seen_at,newer.wording_first_seen_at,newer.id)>
                    (j.wording_last_seen_at,j.wording_first_seen_at,j.id))""",
            (groups,),
        )
        for member in members:
            result[review_group_key(member)]["members"].add(
                (member["board"], member["market"], member["board_job_id"])
            )
    for value in result.values():
        value["members"] = sorted(value["members"])
    return result


def current_review_listing(conn, identifier):
    """Resolve a selected wording to its board listing's latest wording."""
    rows = read_rows(
        conn,
        """SELECT current.* FROM job_sorting.job_listings selected
        JOIN LATERAL (SELECT * FROM job_sorting.job_listings j
            WHERE (j.board,j.market,j.board_job_id)=(selected.board,selected.market,selected.board_job_id)
            ORDER BY wording_last_seen_at DESC,wording_first_seen_at DESC,id DESC LIMIT 1
        ) current ON TRUE WHERE selected.id=%s""",
        (int(identifier),),
    )
    if not rows:
        raise ValueError("Job listing not found")
    return rows[0]


def saved_review_candidates(conn, states):
    """Saved copies remain available outside Review's date and suppression filters."""
    groups, identifiers = [], []
    for state in states.values():
        if state["saved_to_apply_at"] is None:
            continue
        for anchor in state["anchors"]:
            if anchor["canonical_job_id"] is not None:
                groups.append(anchor["canonical_job_id"])
            elif anchor["current_listing_id"] is not None:
                identifiers.append(anchor["current_listing_id"])
    if not groups and not identifiers:
        return []
    return read_rows(
        conn,
        """SELECT j.id,j.board,j.market,j.board_job_id,j.canonical_job_id,j.title,
        j.advertiser_name,j.hiring_company_name,j.bucket,j.repeat_of_id,
        j.wording_first_seen_at,j.wording_last_seen_at,j.listing_first_seen_at,
        j.job_url,j.salary_raw,j.salary_min,j.salary_max,j.salary_currency,j.salary_period,
        j.is_recruitment_firm
        FROM job_sorting.job_listings j
        WHERE (canonical_job_id=ANY(%s) OR id=ANY(%s)) AND NOT EXISTS (
            SELECT 1 FROM job_sorting.job_listings newer
            WHERE (newer.board,newer.market,newer.board_job_id)=(j.board,j.market,j.board_job_id)
            AND (newer.wording_last_seen_at,newer.wording_first_seen_at,newer.id)>
                (j.wording_last_seen_at,j.wording_first_seen_at,j.id))""",
        (groups, identifiers),
    )


# ── job-listing research and contact finding ────────────────────────────────
SEARCH_TABLES = ("direct_application_links", "personnel_searches")

# Share the current group's retained anchors across research readers.
# Each reader joins this CTE and supplies _listing_key_parameters(key).
GROUP_ANCHORS = """
WITH listing_group AS (
    SELECT canonical_job_id FROM job_sorting.job_listings
    WHERE (board, market, board_job_id) = (%(board)s, %(market)s, %(board_job_id)s)
    ORDER BY wording_last_seen_at DESC, wording_first_seen_at DESC, id DESC LIMIT 1
), anchors AS (
    SELECT %(board)s::text AS board, %(market)s::text AS market,
           %(board_job_id)s::text AS board_job_id
    UNION
    SELECT s.board, s.market, s.board_job_id
    FROM listing_group g
    JOIN job_sorting.job_listings j ON j.canonical_job_id = g.canonical_job_id
    JOIN job_tracking.review_status s
        ON (s.board, s.market, s.board_job_id) = (j.board, j.market, j.board_job_id)
    WHERE NOT EXISTS (
        SELECT 1 FROM job_sorting.job_listings newer
        WHERE (newer.board, newer.market, newer.board_job_id)
            = (j.board, j.market, j.board_job_id)
        AND (newer.wording_last_seen_at, newer.wording_first_seen_at, newer.id)
            > (j.wording_last_seen_at, j.wording_first_seen_at, j.id))
)"""

# Display people and posts from the latest successful search in the group.
# Failed searches retain the previous results.
FOUND_BY_LATEST_SEARCH = (
    GROUP_ANCHORS
    + """, found_by AS (
    SELECT p.board, p.market, p.board_job_id, p.bto_run_id, p.finished_at
    FROM job_tracking.personnel_searches p JOIN anchors USING (board, market, board_job_id)
    WHERE p.status IN ('done', 'recruitment_firm')
    ORDER BY p.requested_at DESC LIMIT 1
)"""
)


def _listing_key_parameters(key):
    """A listing key as the named parameters GROUP_ANCHORS expects."""
    board, market, board_job_id = key
    return {"board": board, "market": market, "board_job_id": board_job_id}


def prioritised_listings(conn, *, starred_only=False):
    """Return one retained anchor per saved group, earliest save first.

    starred_only keeps groups eligible for contact finding."""
    listings = []
    for state in review_states(conn).values():
        if state["saved_to_apply_at"] is None:
            continue
        if starred_only and state["starred_at"] is None:
            continue
        anchor = state["anchors"][0]
        listings.append(
            {
                "board": anchor["board"],
                "market": anchor["market"],
                "board_job_id": anchor["board_job_id"],
                "saved_to_apply_at": state["saved_to_apply_at"],
                "starred_at": state["starred_at"],
            }
        )
    return sorted(listings, key=lambda listing: listing["saved_to_apply_at"])


def listing_for_research(conn, key):
    """Read the latest wording and its current copies in the same market.

    Other-market copies are excluded because they may describe another vacancy."""
    board, market, board_job_id = key
    rows = read_rows(
        conn,
        """SELECT id, board, market, board_job_id, title, description_text,
        advertiser_name, hiring_company_name, is_agency_posting, job_country_code,
        job_url, board_facts, is_recruitment_firm, is_public_sector, canonical_job_id
        FROM job_sorting.job_listings
        WHERE (board, market, board_job_id) = (%s, %s, %s)
        ORDER BY wording_last_seen_at DESC, wording_first_seen_at DESC, id DESC LIMIT 1""",
        (board, market, board_job_id),
    )
    if not rows:
        return None
    listing = rows[0]
    listing["same_market_copies"] = []
    if listing["canonical_job_id"] is not None:
        listing["same_market_copies"] = read_rows(
            conn,
            """SELECT j.board, j.board_job_id, j.job_url, j.advertiser_name, j.board_facts
            FROM job_sorting.job_listings j
            WHERE j.canonical_job_id = %s AND j.market = %s
            AND (j.board, j.board_job_id) <> (%s, %s)
            AND NOT EXISTS (
                SELECT 1 FROM job_sorting.job_listings newer
                WHERE (newer.board, newer.market, newer.board_job_id)
                    = (j.board, j.market, j.board_job_id)
                AND (newer.wording_last_seen_at, newer.wording_first_seen_at, newer.id)
                    > (j.wording_last_seen_at, j.wording_first_seen_at, j.id))
            ORDER BY j.board, j.board_job_id""",
            (listing["canonical_job_id"], market, board, board_job_id),
        )
    return listing


def linkedin_listings_with_name(conn, company_name):
    """Newest LinkedIn row per advertiser containing the literal name, newest first."""
    if not company_name:
        return []
    # Escape LIKE metacharacters; company names are values, not patterns.
    escaped = company_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return read_rows(
        conn,
        """SELECT * FROM (
            SELECT DISTINCT ON (j.advertiser_name) j.advertiser_name,
                j.board_facts ->> 'board_company_id' AS board_company_id, j.board_facts,
                j.wording_last_seen_at, j.wording_first_seen_at, j.id
            FROM job_sorting.job_listings j
            WHERE j.board = 'linkedin'
            AND regexp_replace(j.advertiser_name, '[[:space:]]+', ' ', 'g') ILIKE %s
            ORDER BY j.advertiser_name,
                j.wording_last_seen_at DESC, j.wording_first_seen_at DESC, j.id DESC
        ) newest_per_name
        ORDER BY wording_last_seen_at DESC, wording_first_seen_at DESC, id DESC""",
        (f"%{escaped}%",),
    )


def direct_application_link(conn, key):
    """The listing's job-listing research row, or None when it was never searched.

    After a merge, the row of the group's most recently requested search.
    """
    rows = read_rows(
        conn,
        GROUP_ANCHORS
        + """
        SELECT d.* FROM job_tracking.direct_application_links d
        JOIN anchors USING (board, market, board_job_id)
        ORDER BY d.requested_at DESC LIMIT 1""",
        _listing_key_parameters(key),
    )
    return rows[0] if rows else None


def next_queued_direct_application_link(conn):
    """The queued research asked for first, or None when nothing is queued."""
    rows = read_rows(
        conn,
        """SELECT * FROM job_tracking.direct_application_links WHERE status = 'queued'
        ORDER BY requested_at, board, market, board_job_id LIMIT 1""",
    )
    return rows[0] if rows else None


def latest_personnel_search(conn, key):
    """The listing's latest people search, for its job line; None when never searched.

    After a merge, the group's most recently requested search.
    """
    rows = read_rows(
        conn,
        GROUP_ANCHORS
        + """
        SELECT p.* FROM job_tracking.personnel_searches p
        JOIN anchors USING (board, market, board_job_id)
        ORDER BY p.requested_at DESC LIMIT 1""",
        _listing_key_parameters(key),
    )
    return rows[0] if rows else None


def latest_finished_personnel_search(conn, key):
    """Find the successful search that owns the displayed people and lookup costs."""
    rows = read_rows(
        conn,
        FOUND_BY_LATEST_SEARCH + "\nSELECT bto_run_id, finished_at FROM found_by",
        _listing_key_parameters(key),
    )
    return rows[0] if rows else None


def next_queued_personnel_search(conn):
    """The queued people search asked for first, or None when nothing is queued."""
    rows = read_rows(
        conn,
        """SELECT * FROM job_tracking.personnel_searches WHERE status = 'queued'
        ORDER BY requested_at, board, market, board_job_id LIMIT 1""",
    )
    return rows[0] if rows else None


def queue_position(conn, table, key_or_run_id):
    """Return a queued search's position, queue size and preceding job label.

    The running search counts first. Non-queued searches have no position."""
    if table not in SEARCH_TABLES:
        raise ValueError(f"Unknown search table: {table}")
    rows = read_rows(
        conn,
        sql.SQL(
            """SELECT s.*, COALESCE(j.hiring_company_name, j.advertiser_name, j.title) AS label
            FROM job_tracking.{} s
            LEFT JOIN LATERAL (
                SELECT hiring_company_name, advertiser_name, title
                FROM job_sorting.job_listings j
                WHERE (j.board, j.market, j.board_job_id) = (s.board, s.market, s.board_job_id)
                ORDER BY wording_last_seen_at DESC, wording_first_seen_at DESC, id DESC LIMIT 1
            ) j ON TRUE
            WHERE s.status IN ('queued', 'searching')
            ORDER BY s.status = 'queued', s.requested_at, s.board, s.market, s.board_job_id"""
        ).format(sql.Identifier(table)),
    )

    def is_this_search(row):
        if table == "personnel_searches":
            return row["bto_run_id"] == key_or_run_id
        return (row["board"], row["market"], row["board_job_id"]) == tuple(key_or_run_id)

    for position, row in enumerate(rows, start=1):
        if is_this_search(row) and row["status"] == "queued":
            ahead_label = rows[position - 2]["label"] if position > 1 else None
            return position, len(rows), ahead_label
    return None, len(rows), None


def relevant_personnel(conn, key):
    """Read people from the current group's latest successful search."""
    return read_rows(
        conn,
        FOUND_BY_LATEST_SEARCH
        + """
        SELECT r.* FROM job_tracking.relevant_personnel r
        JOIN found_by USING (board, market, board_job_id)
        ORDER BY r.id""",
        _listing_key_parameters(key),
    )


def relevant_person(conn, person_id):
    """One person's row, or None when a newer search has replaced it."""
    rows = read_rows(
        conn, "SELECT * FROM job_tracking.relevant_personnel WHERE id = %s", (person_id,)
    )
    return rows[0] if rows else None


def linkedin_posts(conn, key):
    """The LinkedIn posts found by the same search as relevant_personnel, newest first."""
    return read_rows(
        conn,
        FOUND_BY_LATEST_SEARCH
        + """
        SELECT p.* FROM job_tracking.linkedin_posts p
        JOIN found_by USING (board, market, board_job_id)
        ORDER BY p.posted_at DESC NULLS LAST, p.post_url""",
        _listing_key_parameters(key),
    )


def average_people_search_cost_usd(conn, last=10):
    """Average the last complete search costs; return None until enough are known."""
    costs = conn.execute(
        """SELECT sum(a.cost_usd)
        FROM job_tracking.personnel_searches p
        JOIN costs.apify_find_job_contacts a USING (bto_run_id)
        WHERE p.status = 'done'
        GROUP BY p.bto_run_id, p.finished_at
        HAVING count(a.cost_usd) = count(*)
        ORDER BY p.finished_at DESC LIMIT %s""",
        (last,),
    ).fetchall()
    if len(costs) < last:
        return None
    return float(sum(cost for (cost,) in costs) / len(costs))


def apify_runs_without_cost(conn):
    """List contact-search Actor runs awaiting their reported costs, oldest first."""
    return [
        apify_run_id
        for (apify_run_id,) in conn.execute(
            """SELECT apify_run_id FROM costs.apify_find_job_contacts
            WHERE cost_usd IS NULL ORDER BY started_at, apify_run_id"""
        ).fetchall()
    ]


def hunter_credits_for_search(conn, bto_run_id):
    """Hunter credits one people search used, shown as "email checks used".

    Requests whose credits could not be measured add nothing.
    """
    credits = conn.execute(
        """SELECT COALESCE(sum(credits), 0) FROM costs.hunter_find_job_contacts
        WHERE bto_run_id = %s""",
        (bto_run_id,),
    ).fetchone()[0]
    return float(credits)


def searches_today(conn, table):
    """Count requested searches since Singapore midnight for the daily limit."""
    if table not in SEARCH_TABLES:
        raise ValueError(f"Unknown search table: {table}")
    return conn.execute(
        sql.SQL(
            """SELECT count(*) FROM job_tracking.{}
            WHERE requested_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Singapore')
                AT TIME ZONE 'Asia/Singapore'"""
        ).format(sql.Identifier(table))
    ).fetchone()[0]


def average_search_seconds(conn, table, last=10):
    """Average running time, in seconds, of the last `last` searches that
    finished, for the time Run all shows. None before any has finished."""
    if table not in SEARCH_TABLES:
        raise ValueError(f"Unknown search table: {table}")
    seconds = conn.execute(
        sql.SQL(
            """SELECT avg(extract(epoch FROM finished_at - started_at)) FROM (
                SELECT started_at, finished_at FROM job_tracking.{}
                WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
                ORDER BY finished_at DESC LIMIT %s) recent"""
        ).format(sql.Identifier(table)),
        (last,),
    ).fetchone()[0]
    return float(seconds) if seconds is not None else None
