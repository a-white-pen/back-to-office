"""Check that the canonical mapping still represents the standardized listings.

The mapping names its exact source version. Later commits must provably leave
rows unchanged, and both tables must satisfy the membership checks. Missing
evidence stops the caller; timestamps alone never prove freshness.
"""

import logging

from .connection import (
    JOB_CANONICAL_MAPPING_TABLE,
    STANDARDIZED_JOB_LISTINGS_TABLE,
    silver_table_name,
)

log = logging.getLogger(__name__)

STANDARDIZED_VERSION_PROPERTY = "bto.standardized_version"
STALE_MAPPING_MESSAGE = (
    "canonical mapping not current against standardized_job_listings; "
    "run clean job listings"
)

# Delta history operations that never change rows. Everything not listed
# here — RESTORE, WRITE, DELETE, UPDATE, CREATE OR REPLACE TABLE AS SELECT,
# schema changes, anything unknown — counts as data-changing (fail closed).
NON_DATA_CHANGING_OPERATIONS = frozenset({
    "OPTIMIZE", "COMPUTE STATS", "SET TBLPROPERTIES", "VACUUM START",
    "VACUUM END",
})
_MERGE_MUTATION_METRICS = ("numTargetRowsInserted", "numTargetRowsUpdated",
                           "numTargetRowsDeleted")


def pinned(table, version):
    """Read a table at one fixed Delta version."""
    return f"{table} VERSION AS OF {int(version)}"


class StaleMapping(RuntimeError):
    """The canonical mapping cannot be proven current and must not be used."""


def is_data_changing(operation, inserted=None, updated=None, deleted=None):
    """Treat unknown operations and unproven MERGE metrics as data changes.

    A MERGE leaves rows unchanged only when all three mutation counts are zero."""
    if operation in NON_DATA_CHANGING_OPERATIONS:
        return False
    if operation == "MERGE":
        values = []
        for value in (inserted, updated, deleted):
            if isinstance(value, bool):
                return True
            try:
                count = int(str(value).strip())
            except (TypeError, ValueError):
                return True
            if count < 0:
                return True                       # not a count: unproven
            values.append(count)
        return any(v > 0 for v in values)
    return True


def is_insert_only(operation, inserted=None, updated=None, deleted=None):
    """Allow only maintenance or a proven insert-only MERGE as a rebuild baseline.

    Inserted rows still make an existing mapping stale for consumers."""
    if not is_data_changing(operation, inserted, updated, deleted):
        return True
    if operation != "MERGE":
        return False
    values = []
    for value in (inserted, updated, deleted):
        if isinstance(value, bool):
            return False
        try:
            count = int(str(value).strip())
        except (TypeError, ValueError):
            return False
        if count < 0:
            return False                          # not a count: unproven
        values.append(count)
    return values[1] == 0 and values[2] == 0


def table_exists(dbx, table):
    catalog, schema, name = table.split(".")
    return bool(dbx.query(f"SHOW TABLES IN {catalog}.{schema} LIKE '{name}'"))


def current_version(dbx, table):
    """The table's current Delta version — captured BEFORE the first read of
    a canonicalization, and every read of that canonicalization is then
    pinned to it with `VERSION AS OF`."""
    rows = dbx.query(f"SELECT max(version) FROM (DESCRIBE HISTORY {table})")
    version = rows[0][0] if rows else None
    if version is None:
        raise StaleMapping(f"{table}: no Delta history")
    return int(version)


def commits_above(dbx, table, version):
    """Read later commits in order, with mutation counts or None when unproven."""
    rows = dbx.query(f"""
        SELECT version, operation,
               operationMetrics['{_MERGE_MUTATION_METRICS[0]}'],
               operationMetrics['{_MERGE_MUTATION_METRICS[1]}'],
               operationMetrics['{_MERGE_MUTATION_METRICS[2]}']
        FROM (DESCRIBE HISTORY {table})
        WHERE version > {int(version)} ORDER BY version""")
    return [(int(v), op, *(_metric(m) for m in metrics))
            for v, op, *metrics in rows]


