"""Extend a validated canonical mapping with new standardized evidence.

The semantic product is unchanged: the COMPLETE full-history canonical
mapping. This module only changes how that product is constructed on an
incremental run. Starting from the previous mapping — validated and bound to
standardized version P — plus the standardized rows inserted between P and
the pinned build version N, it seeds every dependency the batch could have
moved, closes a region around those seeds, rebuilds that region from
INDIVIDUAL wording nodes with the full-history rules, and copies every
untouched group's rows forward verbatim. Whenever any step cannot PROVE that
shortcut safe it raises `FullRebuildRequired` and the caller runs the
existing full-history build instead. Nothing here may guess.

Why the result equals the full-history build at N:

- Seeds are complete. `judge_pair` reads exactly the pair's exact Jaccard,
  its candidate routes, same-listing advertiser continuity, both titles,
  whether the two wordings co-occur on one listing, and whether their bodies
  are identical. Under an append-only, in-order batch each of those can move
  only through a wording whose representative semantics changed, a wording
  that gained or drifted a membership, a wording on either side of a new
  transition, or a wording that did not exist at P. So every pair whose
  verdict differs from the previous build has an endpoint in the seed set.
  A wording's exact Jaccard and its LSH route cannot move at all: the
  fingerprint fixes the matching document, and the determinacy guard refuses
  a generation whose maximum-tied representatives disagree in the stored
  signature positions consumed by the frozen banding.
- The region is a union of WHOLE P components, so a component is either
  entirely rebuilt or entirely copied; it is never cut.
- Closure follows PAIR-ACCEPTED edges — `judge_pair` returning a lane —
  rather than the edges the previous build retained. The component-level
  career-stage guard can drop an accepted edge, and an edge it dropped may
  become retainable once the region splits, so an edge the baseline graph does
  not show can still reach outside and must pull that component in.
- At the fixpoint no pair-accepted N edge crosses the boundary, every outside
  pair has the verdict it had at P, and a stage-blocked union is a no-op on
  the partition, so the guarded union decomposes: replaying it over the
  region reproduces exactly what a global run would decide there, and every
  outside component keeps its members, its winner and therefore its id.

Out-of-order usable observations are refused rather than seeded: they can
delete a transition, lower a membership's first-seen and reorder
representative selection all at once, and none of those is provable here.
"""

import logging
from collections import defaultdict

from . import build_candidate_pairs as candidates_module
from . import build_canonical_mapping as canonical
from . import build_matching_features as features

log = logging.getLogger(__name__)

# The shortcut stops being one when the affected region approaches the whole
# graph: a rebuild then costs about the same and is far better exercised.
# Purely a cost guard, so it needs BOTH thresholds — a large share of a tiny
# history is still trivial work, and falling back there would only churn.
AFFECTED_FRACTION_CEILING = 0.25
AFFECTED_MINIMUM = 1_000
# Closure is bounded so an unexpectedly expanding region is refused rather
# than allowed to run away.
MAX_CLOSURE_ROUNDS = 10
# One huge LSH or advertiser+title bucket can make a small region probe an
# enormous number of pairs, which is no longer a shortcut. The ceiling scales
# with the wording count rather than using a bare count; reaching it falls back
# instead of paying twice.
CANDIDATE_PAIRS_PER_WORDING = 14


def region_too_large(affected, total):
    """True when the affected region is large both absolutely and as a share
    of the wordings in history."""
    return (affected > AFFECTED_MINIMUM
            and total and affected / total > AFFECTED_FRACTION_CEILING)


def too_many_candidates(judged, total):
    """True when the region has judged more pairs than a full rebuild of this
    history is expected to."""
    return total and judged > CANDIDATE_PAIRS_PER_WORDING * total


class FullRebuildRequired(Exception):
    """A detected condition the incremental path must not decide.

    Carries a stable machine-readable `reason` for diagnostics; the caller
    falls back to the full-history build, which handles every one of these
    conditions by construction.
    """

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# ------------------------------------------------------------- detectors
#
# Each detector answers one question about the inserted batch with data the
# caller already holds. They return evidence (never booleans) so the fallback
# log can say which rows tripped the rule.

