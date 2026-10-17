"""Estimate Batch billing from the current cohort and measured token usage."""

import math
import statistics
from datetime import datetime, timedelta, timezone

from bto.storage.postgres import job_sorting as store

from .call_gemini import MODEL, _price_factor
from .call_gemini_batch import API, BatchStopped, read_retry
from .prepare_batches import generation_request


def estimate(conn, items, prompts, preferences, keys):
    triage = items["triage"]
    extraction = items["extraction"]
    valid, passed = conn.execute(
        "SELECT count(*),count(*) FILTER(WHERE verdict='pass') FROM job_sorting.triage_results"
    ).fetchone()
    if not valid:
        raise BatchStopped("A fresh approved estimate needs measured triage/extraction usage")
    rate = passed / valid
    upper_rate = min(1, rate + 2 * math.sqrt(rate * (1 - rate) / valid))
    # Before triage the extraction population is conditional, never submitted.
    # Counting every unresolved wording as distinct is a conservative allowance
    # for advertiser differences within otherwise identical triage inputs.
    potential = sum(len(i["ids"]) for i in triage.values())
    numbers = {
        "triage": (len(triage), len(triage)),
        "extraction": (
            len(extraction) + math.ceil(potential * rate),
            len(extraction) + math.ceil(potential * upper_rate),
        ),
    }
    stages = {}
    factor = _price_factor(datetime.now(timezone.utc) + timedelta(hours=49))
    for stage in ("triage", "extraction"):
        population = list(items[stage].values())
        if not population and stage == "extraction":
            from .run import inputs

            population = [
                {**i, "listing": inputs(i["row"], prompts)["extraction"][1]}
                for i in triage.values()
            ]
        calls = store.read_rows(
            conn,
            f"SELECT input_tokens,cached_tokens,output_tokens FROM costs.gemini_{stage} ORDER BY recorded_at DESC LIMIT 2000",
        )
        if numbers[stage][0] and not calls:
            raise BatchStopped("Measured usage unavailable for " + stage)
        if not population:
            stages[stage] = {
                "expected_usd": 0,
                "conservative_usd": 0,
                "uncached_usd": 0,
                "expected_inputs": 0,
                "conservative_inputs": 0,
            }
            continue
        api = API(keys[stage])
        fixed_count = read_retry(
            lambda api=api, stage=stage: api.json(
                f"models/{MODEL}:countTokens",
                {"contents": [{"parts": [{"text": prompts[stage][0]}]}]},
                "POST",
            )
        )["totalTokens"]
        empty = dict(population[0], listing="")
        empty_body = generation_request(stage, empty, prompts[stage][0], preferences)
        empty_body["model"] = "models/" + MODEL
        base_count = read_retry(
            lambda api=api, empty_body=empty_body: api.json(
                f"models/{MODEL}:countTokens", {"generateContentRequest": empty_body}, "POST"
            )
        )["totalTokens"]
        ordered = sorted(population, key=lambda i: len(i["listing"].encode()))
        indices = sorted(
            {min(len(ordered) - 1, int(len(ordered) * q)) for q in (0, 0.25, 0.5, 0.75, 0.95, 0.99)}
        )
        samples = []
        for index in indices:
            body = generation_request(stage, ordered[index], prompts[stage][0], preferences)
            body["model"] = "models/" + MODEL
            count = read_retry(
                lambda api=api, body=body: api.json(
                    f"models/{MODEL}:countTokens", {"generateContentRequest": body}, "POST"
                )
            )["totalTokens"]
            samples.append(
                max(1, count - base_count) / max(1, len(ordered[index]["listing"].encode()))
            )
        input_mean = statistics.mean(len(i["listing"].encode()) for i in population)
        overhead = max(0, base_count - fixed_count)
        mean_tokens = input_mean * statistics.mean(samples) + overhead + 32
        conservative_tokens = input_mean * max(samples) * 1.1 + overhead + 64
        outputs = sorted(r["output_tokens"] for r in calls)
        out_mean = statistics.mean(outputs)
        out_high = outputs[min(len(outputs) - 1, math.ceil(len(outputs) * 0.90))]
        charged = conn.execute(f"SELECT count(*) FROM costs.gemini_{stage}").fetchone()[0]
        reusable = conn.execute(f"SELECT count(*) FROM job_sorting.{stage}_results").fetchone()[0]
        multiplier = max(1.1, charged / max(1, reusable))
        n, high = numbers[stage]
        normal = n * (mean_tokens * 0.375 + fixed_count * 0.0375 + out_mean * 1.875) / 1e6
        output_allowance = (
            out_mean + 2 * statistics.stdev(outputs) / math.sqrt(len(outputs))
            if len(outputs) > 1
            else out_mean
        )
        conservative = (
            high
            * (conservative_tokens * 0.375 + fixed_count * 0.075 + output_allowance * 1.875)
            * multiplier
            * 1.25
            / 1e6
        )
        uncached = (
            high
            * ((conservative_tokens + fixed_count) * 0.375 + output_allowance * 1.875)
            * multiplier
            * 1.25
            / 1e6
        )
        cache = fixed_count * (0.75 + 0.5 * 49) / 1e6
        stages[stage] = {
            "expected_inputs": n,
            "conservative_inputs": high,
            "known_pass_distinct_inputs": len(extraction) if stage == "extraction" else None,
            "measured_usage_responses": len(calls),
            "mean_output_tokens": out_mean,
            "p90_output_tokens": out_high,
            "mean_current_listing_bytes": input_mean,
            "sampled_token_byte_ratios": samples,
            "fixed_tokens": fixed_count,
            "schema_message_overhead_tokens": overhead,
            "retry_multiplier": multiplier,
            "expected_usd": (normal + cache) * factor,
            "conservative_usd": (conservative + cache) * factor,
            "uncached_usd": uncached * factor,
            "cache_49_hour_allowance_usd": cache * factor,
        }
    return {
        "expected_usd": sum(s["expected_usd"] for s in stages.values()),
        "conservative_usd": sum(s["conservative_usd"] for s in stages.values()),
        "conservative_uncached_usd": sum(s["uncached_usd"] for s in stages.values()),
        "measured_triage_pass_fraction": rate,
        "triage_pass_fraction_upper": upper_rate,
        "potential_extraction_wordings_before_triage": potential,
        "stages": stages,
        "pricing": {
            "model": MODEL,
            "input_per_million_usd": ".375",
            "output_per_million_usd": "1.875",
            "cached_per_million_usd": ".0375",
            "conservative_cached_per_million_usd": ".075",
        },
        "maximum": "No provider dollar cap per Batch; submit only finite request chunks whose UTF-8 input-token upper bounds plus unchanged output limits fit the remaining local ceiling. No Flex model-limit reservations.",
        "historical_flex_liabilities": "Separate retained ledgers; excluded from this Batch authority",
    }
