"""Two-stage Batch workflow with short database ownership and durable results."""

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from bto.storage.postgres import job_sorting as store
from bto.storage.postgres.connection import connect

from .call_gemini import MODEL, PRICES, THINKING, ProviderError, _price_factor, is_busy, usage_cost
from .call_gemini_batch import (
    API,
    BatchStopped,
    correlate,
    poll,
    read_retry,
    retrieve,
    run_owner,
    save,
    submit,
    timestamp,
)
from .prepare_batches import checked_answer, generation_request, prepare

log = logging.getLogger(__name__)


def database(operation, *, wait=time.sleep):
    """Only idempotent store operations use this bounded reconnect policy."""
    import psycopg

    for attempt in range(4):
        try:
            with connect() as conn, store.sorting_owner(conn):
                return operation(conn)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if attempt == 3:
                raise
            wait(5 * 2**attempt)


def upper_tokens(body, fixed):
    # UTF-8 bytes plus schema/message allowance bounds text tokenization. This
    # is the actual finite request, not the model's million-token input limit.
    return len(json.dumps(body, ensure_ascii=False).encode()) + len(fixed.encode()) + 1024


def upper_cost(tokens, stage, at):
    factor = _price_factor(at + timedelta(hours=49))
    return (
        (
            Decimal(tokens) * PRICES["batch"][0]
            + Decimal(1024 if stage == "triage" else 8192) * PRICES["batch"][2]
        )
        * factor
        / 1000000
    )


def budget_used(state):
    return sum((Decimal(r["cost_usd"]) for r in state.get("receipts", {}).values()), Decimal(0))


def budget_held(state):
    return sum((Decimal(v) for v in state.get("unknown", {}).values()), Decimal(0)) + Decimal(
        state.get("cache_upper_usd", "0")
    )


def choose_chunk(
    items, stage, fixed, preferences, available, token_limit, cache=None, *, cached_tokens=None
):
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
            allowance = Decimal(cached["tokens"]) * Decimal("0.50") * extra / 1000000
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
    allowance = Decimal(tokens) * (Decimal("0.75") + Decimal("0.50") * 49) / 1000000
    if budget_used(state) + budget_held(state) + allowance > Decimal(state["ceiling_usd"]):
        raise BatchStopped("Cache allowance would exceed the Batch ceiling")
    state["cache_upper_usd"] = str(Decimal(state.get("cache_upper_usd", "0")) + allowance)
    caches[stage] = {"submission_unknown": True}
    save(state_path, state)
    from .call_gemini import ProviderError

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
    from .run import inputs

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
            if "spend" in str(error.get("message", "")).casefold():
                transient = False
            if not transient or item["provider_failures"] >= 3:
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
            if not store.record_gemini_cost(conn, stage, record):
                state.setdefault("cost_write_pending", {})[record["gemini_response_id"]] = {
                    "stage": stage,
                    **record,
                }
        item["answer_attempts"] += 1
        try:
            answer = checked_answer(stage, model_response, item, preferences)
        except (ValueError, TypeError, KeyError) as error:
            item["correction"] = str(error)
            counts["invalid"] += 1
            if item["answer_attempts"] >= 2:
                item["outcome"] = "terminal"
                counts["terminal"] += 1
                rows = store.read_rows(
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
                store.update_results(conn, decisions)
            continue
        ids = []
        for start in range(0, len(item["ids"]), 100):
            rows = store.read_rows(
                conn,
                "SELECT id,title,description_text,advertiser_name,hiring_company_name FROM job_sorting.job_listings WHERE id=ANY(%s)",
                (item["ids"][start : start + 100],),
            )
            ids.extend(r["id"] for r in rows if inputs(r, prompts)[stage][0] == digest)
        store.store_answer(conn, stage, digest, answer, ids)
        item["outcome"] = "valid"
        counts["valid"] += 1
    counts["known_cost_usd"] = str(sum((r["cost_usd"] for r in job_receipts.values()), Decimal(0)))
    counts["billed_items"] = len(job_receipts)
    return counts


def run_stage(state, state_path, stage, preferences, prompts, keys, *, wait=time.sleep):
    items = state["items"][stage]
    api = API(keys[stage])
    stage_dir = state_path.parent / stage
    stage_dir.mkdir(exist_ok=True)
    jobs = state.setdefault("jobs", {}).setdefault(stage, [])
    while any(
        i.get("outcome") not in {"valid", "terminal", "provider_failed"} for i in items.values()
    ):
        pending = next((j for j in jobs if not j.get("ingested_at")), None)
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
                cached_tokens=state["estimate"].get("stages", {}).get(stage, {}).get("fixed_tokens"),
            )
            number = len(jobs) + 1
            directory = stage_dir / str(number)
            directory.mkdir(exist_ok=True)
            input_path = directory / "input.jsonl"
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
                    "input": "0.375",
                    "cached": "0.0375",
                    "output": "1.875",
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
            # Submission data is private, immutable and durable BEFORE upload/POST.
            save(directory / "input-lines.json", lines)
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
        if remote_failed:
            raise BatchStopped(
                f"{stage} Batch {pending['status']}; available partial results ingested"
            )
        if budget_used(state) + budget_held(state) > Decimal(state["ceiling_usd"]):
            raise BatchStopped("Batch accounting ceiling reached; no further submission")
        if state.get("unknown"):
            raise BatchStopped("Batch billing gap retained; reconcile before further submission")
    if any(i.get("outcome") == "provider_failed" for i in items.values()):
        raise BatchStopped(f"{stage} provider items still failed after bounded residual recovery")


