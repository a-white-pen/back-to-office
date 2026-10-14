"""Run and validate Gemini extraction as individual listings pass triage.

This scaffold will use the private extraction_prompt.py and shared
call_gemini.py mechanics to extract kinds of work, requirement lines, and
recruitment-firm and public-sector signals. It owns validation of the
extraction-specific output contract and does not wait for all triage to finish.
Requirement evaluation and final employer classification belong to later steps.
"""
