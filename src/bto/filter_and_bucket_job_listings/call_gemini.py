"""Own the Gemini calling mechanics shared by triage and extraction.

Use Flex only throughout. Normal nights start no new Gemini read after 8:30 AM
Singapore time; calls already sent may finish. Initial catch-up has no 8:30 AM
deadline and needs fresh calls/cost/runtime estimates and Belinda's explicit go.
The shared cache must cover the whole catch-up rather than an ordinary night.

Wait/retry when Google is busy, apply the agreed per-call timeout, and defer
unread work when a refusal stops the step for the night. Enforce one shared
normal-night USD 10 ceiling across triage/extraction. Each call's cost row
records the tier Google reports. Retry corrections do not create a new reusable
first-attempt input identity.

Prompts, verdicts, extraction and their output validation belong to the callers.
This file remains a responsibility docstring, with no calling implementation.
"""