def _metric(value):
    """Parse a mutation count; leave absent or malformed values unproven."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def data_changing_commits_above(dbx, table, version):
    return [c for c in commits_above(dbx, table, version)
            if is_data_changing(c[1], *c[2:])]


def latest_data_version(dbx, table, scan=50):
    """Find the newest row-changing version within the bounded history scan.

    Skip recognised maintenance; stop if the scan cannot establish a data version."""
    rows = dbx.query(f"""
        SELECT version, operation,
               operationMetrics['{_MERGE_MUTATION_METRICS[0]}'],
               operationMetrics['{_MERGE_MUTATION_METRICS[1]}'],
               operationMetrics['{_MERGE_MUTATION_METRICS[2]}']
        FROM (DESCRIBE HISTORY {table})
        ORDER BY version DESC LIMIT {int(scan)}""")
    for version, operation, *metrics in rows:
        if is_data_changing(operation, *(_metric(m) for m in metrics)):
            return int(version)
    raise StaleMapping(f"{table}: no data-changing commit in its latest {int(scan)} versions")


def bound_standardized_version(dbx, mapping):
    """Read the mapping's source version, or None if it is missing or malformed."""
    rows = dbx.query(f"SHOW TBLPROPERTIES {mapping} "
                     f"('{STANDARDIZED_VERSION_PROPERTY}')")
    value = rows[0][1] if rows else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def mapping_invariants(dbx, standardized, mapping):
    """Return SQL-detected membership violations, or an empty list for a valid pair."""
    checks = {
        "mapping rows with NULL identity or id": f"""
            SELECT count(*) FROM {mapping} WHERE board IS NULL OR market IS NULL
              OR board_job_id IS NULL OR canonical_job_id IS NULL""",
        # the logical key (NULL fingerprints group together, so a duplicated
        # sentinel is caught here too)
        "mapping logical-key duplicates": f"""
            SELECT count(*) FROM (SELECT 1 FROM {mapping}
              GROUP BY board, market, board_job_id, fingerprint
              HAVING count(*) > 1)""",
        "sentinels whose id is not board:market:board_job_id": f"""
            SELECT count(*) FROM {mapping} WHERE fingerprint IS NULL
              AND canonical_job_id != concat_ws(':', board, market, board_job_id)""",
        "mapping fingerprints absent from standardized": f"""
            SELECT count(*) FROM (
              SELECT fingerprint FROM {mapping} WHERE fingerprint IS NOT NULL
              EXCEPT SELECT fingerprint FROM {standardized}
              WHERE fingerprint IS NOT NULL)""",
        "usable memberships in standardized but not in mapping": f"""
            SELECT count(*) FROM (
              SELECT DISTINCT board, market, board_job_id, fingerprint
              FROM {standardized} WHERE fingerprint IS NOT NULL
              EXCEPT SELECT board, market, board_job_id, fingerprint
              FROM {mapping} WHERE fingerprint IS NOT NULL)""",
        "mapping memberships not in standardized": f"""
            SELECT count(*) FROM (
              SELECT board, market, board_job_id, fingerprint
              FROM {mapping} WHERE fingerprint IS NOT NULL
              EXCEPT SELECT DISTINCT board, market, board_job_id, fingerprint
              FROM {standardized} WHERE fingerprint IS NOT NULL)""",
        "never-usable listings without exactly one sentinel": f"""
            SELECT count(*) FROM (
              SELECT board, market, board_job_id FROM {standardized}
              GROUP BY board, market, board_job_id
              HAVING max(CASE WHEN fingerprint IS NOT NULL THEN 1 ELSE 0 END) = 0
              EXCEPT
              SELECT board, market, board_job_id FROM {mapping}
              WHERE fingerprint IS NULL)""",
        "sentinels for listings that have a usable wording": f"""
            SELECT count(*) FROM {mapping} n WHERE n.fingerprint IS NULL
              AND EXISTS (SELECT 1 FROM {standardized} s
                          WHERE s.board = n.board AND s.market = n.market
                            AND s.board_job_id = n.board_job_id
                            AND s.fingerprint IS NOT NULL)""",
        # a well-formed sentinel for a listing that standardized never held
        # passes every check above (NULL fingerprints are excluded from the
        # membership checks and the listing side has nothing to compare)
        "sentinels for listings absent from standardized": f"""
            SELECT count(*) FROM {mapping} n WHERE n.fingerprint IS NULL
              AND NOT EXISTS (SELECT 1 FROM {standardized} s
                              WHERE s.board = n.board AND s.market = n.market
                                AND s.board_job_id = n.board_job_id)""",
        "ids naming a non-member winner": f"""
            SELECT count(*) FROM {mapping} m WHERE m.fingerprint IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM {mapping} w
                WHERE w.canonical_job_id = m.canonical_job_id
                  AND w.fingerprint IS NOT NULL
                  AND concat_ws(':', w.board, w.market, w.board_job_id,
                                w.fingerprint) = m.canonical_job_id)""",
        "wordings mapped to several groups": f"""
            SELECT count(*) FROM (SELECT fingerprint FROM {mapping}
              WHERE fingerprint IS NOT NULL GROUP BY fingerprint
              HAVING count(DISTINCT canonical_job_id) > 1)""",
        # Empty mapping → 1; any NULL (count(col) < count(*)) → 1; otherwise
        # distinct values − 1, which is zero only for exactly one value. A
        # naive count(DISTINCT) plus NULL flag misses the all-NULL case because
        # DISTINCT over an all-NULL column is zero.
        "canonicalized_at values other than exactly one non-NULL": f"""
            SELECT CASE WHEN count(*) = 0 THEN 1
                        WHEN count(canonicalized_at) < count(*) THEN 1
                        ELSE count(DISTINCT canonicalized_at) - 1 END
            FROM {mapping}""",
    }
    violations = []
    for name, statement in checks.items():
        rows = dbx.query(statement)
        count = int(rows[0][0] or 0) if rows else 0
        if count:
            violations.append(f"{name}: {count}")
    return violations


