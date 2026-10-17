"""Prepare distinct exact model inputs after free filtering and answer reuse."""

import json
from collections import Counter

from bto.storage.postgres import job_sorting as store

from . import PROCESSING_START, TERMINAL_REASONS
from .bucket_job_listings import bucket_listing
from .extract_job_listing_details import response_schema, validate_extraction
from .triage_job_listings import SCHEMA, validate_triage


def answer_fields(stage, answer):
    names = (
        ("profession", "quote", "verdict")
        if stage == "triage"
        else ("kinds", "lines", "recruitment_firm", "public_sector")
    )
    return {name: answer[name] for name in names}


def validate(stage, value, row, preferences):
    if stage == "triage":
        return validate_triage(value, row)
    return validate_extraction(value, row, [k["name"] for k in preferences["kinds"]])


def prepare(conn, preferences, prompts):
    """Reuse exact answers, apply free rules, and return the current unpaid cohort.

    The caller owns sorting. Reads are bounded because JD columns are wide.
    Terminal failed-twice rows do not gain fresh paid attempts on a later run.
    """
    from .run import inputs

    items = {stage: {} for stage in prompts}
    counts = Counter()
    last = 0
    while True:
        rows = store.read_rows(
            conn,
            (
                "WITH selected AS MATERIALIZED(SELECT id FROM job_sorting.job_listings "
                "WHERE id>%s AND wording_first_seen_at>=%s ORDER BY id LIMIT 100)"
                + store.JOIN_RESULTS
                + " JOIN selected s ON s.id=j.id ORDER BY j.id"
            ),
            (last, PROCESSING_START),
        )
        if not rows:
            break
        wanted = {r["id"]: inputs(r, prompts) for r in rows}
        exact = {
            s: store.find_answers(conn, s, [wanted[r["id"]][s][0] for r in rows]) for s in prompts
        }
        decisions = []
        for stage in prompts:
            links = []
            for row in rows:
                digest = wanted[row["id"]][stage][0]
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
            store.link_answers(conn, stage, links)
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
                    and not (row["triage"] and row["triage"]["verdict"] == "pass")
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
        store.update_results(conn, decisions)
        last = rows[-1]["id"]
    return items, dict(counts)


def generation_request(stage, item, fixed, preferences, *, cache=None):
    from .call_gemini import SAFETY_CATEGORIES, THINKING

    correction = item.get("correction")
    text = item["listing"] + ("\n\nProblems to correct:\n" + correction if correction else "")
    body = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": SCHEMA
            if stage == "triage"
            else response_schema([k["name"] for k in preferences["kinds"]]),
            "maxOutputTokens": 1024 if stage == "triage" else 8192,
            "thinkingConfig": {"thinkingLevel": THINKING},
        },
        "safetySettings": [{"category": c, "threshold": "OFF"} for c in SAFETY_CATEGORIES],
    }
    body.update(
        {"cachedContent": cache} if cache else {"systemInstruction": {"parts": [{"text": fixed}]}}
    )
    return body


def checked_answer(stage, response, item, preferences):
    candidates = response.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        raise ValueError("No complete answer")
    text = "".join(
        p.get("text", "")
        for p in candidates[0].get("content", {}).get("parts", [])
        if not p.get("thought")
    )
    return validate(stage, json.loads(text), item["row"], preferences)
