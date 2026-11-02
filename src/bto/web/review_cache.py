"""Cache Review metadata and immutable listing details in the web process.

Queue snapshots are replaced whole and saved privately for restart recovery.
Mutable owner state is read separately and is never persisted in this snapshot.
"""

import hashlib
import json
import logging
import os
import pickle
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bto.filter_and_bucket_job_listings import BUCKETS
from bto.log_errors import log_failure
from bto.storage.postgres.connection import connect
from bto.web import review_job_listings as review
from bto.web import review_table
from bto.web.review_cache_memory import (
    MIB,
    ByteLRU,
    ObjectIds,
    memory_pressure,
    memory_usage,
    object_bytes,
)

log = logging.getLogger(__name__)

SNAPSHOT_FORMAT = 2
HOT_MAX_BYTES = 96 * MIB
HOT_NIGHTS = 7
RETRY_SECONDS = 300
SOURCE_CHECK_SECONDS = 300
BUILD_BATCH = 256
BUILD_TIMEOUT_S = 900
DETAIL_TIMEOUT_S = 20
PAGE_SIZE = 50
META = review.META.split(",")
HOT_BUCKETS = ("top_picks", "in_lane", "less_interested")

# Only fields used by queue/filter/card serving; full JDs and owner state stay out.
FIELDS = (
    "id",
    "board",
    "market",
    "board_job_id",
    "title",
    "advertiser_name",
    "hiring_company_name",
    "bucket",
    "reason",
    "decided_by",
    "listing_first_seen_at",
    "wording_first_seen_at",
    "wording_last_seen_at",
    "canonical_job_id",
    "fingerprint",
    "repeat_of_id",
    "job_url",
    "salary_raw",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "board_facts",
    "boards",
    "bucket_rank",
    "is_recruitment_firm",
    "is_public_sector",
    "job_country_code",
    "years_over_comfortable",
    "lacks_count",
    "preferred_held",
)
_SELECT = ",".join("cardinality(g.lacks)" if f == "lacks_count" else f"g.{f}" for f in FIELDS)
def build_sql():
    return review.group_query("QUEUE_GROUPS") + f"SELECT {_SELECT} FROM grouped g"


def contract(version=SNAPSHOT_FORMAT):
    return hashlib.sha256(
        json.dumps([version, FIELDS, review.group_query("QUEUE_GROUPS")]).encode()
    ).hexdigest()


def __getattr__(name):
    if name == "BUILD_SQL":
        return build_sql()
    if name == "CONTRACT":
        return contract()
    if name == "LEGACY_CONTRACT":
        return contract(1)
    raise AttributeError(name)


class SnapshotTooLarge(RuntimeError):
    """The complete population cannot be published within its retained-object budget."""


class Snapshot:
    """Hold one complete publication and its precomputed filter and ordering keys."""

    def __init__(
        self,
        *,
        latest,
        older,
        loaded,
        rows,
        leader_advertisers,
        leader_mcf_companies,
        built_at,
        build_seconds,
        board_pairs=(),
        database=None,
        source_published_at=None,
        coverage_since=None,
        coverage_until=None,
    ):
        self.format = SNAPSHOT_FORMAT
        self.contract = contract()
        self.database = database
        self.source_published_at = source_published_at
        self.coverage_since, self.coverage_until = coverage_since, coverage_until
        self.latest, self.older, self.loaded = latest, older, loaded
        self.board_pairs = sorted(board_pairs)
        self.available_markets = (
            *review.MARKETS,
            *sorted({p.split(":")[0] for p in self.board_pairs} - set(review.MARKETS)),
        )
        self.rows = rows
        self.leader_advertisers = frozenset(leader_advertisers)
        self.leader_mcf_companies = frozenset(leader_mcf_companies)
        for g in rows:
            prepare_row(g, self)
        self.built_at, self.build_seconds = built_at, build_seconds
        self.retained_bytes = 0

    def summary(self):
        last = review._night_start(self.latest) if self.latest else None
        hot = [r for r in self.rows if r["bucket"] in HOT_BUCKETS]
        return {
            "built_at": self.built_at.isoformat(),
            "latest_night": str(self.latest),
            "retained_bytes": self.retained_bytes,
            "groups": len(self.rows),
            "coverage_since": self.coverage_since.isoformat() if self.coverage_since else None,
            "coverage_until": self.coverage_until.isoformat() if self.coverage_until else None,
            "loaded": self.loaded,
            "P0_groups": sum(1 for r in hot if r["listing_first_seen_at"] >= last) if last else 0,
            "P1_groups": len(hot),
            "P2_groups": len(self.rows),
            "P0_listings": sum(
                len(r["boards"]) for r in hot if last and r["listing_first_seen_at"] >= last
            ),
            "P1_listings": sum(len(r["boards"]) for r in hot),
            "P2_listings": sum(len(r["boards"]) for r in self.rows),
        }