def out_of_order_observations(batch_usable, latest_by_listing):
    """Usable batch observations that do not extend their listing's history.

    latest_by_listing: {(board, market, board_job_id): (started_at, run_id)}
    over the baseline's usable observations. A batch row at or before that
    point rewrites chronology — transitions, first-seen minimums and
    representative selection may all differ from what the baseline saw.
    """
    stale = []
    for o in batch_usable:
        latest = latest_by_listing.get(
            (o["board"], o["market"], o["board_job_id"]))
        if latest is not None and (o["started_at"], o["run_id"]) <= latest:
            stale.append((o["board"], o["market"], o["board_job_id"],
                          o["run_id"]))
    return stale


def changed_representatives(semantics_p, semantics_n):
    """Wordings whose representative now reads differently to canonicalization.

    semantics: {fingerprint: features.RepresentativeSemantics} for exactly the
    known fingerprints the batch re-observed. Raw equality is the wrong test —
    case, whitespace, the title/body split and non-Latin-script content all
    move without changing a single consumer — so the comparison is the shared
    projection, and a wording that only reads differently on paper costs
    nothing.
    """
    return sorted(fp for fp, semantics in semantics_p.items()
                  if semantics_n.get(fp, semantics) != semantics)


def new_transition_endpoints(transitions_p, transitions_n):
    """Wordings on either side of a transition that did not exist at P.

    A listing's observed wording transitions are its candidate route with no
    similarity floor, and re-observing a wording the listing ALREADY carried
    still creates one: history `A → B → C` plus a later `A` becomes
    `A → B → C → A` and nominates `C ↔ A`. That batch row adds no membership,
    drifts no advertiser and arrives in order, so nothing else here would
    notice it. Both sides must be re-judged.

    Under in-order appends a listing's transitions can only be added to —
    out-of-order arrivals, which could remove one, are refused separately.
    """
    return {fp for transition in set(transitions_n) - set(transitions_p)
            for fp in transition[3:]}


def base_components(base_rows):
    """The baseline partition, exactly: {fingerprint: group id} and
    {group id: set of fingerprints} over the usable rows. A usable
    canonical id embeds its winning fingerprint and is therefore
    component-injective, so grouping by it loses nothing."""
    component_of = {}
    members = defaultdict(set)
    for row in base_rows:
        if row["fingerprint"] is None:
            continue
        component_of[row["fingerprint"]] = row["canonical_job_id"]
        members[row["canonical_job_id"]].add(row["fingerprint"])
    return component_of, dict(members)


def advertiser_drift(member_adv_p, member_adv_n):
    """Re-observed memberships whose representative advertiser changed after
    normalization. Keys: (board, market, board_job_id, fingerprint), values:
    the raw advertiser — restricted to memberships the batch touched.

    Drift is a seed, not a refusal: it can only remove an advertiser+title or
    listing-history edge that one of ITS OWN wording's memberships carried,
    and that wording's whole P component is rebuilt from individual nodes.
    """
    return sorted(
        key for key, adv in member_adv_p.items()
        if features.normalized_advertiser(adv)
        != features.normalized_advertiser(member_adv_n.get(key, adv)))


# ------------------------------------------------------------- closure

