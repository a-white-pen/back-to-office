"""Canonical groups over the complete standardized history → mapping rows.

Implements the canonical mapping contract's grain, winner, id and sentinel
semantics with the locked evidence structure: every distinct usable historical
fingerprint is a node and exact equality is node identity (matching step 1);
non-exact candidate pairs come from build_candidate_pairs; a candidate
connects when ANY evidence lane accepts it — very high text similarity,
medium similarity with compatible titles (the universal title gate), same
advertiser + same title above a low text floor, or the same-listing history
rule (listing + advertiser continuity + compatible title, regardless of
text). Bare listing identity still never creates an edge. Connected
components are canonical groups. Pure data-in / data-out — no Databricks
here; run.py owns reads, staging and the atomic publication.

Main functions:
    judge_pair(jaccard, routes, continuous, title_a, title_b, same_listing,
               identical_bodies) -> verdict dict        (the pair-level rules)
    components_from_edges(fingerprints, accepted, stage_of)
        -> (component_of, edges, counts)                (the guarded union-find)
    rows_from_components(memberships, component_of, never_usable) -> rows
    build_mapping_rows(memberships, docs_by_fingerprint, signatures,
                       never_usable, titles)
        -> list of 5-column row dicts (canonicalized_at is stamped at publish)
    current_assignment(observations)  -> fingerprint | None
    validate_candidate(rows, ...)     -> list of violation strings (empty = pass)
"""

import logging
from collections import defaultdict

from . import build_candidate_pairs as candidates_module
from . import build_matching_features as features

log = logging.getLogger(__name__)

MAPPING_COLUMNS = ("board", "market", "board_job_id", "fingerprint",
                   "canonical_job_id", "canonicalized_at")

# ONE locked deterministic matching recipe. No row-level recipe version is
# stored in either Silver table: matching-visible behaviour is protected by
# these constants, the tests and docs/CONTRACT.md. Any matching-visible change
# requires explicit redesign and revalidation, never a version branch in the
# data.

# Evidence parameters — locked.
HIGH_JACCARD = 0.70              # connect without the title gate
MEDIUM_JACCARD_FLOOR = 0.50      # connect with compatible titles
ADV_TITLE_JACCARD_FLOOR = 0.35   # same advertiser + same title lane


def sentinel_canonical_id(board, market, board_job_id):
    """The never-usable sentinel's three-part self id."""
    return f"{board}:{market}:{board_job_id}"


def usable_canonical_id(board, market, board_job_id, fingerprint):
    """Winning membership serialized with the full 64-hex fingerprint."""
    return f"{board}:{market}:{board_job_id}:{fingerprint}"


def _accepting_lane(jaccard, routes, continuous, compatible, slot_veto):
    """Independent OR-rules; the first accepting lane, or None.

    Candidate routes never decide by themselves — every lane is an evidence
    judgment about the pair, and bare listing identity (a listing_history
    candidate without advertiser continuity or a compatible title) connects
    nothing.

    slot_veto is the rotating-slot rule (locked rule-logic serving the
    contract's intent that rotating slots with materially different titles must
    split): two wordings of the SAME listing whose titles share no words are
    never joined by text similarity alone. Reused template text can otherwise
    exceed the high bar even when title disjointness is the strongest evidence
    of a different job. Cross-listing pairs are never vetoed, and the
    advertiser+title lane is untouched because equal titles cannot be
    disjoint.
    """
    if not slot_veto:
        if jaccard >= HIGH_JACCARD:
            return "high"
        if jaccard >= MEDIUM_JACCARD_FLOOR and compatible:
            return "medium_title"
    if "advertiser_title" in routes and jaccard >= ADV_TITLE_JACCARD_FLOOR:
        return "advertiser_title"
    if "listing_history" in routes and continuous and compatible:
        return "listing_history"
    return None