def prepare_row(g, snapshot):
    """Freeze metadata-derived keys; serving never mutates snapshot rows."""
    g["_bucket"] = g["bucket"] or "not_sorted"
    g["_order"] = _order_key(g)
    g["_company"] = _company_key(g) or f"listing:{g['id']}"
    g["_default"] = _default_scope(g, snapshot)
    g["_text"] = tuple(
        (g[k] or "").lower() for k in ("title", "advertiser_name", "hiring_company_name")
    )


def database_identity(conn):
    return tuple(
        conn.execute(
            "SELECT current_database(),oid FROM pg_database WHERE datname=current_database()"
        ).fetchone()
    )


def validate(snapshot, database=None, *, legacy=False):
    """Check the saved format, database identity and retained-memory budget."""
    compatible = (
        legacy and getattr(snapshot, "format", None) == 1
        and getattr(snapshot, "contract", None) == contract(1)
    )
    if (
        not isinstance(snapshot, Snapshot)
        or not (compatible or (getattr(snapshot, "format", None) == SNAPSHOT_FORMAT
                               and getattr(snapshot, "contract", None) == contract()))
    ):
        raise ValueError("Saved Review snapshot is incompatible")
    if database is not None and snapshot.database != database:
        raise ValueError("Review snapshot belongs to a different database")
    snapshot.retained_bytes = object_bytes(snapshot, ObjectIds()) + 64
    if snapshot.retained_bytes > (200 * MIB if compatible else HOT_MAX_BYTES):
        raise SnapshotTooLarge(
            f"Complete Review snapshot needs {snapshot.retained_bytes} bytes; maximum is {HOT_MAX_BYTES}"
        )
    return snapshot


def build(conn_factory=connect, *, source_published_at=None):
    """One coherent read-only stream; never writes or takes the sorting lock."""
    started = time.perf_counter()
    with conn_factory() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{BUILD_TIMEOUT_S}s'")
        # This host also runs sorting and PostgreSQL: LLVM compilation and
        # parallel executor copies must not consume the snapshot's headroom.
        conn.execute("SET LOCAL jit = off")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        conn.execute("SET LOCAL work_mem = '1MB'")
        conn.execute("SET LOCAL hash_mem_multiplier = 1")
        snapshot = read_snapshot(conn, started)
    snapshot.source_published_at = source_published_at
    return validate(snapshot)


