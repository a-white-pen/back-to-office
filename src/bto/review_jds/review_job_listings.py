"""Read-only Review grouping, search and listing context."""

import difflib
import html
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser

from bto.filter_and_bucket_job_listings import BUCKETS, PROCESSING_START, SINGAPORE, night
from bto.filter_and_bucket_job_listings.bucket_job_listings import kind_answers
from bto.filter_and_bucket_job_listings.config import load_preferences
from bto.filter_and_bucket_job_listings.evaluate_job_requirements import evaluate_requirements
from bto.filter_and_bucket_job_listings.extract_job_listing_details import advertiser_line
from bto.filter_and_bucket_job_listings.suppress_repeated_job_listings import title_key
from bto.storage.postgres.job_sorting import JOIN_RESULTS, read_rows
from bto.storage.postgres.setup_job_sorting import COLUMN_CONTRACT

LIGHT_COLUMNS = ",".join(
    c
    for c, _, _ in COLUMN_CONTRACT["job_listings"]
    if c not in {"description_text", "description_html", "board_facts"}
)
# Keep the newer-wording lookup on the listing identity index. OFFSET 0 prevents
# a full-history hash anti-join when only processing-window candidates are needed.
CURRENT_PREDICATE = """NOT EXISTS(SELECT 1 FROM job_sorting.job_listings newer
 WHERE (newer.board,newer.market,newer.board_job_id)=(j.board,j.market,j.board_job_id)
 AND (newer.wording_last_seen_at,newer.wording_first_seen_at,newer.id)>
     (j.wording_last_seen_at,j.wording_first_seen_at,j.id) OFFSET 0)"""
CURRENT = f"""SELECT {','.join('j.' + c for c in LIGHT_COLUMNS.split(','))},jsonb_build_object('posted_date',j.board_facts->'posted_date') board_facts
 FROM job_sorting.job_listings j WHERE {CURRENT_PREDICATE}"""
RANK = (
    "CASE coalesce(bucket,'not_sorted') "
    + " ".join(f"WHEN '{b}' THEN {i}" for i, b in enumerate(BUCKETS))
    + " END"
)


GROUP_KEYS = (
    "id", "board", "market", "board_job_id", "canonical_job_id", "listing_first_seen_at",
    "wording_first_seen_at", "wording_last_seen_at", "bucket", "repeat_of_id",
    "advertiser_name", "hiring_company_name", "is_recruitment_firm",
)


def availability_query(with_keys=False):
    # A narrow typed projection is shared inside one read-only page statement.
    source = "history_keys j" if with_keys else "job_sorting.job_listings j"
    projection = (
        "history_keys AS MATERIALIZED(SELECT " + ",".join(GROUP_KEYS)
        + " FROM job_sorting.job_listings)," if with_keys else ""
    )
    return projection + f"""availability AS MATERIALIZED(SELECT count(*) loaded,
 max(wording_last_seen_at) latest,
 (date_trunc('day',max(wording_last_seen_at) AT TIME ZONE '{SINGAPORE.key}'
 -INTERVAL '3 hours')+INTERVAL '3 hours') AT TIME ZONE '{SINGAPORE.key}' night_start,
 array_agg(DISTINCT (listing_first_seen_at AT TIME ZONE '{SINGAPORE.key}'
 -INTERVAL '3 hours')::date) dates,
 array_agg(DISTINCT market||':'||board) board_pairs
 FROM {source})"""


