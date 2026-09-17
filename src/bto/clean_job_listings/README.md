# clean_job_listings

Cleaning turns source-specific Bronze observations into clean, comparable job
listings, then works out which source listings appear to describe the same
opportunity.

This README is orientation. [CONTRACT.md](docs/CONTRACT.md) owns exact behavioral
guarantees and invariants; [HTML_NORMALIZATION.md](docs/HTML_NORMALIZATION.md) owns
exact HTML → `description_text` behavior; and the
[Databricks data dictionary](../storage/databricks/data-dictionary.md) owns
persistent schemas, datatypes and field definitions.

## How cleaning works

```text
Bronze observations
        ↓
incremental standardization
        ↓
bto.silver.standardized_job_listings
        ↓
canonicalization: extend when provable, otherwise full-history rebuild
        ↓
bto.silver.job_canonical_mapping
        ↓
freshness proven
        ↓
downstream consumers may use the results
```

Standardization processes only Bronze observations that are not already
present in `standardized_job_listings`. Existing standardized rows do not
change. After the new rows have been added, cleaning reconciles the complete
Bronze and standardized observation-key sets.

Canonicalization always produces the complete mapping for the standardized
history at one captured version. It may safely extend a validated existing
mapping when the incremental safety rules prove that result is equivalent to
the full-history algorithm; otherwise it performs the authoritative
full-history reconstruction. It may skip canonicalization only when
standardization added no rows and the existing mapping is provably current.

## Structure

```text
src/bto/clean_job_listings/
├── __init__.py
├── README.md                       # this document
├── docs/
│   ├── CONTRACT.md                 # exact rules and invariants
│   └── HTML_NORMALIZATION.md       # exact HTML → description_text rules
│
├── run.py                          # orchestrate, validate and publish
├── standardize_job_listings.py     # Bronze source shapes → common Silver schema
├── normalize_description.py        # source descriptions → comparable plain text
├── build_matching_features.py      # fingerprint, shingles, MinHash and shared helpers
├── build_candidate_pairs.py        # nominate non-exact wording pairs to examine
├── build_canonical_mapping.py      # judge pairs and build canonical groups
├── extend_canonical_mapping.py     # decide when a published mapping can be extended
└── canonicalize_on_databricks.py   # canonicalization Spark job, full or extending

src/bto/storage/databricks/
└── freshness.py                    # prove the mapping is current and internally valid
```

Cleaning is one transformation pipeline, not a collection of independent
services.

## What each file does

### `run.py`

The cleaning orchestrator. It reads Bronze observations, runs standardization,
stages and validates the results, publishes new standardized rows, and
reconciles the complete Bronze and standardized histories.

It then decides whether canonicalization is required, coordinates either a
validated incremental construction or its full-history fallback, publishes the
complete validated mapping, and performs the final freshness proof. It also
owns the whole-table standardization rebuild and revalidation path. Deployment
and service-operation details live in
[deploy/README.md](../../../deploy/README.md).

### `standardize_job_listings.py`

Understands how the four Bronze source shapes map into the common Silver
schema. For example:

```text
MCF       postedCompany.name
SEEK      job.advertiser.name
Indeed    companyDetails.name
LinkedIn  companyName
                    ↓
             advertiser_name
```

It handles the other source-specific mappings for dates, company fields,
location, salary, classifications, URLs and contacts in the same way.

Its output remains one row per Bronze observation. Identity-only observations
survive with their identity and stable job URL, while payload-derived fields
remain NULL. If description normalization reports an unsupported construct,
standardization keeps the row and quarantines only the description-derived
matching fields. It standardizes; it does not deduplicate.

### `normalize_description.py`

Defines how the source description is represented as clean
`description_text`. MCF, the SEEK family and Indeed use the shared
deterministic HTML normalizer. LinkedIn supplies plain text and follows its
smaller source-specific cleanup rule.

The module reports deliberately unsupported HTML constructs to the
standardizer. It does not know about candidate pairs, canonical groups or
deduplication.

### `build_matching_features.py`

Builds the normalized matching document from `title` and a matching-only view
of `description_text`, then derives the exact fingerprint and MinHash signature
stored on each usable standardized observation. Stored `description_text`
retains every script; only matching excludes description tokens containing
non-Latin letters. A genuinely nonempty description that leaves no usable
matching description does not fall back to title-only, while an observation
with no description retains the existing title-only behavior.

