"""Read employer email domains and look up or verify work addresses with Hunter.

Charged requests run one at a time so account-credit changes can be assigned
to the right request. Record that change even when the request fails. Cost
writes fail softly, and the API key stays in a header rather than the URL."""

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from ...log_errors import log_failure
from ...storage.postgres import write

log = logging.getLogger(__name__)

BASE = "https://api.hunter.io/v2"
TIMEOUT_S = 30
# One charged request at a time, process-wide: see the module docstring.
CHARGED_REQUEST_LOCK = threading.Lock()


class HunterUnavailable(RuntimeError):
    """Hunter refused the request or could not be reached."""


class HunterClient:
    """Hunter requests for one people search. Credits rows carry its `bto_run_id`."""

    def __init__(self, key, bto_run_id):
        self.key = key
        self.bto_run_id = bto_run_id
        self.credits_used = 0.0
        self.last_credits_left = None

    def credits_left(self):
        """Remaining credits on the plan (a free account read)."""
        requests = self._get("account").get("requests") or {}
        credits = requests.get("credits") or {}
        if not isinstance(credits.get("remaining"), (int, float)):
            raise HunterUnavailable("Hunter's account did not report its remaining credits")
        self.last_credits_left = float(credits["remaining"])
        return self.last_credits_left

    def email_count(self, domain):
        """How many addresses Hunter knows at this domain (free)."""
        return int(self._get("email-count", domain=domain).get("total") or 0)

    def find_domains(self, company):
        """Hunter's Domain Finder (free): [{domain, company_name, email_count}]."""
        if len(company or "") < 3:
            return []
        answer = self._get("domain-finder", company=company)
        # Accept either a domain object or a ranked list of domains.
        items = answer if isinstance(answer, list) else [answer]
        return [item for item in items if isinstance(item, dict) and item.get("domain")]

    def domain_pattern(self, domain):
        """The domain's address pattern, such as "{first}.{last}", or None. A
        Domain Search: charged once when Hunter knows addresses there."""
        return self._charged("domain-search", domain=domain, limit=1).get("pattern")

    def find_email(self, domain, first_name, last_name):
        """Hunter's Email Finder for one person. Charged only when it finds an address."""
        return self._charged("email-finder", domain=domain,
                             first_name=first_name, last_name=last_name)

    def verify_email(self, email):
        """Hunter's Email Verifier: does the mailbox exist?"""
        return self._charged("email-verifier", email=email)

    def _charged(self, endpoint, **params):
        """One charged request, with the credits it took recorded."""
        with CHARGED_REQUEST_LOCK:
            before = self._credits_or_none()
            try:
                return self._get(endpoint, **params)
            finally:
                # Record credits even after failure: a lost response can still be charged.
                after = self._credits_or_none()
                credits = (round(before - after, 2)
                           if before is not None and after is not None else None)
                if credits:
                    self.credits_used += credits
                self._record_credits(endpoint, credits)

    def _credits_or_none(self):
        try:
            return self.credits_left()
        except Exception as error:
            log_failure(log, "Hunter credits unreadable search=%s", self.bto_run_id,
                        error=error, level=logging.WARNING)
            return None

    def _record_credits(self, endpoint, credits):
        try:
            write.record_hunter_find_job_contacts_credits({
                "bto_run_id": self.bto_run_id,
                "endpoint": endpoint,
                "credits": credits,
            })
        except Exception as error:
            log_failure(log, "Hunter credits not recorded endpoint=%s search=%s",
                        endpoint, self.bto_run_id, error=error)

    def _get(self, endpoint, **params):
        if not self.key:
            raise HunterUnavailable("the Hunter key is not configured")
        url = f"{BASE}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "X-API-KEY": self.key})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            # 404: nothing at all for this name or domain. 451: the person asked
            # Hunter not to process their data, so BTO treats it as not found.
            if error.code in (404, 451):
                return {}
            raise HunterUnavailable(f"Hunter answered HTTP {error.code} on {endpoint}") from None
        except (urllib.error.URLError, OSError) as error:
            log_failure(log, "Hunter request failed endpoint=%s search=%s",
                        endpoint, self.bto_run_id, error=error, level=logging.WARNING)
            if isinstance(error, TimeoutError) or isinstance(
                    getattr(error, "reason", None), TimeoutError):
                raise HunterUnavailable(f"timeout after {TIMEOUT_S} s") from None
            raise HunterUnavailable(f"Hunter could not be reached: {error}") from None
        except json.JSONDecodeError as error:
            log_failure(log, "Hunter response was not JSON endpoint=%s search=%s",
                        endpoint, self.bto_run_id, error=error, level=logging.WARNING)
            raise HunterUnavailable(f"Hunter returned invalid JSON on {endpoint}") from None
        return body.get("data") or {}