def close_region(seed_fps, component_of, members, accepted_touching,
                 total_wordings):
    """Grow the seeds to the region that may be rebuilt in isolation.

    Every seed contributes its ENTIRE baseline component — a component is
    rebuilt whole or copied whole, never cut — and a wording that did not
    exist at P contributes itself. The region then expands through every
    CURRENT pair-accepted edge that reaches outside it, pulling that
    wording's whole baseline component in, until a round adds nothing.

    Expansion deliberately follows `judge_pair` accepting the pair, not the
    edges the previous build kept. `components_from_edges` can refuse an
    accepted edge whose union would mix career stages; if the region splits,
    that same edge may become retainable and merge an outside component in,
    so it has to be inside the region to be decided.

    accepted_touching(region) -> [(pair, jaccard, lane)] for every N candidate
    pair with at least one endpoint in `region`, judged with the full N
    context. The caller supplies it because the two engines discover
    candidates differently; the fixpoint rule lives here once.

    Returns (region, accepted, diagnostics) where `accepted` is the final
    round's verdicts — at the fixpoint every one of them has both endpoints
    inside the region.
    """
    region = set()
    pulled = 0

    def pull(fingerprint):
        nonlocal pulled
        group = component_of.get(fingerprint)
        if group is None:
            region.add(fingerprint)               # new at N: its own singleton
            return
        if not members.get(group, ()) <= region:
            pulled += 1
        region.update(members[group])

    for fingerprint in seed_fps:
        pull(fingerprint)
    if not region:
        # Nothing the batch inserted can have moved any verdict, so the
        # candidate is the baseline re-anchored at N with fresh sentinels.
        return region, [], {"closure_rounds": 0,
                            "closure_growth_per_round": [],
                            "baseline_components_pulled": 0}
    # A handful of seeds can pull enormous baseline components, so the size
    # gate has to run BEFORE the first round of candidate generation, not
    # only after an expansion.
    if region_too_large(len(region), total_wordings):
        raise FullRebuildRequired(
            "closure_region_too_large",
            f"{len(region)} of {total_wordings} wordings before the first "
            f"candidate round")

    growth, rounds = [], 0
    while True:
        rounds += 1
        if rounds > MAX_CLOSURE_ROUNDS:
            raise FullRebuildRequired(
                "closure_did_not_converge",
                f"still growing after {MAX_CLOSURE_ROUNDS} rounds "
                f"({len(region)} wordings)")
        accepted = accepted_touching(frozenset(region))
        outside = {fp for pair, _jaccard, _lane in accepted for fp in pair
                   if fp not in region}
        if not outside:
            return region, accepted, {
                "closure_rounds": rounds,
                "closure_growth_per_round": growth,
                "baseline_components_pulled": pulled,
            }
        before = len(region)
        for fingerprint in outside:
            pull(fingerprint)
        growth.append(len(region) - before)
        if region_too_large(len(region), total_wordings):
            raise FullRebuildRequired(
                "closure_region_too_large",
                f"{len(region)} of {total_wordings} wordings after "
                f"{rounds} round(s)")


# ------------------------------------------------------------- assembly

def assemble(base_rows, accepted, new_fps, region_fps, memberships_region,
             titles_region, never_usable_n):
    """The complete candidate rows: rebuilt region + verbatim copy.

    base_rows: the baseline mapping rows (usable and sentinel).
    accepted: [(pair, jaccard, lane)] for every pair-accepted N edge with BOTH
        endpoints in the region.
    region_fps: the closed region — a union of whole baseline components plus
        the wordings new at N.
    memberships_region: memberships at version N for EXACTLY the region's
        wordings (board, market, board_job_id, fingerprint,
        first_seen_membership_at) — winners are recomputed from these.
    titles_region: {fingerprint: title} covering every region wording, for the
        component career-stage guard.
    never_usable_n: the complete never-usable listing set at version N —
        sentinels are always regenerated, never copied, because a baseline
        sentinel listing may have gained its first usable wording.

    The region's wordings are ordinary nodes again: the unchanged
    `components_from_edges` decides them from their own edges, so the region
    may MERGE groups the baseline separated and SPLIT ones it joined. Copying
    a baseline row forward is only sound outside the closure fixpoint, which
    is what makes the untouched rows correct without re-judging them.

    Returns (rows, assembly_diagnostics); raises FullRebuildRequired when the
    inputs do not add up.
    """
    component_of, members = base_components(base_rows)
    region_fps = set(region_fps)

    missing = [fp for fp in region_fps
               if fp not in new_fps and fp not in component_of]
    if missing:
        raise FullRebuildRequired(
            "baseline_missing_wording",
            f"{len(missing)} region wording(s) absent from the baseline "
            f"mapping, e.g. {sorted(missing)[:3]}")
    overlap = [fp for fp in new_fps if fp in component_of]
    if overlap:
        raise FullRebuildRequired(
            "baseline_already_has_wording",
            f"{len(overlap)} 'new' wording(s) already mapped, "
            f"e.g. {sorted(overlap)[:3]}")
    # A half-pulled component would leave the copied half bound to an id its
    # rebuilt half no longer owns.
    for fingerprint in region_fps:
        group = component_of.get(fingerprint)
        if group is not None and not members[group] <= region_fps:
            raise FullRebuildRequired(
                "region_not_component_closed",
                f"baseline group {group} is only partly inside the region")
    crossing = [pair for pair, _jaccard, _lane in accepted
                if pair[0] not in region_fps or pair[1] not in region_fps]
    if crossing:
        raise FullRebuildRequired(
            "edge_outside_region",
            f"{len(crossing)} accepted edge(s) leave the region, "
            f"e.g. {crossing[:2]}")
    provided = {m["fingerprint"] for m in memberships_region}
    if provided != region_fps:
        raise FullRebuildRequired(
            "region_membership_accounting",
            f"memberships cover {len(provided)} wording(s), "
            f"the region holds {len(region_fps)}")
    untitled = [fp for fp in region_fps if fp not in titles_region]
    if untitled:
        raise FullRebuildRequired(
            "region_title_accounting",
            f"{len(untitled)} region wording(s) without a title entry")

    stage_of = {fp: features.career_stage(titles_region.get(fp))
                for fp in region_fps}
    region_of, edges, union_counts = canonical.components_from_edges(
        sorted(region_fps), accepted, stage_of)
    rebuilt = canonical.rows_from_components(memberships_region, region_of,
                                             never_usable=())
    copied = [dict(row) for row in base_rows
              if row["fingerprint"] is not None
              and row["fingerprint"] not in region_fps]
    sentinels = [{
        "board": board, "market": market, "board_job_id": board_job_id,
        "fingerprint": None,
        "canonical_job_id": canonical.sentinel_canonical_id(
            board, market, board_job_id),
    } for board, market, board_job_id in never_usable_n]

    diagnostics = dict(
        union_counts,
        region_wordings=len(region_fps),
        region_memberships=len(memberships_region),
        region_components=len(set(region_of.values())),
        region_edges=len(edges),
        rebuilt_rows=len(rebuilt),
        copied_rows=len(copied),
        sentinel_rows=len(sentinels))
    return copied + rebuilt + sentinels, diagnostics


