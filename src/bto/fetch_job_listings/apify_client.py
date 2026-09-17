"""Shared Apify plumbing for the actor-collected boards: Indeed and LinkedIn.

Called by:
    fetch_job_listings.indeed.fetch, fetch_job_listings.linkedin.fetch

Stdlib-only client for the Apify REST API. The safety property this module
owns: **starting an Actor is never blindly retried** — a lost start response
could otherwise buy the same run twice. A lost POST is reconciled read-only
against the actor's recent runs and adopted only on an exact input match.

Main pieces:
    ApifyClient.run_actor()   start one run, wait, return terminal metadata
                              plus the complete dataset
    ApifyClient.actor()       actor metadata (identity, builds, pricing)
    ApifyClient.account()     plan and month-to-date usage snapshot
    active_pricing()/actor_summary()   pricing helpers for preflight checks
"""

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
COST_SETTLE_SECONDS = 10          # run-cost fields take a moment to settle
DATASET_PAGE_SIZE = 1000
READ_ATTEMPTS = 3
READ_RETRY_SECONDS = (1, 3)
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# IncompleteRead is an HTTPException, not an OSError.
TRANSPORT_ERRORS = (http.client.HTTPException, urllib.error.URLError,
                    TimeoutError, OSError)


class ApifyApiError(RuntimeError):
    """The Apify API returned an invalid or unsuccessful response."""


class AmbiguousActorStartError(ApifyApiError):
    """A billed start may have landed, but no single run could be reconciled."""


