"""Pull cleaned listing history and current observations into PostgreSQL.

Initial population loads all available Silver listing/wording history.
Production processing begins at 3:00 AM Singapore time on 9 Oct 2026
(2026-10-08T19:00:00Z), by wording_first_seen_at. Earlier rows are history-only:
bucket not_sorted, reason "not read: before processing window"; they are not
sent to Gemini or re-sorted on preference changes. The paid initial catch-up
needs freshly recalculated calls/cost/runtime and Belinda's explicit go.

Normal pulling starts from PostgreSQL's represented state/watermark, naturally
catching up missed nights. Keep the cheap unfinished-run guard for runs that
started before that point but had not finished by it; no fixed overlap.

Compute wording_hash from the exact title input, JD input (first 16,000
characters) and extraction advertiser line, including a different named hiring
company as that line specifies. The data dictionary owns its deterministic
SHA-256 encoding. Model, thinking level, prompts, fingerprint and content_hash
do not define listing wording identity.

Refresh last-seen/current facts, salary, employment/job type and board facts.
Check closure for applicable open MCF/JobStreet/JobsDB listings even if not seen
tonight; Indeed/LinkedIn search absence is not closure evidence. Refresh every
applicable stored row's canonical group when cleaning proves it current.
Changed rule-relevant facts rerun free rules for processing-window rows;
history-only rows keep their historical Not sorted result.

Reuse existing storage helpers and return pull counts. This transfer is called
by the runner, not separately scheduled; processing remains unimplemented.
"""
