"""Public template for private skills_and_qualifications.py; data only.

All values are illustrative dummy data, not Belinda's preferences or defaults.
Copy the structure into the private file and supply the agreed values there.
Keep the cannot-do groups distinct; triage specifically needs Platforms.
"""

SKILLS_AND_QUALIFICATIONS = {
    "languages": [{"name": "Example language", "level": "held"}],
    "highest_degree": "degree",
    "has": ["Example qualification"],
    "industries_held": ["Example industry"],
    "industries_lacked": ["Another example industry"],
    "not_strong": ["Example skill"],
    "packages": ["Example package"],
    "cannot_do": {
        "credentials": ["Example credentials item"],
        "programming": ["Example programming item"],
        "games_and_3d": ["Example games and 3d item"],
        "apple": ["Example apple item"],
        "chip_and_electronics_design": ["Example chip and electronics design item"],
        "platforms": ["Example platforms item"],
        "older_systems": ["Example older systems item"],
        "networks_and_telecoms": ["Example networks and telecoms item"],
    },
    "years": {
        "tools": {"Example tool": 1},
        "industries": {"Example industry": 1},
        "managing_people": 1,
    },
    "aliases": {"Example qualification": ["Example abbreviation"]},
}
