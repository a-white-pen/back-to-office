"""Public structure for the private extraction prompt; no production content.

The caller will inject kinds_of_work from preferences/job_preferences.py and
industries from preferences/skills_and_qualifications.py. Preference lists
stay in those files; they are not copied into the prompt. This is a template,
not a working prompt or processing implementation.
"""

PROMPT = """Example extraction instructions.
Kinds of work:
{kinds_of_work}
Industries:
{industries}
"""

# The listing-specific input and closing instruction go here.
LISTING_CLOSING_INSTRUCTION = """Example listing input: {listing_input}
Example closing instruction for this listing.
"""

# The agreed structured-output field definitions and contract belong here.
STRUCTURED_OUTPUT_CONTRACT = {}
