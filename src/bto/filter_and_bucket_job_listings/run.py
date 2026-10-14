"""Coordinate nightly filtering, initial catch-up and free re-sorts.

Call the state-based pull, apply cheap filters to processing-window rows and
coordinate Gemini triage/extraction. Extraction begins per listing as triage
passes. All Silver history is loaded, but processing starts at 3:00 AM
Singapore time on 9 Oct 2026; pre-boundary Not sorted rows stay history-only.

Use Flex throughout. Normal nights start no new read after 8:30 AM Singapore
time; sent calls may finish before deterministic rules, repeats and the normal
morning summary finish. Deferred work remains eligible on later runs; terminal
failed-twice wordings are not automatically paid for again nightly.

Initial catch-up is Flex only without the ordinary cutoff. Recalculate actual
calls/cost/runtime immediately before its paid run and obtain Belinda's go.
call_gemini.py owns cache lifetime, waiting, pricing, cost and the shared ceiling.

Preference re-sorts reuse valid stored results without Gemini, excluding
pre-boundary history. Recheck repeats against each row's fixed
wording_first_seen_at, never a moving current-night window.

Use existing notification machinery and source templates. Reporting counts
listing/wording rows, not grouped Review rows; new/changed wordings first pulled
tonight form the bucket population. Carried-forward work counts in read/cost
totals. Service failure/timeout orchestration is left to implementation.

Individual processing rules and Review queue construction belong elsewhere.
All execution paths remain unimplemented.
"""