def read_snapshot(conn, started=None):
    """Accumulate final metadata with bounded driver batches, without a JSON aggregate."""
    started = started or time.perf_counter()
    latest, dates, loaded, pairs = conn.execute(
        "WITH "
        + review.availability_query()
        + " SELECT latest,dates,loaded,board_pairs FROM availability"
    ).fetchone()
    advertisers, companies = conn.execute(
        review.QUEUE_GROUPS
        + """SELECT
        array_agg(DISTINCT advertiser_name) FILTER(WHERE is_recruitment_firm IS NOT TRUE AND advertiser_name IS NOT NULL),
        array_agg(DISTINCT hiring_company_name) FILTER(WHERE board='mcf' AND hiring_company_name IS NOT NULL)
        FROM company_leaders"""
    ).fetchone()
    snapshot = Snapshot(
        latest=review.night(latest) if latest else None,
        older=sorted(dates or [], reverse=True),
        loaded=loaded,
        rows=[],
        board_pairs=pairs or [],
        leader_advertisers=advertisers or [],
        leader_mcf_companies=companies or [],
        built_at=datetime.now(timezone.utc),
        build_seconds=0,
        database=database_identity(conn),
    )
    if latest is not None:
        nights = sorted({snapshot.latest, *(d for d in (dates or []) if d <= snapshot.latest)}, reverse=True)
        snapshot.coverage_since = review._night_start(nights[:HOT_NIGHTS][-1])
        snapshot.coverage_until = review._night_start(snapshot.latest + timedelta(days=1))
    # Keep exact deduplication without a boxed integer/set entry for every value.
    seen = ObjectIds()
    charged = object_bytes(snapshot, seen)
    if latest is not None:
        with conn.cursor(name="review_metadata") as cursor:
            cursor.itersize = BUILD_BATCH
            cursor.execute(
                build_sql() + " WHERE g.listing_first_seen_at >= %s AND g.listing_first_seen_at < %s"
                " ORDER BY g.listing_first_seen_at DESC,g.id DESC",
                (snapshot.coverage_since, snapshot.coverage_until),
            )
            current_night, night_start, stopped = None, 0, False
            while batch := cursor.fetchmany(BUILD_BATCH):
                usage = memory_usage()
                if usage and (usage[0] > 240 * MIB or 0 < usage[1] < 160 * MIB):
                    # Stop before spending the host reserve; SQL covers excluded nights.
                    del snapshot.rows[night_start:]
                    snapshot.coverage_since = review._night_start(
                        (current_night or snapshot.latest) + timedelta(days=1)
                    )
                    log.info("Review candidate narrowed for headroom process_host_bytes=%s", usage)
                    break
                for values in batch:
                    row = dict(zip(FIELDS, values))
                    row_night = review.night(row["listing_first_seen_at"])
                    if row_night != current_night:
                        current_night, night_start = row_night, len(snapshot.rows)
                    # Keep numeric values consistent between database and snapshot results.
                    for field in ("salary_min", "salary_max", "years_over_comfortable"):
                        if row.get(field) is not None:
                            row[field] = float(row[field])
                    prepare_row(row, snapshot)
                    snapshot.rows.append(row)
                    charged += object_bytes(row, seen) + 8
                    if charged > HOT_MAX_BYTES - MIB:
                        # Never publish half a night: its requests must use SQL in full.
                        del snapshot.rows[night_start:]
                        snapshot.coverage_since = review._night_start(row_night + timedelta(days=1))
                        stopped = True
                        break
                if stopped:
                    break
    del seen
    snapshot.build_seconds = round(time.perf_counter() - started, 1)
    return validate(snapshot)


# ------------------------------------------------------------------ serving


def cacheable(parameters, state, snapshot):
    """Use the snapshot only when it covers every requested collection night."""
    if (
        parameters.get("all") == "1"
        or parameters.get("advertiser")
        or parameters.get("description")
    ):
        return False
    if getattr(snapshot, "format", None) == 1:
        return True  # Retained snapshots remain usable during rollback.
    lower, upper = snapshot.coverage_since, snapshot.coverage_until
    ranges = state.get("ranges", ())
    return bool(lower and upper and ranges and all(lower <= a < b <= upper for a, b in ranges))


def _default_scope(g, snapshot):
    """review_job_listings.DEFAULT_SCOPE for one grouped job."""
    bucket = g["bucket"] or "not_sorted"
    return bucket in ("top_picks", "in_lane", "not_sorted") or (
        g["bucket"] == "less_interested"
        and (
            (
                g["is_recruitment_firm"] is not True
                and g["advertiser_name"] in snapshot.leader_advertisers
            )
            or (
                g["board"] == "mcf"
                and g["hiring_company_name"] is not None
                and g["hiring_company_name"] in snapshot.leader_mcf_companies
            )
        )
    )