def cleanup_caches(state, keys):
    from .call_gemini import ProviderError
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
):
    """Resume the saved logical run; never use a fresh directory to resubmit it."""
    from bto.send_notifications.notify import bucket_validation_report

    from .run import refresh_repeats

    state_dir = Path(state_dir).resolve()
    state_path = state_dir / "batch.json"
    signature = store.hash_input([MODEL, THINKING, prompts])
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
            return state
        if state is None:
            source_context = {}
            if reconcile:
                source_context = database(reconcile)
                source = source_context["versions"]
            items, counts = database(lambda c: prepare(c, preferences, prompts))
            if callable(estimate):
                estimate = estimate(items)
            save(state_dir / "estimate.json", estimate)
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
                        "error": estimate_basis.capitalize() + " Batch billing estimate exceeds the authorised ceiling",
                        "estimate_basis": estimate_basis,
                        "safely_resumable": True,
                        "jobs": {},
                        "receipts": {},
                    },
                )
                raise BatchStopped(
                    estimate_basis.capitalize() + " Batch billing estimate exceeds the authorised ceiling"
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
                "pull": source_context.get("pull"),
                "first_id": source_context.get("first_id", 0),
            }
            save(state_path, state)
        if state.get("estimate_basis", "conservative") != estimate_basis:
            raise BatchStopped("Saved Batch estimate basis differs from this execution authority")
        if Decimal(state["ceiling_usd"]) != ceiling:
            raise BatchStopped("Saved Batch ceiling differs from this execution authority")
        # Failure evidence remains in batch-failure.json; a resumed run's
        # current status and eventual success must not carry a stale failure.
        for field in ("error", "error_type", "failed_stage", "finished_at", "provider_error", "safely_resumable"):
            state.pop(field, None)
        state.update(status="running")
        save(state_path, state)
        save(active_path, {"path": str(state_path), "status": "running", "run_id": state["run_id"]})
        try:
            if "items" not in state:
                items, counts = database(lambda c: prepare(c, preferences, prompts))
                state.update(items=items, preparation=counts)
                save(state_path, state)
            for stage in ("triage", "extraction"):
                state["stage"] = stage
                save(state_path, state)
                run_stage(state, state_path, stage, preferences, prompts, keys)
                if stage == "triage" and not state.get("extraction_prepared"):
                    items, counts = database(lambda c: prepare(c, preferences, prompts))
                    state["items"]["extraction"] = items["extraction"]
                    state.update(extraction_prepared=True, after_triage=counts)
                    save(state_path, state)
            state["stage"] = "rules and validation"
            save(state_path, state)

            def finish(conn):
                remaining, counts = prepare(conn, preferences, prompts)
                state["final_population"] = counts
                if any(remaining[s] for s in ("triage", "extraction")):
                    raise BatchStopped(
                        "Final full-window validation found genuinely unfinished inputs"
                    )
                refresh_repeats(conn)
                for identity, receipt in list(state.get("cost_write_pending", {}).items()):
                    if store.record_gemini_cost(conn, receipt["stage"], receipt):
                        del state["cost_write_pending"][identity]
                return bucket_validation_report(conn)

            state["validation"] = database(finish)
            state["stage"] = "Review publication"
            save(state_path, state)
            if publish_review:
                state["review_refresh"] = publish_review()
                state["review_publication"] = "complete"
            else:
                state["review_publication"] = (
                    "Not connected; PostgreSQL is current, serving cache unchanged"
                )
            cleanup_caches(state, keys)
            state.update(
                status="complete", finished_at=timestamp(), known_batch_usd=str(budget_used(state))
            )
        except Exception as error:
            if isinstance(error, ProviderError):
                # Detailed provider errors stay in the private run checkpoint.
                state["provider_error"] = {"http_status": error.status, "body": error.message}
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