def grouped_query(*, queue=False, advertiser=False, population=False, defer_context=False):
    keys = GROUP_KEYS
    # Keep filter keys narrow; fetch wide fields only after choosing representatives.
    columns = ",".join(("r." if c in keys else "j.") + c for c in LIGHT_COLUMNS.split(","))
    candidates = ""
    source = "job_sorting.job_listings j"
    if population and queue:
        source = "history_keys j"
    if advertiser:
        candidates = """matched_advertiser AS(SELECT DISTINCT canonical_job_id,board,market,board_job_id
 FROM job_sorting.job_listings WHERE advertiser_name=%s),candidate_ids AS(
 SELECT j.id FROM matched_advertiser a JOIN job_sorting.job_listings j
 ON j.canonical_job_id=a.canonical_job_id
 UNION SELECT j.id FROM matched_advertiser a JOIN job_sorting.job_listings j
 ON (j.board,j.market,j.board_job_id)=(a.board,a.market,a.board_job_id)
 WHERE a.canonical_job_id IS NULL),"""
        source += " JOIN candidate_ids candidate ON candidate.id=j.id"
    if queue or advertiser:
        # Filtering post-window wordings first still checks every newer wording,
        # including a return to a pre-window wording, before accepting a current copy.
        predicate = CURRENT_PREDICATE
        if queue:
            predicate += f" AND j.wording_first_seen_at>=TIMESTAMPTZ '{PROCESSING_START.isoformat()}'"
        current_keys = "SELECT " + ",".join("j." + c for c in keys) + f" FROM {source} WHERE {predicate}"
    else:
        current_keys = "SELECT DISTINCT ON(board,market,board_job_id) " + ",".join(keys) + """
 FROM job_sorting.job_listings ORDER BY board,market,board_job_id,
 wording_last_seen_at DESC,wording_first_seen_at DESC,id DESC"""
    selection = "repeat_of_id IS NULL" if queue else "TRUE"
    member_predicate = CURRENT_PREDICATE.replace("j.", "member.")
    available = availability_query(with_keys=queue) + "," if population else ""
    posted = "'{}'::jsonb" if defer_context else "jsonb_build_object('posted_date',j.board_facts->'posted_date')"
    boards = "ARRAY[r.board]" if defer_context else "coalesce(m.boards,ARRAY[r.board])"
    first_seen = "r.listing_first_seen_at" if defer_context else "coalesce(m.job_first_seen_at,r.listing_first_seen_at)"
    members = "" if defer_context else f""" LEFT JOIN LATERAL(
 SELECT array_agg(member.board) boards,min(member.listing_first_seen_at) job_first_seen_at
 FROM job_sorting.job_listings member WHERE r.canonical_job_id IS NOT NULL
 AND member.canonical_job_id=r.canonical_job_id AND {member_predicate}) m ON TRUE"""
    return f"""WITH {available}{candidates}current_keys AS({current_keys}),
 current AS(SELECT j.* FROM job_sorting.job_listings j JOIN current_keys k ON k.id=j.id),
 company_leaders AS(SELECT k.advertiser_name,k.hiring_company_name,k.is_recruitment_firm,k.board
 FROM current_keys k WHERE k.wording_first_seen_at>=TIMESTAMPTZ '{PROCESSING_START.isoformat()}'
 AND k.bucket IN ('top_picks','in_lane') AND k.repeat_of_id IS NULL),identified AS(
 SELECT c.*,coalesce(canonical_job_id,'listing:'||jsonb_build_array(board,market,board_job_id)::text) group_key,{RANK} bucket_rank FROM current_keys c),
 copies AS(SELECT * FROM identified WHERE {selection}),
 representatives AS(SELECT DISTINCT ON(group_key) * FROM copies
 ORDER BY group_key,(repeat_of_id IS NOT NULL),bucket_rank,wording_first_seen_at DESC,id DESC),
 grouped AS NOT MATERIALIZED(SELECT {columns},{posted} board_facts,
 r.group_key,r.bucket_rank,1::bigint representative,{boards} boards,{first_seen} job_first_seen_at
 FROM representatives r JOIN job_sorting.job_listings j ON j.id=r.id{members}) """


GROUPS = grouped_query()
QUEUE_GROUPS = grouped_query(queue=True)
PAGE_QUEUE_GROUPS = grouped_query(queue=True, population=True)
PAGE_GROUPS = grouped_query(population=True)
PAGE_ADVERTISER_GROUPS = grouped_query(advertiser=True, population=True)
ADVERTISER_GROUPS = grouped_query(advertiser=True)

META = "id,board,market,board_job_id,title,advertiser_name,bucket,reason,decided_by,listing_first_seen_at,wording_first_seen_at,wording_last_seen_at,canonical_job_id,fingerprint,repeat_of_id,job_url,salary_raw,salary_min,salary_max,salary_currency,salary_period,is_recruitment_firm,board_facts"


