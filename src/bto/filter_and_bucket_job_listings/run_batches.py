"""Run triage Batch jobs, then extraction for listings that pass triage.

Prepare work, enforce the USD ceiling and store validated answers and costs.
Save progress so an interrupted run can resume. Database connections are
released while waiting for Gemini."""

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from bto.log_errors import log_failure
from bto.storage.postgres import connection as pg_connection
from bto.storage.postgres import read as pg_read
from bto.storage.postgres import write as pg_write
from bto.storage.postgres.connection import connect

from . import REFUSED_INPUT_LIMIT, REFUSED_REASON, spending_refusal
from .call_gemini_batch import (
    API,
    BatchStopped,
    correlate,
    poll,
    read_retry,
    release_checkpoint_memory,
    retrieve,
    run_owner,
    save,
    submit,
    timestamp,
)
from .configure_gemini import (
    MODEL,
    THINKING,
    ProviderError,
    cache_cost,
    is_busy,
    prices,
    usage_cost,
)
from .prepare_batches import checked_answer, generation_request, inputs, prepare

log = logging.getLogger(__name__)


def database(operation, *, wait=time.sleep):
    """Only idempotent store operations use this bounded reconnect policy."""
    import psycopg

    for attempt in range(4):
        try:
            with connect() as conn, pg_connection.sorting_owner(conn):
                return operation(conn)
        except (psycopg.OperationalError, psycopg.InterfaceError) as error:
            if attempt == 3:
                log_failure(log, "Sorting database reconnect exhausted attempts=4", error=error)
                raise
            delay = 5 * 2**attempt
            log.warning("Sorting database reconnect attempt=%s/4 retry_in_seconds=%s error_type=%s",
                        attempt + 1, delay, type(error).__name__)
            wait(delay)


def upper_tokens(body, fixed):
    # Bound the finite request by its UTF-8 size plus schema and message allowance.
    return len(json.dumps(body, ensure_ascii=False).encode()) + len(fixed.encode()) + 1024


def upper_cost(tokens, stage, at):
    input_price, _, output_price = prices(at + timedelta(hours=49))
    return (
        Decimal(tokens) * input_price
        + Decimal(1024 if stage == "triage" else 8192) * output_price
    ) / 1000000


def budget_used(state):
    return sum((Decimal(r["cost_usd"]) for r in state.get("receipts", {}).values()), Decimal(0))


def budget_held(state):
    return sum((Decimal(v) for v in state.get("unknown", {}).values()), Decimal(0)) + Decimal(
        state.get("cache_upper_usd", "0")
    )


def choose_chunk(
    items, stage, fixed, preferences, available, token_limit, cache=None, *, cached_tokens=None
):
    """Select pending inputs that fit both the token quota and remaining cost bound."""
    chunk = {}
    tokens = 0
    maximum = Decimal(0)
    for digest, item in items.items():
        if item.get("outcome") in {"valid", "terminal", "provider_failed"}:
            continue
        body = generation_request(stage, item, fixed, preferences, cache=cache)
        n = upper_tokens(body, fixed if cache else "")
        charge = upper_cost(n, stage, datetime.now(timezone.utc))
        # The measured fixed prompt count can replace its byte upper bound
        # for enqueue quota. Keep the independent cost bound unchanged.
        enqueued = n
        if cache and cached_tokens is not None:
            if not isinstance(cached_tokens, int) or cached_tokens < 0:
                raise ValueError("Invalid measured cached prompt token count")
            enqueued = n - len(fixed.encode()) + cached_tokens
        if tokens + enqueued > token_limit or maximum + charge > available:
            if not chunk:
                raise BatchStopped(
                    "Available Batch budget/quota cannot admit the next finite request"
                )
            break
        tokens += enqueued
        maximum += charge
        chunk[digest] = item
    return chunk, maximum, tokens