def judge_pair(jaccard, routes, continuous, title_a, title_b, same_listing,
               identical_bodies):
    """The ONE pair-level verdict — every rule that decides whether a
    candidate pair may connect, in the locked order: the career-stage pair
    block, the rotating-slot protection with its abbreviation-continuity and
    identical-body exceptions, then the evidence lanes (`_accepting_lane`).
    Pure: primitives in, a dict out, so the ordinary Python path and the
    Spark worker adapter call exactly this function.

    jaccard: exact shingle Jaccard of the pair; routes: the candidate routes
    that nominated it; continuous: same-listing advertiser continuity;
    same_listing: the two wordings co-occur in one listing's history;
    identical_bodies: the two normalized description bodies are exactly
    equal (a missing body never counts as identical — the caller decides).
    Returns {"lane": str | None, "stage_blocked", "slot_disjoint",
    "continuity_override", "body_override", "slot_veto"}.
    """
    if features.career_stages_conflict(title_a, title_b):
        return {"lane": None, "stage_blocked": True, "slot_disjoint": False,
                "continuity_override": False, "body_override": False,
                "slot_veto": False}
    slot_disjoint = bool(same_listing
                         and features.titles_disjoint(title_a, title_b))
    continuity_override = bool(slot_disjoint and
                               features.title_abbreviation_continuity(title_a, title_b))
    body_override = bool(slot_disjoint and not continuity_override
                         and identical_bodies)
    slot_veto = bool(slot_disjoint and not continuity_override
                     and not body_override)
    lane = _accepting_lane(jaccard, routes, continuous,
                           features.titles_compatible(title_a, title_b), slot_veto)
    return {"lane": lane, "stage_blocked": False, "slot_disjoint": slot_disjoint,
            "continuity_override": continuity_override,
            "body_override": body_override, "slot_veto": slot_veto}


def components_from_edges(fingerprints, accepted, stage_of):
    """Connected components over accepted pairs — the ONE union-find.

    accepted: iterable of (pair, jaccard, lane) for pairs judge_pair
    accepted; they are processed in ascending pair order regardless of the
    order supplied. Union roots are the lexicographically smaller root. A
    union that would put a clearly intern-stage and a clearly graduate-stage
    wording in one component is refused and the edge dropped (the
    component-level career-stage guard). Returns (component_of, edges,
    counts): {fingerprint: root}, {pair: jaccard} for the edges actually
    kept, and the union-time diagnostics.
    """
    parent = {fp: fp for fp in fingerprints}
    # career-stage flags per component root: a component may never come to
    # contain both a clearly intern-stage and a clearly graduate-stage
    # wording — the constraint is enforced at union time so a bridge node
    # cannot transitively reunite what the pair rule keeps apart
    stages = {fp: {stage_of.get(fp)} - {None} for fp in fingerprints}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    edges = {}
    counts = defaultdict(int)
    for pair, jaccard, lane in sorted(accepted, key=lambda edge: edge[0]):
        fp_a, fp_b = pair
        root_a, root_b = find(fp_a), find(fp_b)
        if root_a != root_b:
            merged_stages = stages[root_a] | stages[root_b]
            if "intern" in merged_stages and "graduate" in merged_stages:
                counts["stage_blocked_unions"] += 1
                continue                      # edge dropped, not just deferred
            keep, gone = min(root_a, root_b), max(root_a, root_b)
            parent[gone] = keep
            stages[keep] = merged_stages
        edges[pair] = jaccard
        counts[f"edges_{lane}"] += 1
    return {fp: find(fp) for fp in fingerprints}, edges, dict(counts)


def build_components(memberships, docs_by_fingerprint, signatures=None,
                     titles=None, bodies=None, transitions=()):
    """{fingerprint: component_root} over every usable wording node.

    Exact fingerprint equality is node identity by construction (matching
    step 1): identical wordings are one node before any pair is examined.
    Non-exact candidates come from the candidate union; each is judged once by
    `judge_pair` with exact shingle Jaccard plus the pair's
    title/advertiser/history context, and the accepted pairs go through
    `components_from_edges`. Stored signatures may be supplied to skip
    re-hashing; shingle sets are always rebuilt from the documents because
    exact verification needs them.

    Returns (component_of, edges, diagnostics) where edges is
    {pair: jaccard} for accepted pairs and diagnostics carries candidate and
    per-lane acceptance counts.
    """
    titles = titles or {}
    bodies = bodies or {}
    fingerprints = list(dict.fromkeys(
        m["fingerprint"] for m in memberships))
    shingle_sets = {fp: features.shingle_ids(docs_by_fingerprint[fp])
                    for fp in fingerprints}
    minhashes = {}
    for fp in fingerprints:
        values = (signatures or {}).get(fp)
        minhashes[fp] = (features.minhash_from_signature(values) if values
                         else features.minhash_from_signature(
                             features.minhash_signature(shingle_sets[fp])))

    routes_by_pair, continuity, counts = candidates_module.candidate_pairs(
        memberships, titles, minhashes, transitions)
    shared_listing = candidates_module.same_listing_pairs(memberships)
    stage_of = {fp: features.career_stage(titles.get(fp))
                for fp in fingerprints}

    accepted = []
    lane_counts = defaultdict(int)
    for pair in sorted(routes_by_pair):
        fp_a, fp_b = pair
        title_a, title_b = titles.get(fp_a), titles.get(fp_b)
        jaccard = features.exact_jaccard(shingle_sets[fp_a],
                                         shingle_sets[fp_b])
        body_a, body_b = bodies.get(fp_a), bodies.get(fp_b)
        # Identical normalized description bodies establish title-only
        # rotation, so the slot veto must not split the pair. Missing bodies
        # never count as identical.
        identical_bodies = body_a is not None and body_a == body_b
        verdict = judge_pair(jaccard, routes_by_pair[pair],
                             continuity.get(pair, False), title_a, title_b,
                             pair in shared_listing, identical_bodies)
        if verdict["stage_blocked"]:
            lane_counts["stage_blocked_pairs"] += 1
        if verdict["continuity_override"]:
            lane_counts["slot_continuity_overrides"] += 1
        if verdict["body_override"]:
            lane_counts["slot_body_overrides"] += 1
        if verdict["slot_veto"]:
            lane_counts["slot_vetoed_pairs"] += 1
        if verdict["lane"] is not None:
            accepted.append((pair, jaccard, verdict["lane"]))

    component_of, edges, union_counts = components_from_edges(
        fingerprints, accepted, stage_of)
    diagnostics = dict(counts, exact_checks=len(routes_by_pair),
                       **lane_counts, **union_counts)
    return component_of, edges, diagnostics


