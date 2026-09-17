"""Convert one Bronze observation into one 45-column standardized row.

``run.py`` calls this module once per observation. It maps and cleans source
fields without deduplicating listings or assigning canonical groups.
``normalize_description`` cleans the JD, and ``build_matching_features``
produces its fingerprint and MinHash signature.

For overseas MCF jobs, the caller supplies ``address.overseasCountry`` because
resolving it requires the one raw-payload dereference standardization permits.
"""

import re
from datetime import date, datetime, timezone

import pycountry

from . import build_matching_features as features
from . import normalize_description
from .normalize_description import UnsupportedConstruct

# The locked 45-column schema, in the data dictionary's exact order.
COLUMNS = (
    "run_id", "board", "market", "board_job_id", "content_hash",
    "title", "description_html", "description_text",
    "posted_date", "expiry_date", "board_status", "views_count",
    "advertiser_name", "hiring_company_name", "company_registry_id",
    "board_company_id", "is_agency_posting", "company_website",
    "company_employee_count",
    "job_country_code", "job_location",
    "salary_raw", "salary_min", "salary_max", "salary_currency", "salary_period",
    "categories", "employment_types", "job_function", "position_levels",
    "min_years_experience", "skills", "flexible_work_arrangements",
    "screening_questions", "board_attributes",
    "job_url", "apply_url", "apply_type",
    "poster_name", "poster_profile_url", "contacts", "job_source_name",
    "fingerprint", "minhash_signature", "standardized_at",
)

_LINKEDIN_COUNTRY = {"sg": "SG", "hk": "HK", "th": "TH", "au": "AU"}
_SALARY_PERIODS = {"Hourly", "Daily", "Weekly", "Monthly", "Annual"}
_INDEED_PERIOD = {"HOUR": "Hourly", "DAY": "Daily", "WEEK": "Weekly",
                  "MONTH": "Monthly", "YEAR": "Annual"}
_CMP_SLUG = re.compile(r"/cmp/([^/?#]+)")
_ISO_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


# Missing values

def norm_scalar(value):
    """Trim a scalar value; blank becomes None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def norm_string_array(values):
    """Drop NULL/blank members; preserve order and duplicates; empty → None."""
    if values is None:
        return None
    cleaned = [v for v in (norm_scalar(item) for item in values) if v is not None]
    return cleaned or None


def field_of_each(structs, key):
    """Extract one field, skipping NULL members permitted in Delta arrays."""
    return [member.get(key) for member in (structs or []) if member is not None]


def norm_contacts(values):
    """Require value; keep type optional; preserve order and duplicates."""
    if values is None:
        return None
    cleaned = []
    for item in values:
        value = norm_scalar((item or {}).get("value"))
        if value is None:
            continue
        cleaned.append({"type": norm_scalar((item or {}).get("type")),
                        "value": value})
    return cleaned or None


# Country

def country_from_name(name):
    """Use pycountry's exact, non-fuzzy lookup; unknown names become None."""
    name = norm_scalar(name)
    if name is None:
        return None
    try:
        return pycountry.countries.lookup(name).alpha_2
    except LookupError:
        return None


def valid_alpha2(code):
    code = norm_scalar(code)
    if code is None:
        return None
    code = code.upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return code if pycountry.countries.get(alpha_2=code) else None


# Dates

def utc_date_of_instant(value):
    """Convert a SEEK timestamp to its UTC calendar date."""
    value = norm_scalar(value)
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).date().isoformat()


def epoch_ms(value):
    """Parse Indeed epoch milliseconds; malformed values become None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def utc_date_of_epoch_ms(value):
    """Convert Indeed epoch milliseconds to UTC date; out of range → None."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) \
            .date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def date_string(value):
    """Pass through an MCF value already typed as a Bronze DATE."""
    value = norm_scalar(value)
    return str(value)[:10] if value else None


