"""Read a hiring platform and employer account from a board address.

The APIs need the employer's tenant name, such as `example` in
example.wd3.myworkdayjobs.com, plus any vendor-specific host or site details.
"""

import re
import urllib.parse
from dataclasses import dataclass, field

from .hiring_systems import parse_board, vendor_of


@dataclass
class Board:
    """An employer's board as its vendor's API addresses it."""

    # workday, successfactors, oracle_hcm, greenhouse, lever, ashby,
    # smartrecruiters, phenom, eightfold or a privately configured adapter
    vendor: str
    # the employer's tenant name; for vendors reached by host, the board's base address
    tenant: str
    # what else the vendor's API needs: Workday's site and host, Oracle's site
    # number, Phenom's domain, the in-house system's origin
    extra: dict = field(default_factory=dict)

    def label(self):
        return f"{self.vendor}:{self.tenant}"


def board_from_address(url):
    """The searchable board an address belongs to, or None when no known
    vendor answers behind it."""
    if not url:
        return None
    match = re.match(r"https?://([a-z0-9.-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                     r"(?:([a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_-]+)", url)
    if match:
        tenant, workday_number, _locale, site = match.groups()
        workday_host = f"{tenant}.{workday_number}.myworkdayjobs.com"
        return Board("workday", tenant, {"tenant": tenant, "site": site, "host": workday_host})
    match = re.match(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                     r"recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)", url)
    if match:
        workday_number, tenant, site = match.groups()
        workday_host = f"{workday_number}.myworkdaysite.com"
        return Board("workday", tenant, {"tenant": tenant, "site": site, "host": workday_host})
    match = re.match(r"(https?://[a-z0-9.-]+\.oraclecloud\.com)"
                     r"/hcmUI/CandidateExperience/[a-z-]+/sites/([A-Za-z0-9_]+)", url)
    if match:
        return Board("oracle_hcm", match.group(1), {"site": match.group(2)})
    vendor, tenant, extra = parse_board(url)
    if vendor in ("greenhouse", "lever", "ashby", "smartrecruiters") and tenant:
        return Board(vendor, tenant, extra or {})
    netloc = urllib.parse.urlsplit(url).netloc
    base = f"https://{netloc}"
    vendor = vendor_of(url)
    if vendor == "successfactors" or re.search(r"(?i)sapsf|successfactors", url):
        return Board("successfactors", base, {})
    if vendor == "phenom":
        return Board("phenom", base, {"domain": netloc})
    if vendor == "eightfold":
        return Board("eightfold", base, {})
    return None
