"""Pure requirement evaluation shared by sorting and the Review right panel."""

import re
import unicodedata

DEFAULT_REQUIRED = {"years", "language", "work_rights", "contract", "internship", "students_only"}
PREFERRED = re.compile(
    r"\b(?:preferred|desirable|a plus|nice to have|ideally|an advantage|a bonus|a priority|highly regarded|beneficial|desired)\b|\b(?:be|is|are|as)(?: considered)? an asset\b",
    re.IGNORECASE,
)


def normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def effective_status(line):
    quote = re.sub(r"[‐‑‒–—-]", " ", line["quote"])
    if PREFERRED.search(quote) or (
        re.search(r"\bnot (?:essential|mandatory|required)\b", quote, re.IGNORECASE)
        and not re.search(
            r"(?<!not )\b(?:must|required|requires|minimum|mandatory|essential)\b",
            quote,
            re.IGNORECASE,
        )
    ):
        return "preferred"
    status = line["status"]
    return "required" if status == "unknown" and line["type"] in DEFAULT_REQUIRED else status


def name_lookup(skills):
    lookup = {}
    categories = [
        ("has", "have"),
        ("industries_held", "have"),
        ("industries_lacked", "lack"),
        ("not_strong", "other"),
        ("packages", "other"),
    ]
    for key, category in categories:
        for name in skills[key]:
            for alias in [name, *skills["aliases"].get(name, [])]:
                lookup[normalize_name(alias)] = (category, name)
    for group in skills["cannot_do"].values():
        for name in group:
            for alias in [name, *skills["aliases"].get(name, [])]:
                lookup[normalize_name(alias)] = ("wall", name)
    return lookup


def resolve_thing(thing, line, skills, lookup):
    context = " ".join([line["quote"], *line["things"]])
    for alias, rule in skills.get("contextual_aliases", {}).items():
        if normalize_name(thing) == normalize_name(alias) and re.search(
            r"(?<!\w)" + re.escape(rule["when_named"]) + r"(?!\w)", context, re.IGNORECASE
        ):
            thing = rule["canonical"]
    match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)", thing)
    part = match[1].strip() if match else thing
    result = lookup.get(normalize_name(part))
    if result:
        return result
    if match:
        result = lookup.get(normalize_name(match[2]))
        if result:
            return result
    return "lack", part


def combine_alternatives(outcomes, needs):
    if needs == "all":
        return outcomes
    if any(value[0] == "have" for value in outcomes):
        return [next(v for v in outcomes if v[0] == "have")]
    label = " or ".join(value[1] for value in outcomes)
    if needs == "any_one_or_similar" or any(v[0] == "other" for v in outcomes):
        return [("other", label)]
    if outcomes and all(v[0] == "wall" for v in outcomes):
        return [("wall", label)]
    return [("lack", label)] if outcomes else [("other", "")]


