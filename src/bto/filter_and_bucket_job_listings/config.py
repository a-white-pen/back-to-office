"""Load and validate the private, data-only search preferences and prompts."""

import ast
import math
from pathlib import Path

DIRECTORY = Path(__file__).parent


def read_private_data(path):
    """Literal data prevents a preference edit from becoming executable code."""
    try:
        tree = ast.parse(Path(path).read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                raise ValueError("only literal assignments are allowed")
            name = node.targets[0]
            if not isinstance(name, ast.Name):
                raise ValueError("assignment must name a constant")
            values[name.id] = ast.literal_eval(node.value)
        return values
    except (OSError, SyntaxError, ValueError, TypeError) as error:
        raise ValueError(
            f"Private configuration {Path(path).name} is missing or invalid"
        ) from error


def load_preferences(directory=None):
    directory = Path(directory or DIRECTORY / "preferences")
    result = {
        "years": read_private_data(directory / "years_of_experience.py").get("YEARS_OF_EXPERIENCE"),
        "skills": read_private_data(directory / "skills_and_qualifications.py").get(
            "SKILLS_AND_QUALIFICATIONS"
        ),
        "markets": read_private_data(directory / "market_preferences.py").get("MARKET_PREFERENCES"),
        "employers": read_private_data(directory / "employer_classification.py").get(
            "EMPLOYER_CLASSIFICATION"
        ),
    }
    work = read_private_data(directory / "job_preferences.py")
    result.update(work_groups=work.get("WORK_GROUPS"), kinds=work.get("KINDS_OF_WORK"))
    titles = read_private_data(directory / "job_title_filter.py")
    result["titles"] = {"fail": titles.get("FAIL_PHRASES"), "protect": titles.get("PROTECT_WORDS")}
    validate_preferences(result)
    return result


def validate_preferences(preferences):
    def require(condition, message):
        if not condition:
            raise ValueError(f"Private preferences: {message}")

    def strings(value):
        return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)

    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    groups = preferences.get("work_groups")
    require(isinstance(groups, dict) and bool(groups), "WORK_GROUPS must map ids to names")
    require(
        all(isinstance(k, str) and isinstance(v, str) and v for k, v in groups.items()),
        "invalid work group",
    )
    years = preferences.get("years")
    require(isinstance(years, dict), "years must be a mapping")
    for group in [*groups, "overall"]:
        row = years.get(group, {})
        values = [row.get(k) for k in ("comfortable", "stretch", "limit")]
        require(
            all(number(v) for v in values) and values == sorted(values), "invalid years boundaries"
        )
    kinds = preferences.get("kinds")
    require(isinstance(kinds, list) and bool(kinds), "KINDS_OF_WORK must be populated")
    names = set()
    for row in kinds:
        require(isinstance(row, dict), "invalid kind")
        require(
            isinstance(row.get("name"), str) and row["name"] not in names,
            "duplicate/missing kind name",
        )
        names.add(row["name"])
        require(
            isinstance(row.get("meaning"), str) and bool(row["meaning"]), "missing kind meaning"
        )
        require(row.get("years_group") in groups, "unknown years group")
        require(set(row.get("markets", {})) == {"SG", "HK", "TH"}, "missing market answer")
        require(
            all(v in {"preferred", "show me", "maybe", "no"} for v in row["markets"].values()),
            "invalid market answer",
        )
        override = row.get("preferred_when_data_technology_required")
        if override:
            require(
                set(override) == {"markets", "technologies"}, "invalid data technology override"
            )
            require(
                set(override["markets"]) <= {"SG", "HK"} and strings(override["technologies"]),
                "invalid override values",
            )
    markets = preferences.get("markets")
    require(isinstance(markets, dict) and set(markets) == {"SG", "HK", "TH"}, "missing markets")
    for market in markets.values():
        require(type(market.get("work_rights")) is bool, "work_rights must be boolean")
        floor = market.get("salary_floor", {})
        require(
            number(floor.get("amount"))
            and floor.get("period") == "monthly"
            and isinstance(floor.get("currency"), str),
            "invalid salary floor",
        )
        require(
            type(market.get("minimum_contract_months")) is int
            and market["minimum_contract_months"] > 0,
            "invalid contract minimum",
        )
    skills = preferences.get("skills")
    require(isinstance(skills, dict), "missing skills")
    for key in ("has", "industries_held", "industries_lacked", "not_strong", "packages"):
        require(strings(skills.get(key)), f"invalid {key} list")
    require(
        isinstance(skills.get("cannot_do"), dict) and "platforms" in skills["cannot_do"],
        "missing cannot-do groups",
    )
    require(all(strings(v) for v in skills["cannot_do"].values()), "invalid cannot-do list")
    require(
        isinstance(skills.get("aliases"), dict)
        and all(strings(v) for v in skills["aliases"].values()),
        "invalid aliases",
    )
    require(
        isinstance(skills.get("languages"), list) and bool(skills["languages"]), "missing languages"
    )
    require(
        all(
            isinstance(v, dict)
            and isinstance(v.get("name"), str)
            and v.get("level") in {"held", "basic_spoken"}
            for v in skills["languages"]
        ),
        "invalid language level",
    )
    require(isinstance(skills.get("highest_degree"), str), "missing highest degree")
    skill_years = skills.get("years", {})
    require(number(skill_years.get("managing_people")), "missing managing-people years")
    for key in ("tools", "industries"):
        require(isinstance(skill_years.get(key), dict), "missing specific years")
        for value in skill_years[key].values():
            require(
                number(value)
                or isinstance(value, dict)
                and set(value) == {"less_than"}
                and number(value["less_than"]),
                "invalid specific years",
            )
    require(all(strings(v) for v in preferences["titles"].values()), "invalid title phrases")
    require(all(preferences["titles"].values()), "title phrases must be populated")
    employers = preferences.get("employers")
    require(isinstance(employers, dict), "missing employers")
    require(isinstance(employers.get("recruitment_firms"), list), "invalid recruitment firms")
    require(
        all(
            isinstance(v, dict) and isinstance(v.get("name"), str) and strings(v.get("aliases", []))
            for v in employers["recruitment_firms"]
        ),
        "invalid recruitment firm",
    )
    for key in (
        "public_sector_organisations",
        "non_public_sector_organisations",
        "self_hiring_recruitment_licence_holders",
    ):
        require(strings(employers.get(key)), f"invalid {key}")
    return preferences


