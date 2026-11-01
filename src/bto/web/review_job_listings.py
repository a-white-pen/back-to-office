"""Read and group job listings for Review, its filters and its detail panels.

The database and snapshot paths share grouping, ordering and live owner state."""

import difflib
import html
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser

from bto.filter_and_bucket_job_listings import (
    BUCKET_LABELS,
    BUCKETS,
    SINGAPORE,
    night,
    processing_start,
)
from bto.filter_and_bucket_job_listings.bucket_job_listings import kind_answers
from bto.filter_and_bucket_job_listings.config import load_preferences
from bto.filter_and_bucket_job_listings.evaluate_job_requirements import evaluate_requirements
from bto.filter_and_bucket_job_listings.extract_job_listing_details import advertiser_line
from bto.filter_and_bucket_job_listings.suppress_repeated_job_listings import title_key
from bto.storage.postgres.read import JOIN_RESULTS, REVIEW_TIMESTAMPS, read_rows, review_group_key
from bto.storage.postgres.setup import COLUMN_CONTRACT
from bto.web import review_table

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
CURRENT = f"""SELECT {",".join("j." + c for c in LIGHT_COLUMNS.split(","))},jsonb_build_object('posted_date',j.board_facts->'posted_date') board_facts
 FROM job_sorting.job_listings j WHERE {CURRENT_PREDICATE}"""
RANK = (
    "CASE coalesce(bucket,'not_sorted') "
    + " ".join(f"WHEN '{b}' THEN {i}" for i, b in enumerate(BUCKETS))
    + " END"
)


GROUP_KEYS = (
    "id",
    "board",
    "market",
    "board_job_id",
    "canonical_job_id",
    "listing_first_seen_at",
    "wording_first_seen_at",
    "wording_last_seen_at",
    "bucket",
    "repeat_of_id",
    "advertiser_name",
    "hiring_company_name",
    "is_recruitment_firm",
)


def availability_query(with_keys=False):
    """Read the listing count, published nights and available board-market pairs."""
    source = "history_keys j" if with_keys else "job_sorting.job_listings j"
    projection = (
        "history_keys AS MATERIALIZED(SELECT "
        + ",".join(GROUP_KEYS)
        + " FROM job_sorting.job_listings),"
        if with_keys
        else ""
    )
    return (
        projection
        + f"""availability AS MATERIALIZED(SELECT count(*) loaded,
 max(wording_last_seen_at) latest,
 (date_trunc('day',max(wording_last_seen_at) AT TIME ZONE '{SINGAPORE.key}'
 -INTERVAL '3 hours')+INTERVAL '3 hours') AT TIME ZONE '{SINGAPORE.key}' night_start,
 array_agg(DISTINCT (listing_first_seen_at AT TIME ZONE '{SINGAPORE.key}'
 -INTERVAL '3 hours')::date) dates,
 array_agg(DISTINCT market||':'||board) board_pairs
 FROM {source})"""
    )