def evaluate_requirements(lines, main_kind, market, preferences):
    skills = preferences["skills"]
    lookup = name_lookup(skills)
    main = next(k for k in preferences["kinds"] if k["name"] == main_kind)
    result = {
        "for": [],
        "against": [],
        "other": [],
        "lacks": [],
        "counting_lacks": [],
        "walls": {},
        "preferred_held": 0,
        "years_over_comfortable": None,
        "years_zone": 0,
        "years_reason": None,
        "required_types": set(),
        "contract_months": [],
        "required_technologies": set(),
    }
    language_lookup = {
        normalize_name(alias): (language["level"], language["name"])
        for language in skills["languages"]
        for alias in [language["name"], *skills["aliases"].get(language["name"], [])]
    }
    tool_years = {}
    for category in ("tools", "industries"):
        for name, years in skills["years"][category].items():
            canonical = lookup.get(normalize_name(name), (None, name))[1]
            tool_years[normalize_name(canonical)] = years

    def emit(line, outcome, name, status, *, counts=True, note=None):
        entry = {
            "quote": line["quote"],
            "type": line["type"],
            "detail": name or line["detail"],
            "status": status,
            "note": note,
        }
        section = (
            "for"
            if outcome == "have"
            else "other"
            if outcome == "other" or status == "preferred"
            else "against"
        )
        if status == "unknown":
            entry["note"] = "not clear if required"
        elif status == "preferred" and outcome == "have":
            entry["note"] = "preferred"
        result[section].append(entry)
        if status == "required":
            if outcome == "lack":
                result["lacks"].append(name)
                if counts:
                    result["counting_lacks"].append(name)
            if outcome == "wall":
                result["walls"].setdefault(line["type"], []).append(name)

    for line in lines:
        status = effective_status(line)
        kind = line["type"]
        if status == "required":
            result["required_types"].add(kind)
        things = [resolve_thing(thing, line, skills, lookup) for thing in line["things"]]
        if line["status"] == "required" and status == "required":
            result["required_technologies"].update(normalize_name(name) for _, name in things)
        outcomes = []
        counts = True
        note = None
        if kind == "years" and "impossible" in line["years_of"]:
            outcomes = [("other", line["detail"])]
            note = "a years ask longer than the technology has existed, ignored"
        elif kind == "years" and not set(line["years_of"]) & {"thing", "managing_people"}:
            zones = []
            asked = line["number"]
            for group in line["years_of"]:
                group = main["years_group"] if group == "not_named" else group
                numbers = preferences["years"][group]
                zone = next(
                    (
                        i
                        for i, key in enumerate(["comfortable", "stretch", "limit"])
                        if asked <= numbers[key]
                    ),
                    3,
                )
                zones.append((zone, asked - numbers["comfortable"], numbers))
            zone, excess, numbers = min(zones, key=lambda v: (v[0], v[1]))
            note = (
                "years met"
                if zone == 0
                else f"asks {asked:g} years, your {'comfortable' if zone == 1 else 'stretch' if zone == 2 else 'limit'} is {numbers['comfortable' if zone == 1 else 'stretch' if zone == 2 else 'limit']:g}"
            )
            if zone == 3:
                note = f"asks {asked:g} years, past your limit of {numbers['limit']:g}"
            if status == "required" and (
                zone > result["years_zone"]
                or result["years_over_comfortable"] is None
                or zone == result["years_zone"]
                and excess > result["years_over_comfortable"]
            ):
                result.update(
                    years_zone=zone,
                    years_over_comfortable=excess,
                    years_reason=note if zone else None,
                )
            outcome = "have" if zone == 0 else "other" if status == "preferred" else "against"
            outcomes = [(outcome, line["detail"])]
        elif kind == "years":
            if "managing_people" in line["years_of"]:
                outcomes = [
                    (
                        "have" if skills["years"]["managing_people"] >= line["number"] else "lack",
                        "managing people",
                    )
                ]
            else:
                for category, name in things:
                    if category in {"wall", "lack"}:
                        outcomes.append((category, name))
                        continue
                    years = tool_years.get(normalize_name(name))
                    if years is None:
                        outcomes.append(("other", name))
                        note = "years not confirmed"
                        continue
                    met = (
                        line["number"] < years["less_than"]
                        if isinstance(years, dict)
                        else line["number"] <= years
                    )
                    outcomes.append(
                        (
                            "have" if met else "lack",
                            name if met else f"{name} ({line['number']:g}+ years)",
                        )
                    )
                outcomes = combine_alternatives(outcomes, line["needs"])
        elif kind in {"tool", "credential", "industry"}:
            outcomes = (
                combine_alternatives(things, line["needs"])
                if things
                else [("other", line["detail"])]
            )
        elif kind == "language":
            for thing in line["things"]:
                held, name = language_lookup.get(normalize_name(thing), (None, thing))
                outcome = (
                    "wall"
                    if held is None
                    else "have"
                    if held == "held" or line["level"] == "basic_spoken"
                    else "lack"
                    if line["level"] == "not_stated"
                    else "wall"
                )
                if outcome == "lack":
                    name += " (level not stated)"
                    counts = False
                outcomes.append((outcome, name))
            outcomes = combine_alternatives(outcomes, line["needs"])
        elif kind == "education":
            outcomes = [("wall" if line["level"] == "doctorate_only" else "have", line["detail"])]
        elif kind == "work_rights":
            outcomes = [
                (
                    "have" if preferences["markets"][market]["work_rights"] else "wall",
                    line["detail"],
                )
            ]
        elif kind == "contract":
            if status == "required" and line["number"] is not None:
                result["contract_months"].append(line["number"])
            outcomes = [("against", line["detail"])]
        elif kind in {"condition", "internship", "students_only"}:
            outcomes = [("against", line["detail"])]
        else:
            outcomes = [("other", line["detail"])]
        if status == "preferred" and outcomes and all(o[0] == "have" for o in outcomes):
            result["preferred_held"] += 1
        for outcome, name in outcomes:
            emit(line, outcome, name, status, counts=counts, note=note)
    for key in ("lacks", "counting_lacks"):
        result[key] = sorted(
            {normalize_name(v): v for v in result[key]}.values(), key=normalize_name
        )
    return result