def rows_from_components(memberships, component_of, never_usable=()):
    """Mapping rows from a finished component assignment: the deterministic
    winner per component, its four-part id on every member row, and
    one sentinel per never-usable listing."""
    memberships = list(memberships)
    by_component = defaultdict(list)
    for member in memberships:
        by_component[component_of[member["fingerprint"]]].append(member)

    winners = {}
    for root, members in by_component.items():
        winner = min(members, key=lambda m: (
            m["first_seen_membership_at"], m["board"], m["market"],
            m["board_job_id"], m["fingerprint"]))       # no run_id term
        winners[root] = usable_canonical_id(
            winner["board"], winner["market"], winner["board_job_id"],
            winner["fingerprint"])

    rows = [{
        "board": m["board"], "market": m["market"],
        "board_job_id": m["board_job_id"], "fingerprint": m["fingerprint"],
        "canonical_job_id": winners[component_of[m["fingerprint"]]],
    } for m in memberships]

    usable_listings = {(m["board"], m["market"], m["board_job_id"])
                       for m in memberships}
    for board, market, board_job_id in never_usable:
        if (board, market, board_job_id) in usable_listings:
            continue                                     # never both
        rows.append({
            "board": board, "market": market, "board_job_id": board_job_id,
            "fingerprint": None,
            "canonical_job_id": sentinel_canonical_id(board, market, board_job_id),
        })
    return rows


def build_mapping_rows(memberships, docs_by_fingerprint, signatures=None,
                       never_usable=(), titles=None, bodies=None,
                       transitions=()):
    """The complete candidate mapping (without `canonicalized_at`).

    memberships: iterable of dicts with board, market, board_job_id,
        fingerprint, first_seen_membership_at — one entry per DISTINCT
        listing × usable fingerprint, its first-seen already aggregated as
        min(scrape_runs.started_at) over observations of that exact
        membership (never lexical run_id). An optional `advertiser`
        key feeds the advertiser+title and listing-history evidence.
    titles: {fingerprint: title} for the title gate — a node-level
        attribute, since a fingerprint fixes the normalized wording.
    never_usable: iterable of (board, market, board_job_id) for listings that
        have never had a usable fingerprint — exactly one sentinel each.
    """
    memberships = list(memberships)
    component_of, edges, candidate_diagnostics = build_components(
        memberships, docs_by_fingerprint, signatures, titles=titles,
        bodies=bodies, transitions=transitions)
    rows = rows_from_components(memberships, component_of, never_usable)
    diagnostics = dict(graph_diagnostics(component_of, edges, memberships),
                       **candidate_diagnostics)
    return rows, diagnostics