# ------------------------------------------------- in-memory reference

def _representatives(observations):
    """{fingerprint: representative observation} by the observation chronology
    rule — started_at, then bytewise run_id.

    Several observations can share that maximum. They are interchangeable only
    when they agree on what canonicalization reads, and no ordering exists to
    separate them, so a disagreement is refused outright: the full-history
    build would be just as undetermined, and inventing a tie-break would
    silently change which observation the oracle reads.
    """
    by_fingerprint = defaultdict(list)
    for o in observations:
        if o.get("fingerprint") is not None:
            by_fingerprint[o["fingerprint"]].append(o)
    reps, tied_rows = {}, []
    for fingerprint, rows in by_fingerprint.items():
        top = max((o["started_at"], o["run_id"]) for o in rows)
        tied = [o for o in rows if (o["started_at"], o["run_id"]) == top]
        if len(tied) > 1:
            tied_rows += [(fingerprint, o.get("title"),
                           o.get("description_text"),
                           o.get("minhash_signature")) for o in tied]
        reps[fingerprint] = tied[0]
    ambiguous = features.ambiguous_representatives(tied_rows)
    if ambiguous:
        raise features.RepresentativeNotDetermined(
            f"{len(ambiguous)} wording(s) whose observations tied at the "
            f"maximum (started_at, run_id) disagree on what canonicalization "
            f"reads, e.g. {ambiguous[:3]}")
    return reps


def _semantics(reps):
    """{fingerprint: RepresentativeSemantics} for a representative map."""
    return {fp: features.representative_semantics(
                o.get("title"), o.get("description_text"),
                o.get("minhash_signature"))
            for fp, o in reps.items()}


def _representative_signature(observation, shingles):
    """The signature canonicalization actually consumes for this wording.

    Standardization computes it once and stores it; every consumer — the
    Spark job included — reads the STORED value and never recomputes it. The
    reference reads it too, or it would model a different LSH corpus than
    production and a stored-signature defect could pass here and fail there.
    Deriving from the shingles is only the fallback for an in-memory corpus
    that carries no stored value, and is the same recipe standardization ran,
    not a second one.
    """
    stored = observation.get("minhash_signature")
    if stored is None:
        return features.minhash_signature(shingles)
    return features.signature_values(stored)