def grouped_query(*, queue=False, advertiser=False, population=False, defer_context=False):
    """Choose one current representative per job and retain the metadata needed by Review."""
    keys = GROUP_KEYS
    # Keep filter keys narrow; fetch wide fields only after choosing representatives.
    columns = ",".join(("r." if c in keys else "j.") + c for c in LIGHT_COLUMNS.split(","))
    candidates = ""
    source = "job_sorting.job_listings j"
    if population and queue:
        source = "history_keys j"
    if advertiser:
        candidates = """matched_advertiser AS(SELECT DISTINCT canonical_job_id,board,market,board_job_id
 FROM job_sorting.job_listings WHERE advertiser_name=ANY(%s)),candidate_ids AS(
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
            predicate += (
                f" AND j.wording_first_seen_at>=TIMESTAMPTZ '{processing_start().isoformat()}'"
            )
        current_keys = (
            "SELECT " + ",".join("j." + c for c in keys) + f" FROM {source} WHERE {predicate}"
        )
    else:
        current_keys = (
            "SELECT DISTINCT ON(board,market,board_job_id) "
            + ",".join(keys)
            + """
 FROM job_sorting.job_listings ORDER BY board,market,board_job_id,
 wording_last_seen_at DESC,wording_first_seen_at DESC,id DESC"""
        )
    selection = "repeat_of_id IS NULL" if queue else "TRUE"
    member_predicate = CURRENT_PREDICATE.replace("j.", "member.")
    available = availability_query(with_keys=queue) + "," if population else ""
    posted = (
        "'{}'::jsonb"
        if defer_context
        else "jsonb_build_object('posted_date',j.board_facts->'posted_date')"
    )
    boards = "ARRAY[r.board]" if defer_context else "coalesce(m.boards,ARRAY[r.board])"
    first_seen = (
        "r.listing_first_seen_at"
        if defer_context
        else "coalesce(m.job_first_seen_at,r.listing_first_seen_at)"
    )
    members = (
        ""
        if defer_context
        else f""" LEFT JOIN LATERAL(
 SELECT array_agg(member.board) boards,min(member.listing_first_seen_at) job_first_seen_at
 FROM job_sorting.job_listings member WHERE r.canonical_job_id IS NOT NULL
 AND member.canonical_job_id=r.canonical_job_id AND {member_predicate}) m ON TRUE"""
    )
    return f"""WITH {available}{candidates}current_keys AS({current_keys}),
 current AS(SELECT j.* FROM job_sorting.job_listings j JOIN current_keys k ON k.id=j.id),
 company_leaders AS(SELECT k.advertiser_name,k.hiring_company_name,k.is_recruitment_firm,k.board
 FROM current_keys k WHERE k.wording_first_seen_at>=TIMESTAMPTZ '{processing_start().isoformat()}'
 AND k.bucket IN ('top_picks','in_lane') AND k.repeat_of_id IS NULL),identified AS(
 SELECT c.*,coalesce(canonical_job_id,'listing:'||jsonb_build_array(board,market,board_job_id)::text) group_key,{RANK} bucket_rank FROM current_keys c),
 copies AS(SELECT * FROM identified WHERE {selection}),
 representatives AS(SELECT DISTINCT ON(group_key) * FROM copies
 ORDER BY group_key,(repeat_of_id IS NOT NULL),bucket_rank,wording_first_seen_at DESC,id DESC),
 grouped AS NOT MATERIALIZED(SELECT {columns},{posted} board_facts,
 r.group_key,r.bucket_rank,1::bigint representative,{boards} boards,{first_seen} job_first_seen_at
 FROM representatives r JOIN job_sorting.job_listings j ON j.id=r.id{members}) """


_GROUP_QUERY_OPTIONS = {
    "QUEUE_GROUPS": {"queue": True},
    "PAGE_QUEUE_GROUPS": {"queue": True, "population": True},
    "PAGE_GROUPS": {"population": True},
    "PAGE_ADVERTISER_GROUPS": {"advertiser": True, "population": True},
    "ADVERTISER_GROUPS": {"advertiser": True},
}


@lru_cache(maxsize=5)
def group_query(name):
    """Build configured SQL when a reader needs it, keeping module imports public."""
    return grouped_query(**_GROUP_QUERY_OPTIONS[name])


def __getattr__(name):
    if name in _GROUP_QUERY_OPTIONS:
        return group_query(name)
    raise AttributeError(name)

META = "id,board,market,board_job_id,title,advertiser_name,bucket,reason,decided_by,listing_first_seen_at,wording_first_seen_at,wording_last_seen_at,canonical_job_id,fingerprint,repeat_of_id,job_url,salary_raw,salary_min,salary_max,salary_currency,salary_period,is_recruitment_firm,board_facts"


def description_expression(text):
    """Parse boolean description searches into SQL with separately bound values."""
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
    "mcf": "MyCareersFuture",
    "jobstreet": "JobStreet",
    "jobsdb": "JobsDB",
    "seek": "SEEK",
    "indeed": "Indeed",
    "linkedin": "LinkedIn",
}
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


def review_filter_state(
    parameters,
    *,
    latest_night=None,
    older_nights=(),
    available_markets=MARKETS,
    validate_markets=True,
    board_pairs=(),
):
    """Parse URL filters against the latest published collection night in PostgreSQL."""
    markets = _parameter_values(parameters, "markets", "market")
    markets = (
        list(MARKETS)
        if not markets
        else list(available_markets)
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
        if (
            len(parts) != 2
            or parts[1] not in BOARD_LABELS
            or not re.fullmatch(r"[a-z]{2}", parts[0])
        ):
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
    window = (
        seen[0]
        if len(seen) == 1 and seen[0] in {"any", "3d", "7d", "14d", "28d", "7f", "14f", "28f"}
        else ""
    )
    if window.endswith("f") and latest_night is not None:
        published = sorted({latest_night, *older_nights}, reverse=True)[: int(window[:-1])]
        ranges = [(_night_start(published[-1]), _night_start(latest_night + timedelta(days=1)))]
    elif window and window != "any" and latest_night is not None:
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
    published = set(older_nights)
    for offset in (0, 1, 2):
        if latest_night is None:
            choices.append(
                {
                    "label": "Latest fetch" if offset == 0 else "—",
                    "available": False,
                    "selected": False,
                }
            )
            continue
        day = latest_night - timedelta(days=offset)
        label = day.strftime("%a %-d %b")
        # Missing nights show no collected listings; they do not explain why.
        # Without a published-night list, no night is marked as missing.
        if offset and published and day not in published:
            choices.append(
                {
                    "label": label + " · nothing collected",
                    "date": day.isoformat(),
                    "available": False,
                    "title": "Nothing was collected on " + label + ".",
                    "selected": False,
                }
            )
            continue
        key = "last_night" if offset == 0 else day.isoformat()
        selected = any(d == day for _, d in dates)
        next_dates = [(k, d) for k, d in dates if d != day]
        if not selected:
            next_dates.append((key, day))
        next_dates.sort(key=lambda item: item[1], reverse=True)
        choices.append(
            {
                "label": ("Latest fetch · " if offset == 0 else "") + label,
                "date": day.isoformat(),
                "available": True,
                # Fetch start/end times and new-listing counts per night are not stored.
                "title": "Fetched " + label,
                "selected": selected,
                "value": ",".join(k for k, _ in next_dates) or "any",
            }
        )
    older = {
        d for d in older_nights if latest_night is not None and d < latest_night - timedelta(days=2)
    }
    picked = [
        d
        for k, d in dates
        if k != "last_night" and (latest_night is None or d < latest_night - timedelta(days=2))
    ]
    older.update(picked)
    older_choices = [
        {"value": d.isoformat(), "label": d.strftime("%-d %b %Y")}
        for d in sorted(older, reverse=True)
    ]
    more_value = window or (
        seen[0] if len(seen) == 1 and any(o["value"] == seen[0] for o in older_choices) else ""
    )
    controls = {
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
        "more_label": (
            {"7d": "Past week", "14d": "Past 2 weeks", "28d": "Past 4 weeks",
             "7f": "7 fetches", "14f": "14 fetches", "28f": "28 fetches",
             "any": "Any time"}.get(window)
            or (picked[0].strftime("%a %-d %b") if len(picked) == 1
                else f"{len(picked)} dates" if picked else "More…")
        ),
        "more_active": bool(window) or any(
            latest_night and d < latest_night - timedelta(days=2) for _, d in dates
        ),
        "calendar": {
            "latest": latest_night.isoformat() if latest_night else None,
            "earliest": min([latest_night, *older_nights]).isoformat() if latest_night else None,
            "today": datetime.now(SINGAPORE).date().isoformat(),
            "selected": [d.isoformat() for _, d in dates],
            "nights": sorted({d.isoformat() for d in older_nights}),
            "ranges": [(a.astimezone(SINGAPORE).date().isoformat(),
                        b.astimezone(SINGAPORE).date().isoformat()) for a, b in ranges] if window else [],
        },
        "latest_night": latest_night,
        "date_unavailable": not ranges and window != "any",
        "seen_label": (
            {"7d": "past week", "14d": "past 2 weeks", "28d": "past 4 weeks",
             "any": "any time"}.get(window)
            or ("last " + window[:-1] + (" fetches" if window.endswith("f") else " days")
                if window else ", ".join(("Latest fetch · " if d == latest_night else "")
                                         + d.strftime("%a %-d %b") for _, d in dates)
                or "No collection night available")
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
    controls["chips"] = filter_chips(parameters, controls)
    return controls


def filter_chips(parameters, state):
    """Describe the active filters and the URL changes that remove each one."""
    chips = []

    def add(key, label, changes, value=""):
        chips.append(
            {"key": key, "label": label, "changes": changes, "value": value, "removable": True}
        )

    markets = state["markets"]
    if sorted(markets) != sorted(MARKETS):
        label = ", ".join(m.upper() for m in markets) or "none"
        add("markets", "market: " + label, {"markets": "all", "market": None}, "all")
    off, pairs, groups = set(state["boards_off"]), set(state["board_pairs"]), []
    for market in markets:
        boards = [
            (market + ":" + board, label)
            for board, label in BOARD_LABELS.items()
            if market + ":" + board in pairs
        ]
        if any(pair in off for pair, _ in boards):
            on = [label for pair, label in boards if pair not in off]
            words = ", ".join(on[:-1]) + " or " + on[-1] if len(on) > 1 else "".join(on)
            groups.append(market.upper() + " " + (words or "no boards"))
    if groups:
        add("boards_off", "board: " + "; ".join(groups), {"boards_off": None, "board": None})
    add("seen", "first seen: " + state["seen_label"], {"seen": "any", "days": None}, "any")
    chips[-1]["removable"] = state["window"] != "any"
    buckets = state["buckets"]
    if len(buckets) != len(BUCKETS):
        names = ", ".join(BUCKET_LABELS[b] for b in review_table.TABLE_BUCKET_ORDER if b in buckets)
        add("buckets", "bucket: " + (names or "none"), {"buckets": "all", "bucket": None}, "all")
    advertisers = (
        parameters.getlist("advertiser")
        if hasattr(parameters, "getlist")
        else [parameters.get("advertiser")]
    )
    advertisers = [name for name in advertisers if name]
    if advertisers:
        add(
            "advertiser",
            'company has "' + " · ".join(advertisers) + '"',
            {"advertiser": None, "scope": None, "context_days": None, "all": None},
        )
    for key, words in (
        ("q", "title or company: {}"),
        ("title", 'title has "{}"'),
        ("exclude", 'title excludes "{}"'),
        ("company", 'company has "{}"'),
        ("description", "description: {}"),
    ):
        if parameters.get(key):
            add(key, words.format(parameters[key].strip()), {key: None})
    posted_by = {"employer": "employer", "recruitment": "recruitment firm"}.get(
        parameters.get("posted_by")
    )
    if posted_by:
        add("posted_by", "posted by " + posted_by, {"posted_by": None})
    return chips


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
        if state["window"].endswith("f"):
            offset = int(state["window"][:-1]) - 1
            lower = f"coalesce((SELECT d FROM unnest((SELECT dates FROM availability)) d ORDER BY d DESC OFFSET {offset} LIMIT 1),(SELECT min(d) FROM unnest((SELECT dates FROM availability)) d))"
            date_parts.append(
                f"(g.listing_first_seen_at>=(({lower})+INTERVAL '3 hours') AT TIME ZONE '{SINGAPORE.key}' AND g.listing_first_seen_at<{base}+INTERVAL '1 day')"
            )
        elif state["window"] and state["window"] != "any":
            days = int(state["window"][:-1])
            date_parts.append(
                f"(g.listing_first_seen_at>={base}-INTERVAL '{days - 1} days' "
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
        "(g.listing_first_seen_at>=%s AND g.listing_first_seen_at<%s)" for _ in state["ranges"]
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


def _review_groups(parameters):
    if parameters.get("advertiser"):
        names = (
            parameters.getlist("advertiser")
            if hasattr(parameters, "getlist")
            else [parameters["advertiser"]]
        )
        return group_query("PAGE_ADVERTISER_GROUPS"), [names]
    if parameters.get("all") == "1":
        return group_query("PAGE_GROUPS"), []
    return group_query("PAGE_QUEUE_GROUPS"), []



def _ordered_population(source, where):
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
        SELECT {META},boards,leader,position FROM clustered ORDER BY leader,position"""