def iso_date_prefix(value):
    """Keep LinkedIn's first ten characters only when they form a real date."""
    value = norm_scalar(value)
    if value is None or not _ISO_DATE_PREFIX.match(value):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# SEEK salary-label parser

# This is a finite source-label grammar: never infer currency from market or
# amounts from JD prose.

_SEEK_CURRENCY_TOKENS = (
    ("US$", "USD"), ("S$", "SGD"), ("HK$", "HKD"), ("A$", "AUD"),
    ("NZ$", "NZD"), ("฿", "THB"), ("บาท", "THB"), ("£", "GBP"), ("€", "EUR"),
)
_SEEK_ISO_CODES = ("SGD", "HKD", "THB", "AUD", "NZD", "USD", "GBP", "EUR", "MYR")
_SEEK_PERIOD_TOKENS = (
    (r"p\.?\s?a\.?", "Annual"), (r"per\s+annum", "Annual"),
    (r"per\s+year", "Annual"), (r"annually", "Annual"), (r"yearly", "Annual"),
    (r"p\.?\s?m\.?", "Monthly"), (r"per\s+month", "Monthly"),
    (r"monthly", "Monthly"), (r"ต่อเดือน", "Monthly"),
    (r"p\.?\s?h\.?", "Hourly"), (r"per\s+hour", "Hourly"), (r"hourly", "Hourly"),
    (r"per\s+week", "Weekly"), (r"weekly", "Weekly"),
    (r"per\s+day", "Daily"), (r"daily", "Daily"),
)
_SEEK_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK]\b)?")
# Only a whitespace-delimited `` + `` starts a rider; its numbers are ignored.
_SEEK_RIDER = re.compile(r"\s\+\s")
# A two-sided range is EXACTLY two amounts joined by an explicit separator
# (optionally re-stating the currency before the second amount):
# "$5500 - $6k", "฿26,000 – ฿32,000", "$120,000 to $140,000".
_SEEK_CURRENCY_MARK = (r"(?:US\$|S\$|HK\$|A\$|NZ\$|฿|บาท|£|€|\$|RM|USD|SGD|HKD|"
                       r"AUD|NZD|THB|GBP|EUR|MYR)?\s*")
_SEEK_RANGE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*([kK]\b)?\s*" + _SEEK_CURRENCY_MARK
    + r"(?:-|–|—|~|to)\s*" + _SEEK_CURRENCY_MARK
    + r"(\d[\d,]*(?:\.\d+)?)\s*([kK]\b)?", re.IGNORECASE)
# An endpoint written as a small bare number beside a `k` endpoint is a
# thousands shorthand ("$80-$110K" → 80,000–110,000); a full amount keeps its
# own scale ("$5500 - $6k" → 5,500–6,000). The threshold distinguishes the
# shorthand from a full amount.
_SEEK_K_SHORTHAND_BELOW = 1000


def _seek_amount(number, k_flag, other_has_k):
    value = float(number.replace(",", ""))
    if k_flag or (other_has_k and value < _SEEK_K_SHORTHAND_BELOW):
        value *= 1000
    return value


def _seek_currency(label, currency_label):
    code = norm_scalar(currency_label)
    if code and code.upper() in _SEEK_ISO_CODES:
        return code.upper()
    for token, mapped in _SEEK_CURRENCY_TOKENS:
        if re.search(rf"(?<![A-Za-z]){re.escape(token)}", label):
            return mapped
    if re.search(r"(?<![A-Za-z])RM(?![A-Za-z])", label):
        return "MYR"
    codes = [code for code in _SEEK_ISO_CODES
             if re.search(rf"\b{code}\b", label, re.IGNORECASE)]
    # Multiple supported codes without a valid currencyLabel are ambiguous.
    return codes[0] if len(codes) == 1 else None


def _seek_period(label):
    lowered = label.lower()
    for pattern, mapped in _SEEK_PERIOD_TOKENS:
        if re.search(rf"(?:^|[\s\d,.(]){pattern}(?:$|[\s,.;)])", lowered):
            return mapped
    return None


