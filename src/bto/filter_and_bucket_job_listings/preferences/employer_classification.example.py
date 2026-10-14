"""Public template for private employer_classification.py; data only.

All values are illustrative dummy data, not Belinda's preferences or defaults.
Copy the structure into the private file and supply the agreed values there.
"""

EMPLOYER_CLASSIFICATION = {
    "recruitment_firms": [
        {"name": "Example recruitment firm", "aliases": ["Example firm alias"]},
    ],
    "public_sector_organisations": ["Example public organisation"],
    "non_public_sector_organisations": ["Example private organisation"],
    # These employers hire for themselves; their licence alone must not
    # classify them as recruitment firms.
    "self_hiring_recruitment_licence_holders": ["Example self-hiring employer"],
}
