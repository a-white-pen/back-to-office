"""Safe shape for the private prompt; illustrative instructions only."""

PROMPT = "Example triage instructions.\n{kinds_of_work}\n{packages}"

LISTING_CLOSING_INSTRUCTION = (
    "Example title: {title}\nExample JD:\n{jd}\nReturn the specified JSON fields."
)

STRUCTURED_OUTPUT_CONTRACT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profession", "quote", "verdict"],
    "properties": {
        "profession": {"type": "string"},
        "quote": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": [
                "not_english",
                "one_package",
                "chip_or_electronics_design",
                "research_publication",
                "pass",
                "non_tech",
            ],
        },
    },
}
