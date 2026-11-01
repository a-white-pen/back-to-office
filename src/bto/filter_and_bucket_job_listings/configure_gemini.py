"""Shared Gemini settings, input hashes and token-based USD cost calculations.

The Batch workflow uses these helpers; historical receipts retain their
reported service-tier prices."""

import json
import os
import re
import urllib.request
from datetime import date
from decimal import Decimal

from bto.storage.postgres.write import hash_input

MODEL = "gemini-3.8-flash"
THINKING = "LOW"
SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_CIVIC_INTEGRITY",
)
PRICES = {
    "batch": (Decimal("0.375"), Decimal("0.0375"), Decimal("1.875")),
    "flex": (Decimal("0.375"), Decimal("0.0375"), Decimal("1.875")),
    "standard": (Decimal("0.75"), Decimal("0.075"), Decimal("3.75")),
}

CACHE_STORAGE_PRICE = Decimal("0.50")


class ProviderError(Exception):
    def __init__(self, status, message, *, context=None):
        self.status = status
        self.message = message
        self.context = context or {}
        detail = str(message)
        try:
            problem = json.loads(message).get("error", {})
            detail = ": ".join(str(problem[k]) for k in ("status", "message") if problem.get(k))
        except (ValueError, TypeError, AttributeError):
            pass
        for name, value in os.environ.items():
            if value and any(word in name for word in ("PASSWORD", "TOKEN", "API_KEY", "SECRET")):
                detail = detail.replace(value, "[private]")
        detail = re.sub(r"(?:AIza[\w-]{20,}|AQ\.[\w.~-]{20,})", "[private]", detail)
        detail = " ".join(detail.split())[:500]
        super().__init__(f"Google HTTP status {status}" + (": " + detail if detail else ""))

    def details(self):
        """Raw provider diagnostics belong only in the private run checkpoint."""
        result = {"http_status": self.status, "body": self.message}
        if self.context:
            result["request"] = self.context
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError(code, "Unexpected redirect")


def input_hash(fixed, listing, model=MODEL, thinking=THINKING):
    return hash_input([fixed, model, thinking, listing])


def _price_factor(at):
    return 2 if at.date() >= date(2027, 1, 1) else 1


def prices(at, tier="batch"):
    """USD per million input, cached and output tokens at the supplied date."""
    return tuple(rate * _price_factor(at) for rate in PRICES[tier])


def cache_cost(tokens, hours, at, *, create=False):
    """Price cache creation and storage; budget callers supply the latest end date."""
    creation = prices(at, "standard")[0] if create else Decimal(0)
    storage = CACHE_STORAGE_PRICE * _price_factor(at)
    return Decimal(tokens) * (creation + storage * Decimal(str(hours))) / 1_000_000


def usage_cost(response, at, *, execution_tier=None):
    """Validate the usage receipt and price it using its execution tier and date."""
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
    try:
        rates = prices(at, tier.casefold())
    except KeyError:
        raise ValueError("No verified price for the reported service tier") from None
    cost = sum(
        Decimal(n) * rate for n, rate in zip([prompt - cached, cached, output], rates)
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
