"""Non-exact candidate pairs: which wordings are worth comparing properly.

Implements the locked candidate contract in docs/CONTRACT.md: candidate
generation is the union of three routes — the LSH shortlist over MinHash signatures, same
normalized advertiser + same normalized title within a market, and the
OBSERVED consecutive wording transitions of one source listing. Candidates only
nominate pairs; no route ever decides that two wordings connect — evidence
acceptance lives entirely in build_canonical_mapping.

Main functions:
    transitions_from_observations(observations) -> set of transitions
    candidate_pairs(memberships, titles, minhashes, transitions)
        -> (routes_by_pair, history_continuity, counts)
"""

import logging
from collections import defaultdict
from itertools import pairwise

from datasketch import MinHashLSH

from . import build_matching_features as features

log = logging.getLogger(__name__)

# Locked candidate banding: 42 bands × 3 rows over the 128 signature values
# (positions 0–125 indexed, 126–127 unused). The shared constant also defines
# the exact signature prefix representative semantics may consume.
CANDIDATE_LSH_PARAMS = features.CANDIDATE_LSH_PARAMS


def candidate_lsh_index():
    """The candidate shortlist index. Nominations only."""
    return MinHashLSH(threshold=features.JACCARD_THRESHOLD,
                      num_perm=features.NUM_PERM, params=CANDIDATE_LSH_PARAMS)


def _pair(fp_a, fp_b):
    return (fp_a, fp_b) if fp_a < fp_b else (fp_b, fp_a)


def lsh_candidates(minhashes):
    """Route A: every distinct pair sharing at least one LSH band."""
    index = candidate_lsh_index()
    for fp, minhash in minhashes.items():
        index.insert(fp, minhash)
    pairs = set()
    for fp, minhash in minhashes.items():
        for other in index.query(minhash):
            if other != fp:
                pairs.add(_pair(fp, other))
    return pairs


def advertiser_title_candidates(memberships, titles):
    """Route B: same normalized advertiser + same normalized title, same
    market. Deterministic — catches pairs the LSH lottery misses."""
    buckets = defaultdict(set)
    for member in memberships:
        advertiser = features.normalized_advertiser(member.get("advertiser"))
        title = features.normalized_title(titles.get(member["fingerprint"]))
        if advertiser is None or title is None:
            continue
        buckets[(member["market"], advertiser, title)].add(
            member["fingerprint"])
    pairs = set()
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        ordered = sorted(bucket)
        for i, fp_a in enumerate(ordered):
            for fp_b in ordered[i + 1:]:
                pairs.add(_pair(fp_a, fp_b))
    return pairs


def transitions_from_observations(observations):
    """Consecutive usable-wording transitions of each source listing.

    observations: dicts with board, market, board_job_id, fingerprint,
    started_at, run_id — usable rows only (callers drop identity-only and
    quarantined observations, which carry no fingerprint). Per listing the
    observations are ordered by scrape_runs.started_at with run_id as the
    exact-timestamp tie-break ONLY (the observation chronology rule); every change of
    fingerprint between consecutive observations is one transition, so a
    real history A → B → A → C yields {A,B} and {A,C} — never a {B,C} that
    was never observed. Returns a set of (board, market, board_job_id,
    fp_lo, fp_hi). run.py's `_read_transitions` implements this identical
    rule as a SQL window function; the two are cross-checked in validation.
    """
    by_listing = defaultdict(list)
    for observation in observations:
        if observation.get("fingerprint") is None:
            continue
        key = (observation["board"], observation["market"],
               observation["board_job_id"])
        by_listing[key].append(observation)
    transitions = set()
    for key, listing_observations in by_listing.items():
        ordered = sorted(listing_observations,
                         key=lambda o: (o["started_at"], o["run_id"]))
        for earlier, later in pairwise(ordered):
            if earlier["fingerprint"] != later["fingerprint"]:
                lo, hi = _pair(earlier["fingerprint"], later["fingerprint"])
                transitions.add((*key, lo, hi))
    return transitions


def listing_history_candidates(memberships, transitions):
    """Route C: the OBSERVED consecutive wording transitions of one listing.

    transitions: (board, market, board_job_id, fp_lo, fp_hi) tuples from
    transitions_from_observations / run._read_transitions. Returns (pairs,
    continuity) where continuity[pair] is True when at least one listing
    carries the transition with the same normalized advertiser on both
    sides, or with the advertiser missing on either side.
    """
    advertiser = {(m["board"], m["market"], m["board_job_id"],
                   m["fingerprint"]): m.get("advertiser") for m in memberships}
    known = {key[3] for key in advertiser}
    pairs = set()
    continuity = {}
    for board, market, board_job_id, fp_lo, fp_hi in transitions:
        if fp_lo not in known or fp_hi not in known:
            continue                     # not a usable wording node here
        pair = (fp_lo, fp_hi)
        pairs.add(pair)
        adv_a = features.normalized_advertiser(
            advertiser.get((board, market, board_job_id, fp_lo)))
        adv_b = features.normalized_advertiser(
            advertiser.get((board, market, board_job_id, fp_hi)))
        continuous = adv_a is None or adv_b is None or adv_a == adv_b
        continuity[pair] = continuity.get(pair, False) or continuous
    return pairs, continuity


def same_listing_pairs(memberships):
    """Every pair of wordings that co-occur in ONE listing's history.

    Not a candidate route — evidence context for the rotating-slot veto:
    within one listing, template text is at its most dominant and a title
    swap is the strongest rotation signal, so build_canonical_mapping
    refuses text-similarity edges between same-listing wordings whose
    titles share no words at all (locked rule-logic; the rotating-slot intent).
    """
    by_listing = defaultdict(set)
    for member in memberships:
        by_listing[(member["board"], member["market"],
                    member["board_job_id"])].add(member["fingerprint"])
    pairs = set()
    for fingerprints in by_listing.values():
        ordered = sorted(fingerprints)
        for i, fp_a in enumerate(ordered):
            for fp_b in ordered[i + 1:]:
                pairs.add(_pair(fp_a, fp_b))
    return pairs


def candidate_pairs(memberships, titles, minhashes, transitions):
    """The candidate union, deduplicated, with per-route provenance.

    transitions: the listing's observed wording transitions (see
    transitions_from_observations) — the same-listing history route.

    Returns:
        routes_by_pair: {(fp_lo, fp_hi): set of route names}
        history_continuity: {pair: bool} for listing_history pairs
        counts: per-route and union sizes for build diagnostics
    """
    lsh = lsh_candidates(minhashes)
    advertiser_title = advertiser_title_candidates(memberships, titles)
    history, continuity = listing_history_candidates(memberships, transitions)

    routes_by_pair = defaultdict(set)
    for name, pairs in (("lsh", lsh),
                        ("advertiser_title", advertiser_title),
                        ("listing_history", history)):
        for pair in pairs:
            routes_by_pair[pair].add(name)

    counts = {
        "candidates_lsh": len(lsh),
        "candidates_advertiser_title": len(advertiser_title),
        "candidates_listing_history": len(history),
        "candidates_union": len(routes_by_pair),
    }
    return dict(routes_by_pair), continuity, counts
