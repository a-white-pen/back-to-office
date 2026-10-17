"""Apply the ordered deterministic rules, preserving evidence for Review."""

import re

from . import DEFERRED_REASONS, HISTORY_REASON, PROCESSING_START, TERMINAL_REASONS
from .evaluate_job_requirements import evaluate_requirements, normalize_name
from .filter_job_titles import filter_title
from .filter_non_english_jds import filter_language


def listing_market(row):
    country = (row.get("job_country_code") or row["market"]).upper()
    return country if country in {"SG", "HK", "TH"} else row["market"].upper()


def named_employer(name, names):
    text = normalize_name(name or "")
    for candidate in names:
        candidate = normalize_name(candidate)
        if candidate.endswith("*"):
            if text.startswith(candidate[:-1]):
                return True
        elif (
            text == candidate
            or text.startswith(candidate)
            and len(text) > len(candidate)
            and not text[len(candidate)].isalnum()
        ):
            return True
    return False


def employer_flags(row, extraction, preferences):
    config = preferences["employers"]
    advertiser = row.get("advertiser_name") or ""
    name = row.get("hiring_company_name") or advertiser
    if named_employer(name, config["public_sector_organisations"]):
        public = True
    elif named_employer(name, config["non_public_sector_organisations"]):
        public = False
    else:
        public = extraction["public_sector"] if extraction else None
    firms = [
        name for firm in config["recruitment_firms"] for name in [firm["name"], *firm["aliases"]]
    ]
    if (
        (
            named_employer(advertiser, firms)
            or row.get("board") == "mcf"
            and row.get("is_agency_posting") is True
        )
        or (
            listing_market(row) == "SG"
            and not named_employer(advertiser, config["self_hiring_recruitment_licence_holders"])
            and re.search(
                r"\bEA\s*(?:licen[cs]e|lic\.?|reg(?:istration)?)(?:\s*(?:no\.?|number))?\s*[:#]?\s*[A-Z0-9]*\d[A-Z0-9]*",
                row.get("description_text") or "",
                re.IGNORECASE,
            )
        )
        or not named_employer(advertiser, config["public_sector_organisations"])
        and any(
            word in advertiser.casefold()
            for word in (
                "recruit",
                "staffing",
                "manpower",
                "personnel",
                "employment agency",
                "executive search",
                "headhunt",
                "talent",
            )
        )
    ):
        recruitment = True
    else:
        recruitment = extraction["recruitment_firm"] if extraction else None
    return recruitment, public


def kind_answers(kinds, market, evaluation, preferences):
    answers = []
    for name in kinds:
        kind = next((k for k in preferences["kinds"] if k["name"] == name), None)
        if kind is None:
            raise ValueError("Stored extraction names an unconfigured kind")
        answer = kind["markets"][market]
        override = kind.get("preferred_when_data_technology_required")
        if (
            override
            and market in override["markets"]
            and any(
                normalize_name(name) in evaluation["required_technologies"]
                for name in override["technologies"]
            )
        ):
            answer = "preferred"
        answers.append(answer)
    return answers


