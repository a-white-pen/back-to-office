"""Table presentation: bounded windows, stable read membership and column sorting."""

import re

TABLE_BUCKET_ORDER = (
    "top_picks",
    "in_lane",
    "less_interested",
    "ineligible",
    "not_sorted",
    "title_filter",
    "non_tech",
    "tech_not_for_me",
)

SIZES = (25, 50, 75, 100)
COLUMNS = (
    ("read", "Read", "Filled dot: unread. Hollow: you have read it. Click the dot to switch."),
    ("title", "Title", ""),
    ("advertiser", "Advertiser", ""),
    (
        "agency",
        "Recruitment firm",
        "Yes when the board flags it (MyCareersFuture) or the detail read judged the advertiser a recruitment firm.",
    ),
    (
        "bucket",
        "Bucket",
        "Where the job landed, and what decided it: rules or triage. Blank when triage gave no result.",
    ),
    ("reason", "Reason", "The one line that decided the bucket. Same line as on the job page."),
    (
        "salary",
        "Salary",
        "Salary as the board gives it. Blank when the board shows none. Sorts by the first number.",
    ),
    ("market", "Market", ""),
    ("board", "Board", ""),
    (
        "first",
        "First seen",
        "The night BTO first saw this board listing, whatever wording it had then. A later wording change does not reset it. Not the board's posted date.",
    ),
    ("star", "★", ""),
)


def page_size(parameters):
    """Use a 50-job navigation window, or one of the four table page sizes."""
    if parameters.get("view") != "table":
        return 50
    try:
        size = int(parameters.get("size", 25))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid rows per page") from error
    if size not in SIZES:
        raise ValueError("Rows per page must be 25, 50, 75 or 100")
    return size


def kept_ids(parameters):
    """Only membership is retained in the URL; owner state always comes from PostgreSQL."""
    if parameters.get("view") != "table" or parameters.get("show") == "all":
        return set()
    value = parameters.get("table_keep", "")
    if not value:
        return set()
    if len(value) > 16000 or not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
        raise ValueError("Invalid table membership")
    return {int(identifier) for identifier in value.split(",")}


def sort_choice(parameters):
    key = parameters.get("sort", "") if parameters.get("view") == "table" else ""
    direction = parameters.get("direction", "asc")
    if key and key not in {column[0] for column in COLUMNS}:
        raise ValueError("Unknown table column")
    if direction not in {"asc", "desc"}:
        raise ValueError("Unknown sort direction")
    return key, direction


def sort_rows(rows, parameters, state_for, board_labels=None):
    """Apply the chosen table order using current read and star state."""
    key, direction = sort_choice(parameters)
    if not key:
        return rows
    retained = kept_ids(parameters)

    def value(job):
        if key == "read":
            return (
                parameters.get("show", "unread") == "read"
                if job["id"] in retained
                else bool(state_for(job).get("reviewed_at"))
            )
        if key == "star":
            return 0 if state_for(job).get("starred_at") else 1
        if key == "agency":
            return 0 if job.get("is_recruitment_firm") else 1
        if key == "bucket":
            return (
                TABLE_BUCKET_ORDER.index(job["bucket"])
                if job.get("bucket") in TABLE_BUCKET_ORDER
                else len(TABLE_BUCKET_ORDER)
            )
        if key == "salary":
            match = re.search(r"\d+(?:\.\d+)?", (job.get("salary_raw") or "").replace(",", ""))
            return (
                float(match[0])
                if match
                else float(job.get("salary_min") or job.get("salary_max") or -1)
            )
        if key == "first":
            return (
                job["listing_first_seen_at"].timestamp() if job.get("listing_first_seen_at") else 0
            )
        if key == "board":
            return (board_labels or {}).get(job.get("board"), job.get("board") or "").lower()
        if key == "reason":
            # A missing reason shows "—" and sorts after every written reason.
            return not job.get("reason"), (job.get("reason") or "").lower()
        field = {"advertiser": "advertiser_name"}.get(key, key)
        return str(job.get(field) or "").lower()

    return sorted(rows, key=value, reverse=direction == "desc")


def sql_order(parameters, board_labels=None):
    """Whitelist every SQL fragment; URL values never become SQL identifiers."""
    key, direction = sort_choice(parameters)
    if not key:
        return "leader,position"
    expressions = {
        "read": "CASE WHEN id=ANY(%s) THEN %s ELSE has_read END",
        "star": "CASE WHEN has_star THEN 0 ELSE 1 END",
        "title": "lower(coalesce(title,''))",
        "advertiser": "lower(coalesce(advertiser_name,''))",
        "agency": "CASE WHEN is_recruitment_firm THEN 0 ELSE 1 END",
        "bucket": "CASE coalesce(bucket,'not_sorted') "
        + " ".join(
            f"WHEN '{bucket}' THEN {index}" for index, bucket in enumerate(TABLE_BUCKET_ORDER)
        )
        + " END",
        "salary": "coalesce(substring(replace(salary_raw,',','') from '[0-9]+(?:\\.[0-9]+)?')::numeric,salary_min,salary_max,-1)",
        "market": "market",
        "board": "board",
        "first": "listing_first_seen_at",
    }
    if key == "board" and board_labels:
        expressions[key] = (
            "CASE board "
            + " ".join(
                "WHEN '"
                + board.replace("'", "''")
                + "' THEN '"
                + label.replace("'", "''").lower()
                + "'"
                for board, label in board_labels.items()
            )
            + " ELSE board END"
        )
    order = " DESC" if direction == "desc" else " ASC"
    if key == "reason":
        # As in sort_rows: a missing reason sorts after every written reason.
        return f"(coalesce(reason,'')=''){order},lower(coalesce(reason,'')){order},leader,position"
    return expressions[key] + order + ",leader,position"


def presentation(parameters, page, total):
    """Build page links, row ranges and column settings for the table."""
    size = page_size(parameters)
    pages = max(1, (total + size - 1) // size)
    numbers = (
        list(range(1, pages + 1))
        if pages <= 7
        else sorted({1, pages, *range(max(1, page - 1), min(pages, page + 1) + 1)})
    )
    buttons = []
    for number in numbers:
        if buttons and number > buttons[-1] + 1:
            buttons.append(0)
        buttons.append(number)
    key, direction = sort_choice(parameters)
    return {
        "size": size,
        "sizes": SIZES,
        "numbers": buttons,
        "columns": COLUMNS,
        "sort": key,
        "direction": direction,
        "first": (page - 1) * size + 1 if total else 0,
        "last": min(page * size, total),
    }