def ensure_cache(api, state, state_path, stage, fixed):
    """Reserve cache costs before creating or extending the stage's prompt cache."""
    caches = state.setdefault("caches", {})
    if stage in caches:
        cached = caches[stage]
        if cached and cached.get("submission_unknown"):
            raise BatchStopped("Reconcile the saved cache submission before continuing")
        if cached and datetime.fromisoformat(cached["expires"]) <= datetime.now(
            timezone.utc
        ) + timedelta(hours=49):
            end = datetime.now(timezone.utc) + timedelta(hours=49)
            extra = Decimal(
                str((end - datetime.fromisoformat(cached["expires"])).total_seconds() / 3600)
            )
            allowance = cache_cost(cached["tokens"], extra, end)
            if budget_used(state) + budget_held(state) + allowance > Decimal(state["ceiling_usd"]):
                raise BatchStopped("Cache extension would exceed the Batch ceiling")
            state["cache_upper_usd"] = str(Decimal(state.get("cache_upper_usd", "0")) + allowance)
            save(state_path, state)
            read_retry(
                lambda: api.json(
                    cached["name"] + "?updateMask=expireTime",
                    {"expireTime": end.isoformat()},
                    "PATCH",
                )
            )
            cached["expires"] = end.isoformat()
            save(state_path, state)
        return cached["name"] if cached else None
    end = datetime.now(timezone.utc) + timedelta(hours=49)
    tokens = len(fixed.encode()) + 1024
    # Cache creation can lose its receipt too; reserve its finite prompt/storage
    # charge durably before POST. Do not turn an unknown cache into a free one.
    allowance = cache_cost(tokens, 49, end, create=True)
    if budget_used(state) + budget_held(state) + allowance > Decimal(state["ceiling_usd"]):
        raise BatchStopped("Cache allowance would exceed the Batch ceiling")
    state["cache_upper_usd"] = str(Decimal(state.get("cache_upper_usd", "0")) + allowance)
    caches[stage] = {"submission_unknown": True}
    save(state_path, state)
    try:
        result = api.json(
            "cachedContents",
            {
                "model": "models/" + MODEL,
                "systemInstruction": {"parts": [{"text": fixed}]},
                "expireTime": end.isoformat(),
            },
            "POST",
        )
    except ProviderError as error:
        state.setdefault("cache_failures", {})[stage] = {"at": timestamp(), **error.details()}
        save(state_path, state)
        log.warning("Gemini Batch cache unavailable stage=%s http_status=%s checkpoint=%s",
                    stage, error.status, state_path)
        if error.status == 402 or (error.status == 429 and not is_busy(error)):
            # A definite payment/spending refusal cannot admit an uncached fallback.
            # No cache was accepted, so release only this rejected creation hold.
            caches.pop(stage)
            state["cache_upper_usd"] = str(Decimal(state["cache_upper_usd"]) - allowance)
            save(state_path, state)
            raise
        if error.message == "Connection unavailable" or error.status >= 500:
            raise BatchStopped(
                "Cache submission receipt unavailable; allowance retained"
            ) from error
        caches[stage] = None
        save(state_path, state)
        if Decimal(str(state["estimate"].get("conservative_uncached_usd", "Infinity"))) > Decimal(
            state["ceiling_usd"]
        ):
            raise BatchStopped(
                "Explicit cache refused and uncached estimate exceeds the ceiling"
            ) from error
        return None
    caches[stage] = {"name": result["name"], "expires": end.isoformat(), "tokens": str(tokens)}
    save(state_path, state)
    return result["name"]