def parse_seek_salary(label, currency_label):
    """Return ``(min, max, currency, period)`` from a SEEK salary label.

    Bounds require both currency and period plus a recognized range, ``up to``
    or ``from`` form. Unsupported or ambiguous amount forms keep independently
    resolved currency and period while leaving both bounds None.
    """
    label = norm_scalar(label)
    if label is None:
        return None, None, None, None
    currency = _seek_currency(label, currency_label)
    period = _seek_period(label)
    if currency is None or period is None:
        return None, None, currency, period

    numeric_part = _SEEK_RIDER.split(label)[0]
    matches = _SEEK_NUMBER.findall(numeric_part)
    if len(matches) == 1:
        number, flag = matches[0]
        amount = _seek_amount(number, flag, other_has_k=False)
        lowered = numeric_part.lower()
        if re.search(r"\bup\s+to\b", lowered):
            return None, amount, currency, period
        if re.search(r"\bfrom\b", lowered):
            return amount, None, currency, period
        return None, None, currency, period
    if len(matches) == 2:
        found = _SEEK_RANGE.search(numeric_part)
        if found is None:
            return None, None, currency, period
        lo_num, lo_k, hi_num, hi_k = found.groups()
        low = _seek_amount(lo_num, lo_k, other_has_k=bool(hi_k))
        high = _seek_amount(hi_num, hi_k, other_has_k=bool(lo_k))
        if low > high:
            return None, None, currency, period   # inverted ranges are unsupported
        return low, high, currency, period
    return None, None, currency, period


# Identity-derived job URLs

def job_url(board, market, board_job_id):
    if board == "mcf":
        return f"https://www.mycareersfuture.gov.sg/job/{board_job_id}"
    if board == "jobstreet":
        return f"https://{market}.jobstreet.com/job/{board_job_id}"
    if board == "jobsdb":
        return f"https://{market}.jobsdb.com/job/{board_job_id}"
    if board == "seek":
        return f"https://{market}.seek.com/job/{board_job_id}"
    if board == "indeed":
        host = "www.indeed.com" if market == "us" else f"{market}.indeed.com"
        return f"https://{host}/viewjob?jk={board_job_id}"
    if board == "linkedin":
        return f"https://{market}.linkedin.com/jobs/view/{board_job_id}"
    raise ValueError(f"unknown board {board!r}")


# Source payload mappings

def _get(row, *path):
    value = row
    for key in path:
        if value is None:
            return None
        value = value.get(key)
    return value


def _period_from_source(value):
    value = norm_scalar(value)
    if value is None:
        return None
    canonical = value.title()
    return canonical if canonical in _SALARY_PERIODS else None


def _mcf(row, out, overseas_country):
    address = row.get("address") or {}
    overseas = bool(address.get("isOverseas"))
    districts = norm_string_array(field_of_each(address.get("districts"), "location"))
    html = norm_scalar(row.get("description"))
    out.update({
        "title": norm_scalar(row.get("title")),
        "description_html": html,
        "posted_date": date_string(_get(row, "metadata", "newPostingDate")),
        "expiry_date": date_string(_get(row, "metadata", "expiryDate")),
        "board_status": norm_scalar(_get(row, "status", "jobStatus")),
        "views_count": _get(row, "metadata", "totalNumberOfView"),
        "advertiser_name": norm_scalar(_get(row, "postedCompany", "name")),
        "hiring_company_name": norm_scalar(_get(row, "hiringCompany", "name")),
        "company_registry_id": norm_scalar(_get(row, "postedCompany", "uen")),
        "is_agency_posting": _get(row, "metadata", "isPostedOnBehalf"),
        "company_website": norm_scalar(_get(row, "postedCompany", "companyUrl")),
        "company_employee_count": _get(row, "postedCompany", "employeeCount"),
        "job_country_code": (country_from_name(overseas_country) if overseas
                             else "SG"),
        "job_location": "overseas" if overseas
        else ("; ".join(districts) if districts else None),
        "salary_min": _get(row, "salary", "minimum"),
        "salary_max": _get(row, "salary", "maximum"),
        "salary_currency": "SGD",
        "salary_period": _period_from_source(
            _get(row, "salary", "type", "salaryType")),
        "categories": norm_string_array(
            field_of_each(row.get("categories"), "category")),
        "employment_types": norm_string_array(
            field_of_each(row.get("employmentTypes"), "employmentType")),
        "position_levels": norm_string_array(
            field_of_each(row.get("positionLevels"), "position")),
        "min_years_experience": row.get("minimumYearsExperience"),
        "skills": norm_string_array(field_of_each(row.get("skills"), "skill")),
        "flexible_work_arrangements": norm_string_array(
            field_of_each(row.get("flexibleWorkArrangements"),
                          "flexibleWorkArrangement")),
        "screening_questions": norm_string_array(
            field_of_each(row.get("screeningQuestions"), "question")),
    })
    return html, "html"


