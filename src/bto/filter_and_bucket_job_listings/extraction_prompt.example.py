"""Safe shape for the private prompt; illustrative instructions only."""

PROMPT = "Example extraction instructions.\n{kinds_of_work}\n{industries}"

LISTING_CLOSING_INSTRUCTION = "Example title: {title}\nExample advertiser: {advertiser}\nExample JD:\n{jd}\nReturn the specified JSON fields."

STRUCTURED_OUTPUT_CONTRACT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kinds", "lines", "recruitment_firm", "public_sector"],
    "properties": {
        "kinds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "string",
                "enum": ["Example kind of work", "Example data engineering"],
            },
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "quote",
                    "type",
                    "status",
                    "detail",
                    "things",
                    "needs",
                    "number",
                    "years_of",
                    "level",
                ],
                "properties": {
                    "quote": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "years",
                            "tool",
                            "credential",
                            "industry",
                            "language",
                            "work_rights",
                            "education",
                            "contract",
                            "internship",
                            "students_only",
                            "condition",
                            "other",
                        ],
                    },
                    "status": {"type": "string", "enum": ["required", "preferred", "unknown"]},
                    "detail": {"type": "string"},
                    "things": {"type": "array", "items": {"type": "string"}},
                    "needs": {"type": "string", "enum": ["all", "any_one", "any_one_or_similar"]},
                    "number": {"type": ["number", "null"]},
                    "years_of": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "analytics",
                                "research",
                                "data_science_and_ai",
                                "data_engineering",
                                "product_and_delivery",
                                "transformation_and_consulting",
                                "customer_facing_technical",
                                "software_and_specialist",
                                "overall",
                                "not_named",
                                "thing",
                                "managing_people",
                                "impossible",
                            ],
                        },
                    },
                    "level": {
                        "type": ["string", "null"],
                        "enum": [
                            "not_stated",
                            "basic_spoken",
                            "business_or_written",
                            "doctorate_only",
                            "degree",
                            None,
                        ],
                    },
                },
            },
        },
        "recruitment_firm": {"type": "boolean"},
        "public_sector": {"type": "boolean"},
    },
}