It also owns the shared deterministic title, advertiser and career-stage
helpers that candidate generation and pair judgment must interpret
identically. It creates matching inputs; it does not decide that two wordings
belong together.

### `build_candidate_pairs.py`

Nominates non-exact wording pairs that are worth comparing properly, without
comparing every possible pair in the historical corpus. It combines three
routes:

1. a MinHash/LSH shortlist;
2. the same normalized advertiser and title within one market;
3. observed consecutive wording transitions of one
   `(board, market, board_job_id)`.

The third route follows actual history. A sequence `A → B → A → C` nominates
`A ↔ B` and `A ↔ C`; it does not invent an unobserved `B ↔ C` transition.

Candidates are only nominations. This module never decides that two wordings
match.

### `build_canonical_mapping.py`

Judges each candidate pair using the shared matching rules, adds an edge for
each accepted pair, and turns the resulting connected components into
canonical groups.

```text
historical fingerprint nodes
        ↓
candidate pairs
        ↓
evidence judgment
        ↓
accepted edges
        ↓
connected components
        ↓
deterministic canonical_job_id
        ↓
job_canonical_mapping rows
```

It retains historical wording membership and produces mapping rows. It does
not collapse `standardized_job_listings`, choose a “best” listing or create a
current-jobs table.

### `extend_canonical_mapping.py`

Decides whether an already-published mapping can be extended with new
standardized observations instead of rebuilding every historical wording, and
assembles the result when it can.

It owns the safety rules, not the matching rules. Every change that can move a
pair's verdict — a new wording, a representative that now reads differently to
the matching rules, a new listing membership, advertiser drift, a new observed
transition — seeds a region. The region takes in each seed's whole baseline
group, then grows through every current pair-accepted edge crossing its
boundary until a round adds nothing, and is finally rebuilt from individual
wordings with the unchanged full-history judge, guarded union and winner
rules, so it may merge groups and split them.

Its detectors still refuse the shortcut where no proof is available:
observations that arrive out of chronological order, a standardized history
that is not append-only, an unusable baseline, a closure that will not
converge, and a region or candidate volume so large the shortcut saves
nothing. Any refusal raises `FullRebuildRequired` and the caller runs the
full-history build instead; nothing here ever guesses.

Groups proven outside the closed region are carried forward unchanged,
sentinels are regenerated, and the result is a complete candidate mapping. It
must equal what the full-history algorithm would produce, which is what the
offline regression compares.

### `canonicalize_on_databricks.py`

Runs production-scale canonicalization as a Databricks Spark job. Spark
performs the heavy set work needed to find candidates and assemble their
evidence, while the same shared Python matching rules make the actual match
decisions.

The job has one decision point. Given a published mapping to start from, it
first tries the extension above; without one, or on any condition the
extension refuses, it rebuilds from the complete standardized history. Both
paths produce the same complete candidate, so callers cannot tell them apart
except through the reported mode and fallback reason.

The driver finishes component assignment and writes a validated, run-scoped
scratch mapping. `run.py` owns final publication and freshness.

### `storage/databricks/freshness.py`

Provides the single fail-closed check that
`job_canonical_mapping` was built from the standardized data it claims to
represent and still satisfies the required mapping and membership invariants.
Cleaning uses it before reporting success; downstream consumers must use the
same definition before trusting the mapping.

## The two Silver tables

Cleaning deliberately persists only two tables.

### `bto.silver.standardized_job_listings`

One row per Bronze observation.

Think of it as:

> What did this source listing look like in each observation, expressed in the
> common vocabulary?

A job seen on ten scrape runs remains ten observations.

The same real-world job advertised on LinkedIn, MCF and JobStreet remains three
separate source listings.

This table contains cleaned job information and matching inputs, but it does
not collapse duplicates.

### `bto.silver.job_canonical_mapping`

The reconciliation layer.

Think of it as:

> Which source-listing wordings belong to the same job group under the current
> complete corpus?

It records each source listing × distinct usable historical fingerprint and
the canonical group that fingerprint belongs to.

Listings that have never produced usable matching text still receive their
defined sentinel mapping, so every source listing is represented.

