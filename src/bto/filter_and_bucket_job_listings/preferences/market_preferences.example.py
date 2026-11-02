"""Fictional market settings; copy to private market_preferences.py and customise.

Work rights and amounts are dummy values, not legal or salary guidance.
Short-contract and public-sector choices belong in this data.
"""

MARKET_PREFERENCES = {
    "SG": {
        "work_rights": True,
        "salary_floor": {"amount": 1000, "currency": "SGD", "period": "monthly"},
        "minimum_contract_months": 3,
        "short_contract_bucket": "tech_not_for_me",
        "exclude_public_sector": True,
    },
    "HK": {
        "work_rights": False,
        "salary_floor": {"amount": 2000, "currency": "HKD", "period": "monthly"},
        "minimum_contract_months": 6,
        "short_contract_bucket": "less_interested",
        "exclude_public_sector": False,
    },
    "TH": {
        "work_rights": False,
        "salary_floor": {"amount": 3000, "currency": "THB", "period": "monthly"},
        "minimum_contract_months": 6,
        "short_contract_bucket": "less_interested",
        "exclude_public_sector": True,
    },
}
