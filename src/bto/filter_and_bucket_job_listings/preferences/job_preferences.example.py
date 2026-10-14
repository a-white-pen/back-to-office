"""Public template for private job_preferences.py; data only.

All values are illustrative dummy data, not Belinda's preferences or defaults.
Copy the structure into the private file and supply the agreed values there.
"""

WORK_GROUPS = ["Example work group"]
KINDS_OF_WORK = [
    {
        "name": "Example kind of work",
        "meaning": "An illustrative activity, not a real work preference.",
        "years_group": "Example work group",
        # Each answer is preferred, show me, maybe or no.
        "markets": {"SG": "preferred", "HK": "show me", "TH": "maybe"},
    },
    {
        "name": "Example data engineering",
        "meaning": "An illustrative Data Engineering entry.",
        "years_group": "Example work group",
        "markets": {"SG": "show me", "HK": "show me", "TH": "maybe"},
        # This specific Data Engineering override is not a general rule language.
        # The market answers above apply unless a REQUIRED line asks for a named
        # technology; preferred/unknown lines do not activate the override.
        # Only the listed markets change; TH keeps its default.
        "preferred_when_data_technology_required": {
            "markets": ["SG", "HK"],
            "technologies": ["Example data technology"],
        },
    },
]
