"""Assemble extraction input and validate the complete nine-field line contract."""

import math

from .triage_job_listings import quote_found

LINE_TYPES = (
    "years",
    "tool",
    "credential",
    "industry",
    "language",
    "work_rights",
    "education",
    "contract",
    "internship",
    "students_only",
    "condition",
    "other",
)
YEARS_OF = (
    "analytics",
    "research",
    "data_science_and_ai",
    "data_engineering",
    "product_and_delivery",
    "transformation_and_consulting",
    "customer_facing_technical",
    "software_and_specialist",
    "overall",
    "not_named",
    "thing",
    "managing_people",
    "impossible",
)
LANGUAGE_LEVELS = ("not_stated", "basic_spoken", "business_or_written")
EDUCATION_LEVELS = ("doctorate_only", "degree")
LINE_FIELDS = (
    "quote",
    "type",
    "status",
    "detail",
    "things",
    "needs",
    "number",
    "years_of",
    "level",
)


def advertiser_line(row):
    advertiser = row.get("advertiser_name") or ""
    hiring = row.get("hiring_company_name")
    return advertiser + (", hiring for " + hiring if hiring and hiring != advertiser else "")


def listing_input(row, template):
    return template.format(
        title=row.get("title") or "",
        advertiser=advertiser_line(row),
        jd=(row.get("description_text") or "")[:16000],
    )


def response_schema(kind_names):
    line = {
        "type": "object",
        "additionalProperties": False,
        "required": list(LINE_FIELDS),
        "properties": {
            "quote": {"type": "string"},
            "type": {"type": "string", "enum": list(LINE_TYPES)},
            "status": {"type": "string", "enum": ["required", "preferred", "unknown"]},
            "detail": {"type": "string"},
            "things": {"type": "array", "items": {"type": "string"}},
            "needs": {"type": "string", "enum": ["all", "any_one", "any_one_or_similar"]},
            "number": {"type": ["number", "null"]},
            "years_of": {"type": "array", "items": {"type": "string", "enum": list(YEARS_OF)}},
            "level": {
                "type": ["string", "null"],
                "enum": [*LANGUAGE_LEVELS, *EDUCATION_LEVELS, None],
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kinds", "lines", "recruitment_firm", "public_sector"],
        "properties": {
            "kinds": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "enum": list(kind_names)},
            },
            "lines": {"type": "array", "items": line},
            "recruitment_firm": {"type": "boolean"},
            "public_sector": {"type": "boolean"},
        },
    }


def validate_extraction(answer, row, kind_names):
    if not isinstance(answer, dict) or set(answer) != {
        "kinds",
        "lines",
        "recruitment_firm",
        "public_sector",
    }:
        raise ValueError("Extraction needs exactly four listing fields")
    kinds = answer["kinds"]
    if (
        not isinstance(kinds, list)
        or not 1 <= len(kinds) <= 3
        or not all(isinstance(k, str) for k in kinds)
        or len(set(kinds)) != len(kinds)
        or any(k not in kind_names for k in kinds)
    ):
        raise ValueError("Extraction needs one to three distinct configured kinds")
    if any(type(answer[k]) is not bool for k in ["recruitment_firm", "public_sector"]):
        raise ValueError("Employer flags must be boolean")
    if not isinstance(answer["lines"], list):
        raise ValueError("Lines must be an array")
    for line in answer["lines"]:
        if not isinstance(line, dict) or set(line) != set(LINE_FIELDS):
            raise ValueError("Each line needs exactly nine fields")
        if (
            line["type"] not in LINE_TYPES
            or line["status"] not in {"required", "preferred", "unknown"}
            or line["needs"] not in {"all", "any_one", "any_one_or_similar"}
        ):
            raise ValueError("Invalid line type/status/needs")
        for key in ["quote", "detail"]:
            if not isinstance(line[key], str) or not line[key].strip():
                raise ValueError("Quote and detail must be non-empty strings")
        if len(line["quote"].split()) > 30 or not quote_found(line["quote"], row):
            raise ValueError("Quote exceeds 30 words or is not found in sent input")
        for key in ["things", "years_of"]:
            if not isinstance(line[key], list) or any(
                not isinstance(v, str) or not v.strip() for v in line[key]
            ):
                raise ValueError("Things and years_of must be text arrays")
        if any(v not in YEARS_OF for v in line["years_of"]):
            raise ValueError("Invalid years_of value")
        number = line["number"]
        if number is not None and (type(number) not in [int, float] or not math.isfinite(number)):
            raise ValueError("Number must be finite or null")
        kind = line["type"]
        if kind == "years":
            if number is None or not 0 <= number <= 50 or not line["years_of"]:
                raise ValueError("Years need a number from zero to fifty and years_of")
            if "impossible" in line["years_of"] and line["years_of"] != ["impossible"]:
                raise ValueError("Impossible years must stand alone")
            if "thing" in line["years_of"] and not line["things"]:
                raise ValueError("Years of a thing need a named thing")
        elif line["years_of"]:
            raise ValueError("years_of does not apply to this line")
        if (
            kind == "contract"
            and number is not None
            and (not 1 <= number <= 120 or int(number) != number)
        ):
            raise ValueError("Contract months must be whole months from one to 120")
        if kind not in {"years", "contract"} and number is not None:
            raise ValueError("Number does not apply to this line")
        if kind == "language":
            if not line["things"] or line["level"] not in LANGUAGE_LEVELS:
                raise ValueError("Language needs a named language and allowed level")
        elif kind == "education":
            if line["level"] not in EDUCATION_LEVELS:
                raise ValueError("Education needs an allowed level")
        elif line["level"] is not None:
            raise ValueError("Level does not apply to this line")
    return answer