The mapping always represents the complete standardized history and is
published as a complete table. Construction may extend a validated existing
mapping when that is provably equivalent to the full-history algorithm;
otherwise it rebuilds from the complete history. It is rebuildable analytical
state, not application state.

## How the data behaves

### Observation history stays intact

An important rule throughout cleaning is:

**cleaning is not collapsing.**

For example:

```text
Monday     LinkedIn 123    Data Scientist
Tuesday    LinkedIn 123    Data Scientist
Wednesday  LinkedIn 123    Senior Data Scientist
```

Those remain three standardized observations. The canonical mapping separately
records the distinct usable wordings belonging to that listing.

Likewise, if the same vacancy appears on several boards:

```text
LinkedIn 123
Indeed abc
JobStreet 456
```

the standardized table keeps all three source listings. Canonicalization may
then determine that they belong to one group.

This separation lets downstream consumers use both the observation history
and the real-world duplicate group without destroying either.

### Missing payloads stay missing

Bronze sometimes knows that a job was seen even though the correct full payload
was not captured. Standardization preserves that observation.

It does not copy an older description into the row, and it does not borrow a
later payload to fill the gap.

```text
job observed
content_hash = NULL
        ↓
standardized observation still exists
        ↓
identity retained
payload-derived content remains NULL
```

This keeps Silver faithful to the evidence collected in Bronze.

### Current assignment

The mapping preserves historical wording membership, but a later consumer will
often want the simpler question:

> Which canonical job does this source listing belong to now?

That assignment is derived from the listing's latest usable standardized
wording.

A later observation with no usable matching text does not erase the last usable
assignment. The unusable observation remains in
`standardized_job_listings`, while current assignment continues to come from
the latest usable wording. This is derived state rather than a third persistent
table.

## Matching in plain English

Cleaning optimises for “same enough opportunity for BTO,” not perfect
requisition-level identity. It should group copies, reposts and meaningful
rewrites of one opportunity without merging different professions merely
because they share recruiter or employer boilerplate.

```text
matching features
        ↓
candidate nomination
        ↓
exact evidence judgment
        ↓
rotating-slot and career-stage safeguards
        ↓
connected components = canonical job groups
```

Each distinct usable fingerprint is a graph node, so exact wording identity
needs no candidate search. MinHash/LSH, matching advertiser and title, and
observed listing-history transitions nominate different-fingerprint pairs for
closer comparison. Nomination is never acceptance: pair judgment uses exact
shingle overlap and the shared title, advertiser and history rules rather than
a MinHash estimate.

Safeguards prevent a board's rotating listing slot from joining unrelated jobs
and keep internship and graduate opportunities separate. Accepted pairs become
edges; connected components become canonical groups. Transitivity is
intentional, so two wordings can share a group through intermediate accepted
evidence even when they do not form a direct edge themselves.

Each component receives a deterministic `canonical_job_id`. The ID is a group
key, not the current wording, a representative listing or the “best” listing.
[CONTRACT.md](docs/CONTRACT.md) owns the exact candidate routes, thresholds,
safeguards, component rules and identifier construction.

## Where cleaning runs

Lightsail orchestrates cleaning after collection succeeds. The cleaning Python
process performs row standardization and coordinates Databricks reads, staging,
validation and publication. Databricks owns the Bronze and Silver data, and
production canonicalization runs there as a Spark job submitted by Lightsail.

```text
AWS Lightsail
    │
    │ collection
    ▼
Databricks Bronze
    │
    │ standardization and canonicalization
    ▼
Databricks Silver
    │
    │ filtering and ranking
    ▼
filter / triage / rank
    │
    ▼
selected app-facing state
    │
    ▼
PostgreSQL
```

Cleaning does not use PostgreSQL. Its two Silver products are analytical and
rebuildable. PostgreSQL is reserved for later operational state that cannot
simply be reconstructed, such as what the owner reviewed, saved or applied for.

Validation and freshness failures fail closed. The
[deployment README](../../../deploy/README.md) owns service operation,
deployment, alerts and recovery details.

### Build, publication and freshness

