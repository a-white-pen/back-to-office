"""Public template for private market_preferences.py; data only.

All values are illustrative dummy data, not Belinda's preferences or defaults.
Copy the structure into the private file and supply the agreed values there.
Home-versus-overseas behaviour belongs in rule code, not prose in this data.
"""

MARKET_PREFERENCES = {
    "SG": {
        "work_rights": True,
        "salary_floor": {"amount": 1000, "currency": "SGD", "period": "monthly"},
        "minimum_contract_months": 3,
    },
    "HK": {
        "work_rights": False,
        "salary_floor": {"amount": 1000, "currency": "HKD", "period": "monthly"},
        "minimum_contract_months": 3,
    },
    "TH": {
        "work_rights": True,
        "salary_floor": {"amount": 1000, "currency": "THB", "period": "monthly"},
        "minimum_contract_months": 3,
    },
}