class ApifyClient:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, *, query=None, payload=None, timeout=90):
        """Call Apify, retrying only idempotent reads (GET). POSTs get exactly
        one HTTP attempt; run_actor() reconciles a lost start separately."""
        method = method.upper()
        query_string = urllib.parse.urlencode(query or {})
        url = f"{API_BASE}{path}"
        if query_string:
            url += f"?{query_string}"
        body = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        attempts = READ_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as error:
                retryable = (method == "GET"
                             and error.code in TRANSIENT_HTTP_STATUSES
                             and attempt + 1 < attempts)
                try:
                    raw = error.read()
                except http.client.IncompleteRead as read_error:
                    if retryable:
                        log.warning("Apify GET %s HTTP %s; retrying",
                                    path, error.code)
                        time.sleep(READ_RETRY_SECONDS[attempt])
                        continue
                    raise ApifyApiError(
                        f"Apify {method} {path} HTTP {error.code} response "
                        f"body transport error: {read_error}") from None
                if retryable:
                    log.warning("Apify GET %s HTTP %s; retrying", path, error.code)
                    time.sleep(READ_RETRY_SECONDS[attempt])
                    continue
                try:
                    detail = json.loads(raw)
                    error_info = detail.get("error") or {}
                    message = error_info.get("message") or str(detail)
                    error_type = error_info.get("type") or "unknown"
                except json.JSONDecodeError:
                    message = raw[:300].decode(errors="replace")
                    error_type = "invalid-json"
                raise ApifyApiError(
                    f"Apify {method} {path} HTTP {error.code}: "
                    f"{error_type}: {message}") from None
            except TRANSPORT_ERRORS as error:
                if method == "GET" and attempt + 1 < attempts:
                    log.warning("Apify GET %s transport error (%s); retrying", path, error)
                    time.sleep(READ_RETRY_SECONDS[attempt])
                    continue
                raise ApifyApiError(
                    f"Apify {method} {path} transport error: {error}") from None
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                if method == "GET" and attempt + 1 < attempts:
                    time.sleep(READ_RETRY_SECONDS[attempt])
                    continue
                raise ApifyApiError(
                    f"Apify {method} {path} returned invalid JSON: "
                    f"{raw[:200]!r}") from error
        raise AssertionError("unreachable Apify request retry state")

    def actor(self, actor_id):
        return self.request("GET", f"/acts/{actor_id}").get("data") or {}

    def account(self):
        """Plan and month-to-date usage: the quota check made before spending."""
        user = self.request("GET", "/users/me").get("data") or {}
        plan = user.get("plan") or {}
        usage = self.request("GET", "/users/me/usage/monthly").get("data") or {}
        spent = float(usage.get("totalUsageCreditsUsdAfterVolumeDiscount") or 0)
        included = float(plan.get("monthlyUsageCreditsUsd") or 0)
        return {
            "plan": plan.get("id") or plan.get("name") or "unknown",
            "includedUsd": included,
            "spentUsd": spent,
            "remainingIncludedUsd": max(0.0, included - spent),
        }

    def wait_for_run(self, run_id, timeout_s=900):
        deadline = time.monotonic() + timeout_s
        while True:
            run = self.request(
                "GET", f"/actor-runs/{run_id}",
                query={"waitForFinish": 30}, timeout=45,
            ).get("data") or {}
            if run.get("status") in TERMINAL_STATUSES:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Apify run {run_id} did not finish within {timeout_s} seconds")

    def _complete_dataset(self, dataset_id):
        """Fetch every item in a terminal dataset, verified against its
        declared size so a silently truncated read cannot pass as complete."""
        metadata = self.request("GET", f"/datasets/{dataset_id}").get("data") or {}
        item_count = metadata.get("itemCount")
        if not isinstance(item_count, int) or item_count < 0:
            raise ApifyApiError(f"Apify dataset {dataset_id} omitted a valid itemCount")

        rows = []
        while len(rows) < item_count:
            requested = min(DATASET_PAGE_SIZE, item_count - len(rows))
            page = self.request(
                "GET", f"/datasets/{dataset_id}/items",
                query={"clean": "false", "format": "json",
                       "limit": requested, "offset": len(rows)},
            )
            if not isinstance(page, list):
                raise ApifyApiError("Apify dataset page response was not a JSON array")
            if not page:
                raise ApifyApiError(
                    f"Apify dataset {dataset_id} ended after {len(rows)} of "
                    f"{item_count} declared items")
            if len(page) > requested:
                raise ApifyApiError(
                    f"Apify dataset page returned {len(page)} rows after "
                    f"requesting {requested}")
            rows.extend(page)

        if len(rows) != item_count:
            raise ApifyApiError(
                f"Apify dataset {dataset_id} returned {len(rows)} rows but "
                f"declared {item_count}")
        return metadata, rows

    @staticmethod
    def _instant(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _matching_recent_runs(self, actor_id, actor_input, started_after):
        """Read-only reconciliation after an ambiguous POST response: find
        recent runs whose immutable INPUT exactly matches this start."""
        listing = self.request(
            "GET", f"/acts/{actor_id}/runs",
            query={"desc": "1", "limit": 100},
        ).get("data") or {}
        matches = []
        earliest = started_after - timedelta(seconds=30)
        latest = datetime.now(timezone.utc) + timedelta(seconds=30)
        for run in listing.get("items") or []:
            started = self._instant(run.get("startedAt"))
            if started is None or started < earliest or started > latest:
                continue
            store_id = run.get("defaultKeyValueStoreId")
            if not store_id:
                continue
            stored_input = self.request(
                "GET", f"/key-value-stores/{store_id}/records/INPUT")
            if stored_input == actor_input:
                matches.append(run)
        return matches

    def _reconcile_actor_start(self, actor_id, actor_input, started_after,
                               original_error):
        """Adopt one matching run after a lost POST response, or fail closed."""
        for pause in (0, 2, 5):
            if pause:
                time.sleep(pause)
            try:
                matches = self._matching_recent_runs(
                    actor_id, actor_input, started_after)
            except Exception:
                matches = []
            if len(matches) == 1:
                log.warning("Apify start response was lost; adopted run %s "
                            "after input reconciliation", matches[0].get("id"))
                return matches[0]
            if len(matches) > 1:
                break
        raise AmbiguousActorStartError(
            "Apify Actor start response was ambiguous and reconciliation "
            f"found {len(matches)} matching recent runs; no second start was "
            f"attempted ({original_error})") from original_error

    def run_actor(self, actor_id, actor_input, *, max_total_charge_usd=None,
                  timeout_s=900):
        """Start exactly one run and return its terminal metadata and dataset.

        Returns {"input", "run", "datasetMetadata", "dataset", "startReconciled"}.
        The run dict carries Apify's reported `usageTotalUsd`, which is the only
        spend figure ever recorded — never inferred from result count.
        """
        run_query = {"build": "latest", "timeout": int(timeout_s)}
        if max_total_charge_usd is not None:
            run_query["maxTotalChargeUsd"] = f"{max_total_charge_usd:.3f}"
        start_attempted_at = datetime.now(timezone.utc)
        reconciled = False
        try:
            started = self.request(
                "POST", f"/acts/{actor_id}/runs",
                query=run_query, payload=actor_input,
            ).get("data") or {}
            if not started.get("id"):
                raise ApifyApiError("Apify Actor start response did not include a run id")
        except Exception as error:
            started = self._reconcile_actor_start(
                actor_id, actor_input, start_attempted_at, error)
            reconciled = True
        run_id = started.get("id")
        log.info("Apify actor %s run %s started", actor_id, run_id)

        run = self.wait_for_run(run_id, timeout_s=timeout_s)
        # Read-only settlement pause: terminal metadata can briefly carry
        # preliminary usage totals. Never an Actor retry.
        time.sleep(COST_SETTLE_SECONDS)
        run = self.request("GET", f"/actor-runs/{run_id}").get("data") or run
        dataset_metadata = {}
        rows = []
        dataset_id = run.get("defaultDatasetId")
        if dataset_id:
            dataset_metadata, rows = self._complete_dataset(dataset_id)
        if not isinstance(rows, list):
            raise ApifyApiError("Apify dataset response was not a JSON array")
        return {
            "input": actor_input,
            "run": run,
            "datasetMetadata": dataset_metadata,
            "dataset": rows,
            "startReconciled": reconciled,
        }


def active_pricing(actor, now=None):
    """The actor's currently effective pricingInfos entry."""
    now = now or datetime.now(timezone.utc)
    eligible = []
    for info in actor.get("pricingInfos") or []:
        started = datetime.fromisoformat(info["startedAt"].replace("Z", "+00:00"))
        if started <= now:
            eligible.append((started, info))
    return max(eligible, default=(None, {}), key=lambda item: item[0])[1]


def actor_summary(actor):
    """Identity and pricing snapshot kept inside each stored actor envelope."""
    pricing = active_pricing(actor)
    latest = (actor.get("taggedBuilds") or {}).get("latest") or {}
    return {
        "id": actor.get("id"),
        "name": f"{actor.get('username')}/{actor.get('name')}",
        "modifiedAt": actor.get("modifiedAt"),
        "buildId": latest.get("buildId"),
        "buildNumber": latest.get("buildNumber"),
        "pricingModel": pricing.get("pricingModel"),
        "pricePerUnitUsd": pricing.get("pricePerUnitUsd"),
    }
