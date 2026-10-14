"""Run and validate Gemini triage after the two cheap filters.

This scaffold will use the private triage_prompt.py and shared call_gemini.py
mechanics to decide whether the work itself is in scope. It owns validation of
the triage-specific output contract. As a listing passes, extraction can start
while other listings are still being triaged. It does not judge whether Belinda
meets the requirements or assign the final review bucket.
"""
