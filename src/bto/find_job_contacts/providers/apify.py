"""Run contact-finding Apify Actors within the search and account limits.

Reserve each run's maximum charge before starting it, and never retry a start
whose response was lost. Record run ids immediately, then fill in settled
provider-reported costs. Dataset reads wait for their item counts to settle.
Cost-write failures are logged and retried by a later search."""

import logging
import math
import threading
import time
from datetime import datetime, timezone

from ...fetch_job_listings.apify_client import TERMINAL_STATUSES, ApifyClient
from ...log_errors import log_failure
from ...storage.postgres import write
from .. import config

log = logging.getLogger(__name__)

# Actor ids used by LinkedIn people, profile and post searches.
COMPANY_EMPLOYEES = "Vb6LZkh4EqRlR0Ka9"
PROFILE_SCRAPER = "LpVuK3Zozwuipa5bp"
POST_SEARCH = "buIWk2uOUzTmcLsuB"
POST_REACTIONS = "S6mgSO5lezSZKi0zN"
POST_COMMENTS = "ZI6ykbLlGS3APaPE8"

RUN_TIMEOUT_S = 240
# Apify's run cost is not final until a few seconds after the run ends.
COST_SETTLE_SECONDS = 15
# Apify gives up to 5 seconds for a dataset's item count to catch up with its rows.
DATASET_COUNT_SETTLE_SECONDS = 6
DATASET_PAGE_SIZE = 1000


class ApifyRefused(RuntimeError):
    """Apify cannot be used for this search, or one of its runs did not succeed.
    The message is shown as the search's failure reason."""


class PeopleSearchRuns:
    """The Apify runs of one people search: its budget and its cost rows.

    Several step threads share one instance, so the budget is taken under a
    lock. `usd_left` falls by each run's maxTotalChargeUsd, the most the run
    can cost, because the actual cost is known only after the run settles.
    """

    def __init__(self, bto_run_id, *, client=None, budget_usd=None):
        budget_usd = config.APIFY_USD_PER_PEOPLE_SEARCH if budget_usd is None else budget_usd
        if (
            isinstance(budget_usd, bool) or not isinstance(budget_usd, (int, float))
            or not math.isfinite(budget_usd) or budget_usd < 0
        ):
            raise ValueError("Contact-search budget must be finite and nonnegative")
        self.bto_run_id = bto_run_id
        self.client = client or ApifyClient(config.read_key(config.APIFY_TOKEN))
        self.usd_left = budget_usd
        self.run_ids = []
        self.last_finished = 0.0
        self.lock = threading.Lock()
        self.cap_checked = False

    def check_monthly_cap(self):
        """Raise ApifyRefused unless the shared account is below its share of the cap."""
        if not self.client.token:
            raise ApifyRefused("the Apify token is not configured")
        try:
            limits = self.client.request("GET", "/users/me/limits").get("data") or {}
            cap = float((limits.get("limits") or {}).get("maxMonthlyUsageUsd") or 0)
            used = float((limits.get("current") or {}).get("monthlyUsageUsd") or 0)
        except Exception as error:
            log_failure(log, "Apify monthly usage read failed search=%s", self.bto_run_id,
                        error=error, level=logging.WARNING)
            raise ApifyRefused(f"Apify's monthly usage could not be read: {error}") from None
        if cap <= 0:
            raise ApifyRefused("Apify reported no monthly cap, so its usage cannot be checked")
        if used > cap * config.APIFY_MONTHLY_CAP_SHARE:
            raise ApifyRefused(
                f"Apify has used USD {used:.2f} of its USD {cap:.2f} monthly cap; "
                "collection keeps the rest")
        self.cap_checked = True

    def take_budget(self, wanted_usd, minimum_usd=0.0):
        """Reserve up to `wanted_usd` of the budget for one run. Returns the
        amount reserved, or None when less than `minimum_usd` is left."""
        with self.lock:
            granted = round(min(wanted_usd, self.usd_left), 4)
            if granted <= 0 or granted < minimum_usd:
                return None
            self.usd_left -= granted
            return granted

    def run(self, actor_id, actor_input, *, max_charge_usd, minimum_charge_usd=0.0,
            max_items=None):
        """Start one Actor run, wait for it and return its dataset rows.

        Returns None, starting nothing, when the search's budget cannot cover
        `minimum_charge_usd` (some Actors refuse a lower maxTotalChargeUsd).
        Raises ApifyRefused when the account is above its share of the monthly
        cap, or the run cannot be started or does not succeed.
        """
        # The cap is checked before this search's first run, whichever step starts it.
        if not self.cap_checked:
            self.check_monthly_cap()
        charge = self.take_budget(max_charge_usd, minimum_charge_usd)
        if charge is None:
            log.info("Apify run skipped actor=%s search=%s budget_left=%.3f",
                     actor_id, self.bto_run_id, self.usd_left)
            return None
        query = {"timeout": RUN_TIMEOUT_S, "maxTotalChargeUsd": f"{charge:.3f}"}
        if max_items is not None:
            query["maxItems"] = max_items
        try:
            started = self.client.request(
                "POST", f"/acts/{actor_id}/runs", query=query, payload=actor_input,
            ).get("data") or {}
        except Exception as error:
            log_failure(log, "Apify start failed search=%s actor=%s; start will not be retried",
                        self.bto_run_id, actor_id, error=error, level=logging.WARNING)
            raise ApifyRefused(f"Apify did not start the run: {error}") from None
        run_id = started.get("id")
        if not run_id:
            raise ApifyRefused("Apify's start response carried no run id")
        self._record_run_start(run_id, actor_id, started.get("startedAt"))

        try:
            run = self.client.wait_for_run(run_id, timeout_s=RUN_TIMEOUT_S + 60)
        except Exception as error:
            log_failure(log, "Apify run polling failed search=%s run=%s",
                        self.bto_run_id, run_id, error=error, level=logging.WARNING)
            raise ApifyRefused(f"Apify stopped answering about run {run_id}: {error}") from None
        finally:
            with self.lock:
                self.last_finished = time.monotonic()
        if run.get("status") != "SUCCEEDED":
            raise ApifyRefused(f"Apify run {run_id} ended with status {run.get('status')}")
        time.sleep(DATASET_COUNT_SETTLE_SECONDS)
        try:
            return self._dataset_rows(run.get("defaultDatasetId"))
        except Exception as error:
            log_failure(log, "Apify dataset read failed search=%s run=%s",
                        self.bto_run_id, run_id, error=error, level=logging.WARNING)
            raise ApifyRefused(f"Apify run {run_id}'s rows could not be read: {error}") from None

    def record_costs(self):
        """Fill in the cost of every run this search started, from a read made
        after the costs have settled. Fail soft: a cost that cannot be read or
        written is logged and stays NULL, and so does the cost of a run that
        has not ended; record_unread_costs reads them again later."""
        wait = COST_SETTLE_SECONDS - (time.monotonic() - self.last_finished)
        if self.run_ids and wait > 0:
            time.sleep(wait)
        for run_id in self.run_ids:
            _record_cost_of_ended_run(self.client, run_id)

    def _record_run_start(self, run_id, actor_id, started_at):
        self.run_ids.append(run_id)
        try:
            write.record_apify_find_job_contacts_run({
                "apify_run_id": run_id,
                "bto_run_id": self.bto_run_id,
                "apify_actor_id": actor_id,
                "started_at": started_at or datetime.now(timezone.utc),
            })
        except Exception as error:
            log_failure(log, "Apify run row not recorded run=%s search=%s",
                        run_id, self.bto_run_id, error=error)

    def _dataset_rows(self, dataset_id):
        """Every row of a finished run's dataset, checked against its declared
        size so a cut-short read cannot pass as complete."""
        if not dataset_id:
            return []
        metadata = self.client.request("GET", f"/datasets/{dataset_id}").get("data") or {}
        item_count = metadata.get("itemCount")
        if not isinstance(item_count, int) or item_count < 0:
            raise ApifyRefused(f"Apify dataset {dataset_id} gave no item count")
        rows = []
        while len(rows) < item_count:
            page = self.client.request(
                "GET", f"/datasets/{dataset_id}/items",
                query={"clean": "false", "format": "json",
                       "limit": DATASET_PAGE_SIZE, "offset": len(rows)},
            )
            if not isinstance(page, list) or not page:
                raise ApifyRefused(
                    f"Apify dataset {dataset_id} ended after {len(rows)} of {item_count} rows")
            rows.extend(page)
        return [row for row in rows if isinstance(row, dict)]


