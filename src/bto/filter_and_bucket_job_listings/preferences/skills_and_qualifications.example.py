"""Fictional profile with invented products; copy to private skills_and_qualifications.py."""

SKILLS_AND_QUALIFICATIONS = {
    "languages": [
        {"name": "Example language A", "level": "held"},
        {"name": "Example language B", "level": "basic_spoken"},
    ],
    # Supported levels: none, degree, bachelor, master or doctorate.
    "highest_degree": "degree",
    "has": ["ExampleSQL", "ExampleFlow", "Example analytics certificate"],
    "industries_held": ["Example retail sector"],
    "industries_lacked": ["Example insurance sector"],
    "not_strong": ["Example reporting tool"],
    "packages": ["Example business suite"],
    # platforms is required and feeds triage; other groups organise hard exclusions.
    "cannot_do": {
        "platforms": ["Example specialist platform"],
        "credentials": ["Example advanced licence"],
    },
    "years": {
        "tools": {"ExampleSQL": 2, "ExampleFlow": {"less_than": 1}},
        "industries": {"Example retail sector": 2},
        "managing_people": 0,
    },
    # Map each canonical name to names that may appear in a JD.
    "aliases": {
        "ExampleSQL": ["Example Query Language"],
        "ExampleFlow": ["Example Flow SDK"],
    },
    # Optional: resolve an ambiguous abbreviation only with this explicit context.
    "contextual_aliases": {
        "EQL": {"when_named": "ExampleSQL", "canonical": "ExampleSQL"},
    },
}
