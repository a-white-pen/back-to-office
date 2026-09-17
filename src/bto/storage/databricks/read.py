"""Load the latest prior MCF/SEEK Bronze state for change detection.

Called by:
    fetch_job_listings.run (once, at the start of each MCF/SEEK run)

One short batched read per board × market run — never a lookup per job. The
result lives in memory on the Lightsail box for the duration of the run and
is gone when the process moves on; Databricks Bronze stays authoritative.

Main function:
    load_prior_state(dbx, catalog, board, market)
        -> {board_job_id: PriorState(change_signal, content_hash, run_id)}
"""

import logging
from collections import namedtuple

from .connection import observation_table

log = logging.getLogger(__name__)

# content_hash is None when the job was seen without a valid payload for its
# current signal — that job stays fetch-required under the collection contract.
# has_payload says whether the prior row actually carries its payload-derived
# columns. A row with a content_hash but no projection would otherwise be copied
# forward unchanged for ever, so the diff treats it as fetch-required too.
PriorState = namedtuple("PriorState", "change_signal content_hash run_id has_payload")

# One payload-derived column per observation table, always populated when the
# payload was projected: MCF's own job id, and the SEEK family's jobDetails.job
# block. Keyed by the resolved base table name — the same resolution the write
# layer's TABLE_COLUMNS uses — so board aliases (jobstreet, jobsdb, seek) go
# through observation_table() exactly once and are never repeated here.
PAYLOAD_PROBE = {"mcf_job_listings": "uuid", "seek_job_listings": "job"}


def _parse_has_payload(value):
    """Parse the SQL Statement JSON_ARRAY Boolean representation.

    Databricks returns non-NULL result values as strings. Native booleans are
    also accepted for callers that preserve JSON scalar types; NULL means the
    payload projection is absent.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"unexpected has_payload Boolean representation: {value!r}")


def load_prior_state(dbx, catalog, board, market):
    """Latest prior observation per board_job_id, whatever its run's status.

    "Latest" here is the MCF/SEEK collection prior-state lookup's own
    mechanism — NOT general observation chronology (that is the parent
    scrape_runs.started_at; see the data dictionary) and NOT a canonicalization
    ordering rule. This reader orders by the fixed-format timestamp embedded in
    its own tables' run ids. run_id is
    {board}_{market}_{YYYYMMDD_HHMMSS}_{suffix}, so within one board × market
    the timestamp sits at a fixed offset and is compared as fixed-width text.
    The random suffix carries no ordering meaning; the run_id tiebreak below
    only exists to make the (practically impossible) same-second case
    deterministic.
    """
    table = observation_table(catalog, board)
    probe = PAYLOAD_PROBE[table.rsplit(".", 1)[-1]]
    ts_position = len(board) + len(market) + 3   # 1-based start of YYYYMMDD_HHMMSS
    rows = dbx.query(
        f"""
        SELECT board_job_id, change_signal, content_hash, run_id, has_payload FROM (
          SELECT board_job_id, change_signal, content_hash, run_id,
                 `{probe}` IS NOT NULL AS has_payload,
                 row_number() OVER (
                   PARTITION BY board_job_id
                   ORDER BY substr(run_id, {ts_position}, 15) DESC, run_id DESC
                 ) AS rn
          FROM {table}
          WHERE board = :board AND market = :market
        ) WHERE rn = 1
        """,
        parameters={"board": board, "market": market},
    )
    state = {
        board_job_id: PriorState(change_signal, content_hash, run_id,
                                 _parse_has_payload(has_payload))
        for board_job_id, change_signal, content_hash, run_id, has_payload in rows
    }
    missing_payload = sum(1 for s in state.values() if s.content_hash is None)
    unprojected = sum(1 for s in state.values()
                      if s.content_hash is not None and not s.has_payload)
    log.info("%s_%s prior state: %d known jobs, %d without a valid payload",
             board, market, len(state), missing_payload)
    if unprojected:
        log.warning("%s_%s prior state: %d rows carry a content_hash but no "
                    "payload columns — re-fetching rather than copying the gap "
                    "forward", board, market, unprojected)
    return state