def _page_metadata_json(alias, expression):
    """Hydrate copy badges and posted dates only after selecting the displayed page."""
    member_predicate = CURRENT_PREDICATE.replace("j.", "member.")
    return f"""({expression})||jsonb_build_object(
 'board_facts',(SELECT jsonb_build_object('posted_date',full_row.board_facts->'posted_date')
 FROM job_sorting.job_listings full_row WHERE full_row.id={alias}.id),
 'boards',coalesce((SELECT array_agg(member.board) FROM job_sorting.job_listings member
 WHERE {alias}.canonical_job_id IS NOT NULL AND member.canonical_job_id={alias}.canonical_job_id
 AND {member_predicate}),ARRAY[{alias}.board]))"""



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


def _context_metadata(conn, rows):
    """Load display fields for the chosen context rows without changing their order."""
    if not rows:
        return rows
    projection = META.replace(
        "board_facts", "jsonb_build_object('posted_date',board_facts->'posted_date') board_facts"
    )
    found = read_rows(
        conn,
        f"SELECT {projection} FROM job_sorting.job_listings WHERE id=ANY(%s)",
        ([row["id"] for row in rows],),
    )
    by_id = {row["id"]: row for row in found}
    return [{**by_id[row["id"]], **row} for row in rows]


def listing_context(conn, selected, parameters, *, bounded_context=False):
    """Read wording history, copies, other company jobs and the requirement assessment."""
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
            # Other boards or markets only; reposts on this board are under Times seen.
            f"WITH current AS({CURRENT}) SELECT {META} FROM current WHERE canonical_job_id=%s AND (board,market)<>(%s,%s) ORDER BY listing_first_seen_at,id",
            (group, selected["board"], selected["market"]),
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
        )
        if group is not None
        else []
    )
    # Times seen lists this listing, then each board's latest listing, then the
    # earlier posts: a board's other listings, most recently seen first.
    last_seen = lambda c: (c["wording_last_seen_at"], c["listing_first_seen_at"], c["id"])
    latest = {}
    for copy in listing_copies:
        board = (copy["board"], copy["market"])
        if board not in latest or last_seen(copy) > last_seen(latest[board]):
            latest[board] = copy
    for copy in listing_copies:
        copy["copy_kind"] = (
            "this"
            if (copy["board"], copy["market"], copy["board_job_id"]) == identity
            else "other"
            if latest[(copy["board"], copy["market"])] is copy
            else "earlier"
        )
    listing_copies.sort(
        key=lambda c: (
            ("this", "other", "earlier").index(c["copy_kind"]),
            -c["wording_last_seen_at"].timestamp() if c["copy_kind"] == "earlier" else 0,
        )
    )
    times = len(listing_copies) if group is not None else None
    advertiser = selected.get("advertiser_name")
    # Include every employer name used by this job's copies across boards.
    advertiser_names = sorted(
        {c["advertiser_name"] for c in [selected, *listing_copies] if c.get("advertiser_name")}
    )
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
        possible_columns = "id,title" if bounded_context else META
        possible = read_rows(
            conn,
            f"WITH current AS({CURRENT}) SELECT {possible_columns} FROM current WHERE advertiser_name=ANY(%s) AND board<>%s AND (canonical_job_id IS NULL OR %s::text IS NULL OR canonical_job_id<>%s) AND listing_first_seen_at>=%s",
            (
                advertiser_names,
                selected["board"],
                group,
                group,
                datetime.now(timezone.utc) - timedelta(days=30),
            ),
        )
        possible = [
            r
            for r in possible
            if title_key(selected.get("title"))
            and title_key(r.get("title")) == title_key(selected.get("title"))
        ]
        advertiser_columns = (
            "id,bucket,job_first_seen_at,boards"
            if bounded_context
            else META + ",job_first_seen_at,boards"
        )
        rows = read_rows(
            conn,
            group_query("ADVERTISER_GROUPS")
            # Any copy carrying one of the names qualifies a group, whatever its
            # representative's board calls the employer.
            + f"SELECT {advertiser_columns} FROM grouped WHERE group_key<>%s ORDER BY job_first_seen_at DESC,id DESC",
            (advertiser_names, group or "listing:" + json.dumps(identity)),
        )
        matches = lambda r, w, mode=scope: (
            (w == "all" or r["job_first_seen_at"] >= context_now - timedelta(days=int(w)))
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
        advertiser_rows = [r for r in rows if matches(r, days)][:200]
        if bounded_context:
            possible = _context_metadata(conn, possible)
            advertiser_rows = _context_metadata(conn, advertiser_rows)
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
        "advertiser_names": advertiser_names,
        "advertiser_windows": windows,
        "advertiser_mode_counts": mode_counts,
        "advertiser_scope": scope,
        "advertiser_days": days,
        "assessment": assessment,
        # Against also names these listing facts; a wording changed since it was
        # read depends on live Review state, so the panel adds that one.
        "context_against": [
            label
            for label, shown in (
                ("Recruitment firm posting", selected.get("is_recruitment_firm")),
                ("Closed on board", selected.get("closed_on_board") is True),
            )
            if shown
        ],
    }