def _seek(row, out):
    job = row.get("job") or {}
    stub = row.get("job") is not None and row.get("gfjInfo") is None
    country = valid_alpha2(_get(row, "gfjInfo", "location", "countryCode"))
    if country is None and stub and out["board"] == "jobstreet" \
            and out["market"] == "sg":
        # Closed historical JobStreet SG stub; never a general market fallback.
        country = "SG"
    smin, smax, scur, sper = parse_seek_salary(
        _get(job, "salary", "label"), _get(job, "salary", "currencyLabel"))
    html = norm_scalar(job.get("content"))
    out.update({
        "title": norm_scalar(job.get("title")),
        "description_html": html,
        "posted_date": utc_date_of_instant(_get(job, "listedAt", "dateTimeUtc")),
        "expiry_date": utc_date_of_instant(_get(job, "expiresAt", "dateTimeUtc")),
        "board_status": norm_scalar(job.get("status")),
        "advertiser_name": norm_scalar(_get(job, "advertiser", "name")),
        "board_company_id": norm_scalar(_get(job, "advertiser", "id")),
        "job_country_code": country,
        "job_location": norm_scalar(_get(job, "location", "label")),
        "salary_raw": norm_scalar(_get(job, "salary", "label")),
        "salary_min": smin, "salary_max": smax,
        "salary_currency": scur, "salary_period": sper,
        "categories": norm_string_array(
            field_of_each(job.get("classifications"), "label")),
        # gfjInfo.workTypes uses a different vocabulary.
        "employment_types": norm_string_array(
            (_get(job, "workTypes", "label") or "").split(",")),
        "screening_questions": norm_string_array(
            _get(job, "products", "questionnaire", "questions")),
        "contacts": norm_contacts(job.get("contactMatches")),
    })
    return html, "html"


def _indeed(row, out):
    link = norm_scalar(row.get("companyOverviewLink"))
    slug = _CMP_SLUG.search(link) if link else None
    posted_ms = epoch_ms(row.get("pubDate"))
    expiry_ms = epoch_ms(row.get("expirationDate"))
    html = norm_scalar(row.get("jobDescriptionHTML"))
    out.update({
        "title": norm_scalar(row.get("title")),
        "description_html": html,
        "posted_date": utc_date_of_epoch_ms(posted_ms),
        # An expiry is meaningful only with a valid posting it does not precede.
        "expiry_date": (
            utc_date_of_epoch_ms(expiry_ms)
            if posted_ms is not None and expiry_ms is not None
            and expiry_ms >= posted_ms else None),
        "advertiser_name": norm_scalar(_get(row, "companyDetails", "name")),
        "board_company_id": slug.group(1) if slug else None,
        "company_website": norm_scalar(_get(row, "companyDetails", "websiteUrl")),
        "job_country_code": valid_alpha2(_get(row, "location", "countryCode")),
        "job_location": norm_scalar(_get(row, "location", "formatted", "long")),
        "salary_min": _get(row, "salary", "min"),
        "salary_max": _get(row, "salary", "max"),
        "salary_currency": norm_scalar(_get(row, "salary", "currencyCode")),
        "salary_period": _INDEED_PERIOD.get(
            (norm_scalar(_get(row, "salary", "type")) or "").upper()),
        "categories": norm_string_array(field_of_each(row.get("occupations"), "label")),
        "employment_types": norm_string_array(row.get("jobTypes")),
        "board_attributes": norm_string_array(field_of_each(row.get("attributes"), "label")),
        "apply_url": norm_scalar(row.get("originalApplyUrl")),
        "job_source_name": norm_scalar(row.get("jobSourceName")),
    })
    return html, "html"