def ingest(conn, state, job, items, replies, stage, preferences, prompts):
    """Validate correlated replies, record their costs and save reusable answers."""
    correlated = correlate(replies, job["keys"])
    counts = {"valid": 0, "invalid": 0, "provider_failed": 0, "missing": 0, "terminal": 0}
    job_receipts = {}
    for key, digest in job["keys"].items():
        item = items[digest]
        response = correlated.get(key)
        if response is None:
            counts["missing"] += 1
        if not response or response.get("error") or not response.get("response"):
            item["provider_failures"] += 1
            item["last_provider_error"] = (response or {}).get(
                "error", {"message": "Missing Batch item"}
            )
            if not response:
                state.setdefault("unknown", {})[key] = job["upper_by_key"][key]
            error = item["last_provider_error"]
            transient = error.get("code") in {4, 8, 13, 14, 408, 429, 500, 502, 503, 504}
            # Gemini can refuse one request among identical ones it accepts, so
            # a refusal gets one more try. A spending limit gets none.
            attempts = 1 if spending_refusal(error) else 3 if transient else 2
            if item["provider_failures"] >= attempts:
                item["outcome"] = "provider_failed"
                counts["provider_failed"] += 1
            continue
        model_response = response["response"]
        try:
            record = usage_cost(
                model_response, datetime.fromisoformat(job["submission_at"]), execution_tier="batch"
            )
            if record is None:
                raise ValueError("Usage unavailable")
        except (ValueError, TypeError, AttributeError):
            record = None
            state.setdefault("unknown", {})[key] = job["upper_by_key"][key]
        if record:
            job_receipts[record["gemini_response_id"]] = record
            state.setdefault("receipts", {})[record["gemini_response_id"]] = {
                "stage": stage,
                **record,
            }
            if not pg_write.record_gemini_cost(conn, stage, record):
                state.setdefault("cost_write_pending", {})[record["gemini_response_id"]] = {
                    "stage": stage,
                    **record,
                }
        item["answer_attempts"] += 1
        try:
            answer = checked_answer(
                stage, model_response, item, preferences, fixed=prompts[stage][0]
            )
        except (ValueError, TypeError, KeyError) as error:
            item["correction"] = str(error)
            counts["invalid"] += 1
            if item["answer_attempts"] >= 2:
                item["outcome"] = "terminal"
                counts["terminal"] += 1
                rows = pg_read.read_rows(
                    conn, "SELECT * FROM job_sorting.job_listings WHERE id=ANY(%s)", (item["ids"],)
                )
                from .bucket_job_listings import bucket_listing

                decisions = [
                    bucket_listing(
                        {**row, "reason": stage + " failed twice"}, None, None, preferences
                    )
                    for row in rows
                    if inputs(row, prompts)[stage][0] == digest
                ]
                pg_write.update_results(conn, decisions)
            continue
        ids = []
        for start in range(0, len(item["ids"]), 100):
            rows = pg_read.read_rows(
                conn,
                "SELECT id,title,description_text,advertiser_name,hiring_company_name FROM job_sorting.job_listings WHERE id=ANY(%s)",
                (item["ids"][start : start + 100],),
            )
            ids.extend(r["id"] for r in rows if inputs(r, prompts)[stage][0] == digest)
        pg_write.store_answer(conn, stage, digest, answer, ids)
        item["outcome"] = "valid"
        counts["valid"] += 1
    counts["known_cost_usd"] = str(sum((r["cost_usd"] for r in job_receipts.values()), Decimal(0)))
    counts["billed_items"] = len(job_receipts)
    return counts