def bucket_listing(row, triage, extraction, preferences):
    result = {
        "id": row.get("id"),
        "bucket": None,
        "reason": None,
        "decided_by": None,
        "lacks": None,
        "years_over_comfortable": None,
        "preferred_held": None,
        "is_recruitment_firm": None,
        "is_public_sector": None,
    }

    def decide(bucket, reason=None, by="rules"):
        result.update(bucket=bucket, reason=reason, decided_by=by)
        return result

    if row["wording_first_seen_at"] < PROCESSING_START:
        return decide("not_sorted", HISTORY_REASON, None)
    market = listing_market(row)
    if market not in preferences["markets"]:
        # These filters do not depend on market-specific answers.
        reason = filter_title(
            row.get("title"), preferences["titles"]["fail"], preferences["titles"]["protect"]
        )
        bucket = "title_filter"
        if not reason:
            reason = filter_language(row.get("description_text"))
            bucket = "ineligible"
        if reason:
            recruitment, public = employer_flags(row, extraction, preferences)
            result.update(is_recruitment_firm=recruitment, is_public_sector=public)
            return decide(bucket, reason)
        raise ValueError("A processing-window job has an unconfigured market")
    recruitment, public = employer_flags(row, extraction, preferences)
    result.update(is_recruitment_firm=recruitment, is_public_sector=public)
    evaluation = None
    if extraction:
        evaluation = evaluate_requirements(
            extraction["lines"], extraction["kinds"][0], market, preferences
        )
        result.update(
            {k: evaluation[k] for k in ["lacks", "years_over_comfortable", "preferred_held"]}
        )
    reason = filter_title(
        row.get("title"), preferences["titles"]["fail"], preferences["titles"]["protect"]
    )
    if reason:
        return decide("title_filter", reason)
    reason = filter_language(row.get("description_text"))
    if reason:
        return decide("ineligible", reason)
    if triage and triage["verdict"] != "pass":
        verdict = triage["verdict"]
        if verdict == "not_english":
            return decide("ineligible", "JD not in English", "triage")
        if verdict == "non_tech":
            return decide("non_tech", f"non-tech: {triage['profession']}", "triage")
        return decide(
            "tech_not_for_me", f"outside your professions: {triage['profession']}", "triage"
        )
    if not triage or not extraction:
        reason = row.get("reason")
        return decide(
            "not_sorted",
            reason
            if reason in (*TERMINAL_REASONS, *DEFERRED_REASONS)
            else "not read: the night stopped early",
            None,
        )
    ev = evaluation
    main = extraction["kinds"][0]
    answers = kind_answers(extraction["kinds"], market, ev, preferences)
    best = max(answers, key=lambda a: ["no", "maybe", "show me", "preferred"].index(a))
    required = ev["required_types"]
    months = min(ev["contract_months"], default=None)
    labels = {normalize_name(v) for v in row.get("employment_types") or []}
    if "internship" in required or labels & {"internship", "internship/attachment"}:
        return decide("tech_not_for_me", "internship")
    if "students_only" in required or labels & {"graduate programme", "graduate program"}:
        return decide("tech_not_for_me", "students or new graduates only")
    if best == "no":
        return decide("tech_not_for_me", f"{main}: no in {market}")
    if (
        market != "SG"
        and months is not None
        and months < preferences["markets"][market]["minimum_contract_months"]
    ):
        return decide("tech_not_for_me", f"{months:g}-month contract in {market}")
    if market != "SG" and public:
        return decide("tech_not_for_me", f"public sector in {market}")
    walls = ev["walls"]
    if walls.get("language"):
        return decide("ineligible", "requires " + " and ".join(walls["language"]))
    if walls.get("work_rights"):
        return decide("ineligible", "work rights: " + walls["work_rights"][0])
    cannot = [v for key in ["tool", "credential", "years", "industry"] for v in walls.get(key, [])]
    if cannot:
        return decide("ineligible", "requires " + " and ".join(cannot) + ": on your cannot list")
    if len(ev["counting_lacks"]) >= 2:
        return decide("ineligible", "lacks " + " and ".join(ev["counting_lacks"]))
    if walls.get("education"):
        return decide("ineligible", "requires a doctorate")
    if ev["years_zone"] == 3:
        return decide("ineligible", ev["years_reason"])
    floor = preferences["markets"][market]["salary_floor"]
    top = row.get("salary_max")
    period = (row.get("salary_period") or "").casefold()
    if (
        top is not None
        and row.get("salary_currency") == floor["currency"]
        and period in {"monthly", "annual", "yearly"}
    ):
        monthly = top / 12 if period in {"annual", "yearly"} else top
        if monthly <= floor["amount"]:
            return decide(
                "less_interested",
                f"salary up to {floor['currency']} {monthly:,.0f}, at or under your {floor['amount']:,.0f}",
            )
    qa = any(
        k["years_group"] == "software_and_specialist" and k["name"] == "QA and test automation"
        for k in preferences["kinds"]
        if k["name"] in extraction["kinds"]
    )
    if qa:
        return decide("less_interested", "QA and test automation among the kinds")
    if main == "Other technical work":
        return decide("less_interested", "Other technical work as the main kind")
    if best == "maybe":
        return decide("less_interested", f"{main}: maybe in {market}")
    if (
        market == "SG"
        and months is not None
        and months < preferences["markets"][market]["minimum_contract_months"]
    ):
        return decide("less_interested", f"{months:g}-month contract in SG")
    if ev["years_zone"] == 2:
        return decide("less_interested", ev["years_reason"])
    if answers[0] == "preferred" and ev["years_zone"] == 0 and not ev["lacks"]:
        return decide("top_picks")
    reason = (
        f"{main}: {answers[0]} in {market}"
        if answers[0] != "preferred"
        else ev["years_reason"] or ("lacks " + " and ".join(ev["lacks"]) if ev["lacks"] else None)
    )
    return decide("in_lane", reason)