def _other_filters(parameters, state):
    """Every _review_where filter except market and bucket, as one predicate."""
    ranges = state["ranges"]
    q = parameters.get("q", "").strip().lower()
    title = (parameters.get("title") or "").lower()
    exclude = (parameters.get("exclude") or "").lower()
    company = (parameters.get("company") or "").lower()
    posted_by = parameters.get("posted_by")
    unavailable = state["date_unavailable"]

    def keep(g):
        if unavailable:
            return False
        if ranges and not any(a <= g["listing_first_seen_at"] < b for a, b in ranges):
            return False
        text = g["_text"]
        if q and not (q in text[0] or q in text[1] or q in text[2]):
            return False
        if (title and title not in text[0]) or (exclude and exclude in text[0]):
            return False
        if company and company not in text[1]:
            return False
        if posted_by == "recruitment" and g["is_recruitment_firm"] is not True:
            return False
        return not (posted_by == "employer" and g["is_recruitment_firm"] is not False)

    return keep


def _last(value):
    """SQL ASC NULLS LAST."""
    return (value is None, value if value is not None else 0)


def _order_key(g):
    """review_job_listings._ordered_population's ORDER BY, in Python."""
    bucket = g["bucket"]
    years = g["years_over_comfortable"]
    if g["is_recruitment_firm"]:
        source = 2
    elif g["is_public_sector"] and (g["job_country_code"] or g["market"] or "").upper() == "SG":
        source = 1
    else:
        source = 0
    lacks = g["lacks_count"] if bucket == "in_lane" else None
    preferred = g["preferred_held"] if bucket in HOT_BUCKETS else None
    return (
        g["bucket_rank"],
        source,
        _last(abs(years) if bucket == "top_picks" and years is not None else None),
        _last(lacks),
        _last((0 if years is not None and years > 0 else 1) if bucket == "in_lane" else None),
        _last(years if bucket == "in_lane" and years is not None and years > 0 else None),
        (preferred is None, -(preferred or 0)),
        -g["listing_first_seen_at"].timestamp(),
        -g["id"],
    )


def _company_key(g):
    if g["bucket"] not in HOT_BUCKETS:
        return None
    if g["is_recruitment_firm"]:
        return g["hiring_company_name"] if g["board"] == "mcf" else None
    return g["advertiser_name"]