def run_stage(state, state_path, stage, preferences, prompts, keys, *, wait=time.sleep):
    """Resume pending jobs and process bounded retries until the stage can finish."""
    items = state["items"][stage]
    api = API(keys[stage])
    stage_dir = state_path.parent / stage
    stage_dir.mkdir(exist_ok=True)
    jobs = state.setdefault("jobs", {}).setdefault(stage, [])
    log.info("Batch stage started run_id=%s stage=%s distinct_inputs=%s saved_jobs=%s",
             state.get("run_id"), stage, len(items), len(jobs))
    while any(
        i.get("outcome") not in {"valid", "terminal", "provider_failed"} for i in items.values()
    ):
        pending = next(
            (
                j
                for j in jobs
                if not j.get("ingested_at")
                and not (
                    j.get("status") == "submission_rejected" and j.get("verified_not_accepted_at")
                )
            ),
            None,
        )
        if pending is None:
            cache = ensure_cache(api, state, state_path, stage, prompts[stage][0])
            available = Decimal(state["ceiling_usd"]) - budget_used(state) - budget_held(state)
            chunk, maximum, tokens = choose_chunk(
                items,
                stage,
                prompts[stage][0],
                preferences,
                available,
                state["enqueued_token_limit"],
                cache,
                cached_tokens=state["estimate"]
                .get("stages", {})
                .get(stage, {})
                .get("fixed_tokens"),
            )
            number = len(jobs) + 1
            directory = stage_dir / str(number)
            directory.mkdir(exist_ok=True)
            input_path = directory / "input.jsonl"
            rates = prices(datetime.now(timezone.utc))
            pending = {
                "display": f"bto-{state['run_id']}-{stage}-{number}",
                "stage": stage,
                "model": MODEL,
                "status": "prepared",
                "keys": {},
                "upper_by_key": {},
                "input_path": str(input_path),
                "upper_usd": str(maximum),
                "token_upper": tokens,
                "item_count": len(chunk),
                "retried_residual_items": sum(
                    bool(i["answer_attempts"] or i["provider_failures"]) for i in chunk.values()
                ),
                "source": state["source"],
                "pricing": {
                    "tier": "batch",
                    "input": str(rates[0]),
                    "cached": str(rates[1]),
                    "output": str(rates[2]),
                    "per": 1000000,
                },
            }
            lines = []
            for digest, item in chunk.items():
                key = (
                    f"{stage}-{digest}-a{item['answer_attempts'] + 1}-p{item['provider_failures']}"
                )
                pending["keys"][key] = digest
                body = generation_request(stage, item, prompts[stage][0], preferences, cache=cache)
                pending["upper_by_key"][key] = str(
                    upper_cost(
                        upper_tokens(body, prompts[stage][0] if cache else ""),
                        stage,
                        datetime.now(timezone.utc),
                    )
                )
                lines.append(json.dumps({"key": key, "request": body}, ensure_ascii=False))
            # Save the private input durably before uploading or submitting it.
            with input_path.open("w") as output:
                input_path.chmod(0o600)
                output.write("\n".join(lines) + "\n")
                output.flush()
                import os

                os.fsync(output.fileno())
            pending["input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
            jobs.append(pending)
            save(state_path, state)
        directory = Path(pending["input_path"]).parent
        job_path = directory / "job.json"
        if job_path.exists():
            pending.update(json.loads(job_path.read_text()))
        else:
            save(job_path, pending)
        if (
            hashlib.sha256(Path(pending["input_path"]).read_bytes()).hexdigest()
            != pending["input_sha256"]
        ):
            raise BatchStopped("Submitted input checkpoint changed")
        submit(api, job_path, pending, wait=wait)
        save(state_path, state)
        # No open database connection or sorting advisory lock across this wait.
        operation = poll(api, job_path, pending, wait=wait)
        save(state_path, state)
        remote_failed = pending["status"] != "JOB_STATE_SUCCEEDED"
        output = operation.get("response") or operation.get("metadata", {}).get("output") or {}
        if remote_failed and not (output.get("responsesFile") or output.get("inlinedResponses")):
            state.setdefault("unknown", {})[pending["name"]] = pending["upper_usd"]
            save(state_path, state)
            raise BatchStopped(f"{stage} Batch {pending['status']}: {operation.get('error', {})}")
        if pending.get("results_path"):
            result_path = Path(pending["results_path"])
            if hashlib.sha256(result_path.read_bytes()).hexdigest() != pending["results_sha256"]:
                raise BatchStopped("Retrieved Batch results changed")
            replies = json.loads(result_path.read_text())
        else:
            replies = retrieve(api, job_path, pending, operation, wait=wait)
        # Roll back local attempt counters if DB reconnect retries ingestion;
        # committed answers/costs are already idempotent by their locked keys.
        snapshot = json.loads(json.dumps(state, default=str))

        def apply(conn, snapshot=snapshot, pending=pending, replies=replies):
            state.clear()
            state.update(json.loads(json.dumps(snapshot)))
            return ingest(
                conn, state, pending, state["items"][stage], replies, stage, preferences, prompts
            )

        counts = database(apply, wait=wait)
        items = state["items"][stage]
        jobs = state["jobs"][stage]
        pending = jobs[-1]
        pending.update(ingested_at=timestamp(), counts=counts)
        pending["wall_clock_seconds"] = (
            datetime.fromisoformat(pending["completed_at"])
            - datetime.fromisoformat(pending["submission_at"])
        ).total_seconds()
        pending["effective_known_cost_per_item_usd"] = str(
            Decimal(counts["known_cost_usd"]) / pending["item_count"]
        )
        save(state_path, state)
        save(job_path, pending)
        log.info("Batch results ingested run_id=%s stage=%s job=%s valid=%s invalid=%s "
                 "missing=%s provider_failed=%s terminal=%s known_cost_usd=%s",
                 state.get("run_id"), stage, pending["name"], counts.get("valid"), counts.get("invalid"),
                 counts.get("missing"), counts.get("provider_failed"), counts.get("terminal"),
                 counts.get("known_cost_usd"))
        if remote_failed:
            raise BatchStopped(
                f"{stage} Batch {pending['status']}; available partial results ingested"
            )
        if budget_used(state) + budget_held(state) > Decimal(state["ceiling_usd"]):
            raise BatchStopped("Batch accounting ceiling reached; no further submission")
        if state.get("unknown"):
            raise BatchStopped("Batch billing gap retained; reconcile before further submission")
    refused = [digest for digest, i in items.items() if i.get("outcome") == "provider_failed"]
    if any(spending_refusal(items[digest].get("last_provider_error")) for digest in refused):
        raise BatchStopped(f"{stage}: Gemini spending limit reached")
    if len(refused) > REFUSED_INPUT_LIMIT:
        raise BatchStopped(
            f"{stage} provider items still failed after bounded residual recovery: "
            f"Gemini refused {len(refused)} inputs, more than {REFUSED_INPUT_LIMIT}"
        )
    if refused:
        database(lambda conn: set_aside(conn, items, refused, stage, preferences, prompts))
        log.warning("Gemini refused inputs left for a later night run_id=%s stage=%s inputs=%s",
                    state.get("run_id"), stage, len(refused))
    log.info("Batch stage completed run_id=%s stage=%s distinct_inputs=%s jobs=%s",
             state.get("run_id"), stage, len(items), len(jobs))


def set_aside(conn, items, refused, stage, preferences, prompts):
    """Leave refused inputs' listings unread; a later night sends them again."""
    from .bucket_job_listings import bucket_listing

    decisions = []
    for digest in refused:
        rows = pg_read.read_rows(
            conn, "SELECT * FROM job_sorting.job_listings WHERE id=ANY(%s)", (items[digest]["ids"],)
        )
        decisions.extend(
            bucket_listing({**row, "reason": REFUSED_REASON}, None, None, preferences)
            for row in rows
            if inputs(row, prompts)[stage][0] == digest
        )
    pg_write.update_results(conn, decisions)


def cleanup_caches(state, keys):
    """Delete prompt caches only after every submitted job has reached a terminal state."""
    from .call_gemini_batch import TERMINAL

    if any(
        j.get("status") == "submitting" or (j.get("name") and j.get("status") not in TERMINAL)
        for jobs in state.get("jobs", {}).values()
        for j in jobs
    ):
        return
    for stage, cached in state.get("caches", {}).items():
        if not cached or not cached.get("name") or cached.get("deleted_at"):
            continue
        try:
            API(keys[stage]).json(cached["name"], method="DELETE")
        except ProviderError as error:
            if error.status != 404:
                cached["deletion_unconfirmed_at"] = timestamp()
                log.warning("Gemini cache deletion unconfirmed run_id=%s stage=%s http_status=%s",
                            state.get("run_id"), stage, error.status)
                continue
        cached["deleted_at"] = timestamp()
    # Retain the original conservative storage allowance in the run ledger.


def run(
    preferences,
    prompts,
    keys,
    *,
    state_dir,
    source=None,
    ceiling,
    estimate,
    owner_dir,
    estimate_basis="conservative",
    enqueued_token_limit=2500000,
    publish_review=None,
    reconcile=None,
    backfill_started_at=None,
):
    """Resume the saved logical run; never use a fresh directory to resubmit it."""
    from bto.send_notifications.notify import bucket_validation_report

    from .run import refresh_repeats

    state_dir = Path(state_dir).resolve()
    state_path = state_dir / "batch.json"
    signature = pg_write.hash_input([MODEL, THINKING, prompts])
    if estimate_basis not in {"expected", "conservative"}:
        raise ValueError("Unknown Batch estimate admission basis")
    with run_owner(owner_dir) as active_path:
        active = json.loads(active_path.read_text()) if active_path.exists() else None
        if active and active["path"] != str(state_path) and active["status"] != "complete":
            raise BatchStopped("Resume the existing unfinished Batch state: " + active["path"])
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if state and state["signature"] != signature:
            raise BatchStopped("Model/prompt contract differs from the saved Batch run")
        if state and source is not None and state["source"] != source:
            raise BatchStopped("Resume against the saved coherent source boundary first")
        if state and state["status"] == "complete":
            log.info("Completed Batch run retained run_id=%s checkpoint=%s",
                     state.get("run_id"), state_path)
            return state
        prepared_this_time = state is None
        if state is None:
            preparation_started = timestamp()
            source_context = {}
            if reconcile:
                source_context = database(reconcile)
                source = source_context["versions"]
            items, counts = database(lambda c: prepare(c, preferences, prompts))
            if callable(estimate):
                estimate = estimate(items)
            save(state_dir / "estimate.json", estimate)
            log.info("Batch budget admission checkpoint=%s basis=%s estimate_usd=%s ceiling_usd=%s",
                     state_path, estimate_basis, estimate[estimate_basis + "_usd"], ceiling)
            if Decimal(str(estimate[estimate_basis + "_usd"])) > ceiling:
                save(
                    state_dir / "preflight-failure.json",
                    {
                        "status": "failed",
                        "failed_stage": "Batch budget admission",
                        "started_at": timestamp(),
                        "source": source,
                        "preparation": counts,
                        "estimate": estimate,
                        "first_id": source_context.get("first_id", 0),
                        "error": estimate_basis.capitalize()
                        + " Batch billing estimate exceeds the authorised ceiling",
                        "estimate_basis": estimate_basis,
                        "safely_resumable": True,
                        "jobs": {},
                        "receipts": {},
                    },
                )
                raise BatchStopped(
                    estimate_basis.capitalize()
                    + " Batch billing estimate exceeds the authorised ceiling"
                )
            state = {
                "run_id": uuid4().hex,
                "started_at": timestamp(),
                "status": "running",
                "signature": signature,
                "source": source,
                "model": MODEL,
                "ceiling_usd": str(ceiling),
                "estimate": estimate,
                "estimate_basis": estimate_basis,
                "enqueued_token_limit": enqueued_token_limit,
                "stage": "preparation",
                "items": items,
                "preparation": counts,
                "preparation_timing": {
                    "started_at": preparation_started,
                    "finished_at": timestamp(),
                },
                "pull": source_context.get("pull"),
                "first_id": source_context.get("first_id", 0),
                "last_id": source_context.get("last_id"),
            }
            save(state_path, state)
        if state.get("estimate_basis", "conservative") != estimate_basis:
            raise BatchStopped("Saved Batch estimate basis differs from this execution authority")
        if Decimal(state["ceiling_usd"]) != ceiling:
            raise BatchStopped("Saved Batch ceiling differs from this execution authority")
        publication_only = bool(
            state.get("validation") is not None
            and (
                state.get("failed_stage") == "Review publication"
                or state.get("stage") == "Review publication"
            )
        )
        log.info("Batch run %s run_id=%s publication_only=%s checkpoint=%s",
                 "started" if prepared_this_time else "resumed", state.get("run_id"),
                 publication_only, state_path)
        # Failure evidence remains in batch-failure.json; a resumed run's
        # current status and eventual success must not carry a stale failure.
        for field in (
            "error",
            "error_type",
            "failed_stage",
            "finished_at",
            "provider_error",
            "safely_resumable",
        ):
            state.pop(field, None)
        if backfill_started_at and state.get("backfill_started_at") != backfill_started_at:
            state.update(backfill_started_at=backfill_started_at, backfill_steps=[])
        if (
            backfill_started_at
            and prepared_this_time
            and "preparation" not in state["backfill_steps"]
        ):
            state["backfill_steps"].append("preparation")
        state.update(status="running")
        save(state_path, state)
        save(active_path, {"path": str(state_path), "status": "running", "run_id": state["run_id"]})
        try:
            if not publication_only:
                if "items" not in state:
                    items, counts = database(lambda c: prepare(c, preferences, prompts))
                    state.update(items=items, preparation=counts)
                    save(state_path, state)
                for stage in ("triage", "extraction"):
                    if (
                        backfill_started_at
                        and any(
                            item.get("outcome") not in {"valid", "terminal", "provider_failed"}
                            for item in state["items"][stage].values()
                        )
                        and stage not in state["backfill_steps"]
                    ):
                        state["backfill_steps"].append(stage)
                    state["stage"] = stage
                    save(state_path, state)
                    run_stage(state, state_path, stage, preferences, prompts, keys)
                    if stage == "triage" and not state.get("extraction_prepared"):
                        items, counts = database(lambda c: prepare(c, preferences, prompts))
                        state["items"]["extraction"] = items["extraction"]
                        state.update(extraction_prepared=True, after_triage=counts)
                        save(state_path, state)
                if backfill_started_at and "rules and review" not in state["backfill_steps"]:
                    state["backfill_steps"].append("rules and review")
                state["rules_started_at"] = timestamp()
                state["stage"] = "rules and validation"
                save(state_path, state)

                def finish(conn):
                    remaining, counts = prepare(conn, preferences, prompts)
                    state["final_population"] = counts
                    # Inputs Gemini refused were left for a later night.
                    if any(
                        state["items"][s].get(digest, {}).get("outcome") != "provider_failed"
                        for s in ("triage", "extraction")
                        for digest in remaining[s]
                    ):
                        raise BatchStopped(
                            "Final full-window validation found genuinely unfinished inputs"
                        )
                    refresh_repeats(conn)
                    for identity, receipt in list(state.get("cost_write_pending", {}).items()):
                        if pg_write.record_gemini_cost(conn, receipt["stage"], receipt):
                            del state["cost_write_pending"][identity]
                    return bucket_validation_report(conn)

                state["validation"] = database(finish)
            state["stage"] = "Review publication"
            save(state_path, state)
            log.info("Batch validation complete run_id=%s publication_connected=%s",
                     state.get("run_id"), publish_review is not None)
            if publish_review:
                # Completed request bodies stay durable, but must not compete
                # with the old and replacement Review snapshots in host RAM.
                items = None
                state.clear()
                release_checkpoint_memory()
                try:
                    publication = publish_review()
                finally:
                    state.update(json.loads(state_path.read_text()))
                state["review_refresh"] = publication
                state["review_publication"] = "complete"
            else:
                state["review_publication"] = (
                    "Not connected; PostgreSQL is current, serving cache unchanged"
                )
            cleanup_caches(state, keys)
            state.update(
                status="complete", finished_at=timestamp(), known_batch_usd=str(budget_used(state))
            )
            log.info("Batch run completed run_id=%s known_cost_usd=%s publication=%s",
                     state.get("run_id"), state["known_batch_usd"], state["review_publication"])
        except Exception as error:
            if isinstance(error, ProviderError):
                # Detailed provider errors stay in the private run checkpoint.
                state["provider_error"] = error.details()
            log_failure(log, "Batch run failed run_id=%s stage=%s checkpoint=%s",
                        state.get("run_id"), state["stage"], state_path, error=error)
            state.update(
                status="failed",
                failed_stage=state["stage"],
                error=str(error),
                error_type=type(error).__name__,
                finished_at=timestamp(),
                safely_resumable=True,
            )
            raise
        finally:
            save(state_path, state)
            save(
                active_path,
                {"path": str(state_path), "status": state["status"], "run_id": state["run_id"]},
            )
    return state