def wording_diff(conn, first, second):
    """Render escaped changed lines between two stored job wordings."""
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
    # Only changed lines, each removed line before the line that replaced it.
    parts = []
    for value in difflib.ndiff(
        text(mapping[first]).splitlines(), text(mapping[second]).splitlines()
    ):
        if value[:2] not in ("- ", "+ ") or not value[2:].strip():
            continue
        tone, sign = ("removed", "−") if value[0] == "-" else ("added", "+")
        parts.append(
            f'<p class="diff-{tone}"><span>{sign}</span><span>{html.escape(value[2:])}</span></p>'
        )
    return (
        '<div class="diff">'
        + ("".join(parts) or '<p class="diff-same">The wording is the same.</p>')
        + "</div>"
    )


def review_selection(conn, identifier, parameters, *, bounded_context=False):
    """Load one authorised listing and its context without rebuilding the queue."""
    try:
        identifier = int(identifier)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid listing selection") from error
    found = read_rows(conn, JOIN_RESULTS + " WHERE j.id=%s", (identifier,))
    selected = found[0] if found else None
    return (
        dict(
            selected=selected,
            **listing_context(conn, selected, parameters, bounded_context=bounded_context),
        )
        if selected
        else {"selected": None}
    )


def resolved_review_states(states):
    """Current listing identities override stale or historical wording group IDs."""
    result = {} if all("members" in value for value in states.values()) else dict(states)
    for value in states.values():
        for identity in value.get("members", []):
            result[tuple(identity)] = value
    return result