def queue(parameters, state, snapshot, states=None):
    """(page of jobs, total, page number) and facet counts, from memory, in one pass."""
    keep = _other_filters(parameters, state)
    markets_wanted = set(state["markets"])
    every_market = state.get("markets_all")
    buckets_wanted = set(state["buckets"])
    default = state["default_scope"]
    boards_off = set(state.get("boards_off") or [])
    board = (parameters.get("board") or "").lower() if not boards_off else ""
    show = parameters.get("show", "unread")
    states = review.resolved_review_states(states) if states is not None else None
    rows, markets, buckets, boards = [], {}, {}, {}
    for g in snapshot.rows:
        if not keep(g):
            continue
        pair = g["market"] + ":" + g["board"]
        market_ok = every_market or g["market"] in markets_wanted
        bucket_ok = g["_default"] if default else g["_bucket"] in buckets_wanted
        board_ok = pair not in boards_off if boards_off else (not board or g["board"] == board)
        show_ok = (
            states is None
            or show == "all"
            or (review.display_review_state(g, states)["reviewed_at"] is not None)
            == (show == "read")
        )
        # Each facet ignores its own dimension, but always respects live Show state.
        if show_ok:
            if bucket_ok and board_ok:
                markets[g["market"]] = markets.get(g["market"], 0) + 1
            if market_ok and board_ok:
                buckets[g["_bucket"]] = buckets.get(g["_bucket"], 0) + 1
            if market_ok and bucket_ok:
                boards[pair] = boards.get(pair, 0) + 1
        if market_ok and bucket_ok and board_ok:
            rows.append(g)
    rows.sort(key=lambda g: g["_order"])
    leaders = {}
    for position, g in enumerate(rows):
        leaders.setdefault(g["_company"], position)
    clustered = sorted(range(len(rows)), key=lambda i: (leaders[rows[i]["_company"]], i))
    if states is not None:
        jobs, total, page = review.review_queue_state(
            [rows[i] for i in clustered], parameters, state, states
        )
    else:
        total = len(rows)
        size = review_table.page_size(parameters)
        ordered = review_table.sort_rows(
            [rows[i] for i in clustered], parameters, lambda job: {}, review.BOARD_LABELS
        )
        try:
            requested = max(1, int(parameters.get("page", 1)))
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid page") from error
        page = min(requested, max(1, (total + size - 1) // size))
        chosen = ordered[(page - 1) * size : page * size]
        jobs = [{**{k: row.get(k) for k in META}, "boards": row["boards"]} for row in chosen]
    state["market_counts"] = {m: markets.get(m, 0) for m in state["available_markets"]}
    state["bucket_counts"] = {b: buckets.get(b, 0) for b in BUCKETS}
    state["out_of_scope_count"] = sum(state["bucket_counts"][b] for b in review.OUT_OF_SCOPE)
    state["board_counts"] = boards
    return jobs, total, page


DETAIL_LIMIT = 500
DETAIL_MAX_BYTES = 24 * MIB
_details = ByteLRU(DETAIL_MAX_BYTES, DETAIL_LIMIT)
fallback_cache = ByteLRU(8 * MIB, 48)
_details_lock = threading.Lock()
_detail_reads = {}
_generation = None
_building = threading.Event()
_activity = threading.Condition()
_foreground_reads = 0


@contextmanager
def foreground_read():
    """Give current requests priority over optional detail prefetch."""
    global _foreground_reads
    with _activity:
        _foreground_reads += 1
    try:
        yield
    finally:
        with _activity:
            _foreground_reads -= 1
            _activity.notify_all()


def trim_optional():
    _details.clear()
    fallback_cache.clear()


def detail_key(identifier, parameters, snapshot):
    """Key immutable details by publication, listing and advertiser-panel filters."""
    scope = parameters.get("advertiser_scope", "in_scope")
    days = parameters.get("advertiser_days", "3")
    return (
        snapshot.built_at,
        int(identifier),
        scope if scope in {"in_scope", "others"} else "in_scope",
        days if days in {"3", "7", "14", "28", "all"} else "3",
    )


def _read_details(reader, identifier, parameters):
    if callable(reader):
        with reader() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _read_details(conn, identifier, parameters)
    reader.execute(f"SET LOCAL statement_timeout = '{DETAIL_TIMEOUT_S}s'")
    context = review.review_selection(reader, identifier, parameters, bounded_context=True)
    return context.pop("selected"), context


def details(
    reader,
    identifier,
    parameters,
    snapshot,
    *,
    speculative=False,
    listing_only=False,
    cached_only=False,
):
    """Share immutable results and concurrent reads; owner state is never retained."""
    key = detail_key(identifier, parameters, snapshot)
    if listing_only:
        full = _details.get(key)
        if full is not None:
            return full[0]
        key = (*key, "listing")
    with _details_lock:
        hit = _details.get(key)
        if hit is not None:
            return hit
        if cached_only:
            raise RuntimeError("Listing details are unavailable without PostgreSQL")
        pending = _detail_reads.get(key)
        owner = pending is None
        if owner:
            pending = _detail_reads[key] = Future()
    if not owner:
        return pending.result()
    try:

        def read():
            if not listing_only:
                return _read_details(reader, int(identifier), parameters)
            with reader() as conn, conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                rows = review.read_rows(
                    conn, review.JOIN_RESULTS + " WHERE j.id=%s", (int(identifier),)
                )
            selected = rows[0] if rows else None
            if selected:
                selected["display_html"] = (
                    review.safe_listing_html(selected.get("description_html"))
                    if selected["board"] != "linkedin"
                    else ""
                )
            return selected

        if speculative:
            result = read()
        else:
            with foreground_read():
                result = read()
        with _details_lock:
            if (_generation is None or key[0] == _generation) and not memory_pressure():
                _details.put(key, result)
        pending.set_result(result)
        return result
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _details_lock:
            _detail_reads.pop(key, None)


class Prefetch:
    """Prepare upcoming listing details with one worker and at most two replaceable plans."""

    def __init__(self):
        self._condition = threading.Condition()
        self._plans = OrderedDict()
        self._thread = None

    def update(self, reader, parameters, snapshot, identifiers, context):
        if not callable(reader):
            return False
        parameters = parameters.copy()
        parameters.pop("advertiser_scope", None)
        parameters.pop("advertiser_days", None)
        with self._condition:
            self._plans[context] = (
                reader,
                parameters,
                snapshot.built_at,
                tuple(identifiers[:10]),
                time.monotonic(),
            )
            self._plans.move_to_end(context)
            while len(self._plans) > 2:
                self._plans.popitem(last=False)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="review-prefetch"
                )
                self._thread.start()
            self._condition.notify_all()
        return True

    def clear(self):
        with self._condition:
            self._plans.clear()
            self._condition.notify_all()

    def _next(self):
        with self._condition:
            for context in list(self._plans):
                reader, params, generation, identifiers, at = self._plans[context]
                if time.monotonic() - at > 60 or generation != _generation:
                    del self._plans[context]
                    continue
                for identifier in identifiers:
                    key = detail_key(identifier, params, SimpleNamespace(built_at=generation))
                    with _details_lock:
                        missing = key not in _details and key not in _detail_reads
                    if missing:
                        self._plans.move_to_end(context)
                        return reader, params, generation, identifier
        return None

    def _loop(self):
        while True:
            with _activity:
                busy = _foreground_reads > 0
            if _building.is_set() or busy or memory_pressure():
                if memory_pressure():
                    trim_optional()
                with self._condition:
                    self._condition.wait(1)
                continue
            item = self._next()
            if item is None:
                with self._condition:
                    self._condition.wait(1)
                continue
            reader, params, generation, identifier = item
            try:
                details(
                    reader,
                    identifier,
                    params,
                    SimpleNamespace(built_at=generation),
                    speculative=True,
                )
            except Exception as error:
                log_failure(
                    log, "Review prefetch unavailable listing_id=%s", identifier,
                    error=error, level=logging.DEBUG,
                )
            finally:
                # Oversized details are served uncached; do not repeatedly speculate on them.
                with self._condition:
                    for context, plan in list(self._plans.items()):
                        if plan[2] == generation:
                            self._plans[context] = (
                                *plan[:3],
                                tuple(i for i in plan[3] if i != identifier),
                                plan[4],
                            )


_prefetch = Prefetch()


def prefetch(reader, identifier, parameters, snapshot, identifiers=(), *, context="default"):
    state = review.review_filter_state(
        parameters,
        latest_night=snapshot.latest,
        older_nights=snapshot.older,
        available_markets=snapshot.available_markets,
        board_pairs=snapshot.board_pairs,
    )
    if not cacheable(parameters, state, snapshot):
        return False
    return _prefetch.update(reader, parameters, snapshot, identifiers, context)


def activate(snapshot):
    global _generation
    with _details_lock:
        _generation = snapshot.built_at
        trim_optional()
    _prefetch.clear()


def selected_listing(reader, identifier, snapshot=None):
    snapshot = snapshot or SimpleNamespace(built_at=None)
    return details(reader, identifier, {}, snapshot, listing_only=True)


def page(conn, parameters, snapshot, states=None, *, cached_details_only=False):
    """The Review page from the snapshot, or None when the request is outside it."""
    state = review.review_filter_state(
        parameters,
        latest_night=snapshot.latest,
        older_nights=snapshot.older,
        available_markets=snapshot.available_markets,
        board_pairs=snapshot.board_pairs,
    )
    state.update(loaded=snapshot.loaded, board_pairs=snapshot.board_pairs)
    if not cacheable(parameters, state, snapshot):
        return None
    jobs, total, number = queue(parameters, state, snapshot, states)
    identifier = (
        state["review"]["selected_id"]
        if states is not None
        else parameters.get("id") or (jobs[0]["id"] if jobs else None)
    )
    selected, context, detail_error = None, {}, None
    if identifier is not None and parameters.get("view") != "table":
        try:
            identifier = int(identifier)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid listing selection") from error
        try:
            selected, context = details(
                conn, identifier, parameters, snapshot, cached_only=cached_details_only
            )
        except Exception as error:
            log_failure(
                log, "Review details unavailable listing_id=%s", identifier,
                error=error, level=logging.WARNING,
            )
            detail_error = (
                "This job's details are temporarily unavailable because the database "
                "is busy. The list is current as of the snapshot; try the job again soon."
            )
    if selected and states is not None:
        selected = {
            **selected,
            "review_state": review.display_review_state(
                selected, review.resolved_review_states(states)
            ),
        }
    return dict(
        jobs=jobs,
        total=total,
        page=number,
        pages=max(
            1,
            (total + review_table.page_size(parameters) - 1) // review_table.page_size(parameters),
        ),
        loaded=snapshot.loaded,
        selected=selected,
        filters=state,
        error=None,
        detail_error=detail_error,
        snapshot_built_at=snapshot.built_at,
        **context,
    )


# ------------------------------------------------------------------ saved publication


def _saved_path():
    explicit = os.environ.get("BTO_REVIEW_SNAPSHOT_FILE")
    state = os.environ.get("STATE_DIRECTORY")
    if explicit:
        return explicit
    return os.path.join(state.split(":")[0], "review_snapshot.pickle") if state else None


def save(snapshot):
    """Write a private snapshot atomically, preserving the serving file on failure."""
    path = _saved_path()
    if not path:
        raise ValueError("A private Review snapshot path is required")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".review-", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            pickle.dump(snapshot, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def load_saved():
    """Load a compatible saved publication, or let startup rebuild it."""
    path = _saved_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as handle:
            return validate(pickle.load(handle), legacy=True)
    except Exception as error:
        log_failure(
            log, "Saved Review snapshot unavailable", error=error, level=logging.WARNING,
        )
        return None


class HotCache:
    """Keep one saved publication and replace it only after a successful build."""

    def __init__(self, gate=None):
        self.snapshot = None
        self.building_since = None
        self.last_error = None
        self.request_id = None
        self.completed_request = None
        self.failed_request = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._gate = gate
        self._thread = None
        self._pending = None
        self._reader = connect
        self._source_check = None
        self._checked_at = 0

    def start(self, reader=connect, source_check=None):
        self._reader, self._source_check = reader, source_check
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="review-hot-cache")
            self._thread.start()

    def check_for_update(self):
        """Opening Review can wake a cheap background status check, at most once a minute."""
        if self._source_check and time.monotonic() - self._checked_at >= 60:
            self._wake.set()

    def request_rebuild(self, request_id=None, source_published_at=None):
        """Queue one rebuild or pass the request to the external publication helper."""
        from uuid import uuid4

        if os.environ.get("BTO_REVIEW_EXTERNAL_PUBLICATION") == "1":
            from bto.web.build_review_snapshot import request_publication

            return request_publication(request_id, source_published_at)
        with self._lock:
            if self.building_since is not None or self._pending is not None:
                return False
            self._pending = request_id or uuid4().hex, source_published_at
            self.request_id = self._pending[0]
            self._wake.set()
            return self.request_id

    def status(self):
        """Report the loaded publication and the current build or failure state."""
        with self._lock:
            snapshot = self.snapshot
            result = {
                "format": SNAPSHOT_FORMAT,
                "contract": contract(),
                "built_at": snapshot.built_at.isoformat() if snapshot else None,
                "source_published_at": snapshot.source_published_at if snapshot else None,
                "database": snapshot.database if snapshot else None,
                "groups": len(snapshot.rows) if snapshot else None,
                "retained_bytes": snapshot.retained_bytes if snapshot else None,
                "coverage_since": getattr(snapshot, "coverage_since", None),
                "coverage_until": getattr(snapshot, "coverage_until", None),
                "building": self.building_since is not None or self._pending is not None,
                "request_id": self.request_id,
                "completed_request": self.completed_request,
                "failed_request": self.failed_request,
                "error": self.last_error,
                "csrf_token": None,
            }
            if os.environ.get("BTO_REVIEW_EXTERNAL_PUBLICATION") == "1":
                from bto.web.build_review_snapshot import publication_status

                result.update(publication_status(snapshot))
            return result

    def _loop(self):
        self.snapshot = load_saved()
        if self.snapshot is not None:
            activate(self.snapshot)
            log.info(
                "Review loaded saved snapshot groups=%s built_at=%s",
                len(self.snapshot.rows),
                self.snapshot.built_at.isoformat(),
            )
        else:
            self.request_rebuild()
        while True:
            try:
                if self._source_check:
                    self._checked_at = time.monotonic()
                    source = self._source_check()
                    if source and source.get("built_at") and not source.get("building"):
                        snapshot = self.snapshot
                        if snapshot and source.get("database") != list(snapshot.database):
                            raise ValueError(
                                "Remote Review publication belongs to another database"
                            )
                        if not snapshot or source["built_at"] > (
                            snapshot.source_published_at or snapshot.built_at.isoformat()
                        ):
                            self.request_rebuild(source_published_at=source["built_at"])
                        del snapshot
            except Exception as error:
                log_failure(
                    log, "Review publication check unavailable", error=error, level=logging.WARNING,
                )
            if self._pending is not None:
                self._build_once()
            self._wake.wait(
                SOURCE_CHECK_SECONDS
                if self._source_check
                else (RETRY_SECONDS if self.snapshot is None else None)
            )
            self._wake.clear()
            if self.snapshot is None and self._pending is None:
                self.request_rebuild()

    def _build_once(self):
        """Build and save a candidate before replacing the current publication."""
        with self._lock:
            request_id, source_at = self._pending or (self.request_id, None)
            self._pending = None
            self.building_since = datetime.now(timezone.utc)
        _building.set()
        try:
            # Optional JD/fallback objects must not compete with the two metadata generations.
            trim_optional()
            if memory_pressure():
                trim_optional()
                if memory_pressure():
                    raise RuntimeError("Review rebuild deferred: web/host memory pressure")
            if self._gate is not None:
                self._gate.acquire()
            try:
                candidate = build(self._reader, source_published_at=source_at)
            finally:
                if self._gate is not None:
                    self._gate.release()
            save(candidate)
            with self._lock:
                self.snapshot = candidate
                self.last_error = None
                self.completed_request = request_id
                self.failed_request = None
                activate(candidate)
            log.info(
                "Review published snapshot groups=%s bytes=%s seconds=%s",
                len(candidate.rows),
                candidate.retained_bytes,
                candidate.build_seconds,
            )
        except Exception as error:
            with self._lock:
                self.last_error = (
                    str(error)
                    if isinstance(error, (SnapshotTooLarge, ValueError, RuntimeError))
                    else type(error).__name__
                )
                self.failed_request = request_id
            log_failure(
                log, "Review publication failed; keeping previous snapshot request_id=%s",
                request_id, error=error, level=logging.WARNING,
            )
        finally:
            with self._lock:
                self.building_since = None
            _building.clear()