def _memberships(observations):
    """Membership rows exactly as `memberships_sql` aggregates them: one per
    distinct listing × usable fingerprint with the earliest started_at and
    the latest observation's advertiser."""
    first_seen = {}
    latest = {}
    for o in observations:
        if o.get("fingerprint") is None:
            continue
        key = (o["board"], o["market"], o["board_job_id"], o["fingerprint"])
        if key not in first_seen or o["started_at"] < first_seen[key]:
            first_seen[key] = o["started_at"]
        held = latest.get(key)
        if held is None or (o["started_at"], o["run_id"]) > \
                (held["started_at"], held["run_id"]):
            latest[key] = o
    return [{"board": b, "market": m, "board_job_id": j, "fingerprint": fp,
             "first_seen_membership_at": first_seen[(b, m, j, fp)],
             "advertiser": latest[(b, m, j, fp)].get("advertiser")}
            for (b, m, j, fp) in first_seen]


def _never_usable(observations):
    usable = {(o["board"], o["market"], o["board_job_id"])
              for o in observations if o.get("fingerprint") is not None}
    every = {(o["board"], o["market"], o["board_job_id"])
             for o in observations}
    return sorted(every - usable)


def extend_mapping_rows(observations_p, observations_n, base_rows):
    """The in-memory reference: same decisions as the Spark path, pure data.

    observations: dicts with board, market, board_job_id, fingerprint
    (None when unusable), started_at, run_id, title, description_text and
    advertiser — the standardized generations at P and N, chronology
    attached. base_rows: the validated mapping bound to P.

    Returns (rows, diagnostics) for the complete candidate, or raises
    FullRebuildRequired. Candidate discovery here runs over the whole N
    corpus and is then filtered to the region, which is the specification the
    Spark path's restricted joins must reproduce; the parity test binds them.
    """
    key = lambda o: (o["run_id"], o["board"], o["market"], o["board_job_id"])
    keys_p = {key(o) for o in observations_p}
    keys_n = {key(o) for o in observations_n}
    if not keys_p <= keys_n:
        raise FullRebuildRequired(
            "history_not_append_only",
            f"{len(keys_p - keys_n)} baseline observation(s) missing at N")
    batch = [o for o in observations_n if key(o) not in keys_p]
    batch_usable = [o for o in batch if o.get("fingerprint") is not None]

    latest_by_listing = {}
    for o in observations_p:
        if o.get("fingerprint") is None:
            continue
        listing = (o["board"], o["market"], o["board_job_id"])
        stamp = (o["started_at"], o["run_id"])
        if listing not in latest_by_listing or stamp > latest_by_listing[listing]:
            latest_by_listing[listing] = stamp
    stale = out_of_order_observations(batch_usable, latest_by_listing)
    if stale:
        raise FullRebuildRequired(
            "out_of_order_observations",
            f"{len(stale)} batch observation(s) precede standardized "
            f"history, e.g. {stale[:3]}")

    reps_n_all = _representatives(observations_n)
    try:
        reps_p_all = _representatives(observations_p)
    except features.RepresentativeNotDetermined as undetermined:
        raise FullRebuildRequired(
            "baseline_representative_undetermined", str(undetermined)) from None
    fps_p = set(reps_p_all)
    batch_fps = {o["fingerprint"] for o in batch_usable}
    new_fps = batch_fps - fps_p
    reobserved = batch_fps & fps_p
    semantics_p, semantics_n = _semantics(reps_p_all), _semantics(reps_n_all)
    changed = changed_representatives(
        {fp: semantics_p[fp] for fp in reobserved},
        {fp: semantics_n[fp] for fp in reobserved})

    component_of, members = base_components(base_rows)
    memberships_p = _memberships(observations_p)
    memberships_n = _memberships(observations_n)
    adv_p = {(m["board"], m["market"], m["board_job_id"], m["fingerprint"]):
             m["advertiser"] for m in memberships_p}
    adv_n = {(m["board"], m["market"], m["board_job_id"], m["fingerprint"]):
             m["advertiser"] for m in memberships_n}
    touched_members = {(o["board"], o["market"], o["board_job_id"],
                        o["fingerprint"]) for o in batch_usable}
    drifted = advertiser_drift(
        {k: adv_p[k] for k in touched_members if k in adv_p},
        {k: adv_n[k] for k in touched_members if k in adv_n})
    new_memberships = sorted(
        (k[:3], k[3]) for k in touched_members if k not in adv_p)

    # transitions of the listings this batch touched, before and after: a
    # listing the batch did not observe has an unchanged wording sequence
    touched_listings = {(o["board"], o["market"], o["board_job_id"])
                        for o in batch_usable}

    def listing_transitions(observations):
        return candidates_module.transitions_from_observations(
            [o for o in observations
             if o.get("fingerprint") is not None
             and (o["board"], o["market"], o["board_job_id"])
             in touched_listings])

    transitions_p = listing_transitions(observations_p)
    transitions_n = listing_transitions(observations_n)
    endpoints = new_transition_endpoints(transitions_p, transitions_n)

    seeds_old = sorted(
        ({fp for _listing, fp in new_memberships}
         | {k[3] for k in drifted} | endpoints | set(changed)) & fps_p)
    seeds = new_fps | set(seeds_old)
    total = len(reps_n_all)
    if region_too_large(len(seeds), total):
        raise FullRebuildRequired(
            "seed_region_too_large",
            f"{len(seeds)} of {total} wordings seeded")

    docs = {fp: features.matching_document(o.get("title"),
                                           o.get("description_text"))
            for fp, o in reps_n_all.items()}
    titles = {fp: o.get("title") for fp, o in reps_n_all.items()}
    bodies = {fp: features.matching_document(None, o.get("description_text"))
              for fp, o in reps_n_all.items()}
    shingle_sets = {fp: features.shingle_ids(doc)
                    for fp, doc in docs.items()}
    minhashes = {fp: features.minhash_from_signature(
        _representative_signature(reps_n_all[fp], shingle_sets[fp]))
        for fp in docs}
    transitions = candidates_module.transitions_from_observations(
        [o for o in observations_n if o.get("fingerprint") is not None])
    routes_by_pair, continuity, route_counts = \
        candidates_module.candidate_pairs(memberships_n, titles, minhashes,
                                          transitions)
    shared_listing = candidates_module.same_listing_pairs(memberships_n)

    judged = 0

    def accepted_touching(region):
        """Every N candidate pair with an endpoint in `region`, judged.

        The ceiling is checked on the pair COUNT before any pair is judged,
        matching where the Spark path decides, so both engines fall back at
        the same point rather than one of them paying for the round first."""
        nonlocal judged
        touching = [pair for pair in sorted(routes_by_pair)
                    if pair[0] in region or pair[1] in region]
        judged += len(touching)
        if too_many_candidates(judged, len(reps_n_all)):
            raise FullRebuildRequired(
                "candidate_volume_too_large",
                f"{judged} candidate pairs over {len(reps_n_all)} wordings")
        out = []
        for pair in touching:
            fp_a, fp_b = pair
            jaccard = features.exact_jaccard(shingle_sets[fp_a],
                                             shingle_sets[fp_b])
            body_a, body_b = bodies.get(fp_a), bodies.get(fp_b)
            verdict = canonical.judge_pair(
                jaccard, routes_by_pair[pair], continuity.get(pair, False),
                titles.get(fp_a), titles.get(fp_b), pair in shared_listing,
                body_a is not None and body_a == body_b)
            if verdict["lane"] is not None:
                out.append((pair, jaccard, verdict["lane"]))
        return out

    region, accepted, closure = close_region(
        seeds, component_of, members, accepted_touching, total)
    if region_too_large(len(region), total):
        raise FullRebuildRequired(
            "closure_region_too_large",
            f"{len(region)} of {total} wordings in the closed region")

    memberships_region = [m for m in memberships_n
                          if m["fingerprint"] in region]
    rows, assembly = assemble(
        base_rows, accepted, new_fps, region, memberships_region,
        {fp: titles.get(fp) for fp in region},
        _never_usable(observations_n))
    diagnostics = dict(
        assembly, **closure, mode="incremental",
        batch_observations=len(batch),
        batch_usable_observations=len(batch_usable),
        new_fingerprints=len(new_fps),
        seeded_known_fingerprints=len(seeds_old),
        changed_representatives=len(changed),
        drifted_memberships=len(drifted),
        new_memberships=len(new_memberships),
        new_transitions=len(set(transitions_n) - set(transitions_p)),
        region_fraction=round(len(region) / total, 4) if total else 0.0,
        candidates_judged=judged,
        accepted_region_edges=len(accepted),
        **route_counts)
    return rows, diagnostics