def freshness_violations(dbx, catalog, target_suffix=""):
    """Every reason the mapping is NOT provably current; empty = current.
    Stops at the first unprovable fact — later checks would be meaningless."""
    standardized = silver_table_name(
        catalog, STANDARDIZED_JOB_LISTINGS_TABLE + target_suffix)
    mapping = silver_table_name(
        catalog, JOB_CANONICAL_MAPPING_TABLE + target_suffix)
    if not table_exists(dbx, standardized):
        return [f"{standardized} does not exist"]
    if not table_exists(dbx, mapping):
        return [f"{mapping} does not exist"]
    bound = bound_standardized_version(dbx, mapping)
    if bound is None:
        return [f"{mapping} has no integer {STANDARDIZED_VERSION_PROPERTY} "
                f"property"]
    current = current_version(dbx, standardized)
    if bound < 0 or bound > current:
        return [f"{mapping} is bound to standardized version {bound}, "
                f"which is not a version of {standardized} (current {current})"]
    commits = commits_above(dbx, standardized, bound)
    if len(commits) != current - bound:
        return [f"standardized history above version {bound} is incomplete "
                f"({len(commits)} of {current - bound} commits retained); "
                f"freshness cannot be proven"]
    changed = [c for c in commits if is_data_changing(c[1], *c[2:])]
    violations = []
    if changed:
        violations.append(
            f"standardized data changed after version {bound}: "
            + ", ".join(f"v{v} {op}" for v, op, *_ in changed[:5]))
    violations += mapping_invariants(dbx, standardized, mapping)
    return violations


def require_current_mapping(dbx, catalog, target_suffix=""):
    """Fail closed: raise StaleMapping with the operator-facing message,
    logging every internal reason. Returns the bound version N when current."""
    violations = freshness_violations(dbx, catalog, target_suffix)
    if violations:
        for violation in violations:
            log.error("MAPPING NOT CURRENT: %s", violation)
        raise StaleMapping(STALE_MAPPING_MESSAGE)
    mapping = silver_table_name(
        catalog, JOB_CANONICAL_MAPPING_TABLE + target_suffix)
    return bound_standardized_version(dbx, mapping)