def _linkedin(row, out):
    salary_info = [norm_scalar(v) for v in (row.get("salaryInfo") or [])]
    salary_info = [v for v in salary_info if v is not None]
    out.update({
        "title": norm_scalar(row.get("jobTitle")),
        "description_html": None,            # LinkedIn supplies plaintext only.
        "posted_date": iso_date_prefix(row.get("publishedAt")),
        "expiry_date": None,
        "advertiser_name": norm_scalar(row.get("companyName")),
        "board_company_id": norm_scalar(row.get("companyId")),
        "company_website": norm_scalar(row.get("companyWebsite")),
        "company_employee_count": row.get("companyEmployeeCount"),
        "job_country_code": _LINKEDIN_COUNTRY.get(out["market"]),
        "job_location": norm_scalar(row.get("location")),
        "salary_raw": " – ".join(salary_info) if salary_info else None,
        "categories": norm_string_array(
            [row.get("sector")] if row.get("sector") else None),
        "employment_types": norm_string_array(
            [row.get("contractType")] if row.get("contractType") else None),
        "job_function": norm_scalar(row.get("workType")),
        "position_levels": norm_string_array(
            [row.get("experienceLevel")] if row.get("experienceLevel") else None),
        "apply_type": norm_scalar(row.get("applyType")),
        "poster_name": norm_scalar(row.get("posterFullName")),
        "poster_profile_url": norm_scalar(row.get("posterProfileUrl")),
    })
    return row.get("jobDescription"), "linkedin"


_BUILDERS = {"mcf": _mcf, "seek": _seek, "jobstreet": _seek, "jobsdb": _seek,
             "indeed": _indeed, "linkedin": _linkedin}


def standardize_observation(board, row, overseas_country=None,
                            standardized_at=None):
    """Build all 45 fields and return ``(row, quarantine_reason)``.

    Identity, identity-derived ``job_url`` and ``standardized_at`` are set
    before the payload gate. A NULL ``content_hash`` therefore produces an
    identity-only row. Payload-bearing rows use one source mapper, then clean
    the description. Unsupported HTML preserves other derived fields but
    leaves description text and the matching pair NULL. Matching features are
    generated only when description normalization does not quarantine the row.
    """
    out = dict.fromkeys(COLUMNS)
    out.update({
        "run_id": row["run_id"], "board": row["board"],
        "market": row["market"], "board_job_id": row["board_job_id"],
        "content_hash": row.get("content_hash"),
        "job_url": job_url(row["board"], row["market"], row["board_job_id"]),
        "standardized_at": standardized_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if out["content_hash"] is None:
        return out, None

    builder = _BUILDERS[board]
    if builder is _mcf:
        source_text, kind = builder(row, out, overseas_country)
    else:
        source_text, kind = builder(row, out)

    quarantine = None
    if kind == "linkedin":
        out["description_text"] = \
            normalize_description.description_text_from_linkedin(source_text)
    else:
        try:
            out["description_text"] = \
                normalize_description.description_text_from_html(source_text)
        except UnsupportedConstruct as unsupported:
            quarantine = unsupported.reason

    if quarantine is None:
        out["fingerprint"], out["minhash_signature"] = \
            features.matching_features(out["title"], out["description_text"])
    return out, quarantine