Normal runs standardize only missing Bronze observations. Canonicalization
always produces the complete mapping for the standardized history at the
captured version, using a validated incremental extension when provable and a
full-history reconstruction otherwise. Both products are validated before
publication, and the run reports success only when the mapping is provably
current against the standardized data it represents.
[CONTRACT.md](docs/CONTRACT.md) owns the exact publication sequence and freshness
rules.

#### Standardization is incremental

The observation key is:

```text
(run_id, board, market, board_job_id)
```

Each normal run finds Bronze keys missing from
`standardized_job_listings`, standardizes those observations into
run-scoped scratch, validates them, and inserts them. Existing standardized
rows do not change.

Afterward, cleaning compares the complete Bronze and standardized key sets in
both directions. A mismatch fails the run rather than allowing incomplete
history downstream.

A wholesale standardization path remains available for rebuild and
revalidation. It uses the same row transformation and must produce the same
result for a given Bronze observation.

#### Canonicalization has full-history semantics

Canonicalization captures one Delta version N and must produce the same
complete mapping that the full-history algorithm defines for that version.
When the incremental safety rules hold, it can construct that result by
extending a validated mapping from an earlier version; otherwise it reads the
complete standardized history and performs the authoritative full-history
reconstruction. Either path publishes a complete replacement mapping.

The complete-result requirement matters because new evidence can bridge groups
that were previously separate:

```text
BEFORE

A ── A2          B ── B2

NEW WORDING ARRIVES

A ── A2 ── C ── B ── B2
```

The new wording C connects the two older groups, so their historical
memberships must now receive one group assignment. New data can therefore
change old canonical assignments.

#### Freshness is version-bound

The mapping records the Delta version of `standardized_job_listings` from
which it was built. Freshness uses that binding rather than timestamps; a
later maintenance-only commit need not make the mapping stale.

If cleaning cannot prove that the mapping still represents the standardized
data and satisfies the required membership rules, it fails closed. Downstream
consumers must not use an unproven mapping. Exact mechanics live in
[CONTRACT.md](docs/CONTRACT.md) and `storage/databricks/freshness.py`.

## Downstream interface

Cleaning provides exactly two persistent Silver products:

```text
bto.silver.standardized_job_listings    complete standardized observation history

bto.silver.job_canonical_mapping        source listing × historical usable wording
                                        → canonical group under the current
                                          complete corpus
```

There is no third persistent table, and cleaning does not suppress
previously seen opportunities.

Filtering and ranking owns the recency rule and every suppression decision.
Cleaning supplies the inputs for it: the current canonical assignment of each
source listing, the enumeration of a component's members, and each member's
own observation history. Whether recency is judged per member or per component
is a filtering-and-ranking decision, not one this package makes — a component
can hold several genuinely different vacancies (see the contract), so treating
one as a single opportunity may hide useful jobs.

It must not treat `canonical_job_id` as durable suppression state: new evidence
can merge groups and change IDs on a later rebuild. The durable identity is the
membership key `(board, market, board_job_id, fingerprint)`.

Observation time comes from `bto.bronze.scrape_runs` through `run_id`, so
that Bronze table is part of the downstream interface.
[CONTRACT.md](docs/CONTRACT.md) defines the exact consumer contract and its edge
cases.

## What this package does not do

Cleaning stops once job data has been standardized and reconciled. It does not:

- decide whether the owner should apply;
- remove job listings because they are irrelevant;
- perform LLM triage;
- extract application requirements for scoring;
- rank or tier job listings;
- tailor a CV;
- research recruiters or hiring managers;
- track applications;
- store the owner's review or application state in PostgreSQL.

Those responsibilities belong to filtering and ranking and to the review,
application, CV, research and tracking packages.

## Related documentation

| Document | Use it for |
| :--- | :--- |
| [CONTRACT.md](docs/CONTRACT.md) | Exact transformation, matching, canonicalization and freshness rules |
| [HTML_NORMALIZATION.md](docs/HTML_NORMALIZATION.md) | Exact HTML → `description_text` behavior |
| [Databricks data dictionary](../storage/databricks/data-dictionary.md) | Silver schemas, datatypes and field definitions |
| [Collection README](../fetch_job_listings/README.md) | How the Bronze input is collected |
| [Root README](../../../README.md) | How cleaning fits into the whole Back to Office pipeline |
| [Deployment README](../../../deploy/README.md) | Deployment, service operation, alerts and recovery |