def display_review_state(job, states):
    identity = (job["board"], job["market"], job["board_job_id"])
    value = states.get(identity, states.get(review_group_key(job), {}))
    return {field: value.get(field) for field in REVIEW_TIMESTAMPS}


def review_queue_state(base, parameters, controls, states):
    """Apply live read state after ordering and grouping the jobs.

    Company positions use the complete filtered list, including jobs already read."""
    show = parameters.get("show", "unread")
    if show not in {"unread", "read", "all"}:
        raise ValueError("Unknown read filter")
    is_read = lambda job: (
        states.get(
            (job["board"], job["market"], job["board_job_id"]),
            states.get(review_group_key(job), {}),
        ).get("reviewed_at")
        is not None
    )
    read_count = sum(is_read(job) for job in base)
    size = review_table.page_size(parameters)
    retained = review_table.kept_ids(parameters)
    base = review_table.sort_rows(
        base, parameters, lambda job: display_review_state(job, states), BOARD_LABELS
    )
    visible = [
        job
        for job in base
        if show == "all" or is_read(job) == (show == "read") or job["id"] in retained
    ]
    first_unread_id = next(
        (job["id"] for job in visible if not is_read(job)), visible[0]["id"] if visible else None
    )
    selected_id = parameters.get("id")
    if selected_id is not None:
        try:
            selected_id = int(selected_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid listing selection") from error
    elif parameters.get("view") != "table":
        selected_id = first_unread_id
    try:
        page = max(1, int(parameters.get("page", 1)))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid page") from error
    ids = [job["id"] for job in visible]
    index = ids.index(selected_id) if selected_id in ids else -1
    if index >= 0 and parameters.get("view") != "table":
        page = index // size + 1
    page = min(page, max(1, (len(visible) + size - 1) // size))
    jobs = [
        {**job, "review_state": display_review_state(job, states)}
        for job in visible[(page - 1) * size : page * size]
    ]
    base_index = next((i for i, job in enumerate(base) if job["id"] == selected_id), -1)
    advertiser = None
    if index >= 0 and base_index >= 0:
        selected = base[base_index]
        if selected.get("advertiser_name") and selected.get("bucket") in {
            "top_picks",
            "in_lane",
            "less_interested",
        }:
            siblings = [
                job["id"]
                for job in base
                if job.get("advertiser_name") == selected["advertiser_name"]
                and job.get("bucket") in {"top_picks", "in_lane", "less_interested"}
            ]
            if len(siblings) > 1:
                advertiser = {
                    "name": selected["advertiser_name"],
                    "position": siblings.index(selected_id) + 1,
                    "total": len(siblings),
                }
    if index >= 0:
        previous_id = ids[index - 1] if index else None
        next_id = ids[index + 1] if index + 1 < len(ids) else None
    elif base_index >= 0:
        visible_ids = set(ids)
        previous_id = next(
            (job["id"] for job in reversed(base[:base_index]) if job["id"] in visible_ids), None
        )
        next_id = next(
            (job["id"] for job in base[base_index + 1 :] if job["id"] in visible_ids), None
        )
    else:
        previous_id, next_id = None, ids[0] if ids else None
    if index >= 0:
        controls["_prefetch_ids"] = ids[index + 1 : index + 11]
    elif base_index >= 0:
        wanted = set(ids)
        controls["_prefetch_ids"] = [
            job["id"] for job in base[base_index + 1 :] if job["id"] in wanted
        ][:10]
    else:
        controls["_prefetch_ids"] = ids[:10]
    controls["review"] = {
        "first_unread_id": first_unread_id,
        "show": show,
        "counts": {"unread": len(base) - read_count, "read": read_count, "all": len(base)},
        "to_go": len(base) - read_count,
        "selected_id": selected_id,
        "position": index + 1 if index >= 0 else None,
        "total": len(ids),
        "next_id": next_id,
        "previous_id": previous_id,
        "advertiser": advertiser,
        "ids": [job["id"] for job in jobs],
        "page": page,
        "pages": max(1, (len(ids) + size - 1) // size),
        "size": size,
    }
    return jobs, len(visible), page


def review_state_page(conn, parameters, states):
    """SQL fallback with live state; only the requested bounded window leaves PostgreSQL."""
    controls = review_filter_state(parameters, validate_markets=False)
    controls["query_dates"] = True
    size = review_table.page_size(parameters)
    retained = review_table.kept_ids(parameters)
    table_order = review_table.sql_order(parameters, BOARD_LABELS)
    groups, values = _review_groups(parameters)
    defer_context = not parameters.get("advertiser") and parameters.get("all") != "1"
    if defer_context:
        groups = grouped_query(queue=True, population=True, defer_context=True)
    show = parameters.get("show", "unread")
    if show not in {"unread", "read", "all"}:
        raise ValueError("Unknown read filter")
    read_keys = [key for key, value in states.items() if value["reviewed_at"] is not None]
    fragments, notice = [], None
    for name, skip, projection, group in [
        ("population", None, "g.*", ""),
        ("market_counts", "market", "g.market key,count(*) n", " GROUP BY g.market"),
        (
            "bucket_counts",
            "bucket",
            "coalesce(g.bucket,'not_sorted') key,count(*) n",
            " GROUP BY coalesce(g.bucket,'not_sorted')",
        ),
        (
            "board_counts",
            "board",
            "g.market||':'||g.board key,count(*) n",
            " GROUP BY g.market,g.board",
        ),
    ]:
        source, where, arguments, warning = _review_where(parameters, controls, skip=skip)
        if skip is not None and show != "all":
            read = "coalesce(g.canonical_job_id,'listing:'||jsonb_build_array(g.board,g.market,g.board_job_id)::text)=ANY(%s)"
            where += f" AND {'NOT ' if show == 'unread' else ''}({read})"
            arguments.append(read_keys)
        fragments.append(f"{name} AS(SELECT {projection} FROM {source} WHERE {where}{group})")
        values.extend(arguments)
        notice = notice or warning
    selected_id = int(parameters["id"]) if parameters.get("id") else None
    requested_page = max(1, int(parameters.get("page", 1)))
    order = _ordered_population("population g", "TRUE")
    predicate = "TRUE" if show == "all" else "has_read" if show == "read" else "NOT has_read"
    star_keys = [key for key, value in states.items() if value.get("starred_at") is not None]
    page_json = "to_jsonb(c)-'leader'-'position'-'ordinal'-'has_read'-'has_star'"
    if defer_context:
        page_json = _page_metadata_json("c", page_json)
    query = (
        groups
        + ","
        + ",".join(fragments)
        + f""",
 ordered AS(WITH selected AS(SELECT * FROM population){order}),
 annotated AS(SELECT *,coalesce(canonical_job_id,'listing:'||jsonb_build_array(board,market,board_job_id)::text)=ANY(%s) has_read,coalesce(canonical_job_id,'listing:'||jsonb_build_array(board,market,board_job_id)::text)=ANY(%s) has_star FROM ordered),
 visible AS(SELECT *,row_number() OVER(ORDER BY {table_order}) ordinal FROM annotated WHERE ({predicate}) OR id=ANY(%s)),
 selection AS(SELECT coalesce(%s::bigint,(SELECT id FROM visible ORDER BY has_read,ordinal LIMIT 1)) id),
 tally AS(SELECT count(*) total FROM visible),
 effective AS(SELECT total,least(greatest(1,(total+{size - 1})/{size}),coalesce(
     (SELECT (ordinal-1)/{size}+1 FROM visible,selection WHERE visible.id=selection.id AND %s),%s)) page FROM tally),
 chosen AS(SELECT * FROM visible WHERE ordinal>(SELECT (page-1)*{size} FROM effective)
     AND ordinal<=(SELECT page*{size} FROM effective)),
 selected_base AS(SELECT annotated.* FROM annotated,selection WHERE annotated.id=selection.id)
 SELECT total,page,coalesce((SELECT jsonb_agg({page_json} ORDER BY ordinal) FROM chosen c),'[]'),
 (SELECT count(*) FROM annotated),(SELECT count(*) FROM annotated WHERE has_read),
 (SELECT id FROM selection),(SELECT ordinal FROM visible WHERE id=(SELECT id FROM selection)),
 (SELECT id FROM visible WHERE (leader,position)>(SELECT leader,position FROM selected_base) ORDER BY ordinal LIMIT 1),
 (SELECT id FROM visible WHERE (leader,position)<(SELECT leader,position FROM selected_base) ORDER BY ordinal DESC LIMIT 1),
 (SELECT jsonb_build_object('name',max(a.advertiser_name),'total',count(*),
     'position',count(*) FILTER(WHERE (a.leader,a.position)<=(SELECT leader,position FROM selected_base)))
  FROM annotated a WHERE a.bucket IN ('top_picks','in_lane','less_interested')
  AND a.advertiser_name=(SELECT advertiser_name FROM selected_base)),
 (SELECT jsonb_build_object('latest',latest,'dates',dates,'loaded',loaded,'board_pairs',board_pairs) FROM availability),
 coalesce((SELECT jsonb_object_agg(key,n) FROM market_counts),'{{}}'),
 coalesce((SELECT jsonb_object_agg(key,n) FROM bucket_counts),'{{}}'),
 coalesce((SELECT jsonb_object_agg(key,n) FROM board_counts),'{{}}'),
 (SELECT id FROM visible ORDER BY has_read,ordinal LIMIT 1) FROM effective"""
    )
    values.extend([read_keys, star_keys])
    if review_table.sort_choice(parameters)[0] == "read":
        values.extend([list(retained), show == "read"])
    values.extend([list(retained), selected_id, parameters.get("view") != "table", requested_page])
    (
        total,
        page,
        jobs,
        base_count,
        read_count,
        selected_id,
        position,
        next_id,
        previous_id,
        advertiser,
        meta,
        markets,
        buckets,
        boards,
        first_unread_id,
    ) = conn.execute(query, values).fetchone()
    latest = datetime.fromisoformat(meta["latest"]) if meta["latest"] else None
    pairs = meta["board_pairs"] or []
    available = (*MARKETS, *sorted({pair.split(":")[0] for pair in pairs} - set(MARKETS)))
    controls = review_filter_state(
        parameters,
        latest_night=night(latest) if latest else None,
        older_nights=[date.fromisoformat(d) for d in meta["dates"] or []],
        available_markets=available,
        board_pairs=pairs,
    )
    for job in jobs:
        for field in ("listing_first_seen_at", "wording_first_seen_at", "wording_last_seen_at"):
            if job[field]:
                job[field] = datetime.fromisoformat(job[field])
        job["review_state"] = display_review_state(job, states)
    selected_bucket = next((job["bucket"] for job in jobs if job["id"] == selected_id), None)
    if (
        not position
        or selected_bucket not in {"top_picks", "in_lane", "less_interested"}
        or not advertiser
        or advertiser["total"] < 2
    ):
        advertiser = None
    controls.update(
        loaded=meta["loaded"],
        board_pairs=pairs,
        market_counts={m: markets.get(m, 0) for m in available},
        bucket_counts={b: buckets.get(b, 0) for b in BUCKETS},
        board_counts=boards,
    )
    controls["out_of_scope_count"] = sum(controls["bucket_counts"][b] for b in OUT_OF_SCOPE)
    controls["review"] = {
        "first_unread_id": first_unread_id,
        "show": show,
        "counts": {"all": base_count, "read": read_count, "unread": base_count - read_count},
        "to_go": base_count - read_count,
        "selected_id": selected_id,
        "position": position,
        "total": total,
        "next_id": next_id,
        "previous_id": previous_id,
        "advertiser": advertiser,
        "ids": [job["id"] for job in jobs],
        "page": page,
        "pages": max(1, (total + size - 1) // size),
        "size": size,
    }
    context = (
        review_selection(conn, selected_id, parameters, bounded_context=True)
        if selected_id and parameters.get("view") != "table"
        else {"selected": None}
    )
    if context.get("selected"):
        context["selected"] = {
            **context["selected"],
            "review_state": display_review_state(
                context["selected"], resolved_review_states(states)
            ),
        }
    return dict(
        jobs=jobs,
        total=total,
        page=page,
        pages=max(1, (total + size - 1) // size),
        loaded=meta["loaded"],
        filters=controls,
        error=notice,
        **context,
    )