def graph_diagnostics(component_of, edges, memberships):
    """Monitoring values — reported each rebuild, never blocking."""
    sizes = defaultdict(int)
    rows_per = defaultdict(int)
    for root in component_of.values():
        sizes[root] += 1
    for member in memberships:
        rows_per[component_of[member["fingerprint"]]] += 1
    min_edge = defaultdict(lambda: 1.0)
    degree = defaultdict(int)
    for (a, b), jaccard in edges.items():
        root = component_of[a]
        min_edge[root] = min(min_edge[root], jaccard)
        degree[a] += 1
        degree[b] += 1
    size_values = sorted(sizes.values())
    return {
        "components": len(sizes),
        "near_components": sum(1 for root in sizes if root in min_edge),
        "largest_component_nodes": size_values[-1] if size_values else 0,
        "largest_component_rows": max(rows_per.values(), default=0),
        "accepted_edges": len(edges),
        "weakest_accepted_edge": min(min_edge.values(), default=None),
        "max_node_degree": max(degree.values(), default=0),
        "size_p50": size_values[len(size_values) // 2] if size_values else 0,
    }


def current_assignment(observations):
    """The listing's latest usable fingerprint, or None if never usable.

    observations: iterables of dicts with fingerprint, started_at, run_id for
    ONE source listing. Ordered by started_at DESC then run_id DESC — run_id
    only breaks an exact started_at tie, as a determinism device, never
    chronology. Identity-only/unusable observations never erase the latest
    usable assignment (they simply have fingerprint None and are skipped).
    """
    usable = [o for o in observations if o.get("fingerprint") is not None]
    if not usable:
        return None
    latest = max(usable, key=lambda o: (o["started_at"], o["run_id"]))
    return latest["fingerprint"]


def validate_candidate(rows, expected_listings=None, standardized_fingerprints=None,
                       titles=None):
    """Candidate-side hard invariants (the pure-data subset).

    Returns violation strings; the publisher must refuse to publish on any.
    The canonicalized_at single-value check runs after stamping and
    therefore lives with the publisher, not here. With `titles`, also
    asserts the career-stage constraint on the FINAL groups: no group may
    hold both a clearly intern-stage and a clearly graduate-stage wording.
    """
    violations = []
    # every usable id names a membership that must itself sit in that group
    keys_by_group = defaultdict(set)
    for row in rows:
        if row["fingerprint"] is not None:
            keys_by_group[row["canonical_job_id"]].add(
                (row["board"], row["market"], row["board_job_id"],
                 row["fingerprint"]))
    for canonical_id, keys in keys_by_group.items():
        head, _, winner_fp = canonical_id.rpartition(":")
        winner = tuple(head.split(":", 2)) + (winner_fp,)
        if winner not in keys:
            violations.append(f"id names a non-member winner: {canonical_id}")
    if titles is not None:
        stages_by_group = defaultdict(set)
        for row in rows:
            if row["fingerprint"] is None:
                continue
            stage = features.career_stage(titles.get(row["fingerprint"]))
            if stage:
                stages_by_group[row["canonical_job_id"]].add(stage)
        for canonical_id, stages in stages_by_group.items():
            if {"intern", "graduate"} <= stages:
                violations.append(
                    f"group mixes intern and graduate stages: {canonical_id}")
    seen_keys = set()
    listings_covered = set()
    by_group = defaultdict(set)
    sentinels = set()

    for row in rows:
        key = (row["board"], row["market"], row["board_job_id"],
               row["fingerprint"])
        if key in seen_keys:
            violations.append(f"duplicate logical key: {key}")
        seen_keys.add(key)
        listing = key[:3]
        listings_covered.add(listing)
        if row["fingerprint"] is None:
            sentinels.add(listing)
            if row["canonical_job_id"] != sentinel_canonical_id(*listing):
                violations.append(f"sentinel id malformed: {listing}")
        else:
            by_group[row["fingerprint"]].add(row["canonical_job_id"])
            if row["canonical_job_id"].count(":") != 3:
                violations.append(
                    f"usable id not four-part: {row['canonical_job_id']}")
            if standardized_fingerprints is not None \
                    and row["fingerprint"] not in standardized_fingerprints:
                violations.append(
                    f"fingerprint not in standardized input: {key}")

    usable_listings = {k[:3] for k in seen_keys if k[3] is not None}
    for listing in sentinels & usable_listings:
        violations.append(f"sentinel coexists with usable rows: {listing}")

    if expected_listings is not None:
        missing = set(expected_listings) - listings_covered
        for listing in sorted(missing):
            violations.append(f"listing not covered by mapping: {listing}")

    # exact fingerprint equality is node identity: one wording, one group id
    for group_ids in by_group.values():
        if len(group_ids) > 1:
            violations.append(
                f"one fingerprint mapped to several groups: {sorted(group_ids)}")
    # a usable id embeds its winning fingerprint, which lives in exactly one
    # component — two groups can therefore never legitimately share an id
    id_owners = defaultdict(set)
    for group_ids in by_group.values():
        for canonical_id in group_ids:
            id_owners[canonical_id].add(canonical_id.rsplit(":", 1)[-1])
    for canonical_id, winners_seen in id_owners.items():
        if len(winners_seen) > 1:
            violations.append(f"id names several winners: {canonical_id}")

    return violations