def record_unread_costs(run_ids, *, client=None):
    """Fill in costs left NULL by earlier searches: runs still going when
    their search read them, such as those of a search cut off by a restart,
    and reads or writes that failed at the time. `run_ids` come from
    read.apify_runs_without_cost. Fail soft, like record_costs."""
    if not run_ids:
        return
    client = client or ApifyClient(config.read_key(config.APIFY_TOKEN))
    if not client.token:
        log.warning("Apify costs not read: the Apify token is not configured runs=%d",
                    len(run_ids))
        return
    for run_id in run_ids:
        _record_cost_of_ended_run(client, run_id, settled_only=True)


def _record_cost_of_ended_run(client, run_id, *, settled_only=False):
    """Write one run's reported cost once the run has ended. Fail soft.

    A run still going keeps its NULL cost, because its figure is not final.
    With `settled_only`, so does a run that ended less than
    COST_SETTLE_SECONDS ago: record_costs waits for that itself, but an
    earlier run's cost is read without waiting.
    """
    try:
        run = client.request("GET", f"/actor-runs/{run_id}").get("data") or {}
        if run.get("status") not in TERMINAL_STATUSES:
            log.info("Apify cost not final yet run=%s status=%s", run_id, run.get("status"))
            return
        if settled_only:
            try:
                ended = datetime.fromisoformat(str(run.get("finishedAt")).replace("Z", "+00:00"))
            except ValueError:
                return
            if (datetime.now(timezone.utc) - ended).total_seconds() < COST_SETTLE_SECONDS:
                return
        cost = run.get("usageTotalUsd")
        write.record_apify_find_job_contacts_cost(
            run_id, float(cost) if cost is not None else None)
    except Exception as error:
        log_failure(log, "Apify cost not recorded run=%s", run_id, error=error)