def description_expression(text):
    if len(text) > 2000 or text.count("(") > 40:
        raise ValueError("Description expression too long")
    tokens = re.findall(r'"[^"\n]*"|\(|\)|[^\s()]+', text)
    tokens = [t.upper() if t.upper() in {"AND", "OR", "NOT"} else t for t in tokens]
    position = 0
    values = []

    def atom():
        nonlocal position
        if position >= len(tokens):
            raise ValueError("Missing search term")
        token = tokens[position]
        position += 1
        if token == "NOT":
            return "(NOT " + atom() + ")"
        if token == "(":
            expression = or_expression()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("Missing closing bracket")
            position += 1
            return expression
        if token in {")", "AND", "OR"}:
            raise ValueError("Unexpected operator")
        token = token[1:-1] if token.startswith('"') else token
        if not token or '"' in token:
            raise ValueError("Invalid quoted term")
        values.append(
            "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        return "coalesce(doc.description_text,'') ILIKE %s"

    def and_expression():
        nonlocal position
        expression = atom()
        while position < len(tokens) and tokens[position] not in {")", "OR"}:
            if tokens[position] == "AND":
                position += 1
            expression = "(" + expression + " AND " + atom() + ")"
        return expression

    def or_expression():
        nonlocal position
        expression = and_expression()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            expression = "(" + expression + " OR " + and_expression() + ")"
        return expression

    expression = or_expression()
    if position != len(tokens):
        raise ValueError("Unmatched bracket")
    return expression, values


MARKETS = ("sg", "hk", "th")
BOARD_LABELS = {
    "mcf": "MyCareersFuture", "jobstreet": "JobStreet", "jobsdb": "JobsDB",
    "seek": "SEEK", "indeed": "Indeed", "linkedin": "LinkedIn",
}
PRIMARY_BUCKETS = ("top_picks", "in_lane", "less_interested", "ineligible", "not_sorted")
OUT_OF_SCOPE = ("title_filter", "non_tech", "tech_not_for_me")
DEFAULT_BUCKETS = ("top_picks", "in_lane", "not_sorted")
# Uncorrelated membership queries build each company set once for the queue.
DEFAULT_SCOPE = """(coalesce(g.bucket,'not_sorted') IN ('top_picks','in_lane','not_sorted') OR
 (g.bucket='less_interested' AND (
 (g.is_recruitment_firm IS NOT TRUE AND g.advertiser_name IN
  (SELECT advertiser_name FROM company_leaders WHERE is_recruitment_firm IS NOT TRUE))
 OR (g.board='mcf' AND g.hiring_company_name IS NOT NULL AND g.hiring_company_name IN
  (SELECT hiring_company_name FROM company_leaders WHERE board='mcf')))))"""


def _parameter_values(parameters, key, fallback=None):
    raw = parameters.getlist(key) if hasattr(parameters, "getlist") else [parameters.get(key)]
    if not any(value is not None for value in raw) and fallback:
        return _parameter_values(parameters, fallback)
    return list(
        dict.fromkeys(
            part.strip().lower()
            for value in raw
            if value is not None
            for part in str(value).split(",")
            if part.strip()
        )
    )


def _night_start(day):
    return datetime.combine(day, time(3), SINGAPORE)


def available_review_nights(conn):
    """Read published collection-night choices without using the wall clock."""
    latest, dates = conn.execute(
        "WITH " + availability_query() + " SELECT latest,dates FROM availability"
    ).fetchone()
    return night(latest) if latest else None, sorted(dates or [], reverse=True)



def review_filter_state(
    parameters, *, latest_night=None, older_nights=(), available_markets=MARKETS,
    validate_markets=True, board_pairs=(),
):
    """Parse URL filters against the latest published collection night in PostgreSQL."""
    markets = _parameter_values(parameters, "markets", "market")
    markets = (
        list(MARKETS) if not markets else list(available_markets)
        if markets == ["all"]
        else []
        if markets == ["none"]
        else markets
    )
    if any(not re.fullmatch(r"[a-z]{2}", value) for value in markets) or (
        validate_markets and any(value not in available_markets for value in markets)
    ):
        raise ValueError("Unknown market")
    boards_off = _parameter_values(parameters, "boards_off")
    if not boards_off and parameters.get("board"):
        boards_off = [p for p in board_pairs if p.split(":")[1] != parameters["board"].lower()]
    if parameters.get("board") and parameters["board"].lower() not in BOARD_LABELS:
        raise ValueError("Unknown board")
    for pair in boards_off:
        parts = pair.split(":")
        if len(parts) != 2 or parts[1] not in BOARD_LABELS or not re.fullmatch(r"[a-z]{2}", parts[0]):
            raise ValueError("Unknown board selection")
        if validate_markets and parts[0] not in available_markets:
            raise ValueError("Unknown board market")
    buckets = _parameter_values(parameters, "buckets", "bucket")
    default_scope = not buckets or buckets == ["default"]
    if default_scope:
        buckets = (
            list(BUCKETS)
            if not buckets and (parameters.get("all") == "1" or parameters.get("advertiser"))
            else list(DEFAULT_BUCKETS)
        )
    elif buckets == ["all"]:
        buckets = list(BUCKETS)
    elif buckets == ["none"]:
        buckets = []
    if any(value not in BUCKETS for value in buckets):
        raise ValueError("Unknown bucket")
    seen = _parameter_values(parameters, "seen")
    if not seen:
        legacy = parameters.get("days")
        seen = ["any" if legacy == "all" else legacy + "d"] if legacy else ["last_night"]
    if seen == ["all"]:
        seen = ["any"]
    ranges, dates = [], []
    window = seen[0] if len(seen) == 1 and seen[0] in {"any", "3d", "7d", "14d", "28d"} else ""
    if window and window != "any" and latest_night is not None:
        ranges = [
            (
                _night_start(latest_night - timedelta(days=int(window[:-1]) - 1)),
                _night_start(latest_night + timedelta(days=1)),
            )
        ]
    elif not window:
        for value in seen:
            if value == "last_night" and latest_night is None:
                continue
            try:
                day = latest_night if value == "last_night" else date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("Unknown first-seen date or window") from error
            if day.isoformat() != value and value != "last_night":
                raise ValueError("Use YYYY-MM-DD for a first-seen date")
            dates.append((value, day))
            ranges.append((_night_start(day), _night_start(day + timedelta(days=1))))
    choices = []
    for offset in (0, 1, 2):
        if latest_night is None:
            choices.append(
                {
                    "label": "Last night" if offset == 0 else "—",
                    "available": False,
                    "selected": False,
                }
            )
            continue
        day = latest_night - timedelta(days=offset)
        key = "last_night" if offset == 0 else day.isoformat()
        selected = any(d == day for _, d in dates)
        next_dates = [(k, d) for k, d in dates if d != day]
        if not selected:
            next_dates.append((key, day))
        next_dates.sort(key=lambda item: item[1], reverse=True)
        choices.append(
            {
                "label": "Last night" if offset == 0 else day.strftime("%-d %b"),
                "date": day.isoformat(),
                "available": True,
                "title": day.strftime("%a %-d %b") + " · 3:00 AM–3:00 AM SGT",
                "selected": selected,
                "value": ",".join(k for k, _ in next_dates) or "any",
            }
        )
    older = {
        d for d in older_nights if latest_night is not None and d < latest_night - timedelta(days=2)
    }
    older.update(
        d
        for k, d in dates
        if k != "last_night" and (latest_night is None or d < latest_night - timedelta(days=2))
    )
    older_choices = [
        {"value": d.isoformat(), "label": d.strftime("%-d %b %Y")}
        for d in sorted(older, reverse=True)
    ]
    more_value = window or (
        seen[0] if len(seen) == 1 and any(o["value"] == seen[0] for o in older_choices) else ""
    )
    return {
        "markets": markets,
        "boards_off": boards_off,
        "board_pairs": list(board_pairs),
        "markets_all": _parameter_values(parameters, "markets", "market") == ["all"],
        "available_markets": list(available_markets),
        "buckets": buckets,
        "default_scope": default_scope,
        "seen": ",".join(seen),
        "window": window,
        "older_dates": older_choices,
        "more_value": more_value,
        "latest_night": latest_night,
        "date_unavailable": not ranges and window != "any",
        "seen_label": (
            "Any time"
            if window == "any"
            else window[:-1] + " collection nights"
            if window
            else ", ".join(
                "Last night · " + d.strftime("%-d %b")
                if k == "last_night"
                else d.strftime("%-d %b")
                for k, d in dates
            )
            or "No collection night available"
        ),
        "ranges": ranges,
        "dates": choices,
        "query": {
            "markets": ",".join(markets) or "none",
            "buckets": "default" if default_scope else ",".join(buckets) or "none",
            "seen": ",".join(seen),
            **({"boards_off": ",".join(boards_off)} if boards_off else {}),
        },
    }


def _review_where(parameters, state, *, skip=None):
    filters, values = [], []
    normal = parameters.get("all") != "1" and not parameters.get("advertiser")
    if skip != "market" and not state.get("markets_all"):
        filters.append("g.market=ANY(%s)")
        values.append(state["markets"])
    if skip != "bucket":
        if normal and state["default_scope"]:
            filters.append(DEFAULT_SCOPE)
        else:
            filters.append("coalesce(g.bucket,'not_sorted')=ANY(%s)")
            values.append(state["buckets"])
    if skip != "board":
        if state["boards_off"]:
            filters.append("NOT ((g.market||':'||g.board)=ANY(%s))")
            values.append(state["boards_off"])
        elif parameters.get("board"):
            filters.append("g.board=%s")
            values.append(parameters["board"].lower())
    date_parts = []
    if state.get("query_dates"):
        base = "(SELECT night_start FROM availability)"
        if state["window"] and state["window"] != "any":
            days = int(state["window"][:-1])
            date_parts.append(
                f"(g.listing_first_seen_at>={base}-INTERVAL '{days-1} days' "
                f"AND g.listing_first_seen_at<{base}+INTERVAL '1 day')"
            )
        elif "last_night" in state["seen"].split(","):
            date_parts.append(
                f"(g.listing_first_seen_at>={base} "
                f"AND g.listing_first_seen_at<{base}+INTERVAL '1 day')"
            )
    elif state["date_unavailable"]:
        filters.append("FALSE")
    date_parts.extend(
        "(g.listing_first_seen_at>=%s AND g.listing_first_seen_at<%s)"
        for _ in state["ranges"]
    )
    values.extend(boundary for pair in state["ranges"] for boundary in pair)
    if date_parts:
        filters.append("(" + " OR ".join(date_parts) + ")")
    if parameters.get("q", "").strip():
        pattern = _search_pattern(parameters["q"].strip())
        filters.append(
            "(coalesce(g.title,'') ILIKE %s OR coalesce(g.advertiser_name,'') ILIKE %s OR coalesce(g.hiring_company_name,'') ILIKE %s)"
        )
        values.extend([pattern] * 3)
    for parameter, column, negative in [
        ("title", "title", False),
        ("exclude", "title", True),
        ("company", "advertiser_name", False),
    ]:
        if parameters.get(parameter):
            filters.append(f"coalesce(g.{column},'') {'NOT ILIKE' if negative else 'ILIKE'} %s")
            values.append(_search_pattern(parameters[parameter]))
    if parameters.get("advertiser"):
        filters.append("g.advertiser_name=%s")
        values.append(parameters["advertiser"])
        filters.append(
            "g.bucket IN ('top_picks','in_lane','less_interested')"
            if parameters.get("scope", "in_scope") == "in_scope"
            else "coalesce(g.bucket,'not_sorted') NOT IN ('top_picks','in_lane','less_interested')"
        )
        context_days = parameters.get("context_days")
        if context_days and context_days != "all":
            if context_days not in {"3", "7", "14", "28"}:
                raise ValueError("Unknown advertiser window")
            filters.append("g.job_first_seen_at>=%s")
            values.append(datetime.now(timezone.utc) - timedelta(days=int(context_days)))
    if parameters.get("posted_by") in {"employer", "recruitment"}:
        filters.append(
            "g.is_recruitment_firm IS "
            + ("TRUE" if parameters["posted_by"] == "recruitment" else "FALSE")
        )
    source = "grouped g"
    notice = None
    if parameters.get("description"):
        try:
            expression, arguments = description_expression(parameters["description"])
            filters.append(expression)
            values.extend(arguments)
            source += " JOIN (SELECT id description_id,description_text FROM job_sorting.job_listings) doc ON doc.description_id=g.id"
        except ValueError:
            notice = "Couldn't read the description expression — check brackets and quotes. It's being ignored."
    return source, " AND ".join(filters) or "TRUE", values, notice


def _search_pattern(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _review_groups(parameters, state=None):
    page = state is not None and state.get("query_dates")
    if parameters.get("advertiser"):
        return (PAGE_ADVERTISER_GROUPS if page else ADVERTISER_GROUPS), [parameters["advertiser"]]
    if parameters.get("all") == "1":
        return (PAGE_GROUPS if page else GROUPS), []
    return (PAGE_QUEUE_GROUPS if page else QUEUE_GROUPS), []


def review_filter_counts(conn, parameters, state):
    """Facets count grouped jobs under every active filter except their own dimension."""
    groups_sql, grouping_values = _review_groups(parameters, state)
    counts = {}
    for dimension, column in [
        ("market", "g.market"),
        ("bucket", "coalesce(g.bucket,'not_sorted')"),
        ("board", "g.market||':'||g.board"),
    ]:
        source, where, values, _ = _review_where(parameters, state, skip=dimension)
        rows = conn.execute(
            groups_sql + f"SELECT {column},count(*) FROM {source} WHERE {where} GROUP BY {column}",
            [*grouping_values, *values],
        ).fetchall()
        counts[dimension] = dict(rows)
    state["market_counts"] = {m: counts["market"].get(m, 0) for m in MARKETS}
    state["bucket_counts"] = {b: counts["bucket"].get(b, 0) for b in BUCKETS}
    state["out_of_scope_count"] = sum(state["bucket_counts"][b] for b in OUT_OF_SCOPE)
    state["board_counts"] = counts["board"]
    return state


def _ordered_population(source, where, offset="%s", *, positions=False):
    return f""",filtered AS(SELECT g.*,
        CASE WHEN bucket IN ('top_picks','in_lane','less_interested') THEN CASE WHEN is_recruitment_firm THEN CASE WHEN board='mcf' THEN hiring_company_name END ELSE advertiser_name END END company_key,
        row_number() OVER(ORDER BY bucket_rank,
          CASE WHEN is_recruitment_firm THEN 2 WHEN is_public_sector AND upper(coalesce(job_country_code,market))='SG' THEN 1 ELSE 0 END,
          CASE WHEN bucket='top_picks' THEN abs(years_over_comfortable) END NULLS LAST,
          CASE WHEN bucket='in_lane' THEN cardinality(lacks) END NULLS LAST,
          CASE WHEN bucket='in_lane' THEN CASE WHEN years_over_comfortable>0 THEN 0 ELSE 1 END END,
          CASE WHEN bucket='in_lane' AND years_over_comfortable>0 THEN years_over_comfortable END,
          CASE WHEN bucket IN ('top_picks','in_lane','less_interested') THEN preferred_held END DESC NULLS LAST,
          listing_first_seen_at DESC,id DESC) position FROM {source} WHERE {where}),
        clustered AS(SELECT *,min(position) OVER(PARTITION BY coalesce(company_key,'listing:'||id::text)) leader FROM filtered)
        SELECT {META},boards{",leader,position" if positions else ""} FROM clustered ORDER BY leader,position LIMIT 50 OFFSET {offset}"""


def review_population(conn, parameters, *, state=None):
    if state is None:
        latest, older = available_review_nights(conn)
        state = review_filter_state(parameters, latest_night=latest, older_nights=older)
    groups_sql, grouping_values = _review_groups(parameters, state)
    source, where, values, notice = _review_where(parameters, state)
    count = conn.execute(
        groups_sql + f"SELECT count(*) FROM {source} WHERE {where}", [*grouping_values, *values]
    ).fetchone()[0]
    try:
        page = max(1, int(parameters.get("page", 1)))
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid page") from error
    page = min(page, max(1, (count + 49) // 50))
    ordering = _ordered_population(source, where)
    jobs = read_rows(conn, groups_sql + ordering, [*grouping_values, *values, (page - 1) * 50])
    return jobs, count, page, notice


def review_read(conn, parameters, state):
    """Share current-wording/group selection across the page and facet counts."""
    groups_sql, values = _review_groups(parameters, state)
    defer_context = state.get("query_dates") and not parameters.get("advertiser") and parameters.get("all") != "1"
    if defer_context:
        # Copy badges and posted dates are presentation, not filters or ordering.
        groups_sql = grouped_query(queue=True, population=True, defer_context=True)
    fragments = []
    notice = None
    for name, skip, projection, group in [
        ("population", None, "g.*", ""),
        ("market_counts", "market", "g.market key,count(*) n", " GROUP BY g.market"),
        ("bucket_counts", "bucket", "coalesce(g.bucket,'not_sorted') key,count(*) n",
         " GROUP BY coalesce(g.bucket,'not_sorted')"),
        ("board_counts", "board", "g.market||':'||g.board key,count(*) n",
         " GROUP BY g.market,g.board"),
    ]:
        source, where, arguments, warning = _review_where(parameters, state, skip=skip)
        fragments.append(f"{name} AS(SELECT {projection} FROM {source} WHERE {where}{group})")
        values.extend(arguments)
        notice = notice or warning
    try:
        requested_page = max(1, int(parameters.get("page", 1)))
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid page") from error
    values.append(requested_page)
    ordering = _ordered_population(
        "selected g", "TRUE", "(SELECT (page-1)*50 FROM effective_page)", positions=True
    )
    metadata = (
        ",(SELECT jsonb_build_object('latest',latest,'dates',dates,'loaded',loaded,"
        "'board_pairs',board_pairs) FROM availability)"
        if state.get("query_dates") else ""
    )
    page_json = "to_jsonb(p)-'leader'-'position'"
    if defer_context:
        member_predicate = CURRENT_PREDICATE.replace("j.", "member.")
        # Hydrate only the displayed 50 rows after counts and pagination.
        page_json = f"""({page_json})||jsonb_build_object(
 'board_facts',(SELECT jsonb_build_object('posted_date',full_row.board_facts->'posted_date')
 FROM job_sorting.job_listings full_row WHERE full_row.id=p.id),
 'boards',coalesce((SELECT array_agg(member.board) FROM job_sorting.job_listings member
 WHERE p.canonical_job_id IS NOT NULL AND member.canonical_job_id=p.canonical_job_id
 AND {member_predicate}),ARRAY[p.board]))"""
    query = groups_sql + "," + ",".join(fragments) + f""",
 tally AS(SELECT count(*) total FROM population),
 effective_page AS(SELECT total,least(%s,greatest(1,(total+49)/50)) page FROM tally),
 page_data AS(WITH selected AS(SELECT * FROM population){ordering})
 SELECT total,page,coalesce((SELECT jsonb_agg({page_json} ORDER BY p.leader,p.position) FROM page_data p),'[]'),
 coalesce((SELECT jsonb_object_agg(key,n) FROM market_counts),'{{}}'),
 coalesce((SELECT jsonb_object_agg(key,n) FROM bucket_counts),'{{}}'),
 coalesce((SELECT jsonb_object_agg(key,n) FROM board_counts),'{{}}'){metadata} FROM effective_page"""
    result = conn.execute(query, values).fetchone()
    total, page, jobs, markets, buckets, boards = result[:6]
    if state.get("query_dates"):
        meta = result[6]
        pairs = meta["board_pairs"] or []
        available = (*MARKETS, *sorted({p.split(':')[0] for p in pairs} - set(MARKETS)))
        latest = datetime.fromisoformat(meta["latest"]) if meta["latest"] else None
        controls = review_filter_state(
            parameters, latest_night=night(latest) if latest else None,
            older_nights=[date.fromisoformat(d) for d in meta["dates"] or []],
            available_markets=available, board_pairs=pairs,
        )
        state.clear()
        state.update(controls, loaded=meta["loaded"], board_pairs=pairs)
    for job in jobs:
        for key in ("listing_first_seen_at", "wording_first_seen_at", "wording_last_seen_at"):
            if job[key]:
                job[key] = datetime.fromisoformat(job[key])
    state["market_counts"] = {m: markets.get(m, 0) for m in state["available_markets"]}
    state["bucket_counts"] = {b: buckets.get(b, 0) for b in BUCKETS}
    state["out_of_scope_count"] = sum(state["bucket_counts"][b] for b in OUT_OF_SCOPE)
    state["board_counts"] = boards
    return jobs, total, page, notice


class ListingHTML(HTMLParser):
    """Keep board formatting while excluding executable HTML and unsafe URLs."""

    allowed: frozenset = frozenset(
        {
            "p",
            "br",
            "ul",
            "ol",
            "li",
            "strong",
            "b",
            "em",
            "i",
            "u",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "blockquote",
            "div",
            "span",
            "a",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.blocked += 1
            return
        if self.blocked or tag not in self.allowed:
            return
        attribute = ""
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("https://", "http://")):
                attribute = (
                    ' href="'
                    + html.escape(href, quote=True)
                    + '" target="_blank" rel="noopener noreferrer"'
                )
        self.output.append("<" + tag + attribute + ">")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"}:
            self.blocked = max(0, self.blocked - 1)
            return
        if not self.blocked and tag in self.allowed and tag != "br":
            self.output.append("</" + tag + ">")

    def handle_data(self, data):
        if not self.blocked:
            self.output.append(html.escape(data))


def safe_listing_html(value):
    parser = ListingHTML()
    parser.feed(value or "")
    return "".join(parser.output)


def listing_context(conn, selected, parameters):
    identity = (selected["board"], selected["market"], selected["board_job_id"])
    group = selected["canonical_job_id"]
    history = read_rows(
        conn,
        f"SELECT {META} FROM job_sorting.job_listings WHERE (board,market,board_job_id)=(%s,%s,%s) ORDER BY wording_first_seen_at,id",
        identity,
    )
    selected["listing_last_seen_at"] = max(r["wording_last_seen_at"] for r in history)
    same = (
        read_rows(
            conn,
            f"WITH current AS({CURRENT}) SELECT {META} FROM current WHERE canonical_job_id=%s AND (board,market,board_job_id)<>(%s,%s,%s) ORDER BY listing_first_seen_at,id",
            (group, *identity),
        )
        if group is not None
        else []
    )
    for copy in same:
        known = copy["fingerprint"] is not None and selected["fingerprint"] is not None
        copy["match"] = (
            "Exact match"
            if known and copy["fingerprint"] == selected["fingerprint"]
            else "Near match"
            if known
            else "Match type unknown"
        )
    listing_copies = (
        read_rows(
            conn,
            f"SELECT DISTINCT ON(board,market,board_job_id) {META},closed_on_board "
            "FROM job_sorting.job_listings WHERE canonical_job_id=%s "
            "ORDER BY board,market,board_job_id,wording_last_seen_at DESC,wording_first_seen_at DESC,id DESC",
            (group,),
        ) if group is not None else []
    )
    times = len(listing_copies) if group is not None else None
    advertiser = selected.get("advertiser_name")
    possible = []
    advertiser_rows = []
    windows = []
    mode_counts = {"in_scope": 0, "others": 0}
    context_now = datetime.now(timezone.utc)
    scope = parameters.get("advertiser_scope", "in_scope")
    if scope not in mode_counts:
        scope = "in_scope"
    days = parameters.get("advertiser_days", "3")
    if days not in {"3", "7", "14", "28", "all"}:
        days = "3"
    if advertiser:
        possible = read_rows(
            conn,
            f"WITH current AS({CURRENT}) SELECT {META} FROM current WHERE advertiser_name=%s AND board<>%s AND (canonical_job_id IS NULL OR %s::text IS NULL OR canonical_job_id<>%s) AND listing_first_seen_at>=%s",
            (advertiser, selected["board"], group, group, datetime.now(timezone.utc) - timedelta(days=30)),
        )
        possible = [
            r
            for r in possible
            if title_key(selected.get("title"))
            and title_key(r.get("title")) == title_key(selected.get("title"))
        ]
        rows = read_rows(
            conn,
            ADVERTISER_GROUPS
            + f"SELECT {META},job_first_seen_at,boards FROM grouped WHERE advertiser_name=%s AND group_key<>%s ORDER BY job_first_seen_at DESC,id DESC",
            (advertiser, advertiser, group or "listing:" + json.dumps(identity)),
        )
        matches = lambda r, w, mode=scope: (
            (
                w == "all"
                or r["job_first_seen_at"] >= context_now - timedelta(days=int(w))
            )
            and (
                (r.get("bucket") in {"top_picks", "in_lane", "less_interested"})
                == (mode == "in_scope")
            )
        )
        windows = [
            {"days": w, "count": sum(matches(r, w) for r in rows)}
            for w in ["3", "7", "14", "28", "all"]
        ]
        mode_counts = {name: sum(matches(r, days, name) for r in rows) for name in mode_counts}
        advertiser_rows = [r for r in rows if matches(r, days)][:30]
    assessment = None
    if selected.get("extraction"):
        preferences = load_preferences()
        assessment = evaluate_requirements(
            selected["extraction"]["lines"],
            selected["extraction"]["kinds"][0],
            (selected.get("job_country_code") or selected["market"]).upper(),
            preferences,
        )
        assessment["kind_answers"] = list(
            zip(
                selected["extraction"]["kinds"],
                kind_answers(
                    selected["extraction"]["kinds"],
                    (selected.get("job_country_code") or selected["market"]).upper(),
                    assessment,
                    preferences,
                ),
            )
        )
    selected["display_html"] = (
        safe_listing_html(selected.get("description_html"))
        if selected["board"] != "linkedin"
        else ""
    )
    return {
        "history": history,
        "same": same,
        "possible": possible,
        "times_seen": times,
        "listing_copies": listing_copies,
        "advertiser_rows": advertiser_rows,
        "advertiser_windows": windows,
        "advertiser_mode_counts": mode_counts,
        "advertiser_scope": scope,
        "advertiser_days": days,
        "assessment": assessment,
        "context_against": [
            label for show, label in (
                (selected.get("is_recruitment_firm"), "Posted by a recruitment firm"),
                (selected.get("is_public_sector") and
                 (selected.get("job_country_code") or selected["market"]).upper() == "SG",
                 "Public sector in Singapore"),
                (selected.get("closed_on_board") is True, "Closed on board"),
            ) if show
        ],
    }


def wording_diff(conn, first, second):
    rows = read_rows(
        conn,
        "SELECT id,title,advertiser_name,hiring_company_name,description_text FROM job_sorting.job_listings WHERE id=ANY(%s)",
        ([first, second],),
    )
    if len(rows) != 2:
        raise ValueError("Both wording versions must exist")
    mapping = {r["id"]: r for r in rows}
    text = lambda r: "\n".join(
        [r.get("title") or "", advertiser_line(r), r.get("description_text") or ""]
    )
    parts = []
    for value in difflib.ndiff(
        text(mapping[first]).splitlines(), text(mapping[second]).splitlines()
    ):
        if value.startswith("? "):
            continue
        tag = "del" if value.startswith("- ") else "ins" if value.startswith("+ ") else "span"
        parts.append("<" + tag + ">" + html.escape(value[2:]) + "</" + tag + ">")
    return '<div class="diff">' + "\n".join(parts) + "</div>"


def review_selection(conn, identifier, parameters):
    """Load one authorised listing and its context without rebuilding the queue."""
    try:
        identifier = int(identifier)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid listing selection") from error
    found = read_rows(conn, JOIN_RESULTS + " WHERE j.id=%s", (identifier,))
    selected = found[0] if found else None
    return dict(selected=selected, **listing_context(conn, selected, parameters)) if selected else {"selected": None}


def review_page(conn, parameters):
    state = review_filter_state(parameters, validate_markets=False)
    state["query_dates"] = True
    jobs, total, page, notice = review_read(conn, parameters, state)
    loaded = state["loaded"]
    controls = state
    selected = None
    context = {}
    identifier = parameters.get("id") or (jobs[0]["id"] if jobs else None)
    if identifier is not None and parameters.get("view") != "table":
        selection = review_selection(conn, identifier, parameters)
        selected = selection.pop("selected")
        context = selection
    return dict(
        jobs=jobs,
        total=total,
        page=page,
        pages=max(1, (total + 49) // 50),
        loaded=loaded,
        selected=selected,
        filters=controls,
        error=notice,
        **context,
    )
