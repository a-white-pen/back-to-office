"""Map SEEK-family payloads into Bronze seek_job_listings observation rows.

Called by:
    fetch_job_listings.run (through the shared card-board collection loop)

A SEEK detail payload is `{"data": {"jobDetails": {job, companyProfile,
gfjInfo}}}`; the Bronze table stores those three blocks under SEEK's own
names, so this is a selection, not a translation. A minimal stub record
(id/title/content/status/listedAt/expiresAt only) passes through with its
other fields absent — the source's own shape, not a parser failure.

Main function:
    observation_row(board, market, board_job_id, run_id, change_signal,
                    content_hash, payload) -> {column: value}

`payload` is the parsed detail JSON, or None for an observation whose payload
was not captured this run — payload-derived columns then stay absent and
land as NULL, per contract.
"""

# The jobDetails blocks that become Bronze columns, verbatim.
PAYLOAD_COLUMNS = ("job", "companyProfile", "gfjInfo")


def observation_row(board, market, board_job_id, run_id, change_signal,
                    content_hash, payload):
    row = {
        "board": board,
        "market": market,
        "board_job_id": board_job_id,
        "run_id": run_id,
        "content_hash": content_hash,
        "change_signal": change_signal,
    }
    if payload is not None:
        job_details = ((payload.get("data") or {}).get("jobDetails") or {})
        for column in PAYLOAD_COLUMNS:
            row[column] = job_details.get(column)
    return row
