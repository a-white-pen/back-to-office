"""Choose the distinct inputs that still need triage or extraction.

Apply free rules and link reusable stored answers before preparing paid work.
Keep validated historical answers; do not submit failed-twice listings again."""

import json
from collections import Counter

from bto.storage.postgres import read as pg_read
from bto.storage.postgres import write as pg_write

from . import TERMINAL_REASONS, processing_start
from .bucket_job_listings import bucket_listing
from .config import load_triage_policy
from .extract_job_listing_details import response_schema, validate_extraction
from .triage_job_listings import response_schema as triage_schema
from .triage_job_listings import validate_triage


def inputs(row, prompts):
    from .configure_gemini import input_hash
    from .extract_job_listing_details import listing_input as extract_input
    from .triage_job_listings import listing_input as triage_input

    listings = {
        "triage": triage_input(row, prompts["triage"][1]),
        "extraction": extract_input(row, prompts["extraction"][1]),
    }
    return {stage: (input_hash(prompts[stage][0], text), text) for stage, text in listings.items()}


def answer_fields(stage, answer):
    names = (
        ("profession", "quote", "verdict")
        if stage == "triage"
        else ("kinds", "lines", "recruitment_firm", "public_sector")
    )
    return {name: answer[name] for name in names}


def validate(stage, value, row, preferences, *, fixed=""):
    if stage == "triage":
        return validate_triage(value, row, fixed=fixed)
    return validate_extraction(value, row, [k["name"] for k in preferences["kinds"]], preferences["work_groups"])


def prepare(conn, preferences, prompts):
    """Reuse exact answers, apply free rules, and return the current unpaid cohort.

    The caller owns sorting. Reads are bounded because JD columns are wide.
    Validated historical answers keep their original hashes when prompts change.
    Terminal failed-twice rows do not gain fresh paid attempts on a later run.
    """
    items = {stage: {} for stage in prompts}
    counts = Counter()
    last = 0
    while True:
        rows = pg_read.read_rows(
            conn,
            (
                "WITH selected AS MATERIALIZED(SELECT id FROM job_sorting.job_listings "
                "WHERE id>%s AND wording_first_seen_at>=%s ORDER BY id LIMIT 100)"
                + pg_read.JOIN_RESULTS
                + " JOIN selected s ON s.id=j.id ORDER BY j.id"
            ),
            (last, processing_start()),
        )
        if not rows:
            break
        wanted = {r["id"]: inputs(r, prompts) for r in rows}
        exact = {
            s: pg_read.find_answers(conn, s, [wanted[r["id"]][s][0] for r in rows]) for s in prompts
        }
        decisions = []
        for stage in prompts:
            links = []
            for row in rows:
                digest = wanted[row["id"]][stage][0]
                if row.get(stage) and row.get(stage + "_input_hash"):
                    # Prompt corrections apply to unpaid work, not a paid reread
                    # of validated history. Keep the answer's actual input hash.
                    counts[stage + "_retained_answers"] += 1
                    continue
                answer = exact[stage].get(digest)
                if row.get("reason") in TERMINAL_REASONS and row.get(stage + "_input_hash") != digest:
                    # A failed-twice wording stays terminal; another wording's
                    # later valid answer does not reopen its exhausted attempts.
                    answer = None
                if answer:
                    # Recheck the sent quote/line contract before linking another wording.
                    validate(stage, answer_fields(stage, answer), row, preferences)
                if row.get(stage + "_input_hash") != (digest if answer else None):
                    links.append({"id": row["id"], "input_hash": digest if answer else None})
                row[stage] = answer
            pg_write.link_answers(conn, stage, links)
            counts[stage + "_reused_links"] += sum(p["input_hash"] is not None for p in links)
        for row in rows:
            counts["processing_window"] += 1
            decision = bucket_listing(row, row["triage"], row["extraction"], preferences)
            decisions.append(decision)
            if decision["bucket"] != "not_sorted" or decision["reason"] in TERMINAL_REASONS:
                continue
            for stage in prompts:
                digest, listing = wanted[row["id"]][stage]
                if (
                    row[stage]
                    or stage == "extraction"
                    and not (row["triage"] and row["triage"]["verdict"] == load_triage_policy()["accept"])
                ):
                    continue
                item = items[stage].setdefault(
                    digest,
                    {
                        "input_hash": digest,
                        "listing": listing,
                        "ids": [],
                        "row": {
                            k: row.get(k)
                            for k in (
                                "title",
                                "description_text",
                                "advertiser_name",
                                "hiring_company_name",
                            )
                        },
                        "answer_attempts": 0,
                        "provider_failures": 0,
                    },
                )
                item["ids"].append(row["id"])
            counts["unfinished_wordings"] += 1
            counts["unfinished_" + row["market"]] += 1
        pg_write.update_results(conn, decisions)
        last = rows[-1]["id"]
    return items, dict(counts)


def generation_request(stage, item, fixed, preferences, *, cache=None):
    """Build one bounded model request using the saved input and any validation correction."""
    from .configure_gemini import SAFETY_CATEGORIES, THINKING

    correction = item.get("correction")
    text = item["listing"] + ("\n\nProblems to correct:\n" + correction if correction else "")
    body = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": triage_schema()
            if stage == "triage"
            else response_schema([k["name"] for k in preferences["kinds"]], preferences["work_groups"]),
            "maxOutputTokens": 1024 if stage == "triage" else 8192,
            "thinkingConfig": {"thinkingLevel": THINKING},
        },
        "safetySettings": [{"category": c, "threshold": "OFF"} for c in SAFETY_CATEGORIES],
    }
    body.update(
        {"cachedContent": cache} if cache else {"systemInstruction": {"parts": [{"text": fixed}]}}
    )
    return body


def checked_answer(stage, response, item, preferences, *, fixed=""):
    candidates = response.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        raise ValueError("No complete answer")
    text = "".join(
        p.get("text", "")
        for p in candidates[0].get("content", {}).get("parts", [])
        if not p.get("thought")
    )
    return validate(stage, json.loads(text), item["row"], preferences, fixed=fixed)
