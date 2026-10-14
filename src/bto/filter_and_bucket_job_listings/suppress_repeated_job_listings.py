"""Apply Rule 1 after bucketing, preserving the substantive bucket.

A qualifying OTHER board listing of the same known canonical job must have
been first seen in the 30 days before the evaluated wording's first-seen night.
Anchor this window to wording_first_seen_at permanently, including on rechecks.
Compare titles ignoring case and collapsing extra whitespace, without fuzzy
matching; today's bucket, reason and lacks must match.

Another wording of the same (board, market, board_job_id) never suppresses
itself. A new listing/repost on the same board or a copy on another board may
qualify. repeat_of_id points to the qualifying row of the earliest qualifying
OTHER board listing by listing_first_seen_at.

Unjudged pre-window history initially limits suppression; do not pay for old
Gemini reads to improve it. This scaffold does not build the Review queue.
"""
