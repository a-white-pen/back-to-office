"""Fictional work preferences; copy to private job_preferences.py and customise."""

WORK_GROUPS = {
    "example_group_one": "Reporting and analysis",
    "example_group_two": "Data pipelines",
}

KINDS_OF_WORK = [
    {
        "name": "Example reporting and dashboards",
        "meaning": "Build reports and explain patterns in business data.",
        # Must name a WORK_GROUPS key and a years-of-experience entry.
        "years_group": "example_group_one",
        # Allowed answers: preferred, show me, maybe, no.
        "markets": {"SG": "preferred", "HK": "show me", "TH": "maybe"},
    },
    {
        "name": "Example data pipelines",
        # Optional: main limits only the main kind; present also limits secondary kinds.
        "less_interested_when": "main",
        "meaning": "Build and maintain pipelines that move and transform data.",
        "years_group": "example_group_two",
        "markets": {"SG": "show me", "HK": "show me", "TH": "no"},
        # Optional: a required named technology promotes only these markets.
        # Preferred or unknown requirements leave the defaults unchanged.
        "preferred_when_data_technology_required": {
            "markets": ["SG", "HK"],
            "technologies": ["ExampleFlow"],
        },
    },
]
