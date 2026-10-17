"""Model identity, returned-usage pricing and legacy synchronous transport."""

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bto.storage.postgres.job_sorting import hash_input

from . import SINGAPORE, night

log = logging.getLogger(__name__)
MODEL = "gemini-3.8-flash"
THINKING = "LOW"
SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_CIVIC_INTEGRITY",
)
# Revalidate provider pricing before any approved live verification/catch-up.
MODEL_INPUT_LIMIT = 1_048_576
CACHE_STORAGE_RATE = Decimal("0.50")

PRICES = {
    "batch": (Decimal("0.375"), Decimal("0.0375"), Decimal("1.875")),
    "flex": (Decimal("0.375"), Decimal("0.0375"), Decimal("1.875")),
    "standard": (Decimal("0.75"), Decimal("0.075"), Decimal("3.75")),
}


class ProviderError(Exception):
    def __init__(self, status, message):
        super().__init__(f"Google HTTP status {status}")
        self.status = status
        self.message = message


class ReadDeferred(Exception):
    """A transport gate closed before this HTTP read was sent."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError(code, "Unexpected redirect")


def request_json(key, route, body, method="POST"):
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/" + route,
        data=None if method in {"GET", "DELETE"} else json.dumps(body).encode(),
        method=method,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
            "X-Server-Timeout": "900",
        },
    )
    try:
        # The server wait is a hint; allow delivery margin for a late receipt.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=960) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise ProviderError(error.code, message) from None
    except (ValueError, UnicodeError):
        return {}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(503, "Connection unavailable") from error


@dataclass
class CallBudget:
    mode: str
    ceiling: Decimal
    spent: Decimal = Decimal(0)
    now: object = lambda: datetime.now(timezone.utc)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: object = field(default_factory=threading.RLock)
    refused: set = field(default_factory=set)
    caches: dict = field(default_factory=dict)
    cache_locks: dict = field(
        default_factory=lambda: {"triage": threading.Lock(), "extraction": threading.Lock()}
    )
    notes: list = field(default_factory=list)
    ceiling_time: object = None
    response_ids: set = field(default_factory=set)
    usages: list = field(default_factory=list)
    on_usage: object = None
    on_budget: object = None
    pending_usd: Decimal = Decimal(0)
    cache_ceiling_usd: Decimal = Decimal(0)
    unknown_ceiling_usd: Decimal = Decimal(0)
    precharged_records: dict = field(default_factory=dict)
    admission_closed: bool = False
    guarded: bool = False

    def __post_init__(self):
        self.admission = threading.Condition(self.lock)

    def snapshot(self):
        with self.lock:
            return {
                "mode": self.mode,
                "night": night(self.now()).isoformat(),
                "guarded": self.guarded,
                "known_inference_usd": str(self.spent),
                "pending_upper_usd": str(self.pending_usd),
                "cache_upper_usd": str(self.cache_ceiling_usd),
                "unknown_upper_usd": str(self.unknown_ceiling_usd),
            }

    def restore(self, snapshot, recorded=Decimal(0)):
        # A crashed request may have been charged: retain its maximum, not zero.
        amounts = [
            Decimal(snapshot.get(key, "0"))
            for key in (
                "known_inference_usd",
                "pending_upper_usd",
                "cache_upper_usd",
                "unknown_upper_usd",
            )
        ]
        if any(not value.is_finite() or value < 0 for value in amounts):
            raise ValueError("Invalid saved Gemini budget")
        self.spent = max(amounts[0], recorded)
        self.cache_ceiling_usd = amounts[2]
        self.unknown_ceiling_usd = amounts[1] + amounts[3]
        self.guarded = True

    def _publish_budget(self):
        if self.on_budget:
            try:
                self.on_budget(self.snapshot())
            except OSError:
                self.stop.set()
                self.notes.append("Gemini budget checkpoint unavailable; remaining reads stopped")
                return False
        return True

    def protect(self, stage, transport):
        def send(key, route, body, method="POST"):
            generation = route.endswith(":generateContent")
            cache = route == "cachedContents" or method == "PATCH"
            if not generation and not cache:
                return transport(key, route, body, method)
            with self.admission:
                self.guarded = True
                while True:
                    reason = self.reason(stage)
                    if reason:
                        raise ReadDeferred(reason)
                    now = self.now()
                    # A sent Flex call can span the published price change.
                    factor = _price_factor(now + timedelta(minutes=16))
                    if cache:
                        expiry = datetime.fromisoformat(body["expireTime"].replace("Z", "+00:00"))
                        hours = Decimal(str(max(0, (expiry - now).total_seconds()) / 3600))
                        unit = (
                            max(rates[0] for rates in PRICES.values()) * factor
                            + CACHE_STORAGE_RATE * hours
                        )
                        maximum = MODEL_INPUT_LIMIT * unit / Decimal(1_000_000)
                    else:
                        maximum = (
                            (
                                MODEL_INPUT_LIMIT * max(r[0] for r in PRICES.values())
                                + body["generationConfig"]["maxOutputTokens"]
                                * max(r[2] for r in PRICES.values())
                            )
                            * factor
                            / Decimal(1_000_000)
                        )
                    exposure = (
                        self.spent
                        + self.cache_ceiling_usd
                        + self.unknown_ceiling_usd
                        + self.pending_usd
                    )
                    if exposure + maximum <= self.ceiling:
                        self.pending_usd += maximum
                        if not self._publish_budget():
                            self.pending_usd -= maximum
                            self.admission.notify_all()
                            raise ReadDeferred(self.reason(stage))
                        break
                    if not self.pending_usd:
                        self.admission_closed = True
                        self.ceiling_time = self.ceiling_time or now
                        self._publish_budget()
                        raise ReadDeferred("not read: spend ceiling reached")
                    self.admission.wait(timeout=1)
            try:
                reply = transport(key, route, body, method)
            except BaseException as error:
                with self.admission:
                    self.pending_usd -= maximum
                    if (
                        not isinstance(error, ProviderError)
                        or error.message == "Connection unavailable"
                        or cache
                        and error.status >= 500
                    ):
                        self.unknown_ceiling_usd += maximum
                        self.notes.append(
                            f"{stage}: request billing unavailable; maximum charge retained"
                        )
                    self._publish_budget()
                    self.admission.notify_all()
                raise
            with self.admission:
                self.pending_usd -= maximum
                if generation:
                    try:
                        record = usage_cost(reply, self.now())
                    except (ValueError, TypeError, AttributeError):
                        record = None
                    if record is None:
                        self.unknown_ceiling_usd += maximum
                        self.stop.set()
                        self.notes.append(
                            f"{stage}: Gemini accounting gap; maximum charge retained"
                        )
                    else:
                        identity = record["gemini_response_id"]
                        if (
                            identity not in self.response_ids
                            and identity not in self.precharged_records
                        ):
                            self.spent += record["cost_usd"]
                            self.precharged_records[identity] = record
                        if record["service_tier"].casefold() != "flex":
                            self.stop.set()
                else:
                    metadata = reply.get("usageMetadata") if isinstance(reply, dict) else None
                    tokens = metadata.get("totalTokenCount") if isinstance(metadata, dict) else None
                    if type(tokens) is not int or not 0 <= tokens <= MODEL_INPUT_LIMIT:
                        tokens = MODEL_INPUT_LIMIT
                    self.cache_ceiling_usd += tokens * unit / Decimal(1_000_000)
                self._publish_budget()
                self.admission.notify_all()
            return reply

        return send

    def reason(self, stage):
        with self.lock:
            if self.stop.is_set():
                return "not read: the night stopped early"
            if stage in self.refused:
                return "not read: Google refused"
            if (
                self.admission_closed
                or self.spent + self.cache_ceiling_usd + self.unknown_ceiling_usd >= self.ceiling
            ):
                if self.ceiling_time is None:
                    self.ceiling_time = self.now()
                return "not read: spend ceiling reached"
            current = self.now().astimezone(SINGAPORE)
            cutoff = datetime.combine(
                night(self.now()), datetime.min.time(), SINGAPORE
            ) + timedelta(hours=8, minutes=30)
            if self.mode == "night" and current >= cutoff:
                return "not read by 8:30 AM"
        return None


def input_hash(fixed, listing, model=MODEL, thinking=THINKING):
    return hash_input([fixed, model, thinking, listing])


def _price_factor(at):
    return 2 if at.date() >= datetime(2027, 1, 1).date() else 1


def usage_cost(response, at, *, execution_tier=None):
    usage = response.get("usageMetadata")
    if not usage:
        return None
    tier = usage.get("serviceTier") or response.get("serviceTier")
    # Batch is established by the remote Batch resource, not a claimed tier
    # in GenerateContentRequest. Record its billing path even if usage omits it.
    if execution_tier == "batch":
        tier = "batch"
    response_id = response.get("responseId")
    if not isinstance(tier, str) or not tier or not isinstance(response_id, str) or not response_id:
        raise ValueError("Returned usage is missing response identity or reported service tier")

    def count(key):
        value = usage.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid usage token count")
        return value

    cached = count("cachedContentTokenCount")
    prompt = count("promptTokenCount")
    if cached > prompt:
        raise ValueError("Cached tokens exceed prompt tokens")
    output = count("candidatesTokenCount") + count("thoughtsTokenCount")
    rates = PRICES.get(tier.casefold())
    if rates is None:
        raise ValueError("No verified price for the reported service tier")
    factor = _price_factor(at)
    cost = sum(
        Decimal(n) * rate * factor for n, rate in zip([prompt - cached, cached, output], rates)
    ) / Decimal(1_000_000)
    return {
        "gemini_response_id": response_id,
        "service_tier": tier,
        "input_tokens": prompt - cached,
        "cached_tokens": cached,
        "output_tokens": output,
        "cost_usd": cost.quantize(Decimal("0.000001")),
    }


def is_busy(error):
    refusal = any(
        word in error.message.casefold() for word in ("spending cap", "spend cap", "monthly cap")
    )
    return error.status in {429, 500, 502, 503, 504} and not refusal


def prepare_cache(stage, key, fixed, budget, transport):
    with budget.cache_locks[stage]:
        now = budget.now()
        end = (
            datetime.combine(night(now), datetime.min.time(), SINGAPORE) + timedelta(hours=9)
            if budget.mode == "night"
            else now + timedelta(hours=24)
        )
        end = end.astimezone(timezone.utc)
        cache = budget.caches.get(stage)
        if stage in budget.caches:
            if (
                cache
                and cache["expires"] - now < timedelta(minutes=30)
                and budget.mode == "catch-up"
            ):
                try:
                    transport(
                        key,
                        cache["name"] + "?updateMask=expireTime",
                        {"expireTime": end.isoformat()},
                        "PATCH",
                    )
                    cache["expires"] = end
                except (ProviderError, ValueError):
                    budget.caches[stage] = None
                    budget.notes.append(
                        f"{stage}: cache extension unavailable; fixed prompt sent in full"
                    )
                    return None
            return cache["name"] if cache else None
        try:
            reply = transport(
                key,
                "cachedContents",
                {
                    "model": "models/" + MODEL,
                    "systemInstruction": {"parts": [{"text": fixed}]},
                    "expireTime": end.isoformat(),
                },
            )
            if not isinstance(reply, dict):
                raise ValueError("Invalid cache response")
            name = reply.get("name")
            if not isinstance(name, str) or not name.startswith("cachedContents/"):
                raise ValueError("Invalid cache response")
            budget.caches[stage] = {"name": name, "expires": end}
            log.info("Gemini cache ready step=%s", stage)
            return name
        except (ProviderError, ValueError):
            budget.caches[stage] = None
            budget.notes.append(f"{stage}: cache unavailable; fixed prompt sent in full")
            log.warning("Gemini cache unavailable step=%s", stage)
            return None


def call_checked(
    stage,
    key,
    fixed,
    listing,
    schema,
    validate,
    budget,
    record_cost,
    *,
    transport=None,
    wait=None,
):
    transport = budget.protect(stage, request_json) if transport is None else transport
    wait = wait or budget.stop.wait
    corrections = None
    attempt = 0
    busy_wait = 5
    retried = False
    called = False
    tiers = set()
    while attempt < 2:
        reason = budget.reason(stage)
        if reason:
            log.info("Gemini deferred step=%s reason=%s", stage, reason)
            return {
                "answer": None,
                "reason": reason,
                "retried": retried,
                "called": called,
                "tiers": list(tiers),
            }
        previously_called = called
        try:
            cache = prepare_cache(stage, key, fixed, budget, transport)
            reason = budget.reason(stage)
            if reason:
                return {
                    "answer": None,
                    "reason": reason,
                    "retried": retried,
                    "called": called,
                    "tiers": list(tiers),
                }
            body = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": listing
                                + (
                                    "\n\nProblems to correct:\n" + corrections
                                    if corrections
                                    else ""
                                )
                            }
                        ],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                    "maxOutputTokens": 1024 if stage == "triage" else 8192,
                    "thinkingConfig": {"thinkingLevel": THINKING},
                },
                "service_tier": "flex",
                "safetySettings": [{"category": c, "threshold": "OFF"} for c in SAFETY_CATEGORIES],
            }
            if cache:
                body["cachedContent"] = cache
            else:
                body["systemInstruction"] = {"parts": [{"text": fixed}]}
            log.info("Gemini read started step=%s attempt=%d", stage, attempt + 1)
            called = True
            response = transport(key, f"models/{MODEL}:generateContent", body)
        except ReadDeferred as error:
            return {
                "answer": None,
                "reason": str(error),
                "retried": retried,
                "called": previously_called,
                "tiers": list(tiers),
            }
        except ProviderError as error:
            if is_busy(error):
                log.info(
                    "Gemini busy step=%s status=%d wait_seconds=%d", stage, error.status, busy_wait
                )
                wait(busy_wait)
                busy_wait = min(busy_wait * 2, 300)
                continue
            with budget.lock:
                budget.refused.add(stage)
            log.error("Gemini refused step=%s status=%d", stage, error.status)
            return {
                "answer": None,
                "reason": "not read: Google refused",
                "retried": retried,
                "called": called,
                "tiers": list(tiers),
            }
        try:
            with budget.lock:
                prepaid = (
                    budget.precharged_records.get(response.get("responseId"))
                    if isinstance(response, dict)
                    else None
                )
            record = prepaid if prepaid is not None else usage_cost(response, budget.now())
            if record is None and isinstance(response, dict) and response.get("candidates"):
                raise ValueError("Answer arrived without reported usage")
            if record:
                tiers.add(record["service_tier"].casefold())
                with budget.lock:
                    fresh = record["gemini_response_id"] not in budget.response_ids
                    if fresh:
                        budget.response_ids.add(record["gemini_response_id"])
                        prepaid = budget.precharged_records.pop(record["gemini_response_id"], None)
                        if prepaid is None:
                            budget.spent += record["cost_usd"]
                        budget.usages.append({"stage": stage, **record})
                if fresh:
                    try:
                        written = record_cost(stage, record)
                    except Exception:
                        written = False
                    if budget.on_usage is not None:
                        try:
                            budget.on_usage(stage, record)
                        except OSError:
                            budget.stop.set()
                            budget.notes.append(
                                "Spend checkpoint unavailable; remaining reads stopped"
                            )
                    if written is False:
                        budget.notes.append(f"{stage}: cost row could not be written")
                log.info("Gemini usage received step=%s cost_usd=%s", stage, record["cost_usd"])
                if record["service_tier"].casefold() != "flex":
                    budget.stop.set()
                    budget.notes.append(
                        "Google reported a non-Flex response; remaining reads stopped"
                    )
        except (ValueError, TypeError, AttributeError):
            # The returned answer may still be usable; unknown billing stops further spend.
            budget.stop.set()
            budget.notes.append("Gemini accounting gap; remaining reads stopped")
            log.error("Gemini usage could not be priced/identified step=%s", stage)
        try:
            if not isinstance(response, dict):
                raise ValueError("Invalid provider response")
            candidates = response.get("candidates") or []
            if not candidates or candidates[0].get("finishReason") != "STOP":
                raise ValueError("No complete answer")
            text = "".join(
                p.get("text", "")
                for p in candidates[0].get("content", {}).get("parts", [])
                if not p.get("thought")
            )
            answer = validate(json.loads(text))
            return {
                "answer": answer,
                "reason": None,
                "retried": retried,
                "called": called,
                "tiers": list(tiers),
            }
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            attempt += 1
            retried = attempt == 1 or retried
            corrections = str(error)
            log.info("Gemini answer validation failed step=%s attempt=%d", stage, attempt)
    return {
        "answer": None,
        "reason": f"{stage} failed twice",
        "retried": retried,
        "called": called,
        "tiers": list(tiers),
    }
