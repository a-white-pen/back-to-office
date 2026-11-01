"""Invented employers; copy to private employer_classification.py and customise."""

EMPLOYER_CLASSIFICATION = {
    "recruitment_firms": [
        {"name": "Example Recruitment Ltd", "aliases": ["Example Recruit"]},
    ],
    "public_sector_organisations": ["Example Public Agency"],
    "non_public_sector_organisations": ["Example Private Company"],
    # Their licence alone must not classify these self-hiring employers as agencies.
    "self_hiring_recruitment_licence_holders": ["Example Self-Hiring Company"],
}