def load_prompt(stage, preferences, directory=None):
    if stage not in {"triage", "extraction"}:
        raise ValueError("Unknown Gemini step")
    data = read_private_data(Path(directory or DIRECTORY) / f"{stage}_prompt.py")
    fixed = data.get("PROMPT")
    listing = data.get("LISTING_CLOSING_INSTRUCTION")
    schema = data.get("STRUCTURED_OUTPUT_CONTRACT")
    if (
        not isinstance(fixed, str)
        or not fixed
        or not isinstance(listing, str)
        or not listing
        or not isinstance(schema, dict)
        or not schema
    ):
        raise ValueError(f"Private {stage} prompt is unconfigured")
    blocks = []
    for group, name in preferences["work_groups"].items():
        heading = f"Group: {name}" if stage == "triage" else f"Group {group}: {name}"
        kinds = [
            f"- {k['name']}: {k['meaning']}"
            for k in preferences["kinds"]
            if k["years_group"] == group
        ]
        blocks.append("\n".join([heading, *kinds]))
    skills = preferences["skills"]
    names = (
        skills["packages"] + skills["cannot_do"]["platforms"]
        if stage == "triage"
        else skills["industries_held"] + skills["industries_lacked"]
    )
    rendered_names = [
        name
        + (
            " (" + ", ".join(skills["aliases"].get(name, [])) + ")"
            if skills["aliases"].get(name)
            else ""
        )
        for name in names
    ]
    fixed = fixed.replace("{kinds_of_work}", "\n\n".join(blocks))
    fixed = fixed.replace(
        "{packages}" if stage == "triage" else "{industries}", "\n".join(rendered_names)
    )
    return fixed, listing, schema
