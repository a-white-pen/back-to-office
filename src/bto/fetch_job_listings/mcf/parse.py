"""Map MCF payloads into Bronze mcf_job_listings observation rows.

Called by:
    fetch_job_listings.run (through the shared card-board collection loop)

Bronze keeps MCF's own field names, so this is a selection, not a
translation: the documented payload fields pass through under their source
names and structure, and the write layer's schema does the typing. Fields the
data dictionary excluded (otherRequirements, psdUrl, schemes, badges, …) are
simply not selected.

Main function:
    observation_row(market, board_job_id, run_id, change_signal,
                    content_hash, payload) -> {column: value}

`payload` is the parsed detail JSON, or None for an observation whose payload
was not captured this run (deferred/failed) — payload-derived columns then
stay absent and land as NULL, per contract.
"""

BOARD = "mcf"

# Payload keys that become Bronze columns, verbatim (see the data dictionary).
PAYLOAD_COLUMNS = (
    "uuid", "title", "description", "postedCompany", "hiringCompany",
    "metadata", "status", "salary", "minimumYearsExperience",
    "numberOfVacancies", "ssocCode", "ssocVersion", "occupationId",
    "ssecEqa", "ssecFos", "categories", "employmentTypes", "positionLevels",
    "skills", "screeningQuestions", "flexibleWorkArrangements", "address",
)


def observation_row(market, board_job_id, run_id, change_signal, content_hash,
                    payload):
    row = {
        "board": BOARD,
        "market": market,
        "board_job_id": board_job_id,
        "run_id": run_id,
        "content_hash": content_hash,
        "change_signal": change_signal,
    }
    if payload is not None:
        for column in PAYLOAD_COLUMNS:
            row[column] = payload.get(column)
    return row
