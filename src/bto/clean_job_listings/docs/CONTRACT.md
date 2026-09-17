# Clean Job Listings Contract

**Status: LOCKED.** This document defines the behavior that must survive an
implementation rewrite. [README.md](../README.md) is orientation;
[HTML_NORMALIZATION.md](HTML_NORMALIZATION.md) defines HTML-to-text behavior;
the [Databricks data dictionary](../../storage/databricks/data-dictionary.md) owns
table schemas, datatypes, ordinary source mappings and field definitions.

## 1. Scope and authoritative products

This package standardizes Bronze job-listing observations, preserves their
history, derives matching inputs, and groups equivalent historical wordings.

It has exactly two persistent Silver products:

```text
bto.silver.standardized_job_listings
bto.silver.job_canonical_mapping
```

There is no persistent `canonical_jobs`, `deduplicated_jobs`, pair-evidence,
diagnostics or assignment-history table. Run-scoped build scratch is allowed
but is not a product. This package does not write PostgreSQL, and it does not
own suppression, filtering, triage, ranking or review state.

## 2. Standardized observation contract

### Grain and identity

One `standardized_job_listings` row is one Bronze job observation in one
board × market scrape run. Its exact observation key is:

```text
(run_id, board, market, board_job_id)
```

Repeated observations and cross-board copies remain separate. Standardization
must never collapse, deduplicate or rewrite observation history.

`run_id`, `board`, `market`, `board_job_id` and `content_hash` are copied from
Bronze. `run_id` is opaque: copy it verbatim and never parse its suffixes or
use lexical order as chronology. When two observations have the same
`scrape_runs.started_at`, bytewise `run_id` order may be used only as a
deterministic tie-break.

Ordinary columns and source mappings are defined only in the data dictionary.

### Payload boundary

`content_hash IS NULL` means Bronze recorded the observation without a valid
full payload. The standardized row must survive with its payload-derived fields
NULL. Never borrow or carry forward content from another observation.

The one payload-independent exception is `job_url`: it may be constructed from
`board`, `market` and `board_job_id` even when `content_hash` is NULL. It must
be a stable board-listing URL derived from identity, not a mutable source slug
or tracking URL.

A payload-bearing observation may still have no matching pair when its
description is quarantined or both title and normalized description are
unusable.

### Missing values and collections

| Input kind | Required standardized behavior |
| :--- | :--- |
| Scalar string | Trim; blank becomes NULL; otherwise preserve the value. Do not guess that `N/A`, `none`, `-` or `.` means missing without a field-specific rule. |
| Array of strings | Trim members, remove NULL/blank members, preserve order and duplicates, and use NULL when none remain. |
| Struct or array of structs | Normalize optional scalar members, retain objects with meaningful information, discard only empty objects, and preserve meaningful source order and duplicates. |
| Any standardized array | Never store an empty array; use NULL. |

For contacts, `value` is required for a struct to survive; `type` may be NULL
and must not be inferred from the value.

### Descriptions and quarantine

HTML-bearing boards use the single recipe in
[HTML_NORMALIZATION.md](HTML_NORMALIZATION.md). LinkedIn is outside that
normalizer: use its plaintext `jobDescription`, trim it, and remove exactly one
trailing `Show more` + whitespace + `Show less` sequence when present.
Occurrences elsewhere remain. Do not recover retired LinkedIn HTML or add
broader source-specific cleanup.

Matching uses `description_text`, never `description_html` or raw payload prose.

When the HTML normalizer reports an unsupported description:

- preserve the standardized observation and every independently derivable
  field;
- set `description_text`, `fingerprint` and `minhash_signature` to NULL;
- report the typed reason without logging JD prose or contact values;
- do not persist a quarantine-status column.

Individual quarantines continue and make the affected board × market partial.
If every payload-bearing row for a board × market in the selected batch
quarantines, the build must fail before any row from that batch is merged.
The build-level failure check belongs to orchestration, not to the normalizer.

### Exceptional transformation rules

The following constraints are retained here because a plausible
“simplification” would change meaning:

- Titles are trimmed but otherwise not rewritten.
- Board status remains board-reported; never manufacture a common lifecycle
  state.
- `advertiser_name` is the board-displayed advertiser and may be an agency.
  `hiring_company_name` is used only when the source explicitly supplies a
  separate end employer; never infer one or erase it for equalling the
  advertiser.
- For an overseas MCF observation, `address.overseasCountry` is the only field
  that may be read from `raw_job_listings/{content_hash}.json.gz` when it is
  absent from the Bronze projection. Resolve it through
  `pycountry == 26.2.16` using its case-insensitive exact lookup; never use
  fuzzy matching, aliases not stated in this contract, or other raw fields.
  Unknown values become NULL.
- The closed JobStreet SG stub shape—payload present, `job` present and
  `gfjInfo` absent—maps to country `SG`. This must not become a general
  missing-country-to-market fallback.
- Country codes are either two uppercase ASCII letters accepted by the
  field-specific rule or NULL. Future LinkedIn markets require an explicit
  mapping; do not uppercase arbitrary market codes.
- `apply_url` is populated only from a distinct source-supplied application
  destination. Never manufacture it from `job_url` or use `job_url` as its
  Silver fallback.
- SEEK instants and Indeed epochs use UTC calendar dates, not local time;
  LinkedIn `publishedAt` uses its first ten date characters as defined in the
  data dictionary. An epoch outside years 1–9999 becomes NULL rather than
  failing the row. An Indeed expiry before its publication instant becomes
  NULL; internally consistent unusual or far-future dates are not “repaired.”
- Preserve source order and duplicates in screening questions and source
  attribute arrays. LinkedIn `sector` remains one category even when it
  contains commas, and LinkedIn `workType` is a job function, not an
  employment type.
- Do not infer salary from arbitrary JD prose, infer SEEK currency from market,
  convert currency, annualize, estimate working time, infer bonus/OTE splits,
  or repair suspicious source values.

### SEEK salary label grammar

SEEK numeric bounds may be parsed only from its dedicated salary label, using
this finite grammar. `salary_raw` remains the trimmed source wording, including
placeholder strings.

Currency and period are independent outputs, but numeric bounds are emitted
only when both resolve.

| Part | Locked rule |
| :--- | :--- |
| Currency precedence | A supported ISO `currencyLabel` wins; otherwise use an explicit label token. A bare `$` resolves nothing. |
| Currency tokens | `฿`/`บาท`→THB, `S$`→SGD, `HK$`→HKD, `A$`→AUD, `NZ$`→NZD, `US$`→USD, `RM`→MYR, `£`→GBP, `€`→EUR, or explicit SGD/HKD/THB/AUD/NZD/USD/GBP/EUR/MYR. |
| Period tokens | The `p.a.`, `p.m.` and `p.h.` variants; `per annum`, `per year`, `annually`, `yearly`, `per month`, `monthly`, `ต่อเดือน`, `per hour`, `hourly`, `per week`, `weekly`, `per day` or `daily`. |
| Supported amounts | Exactly two amounts joined by `-`, `–`, `—`, `~` or `to`; or one amount introduced by `up to` or `from`. A bare single amount, two unjoined amounts, three or more amounts, or an inverted range yields no bounds. |
| One-sided result | `up to X` gives only max; `from X` gives only min. |
| `k` scaling | A `k` suffix scales its own endpoint by 1,000. It also scales the other endpoint only when that endpoint is bare and below 1,000; a full amount keeps its own scale. Decimal `k` is allowed. |
| Currency across a range | An explicit currency marker on either endpoint applies to the range. |
| Riders | Text after a whitespace-delimited ` + ` rider does not contribute numbers and remains in `salary_raw`. |

If the grammar cannot safely resolve an output, that output remains NULL. It
must not fall back to general salary parsing.

## 3. Matching inputs

### Matching document

For normalized `title` and `description_text`:

```text
matching_description = latin_script_only(description_text)
parts = non-NULL values among (title, matching_description), in that order
doc   = lower(collapse_ws(join(parts, " ")))
```

`collapse_ws` replaces each run matched by Python regular-expression `\s+`
with one ASCII space and trims. `lower` is Unicode default lowercasing, not
casefolding. Each input is NULL or a nonempty normalized string.

One usable part is a valid document. If both are NULL, or the defensive final
collapse is empty, no matching document exists. Never compute `sha256("")` or
an empty-set sentinel signature.

### Non-Latin-script description content

Non-Latin-script description content is excluded from matching. Stored
`description_text` is never filtered; only this matching view of it is.

This is a script rule, not language identification: Latin-script text stays
eligible whatever language it is written in. Do not describe the matching
description as English-only.

`latin_script_only` operates on the LF-separated logical lines that
`description_text` already carries:

1. A character is a letter when its Unicode general category begins with `L`,
   and a Latin letter when its Unicode name begins with `LATIN `. Digits,
   punctuation, symbols and emoji are script-neutral and are never removed.
2. A pure-ASCII description is returned unchanged. ASCII holds no non-Latin
   letter, so nothing is removed and nothing needs cleaning up afterwards.
3. Otherwise, within each line, drop every whitespace-separated token holding
   at least one non-Latin letter and rejoin the survivors with one ASCII space.
4. Drop a line that retains no Latin letter.
5. Join the surviving lines with one LF. If none survive, there is no matching
   description.

A mixed line keeps its Latin-script tokens, so
`Skills: Python / SQL / การวิเคราะห์ข้อมูล` contributes `Skills: Python / SQL /`.
Residual punctuation is accepted, not reconstructed.

When `description_text` is non-empty but contributes no matching description,
the observation has **no matching document at all**: `fingerprint` and
`minhash_signature` are both NULL. Matching must not fall back to the title
alone, because unrelated adverts sharing one generic non-Latin title would then
share a fingerprint and exact equality groups them with no evidence. An
observation whose `description_text` was genuinely absent keeps its title-only
behaviour.

### Fingerprint, shingles and MinHash

For every usable matching document:

1. `fingerprint = SHA-256(UTF-8(doc))` as 64 lowercase hexadecimal characters.
2. Split `doc` on its single ASCII spaces and create the set of consecutive
   five-word shingles. A document of one through five words is one whole-doc
   shingle.
3. Convert each distinct shingle to an unsigned big-endian integer from
   `BLAKE2b(UTF-8(shingle), digest_size=8)`.
4. Build a 128-permutation MinHash with `datasketch == 2.0.0` and its default
   seed. Store the 128 unsigned 32-bit values.

`fingerprint` and `minhash_signature` are an all-or-nothing matching pair.
They are both NULL at the payload gate, after description quarantine, or when
no usable matching document exists.

MinHash nominates candidates only. Exact fingerprint equality and exact
shingle-set Jaccard determine matching evidence; a MinHash estimate never
accepts a pair.

### One recipe and change policy

There is one matching recipe and no recipe-version column or table/run/row
recipe version in Silver. The mapping property `bto.standardized_version` is a
data-generation binding, not a recipe version.

A matching-visible change includes any change that can alter the collapsed
matching document, fingerprint, shingle/signature values, candidate set,
evidence verdict or component guard. It requires explicit review and
revalidation, updated regression vectors, and a wholesale rebuild of both
Silver products. It must never be introduced as a silent implementation
change or an in-data recipe branch.

A `description_text` byte change proven invariant after matching collapse
follows the HTML specification’s stored-text-only policy. A change producing
no output-byte difference does not change the recipe.

## 4. Candidate and match contract

The product rule is “same enough opportunity”, not requisition-level identity.
Exact and near-identical reposts group; clearly different opportunities stay
separate. Closely related roles in one hiring family may collapse under the
rules below, but vague similarity alone is never enough.

### Representative values

For a fingerprint’s title, matching document and signature, use one
observation: the latest carrying that fingerprint by
`scrape_runs.started_at`, then bytewise `run_id` as the exact-timestamp
tie-break. For a source-listing × fingerprint membership, select its
advertiser by the same order. Never combine fields from arbitrary
observations.

### Exact wording

Every distinct usable fingerprint is one graph node. Listings sharing the same
fingerprint share that node by construction; no candidate search or score is
needed.

### Non-exact candidate union

A pair of distinct fingerprints is examined when any route nominates it:

1. **LSH:** 42 bands × 3 rows over MinHash positions 0–125; positions 126–127
   are unused. A shared band nominates the pair.
2. **Advertiser + title:** same market, same non-NULL normalized advertiser and
   same non-NULL normalized title. Both normalizations use casefolding,
   whitespace collapse to one ASCII space, and trimming.
3. **Listing history:** an observed transition between consecutive usable
   fingerprints of one `(board, market, board_job_id)`, ordered by
   `scrape_runs.started_at` and then bytewise `run_id` only for an exact-time
   tie. NULL-fingerprint observations are removed before adjacency is
   computed, so `A → NULL → B` nominates `A ↔ B`. Repeats do not create a
   transition: `A → B → A → C` nominates `A ↔ B` and `A ↔ C`, not an
   unobserved `B ↔ C`.

Candidate status alone never connects a pair. Source-listing identity alone
never creates an edge.

### Exact evidence lanes

For every nominated pair, calculate exact Jaccard over the two five-word
shingle sets:

```text
J(A, B) = |A ∩ B| / |A ∪ B|
```

The pair connects when any applicable lane accepts it:

| Lane | Acceptance rule |
| :--- | :--- |
| High similarity | `J >= 0.70`. Title compatibility is not required. |
| Medium similarity | `J >= 0.50` and both titles are compatible. |
| Same advertiser + title | The advertiser+title candidate route nominated the pair and `J >= 0.35`. |
| Listing history | The history route nominated the pair, advertiser is equal after normalization or missing on either side, and titles are compatible. No Jaccard floor. |

The medium lane naturally covers `0.50 <= J < 0.70` because the high lane is
tested first. No match score, lane, confidence or provenance is persisted.

### Title semantics

Title tokens are maximal ASCII `[0-9a-z]+` runs after casefolding, with the
bare tokens `senior`, `snr`, `sr`, `junior` and `jr` removed. Tokenization
retains only ASCII letters and digits present after casefolding; every other
character separates runs.

Two titles are compatible only when both filtered token sets are nonempty and
either one set contains the other or they share at least two tokens.

Two titles are disjoint only when both filtered sets are nonempty and share no
token. Missing or token-empty titles are not disjoint evidence.

### Rotating-slot protection

A board may reuse one source listing id for unrelated jobs. Therefore, for any
two different wordings that occur anywhere in the same source listing’s
history, disjoint titles veto the high- and medium-similarity lanes. The
advertiser+title and listing-history lanes retain their own requirements. The
veto is pair-level: it does not prohibit transitive membership through other
accepted edges.

The veto does not apply when either exception proves title continuity:

- **Mechanical abbreviation:** an abbreviation derivable from one title
  appears as a token in the other. Allowed derivations are the initials of
  one hyphenated word, or an initialism of three through five consecutive
  words. A two-letter initialism is allowed only for a hyphenated word, never
  for a word sequence. No dictionary or semantic expansion is used.
- **Identical body:** both normalized description bodies, with titles
  excluded, exist and are exactly equal after the matching-document
  whitespace/lowercase normalization. Fuzzy body similarity and two missing
  bodies do not qualify.

The rule is intentionally narrow: shared template prose can otherwise make
different jobs under one rotating id appear highly similar.

### Career-stage protection

A title is clearly internship-stage when its filtered tokens contain `intern`
or `internship`, and clearly graduate-stage when they contain `graduate` or
`grad`. A title containing markers from both sets is unclassified.

An internship-stage wording and a graduate-stage wording must never share a
canonical group. Reject the pair before all evidence lanes, including the
identical-body exception, and reject any component union that would introduce
both stages. Ordinary seniority wording is not a career stage.

### Components

Accepted edges form deterministic connected components, which are the
canonical groups. Transitivity is intentional: every pair inside a component
need not independently meet an evidence lane. Do not replace this with
highest-score-wins or require a clique.

### What a component does and does not assert

A canonical component is a matching construct. It is not a guarantee that
every member represents one literal vacancy.

Employer and recruiter boilerplate can place several genuinely different
vacancies in one component because a long shared template raises text
similarity between unrelated postings. This is most pronounced for large or
template-heavy employers, where a single component can span many distinct
roles. It is an accepted limitation of the current recipe, not a defect to be
worked around in this package.

The limitation is acceptable because the products keep every constituent
visible:

- each source listing in a component remains individually enumerable through
  the mapping's logical key;
- each listing's standardized observations and historical wordings remain
  available in full;
- a consumer can therefore inspect, rank and select members individually
  rather than treating a component as one indivisible opportunity.

A consumer that collapses a component to a single representative, or that
acts on a component as a whole, accepts that it may be acting on several
different vacancies at once.

## 5. Canonical mapping contract

### Grain and historical membership

One `job_canonical_mapping` row is one source listing × one distinct usable
historical fingerprint:

```text
(board, market, board_job_id, fingerprint)
```

`run_id` is not part of the grain. A listing with history `F1 → F2 → F1` has
two mapping rows. Historical wordings remain eligible nodes even after
delisting or after no listing currently carries them; they may bridge future
components.

Logical-key uniqueness is a build invariant rather than a Delta primary key
because `fingerprint` is nullable. NULLs must be treated as equal when checking
duplicate sentinel keys.

The six-column schema and datatypes belong to the data dictionary. The mapping
does not persist score, lane, confidence, match provenance, diagnostics,
current assignment or assignment history.

### Never-usable sentinel

Every source listing in standardized history must appear in the mapping.

A listing that has never had a usable fingerprint receives exactly one row
with:

```text
fingerprint = NULL
canonical_job_id = "{board}:{market}:{board_job_id}"
```

A listing that has ever had a usable fingerprint receives no sentinel,
including after later identity-only, quarantined or no-document observations.
When its first usable fingerprint appears, the next full rebuild removes the
sentinel and emits ordinary usable membership.

### Deterministic canonical ID

For every usable connected component:

1. For each membership, calculate `first_seen_membership_at` as the minimum
   `bto.bronze.scrape_runs.started_at` over observations of that exact
   `(board, market, board_job_id, fingerprint)`.
2. Choose the minimum membership in ascending order of
   `first_seen_membership_at`, `board`, `market`, `board_job_id`, then
   `fingerprint`. `run_id` is not a term.
3. Serialize the winner as
   `{board}:{market}:{board_job_id}:{fingerprint}`, including the full
   64-character fingerprint.

The serialization assumes that its components contain no colon and introduces
no escaping, truncation, UUID or surrogate. Embedding the full winning
fingerprint makes the identifier component-injective because a fingerprint
belongs to exactly one component.

The winning fingerprint may be historical. The identifier is a component key,
not a display representative, current wording or “best” listing.

### Current assignment and identifier changes

A source listing’s current canonical assignment is derived, never stored.
Select its latest usable fingerprint by `scrape_runs.started_at DESC` and then
`run_id DESC` as an exact-time tie-break, and read that mapping row’s
`canonical_job_id`. A later unusable observation does not erase the latest
usable assignment. A never-usable listing resolves through its sentinel.

A full rebuild may merge components, replace a sentinel with usable membership,
or introduce an earlier winning membership. Existing `canonical_job_id` values
may therefore change. They are deterministic for one complete input history,
not durable application-state identifiers.

### The durable identity this package provides

```text
(board, market, board_job_id, fingerprint)
```

is the mapping's logical membership key and the durable wording identity this
package offers across canonical rebuilds. A rebuild may reassign which
component a membership belongs to; it does not rename the membership itself.

`canonical_job_id` is not durable operational state. Rebuilds may legitimately
merge, split or reassign components, so a consumer must not persist long-lived
suppression, application or review state against a historical
`canonical_job_id` alone.

`content_hash` is not a cross-board or source-agnostic wording identity. Its
meaning depends on the capture path: for MCF and SEEK it names the detail
payload version, while for Indeed and LinkedIn it is capture identity, so a
changed hash there does not necessarily mean the job description changed.
`fingerprint` is the matching wording identity; `content_hash` is not a
substitute for it.

This section states the identities and guarantees this package provides. It
deliberately does not prescribe which key a consumer should use for persistent
review or suppression state — that choice belongs to the consuming stage,
which must also account for boards reusing one `board_job_id` for unrelated
roles (the reason rotating-slot protection exists).

## 6. Build and publication invariants

### Standardization

Construct complete standardized rows before writing; do not insert partial
rows and repeatedly update fields.

Normal maintenance is incremental:

1. Select Bronze observations whose exact observation key is absent from the
   published standardized table. Never use a date, run-id or content-hash
   watermark.
2. Build and validate those rows in run-scoped scratch.
3. Prove their keys are disjoint from the published table.
4. Merge with insert-on-not-matched behavior only. Existing standardized rows
   never change. A zero-row batch issues no merge.
5. Reconcile the complete Bronze and standardized observation-key sets in both
   directions.

A wholesale standardization path remains available for rebuild and
revalidation. It uses the same transformation and must produce the same row
for the same Bronze observation.

### Canonicalization and publication

Canonicalization always produces the complete canonical mapping for the
standardized history at one pinned Delta version N. Its semantic result is
defined by the full-history algorithm, which remains the authoritative
rebuild, oracle and fallback.

Construction may be incremental, and only under all of these conditions:

- it starts from a mapping that was itself validated and published, bound to
  an earlier standardized version P, and still satisfies its invariants
  against the standardized data at P;
- every standardized commit in `(P, N]` is provably append-only — a commit
  that updated or deleted any standardized row disqualifies the shortcut;
- the result is identical to what the full-history algorithm would produce
  from the complete history at N: same rows, same groups, same identifiers.

An incremental construction that cannot prove any part of this must fall back
to the full-history algorithm rather than approximate it.

Evidence the previous mapping relied on is re-judged, never assumed: every
change that can move a pair's verdict seeds a region, the region is closed
over the current pair-accepted edges and rebuilt from individual wordings
with the full-history rules, so it may both merge and split groups. Closure
follows a pair's verdict rather than the previous build's retained edges,
because the component-level career-stage guard can drop an accepted edge and
that edge may become retainable once the region splits. Only a group proven
outside the closed region is carried forward unaltered.

Publication never patches the mapping in place: the incremental path
assembles a complete candidate — rebuilt groups, untouched groups carried
forward unaltered, sentinels regenerated — and that candidate is validated
and published exactly like a full rebuild's.

### The representative must be determined by the data

A wording's representative observation is the latest carrying it, ordered by
`scrape_runs.started_at` then `run_id`. Several observations may share that
maximum. They are interchangeable only when they agree on everything
canonicalization reads — the matching document, the normalized title, the
title and abbreviation tokens, the career stage, the description-only
normalized body and positions 0–125 of the stored MinHash consumed by the
frozen 42 × 3 candidate banding. The stored signature must still contain
exactly 128 values; positions 126–127 are validated but do not distinguish
representatives because canonicalization does not consume them. A wording
whose maximum-tied observations disagree has no defined representative, and
that refuses the run rather than falling back: the full-history algorithm
reads the same undetermined value, and no tie-break may be invented.

The requirement stops at the maximum. A wording whose latest observation is
unique is determinate however much its OLDER observations differ, including
in their stored MinHash — those rows are never read — so they are not a
reason to refuse anything.

The two generations an incremental build compares mean different things when
they are undetermined. At N the result being built is undefined, so the run
stops. At P only the baseline is unusable, and N may be perfectly determinate
because a later observation superseded the tie: that is a fallback to the
full-history build at N, never a refusal of it.

Build the complete candidate mapping in run-scoped scratch and validate it
before publication. Only after substantive validation succeeds, capture one
`canonicalized_at` value, stamp every candidate row with it, and verify that
the complete nonempty candidate has exactly one non-NULL value.

Publication must be one atomic operation that both replaces all mapping rows
and sets the table property `bto.standardized_version = N`. Readers must see
either the prior complete mapping and binding or the new complete mapping and
binding—never mixed generations. Do not drop/truncate the target before
writing or publish through partial appends.

A failure before that atomic commit leaves the previous mapping and binding
untouched. A standardized data change that lands after the commit makes the
new mapping stale; the mapping remains truthfully bound to N and the run fails
closed.

Scratch names are run-specific. One run must never reuse or take over another
run’s scratch based on age.

### Skip rule

Canonicalization may be skipped only when the incremental standardization
merge inserted zero rows and the existing mapping passes the freshness guard.
A stale or unprovable mapping is rebuilt even when no observation was added.

## 7. Validation guarantees

### Runtime publication blockers

Before standardized publication, the run rejects:

- incremental scratch keys overlapping the target;
- staged row counts differing from rows read;
- NULL observation identity;
- duplicate observation keys;
- mixed matching pairs where only fingerprint or MinHash is populated;
- stored empty arrays;
- any mismatch between the relevant Bronze and standardized observation-key
  sets;
- systematic all-payload quarantine for a selected board × market.

Before mapping publication, the run rejects violations of the candidate and
joint-table checks, including:

- duplicate or NULL mapping identity/id values;
- usable memberships missing from either standardized history or the mapping;
- missing, extra, coexisting or malformed sentinels;
- a mapping fingerprint absent from standardized history;
- one fingerprint assigned to more than one group;
- a usable ID that does not name a membership in its own group;
- missing listing coverage;
- a final group containing both internship and graduate stages;
- an empty mapping, NULL/multiple `canonicalized_at` values, or a mapping
  generation timestamp older than its standardized input;
- an unprovable or changed standardized generation before publication.

After publication—or on the skip path—the freshness proof is mandatory. Any
violation fails the run.

### Deterministic construction invariants

The single construction path, rather than a second runtime recomputation,
guarantees the exact source transformations, matching-document derivation,
fingerprint and MinHash recipe, candidate routes, evidence lanes, guarded
connected components, earliest-member winner ordering and ID serialization.
These are contract requirements even where publication validation checks only
the resulting shape and membership.

### Regression and golden guarantees

Offline regression and golden coverage must pin:

- source-family transformations and exceptional NULL behavior;
- HTML normalization and quarantine behavior;
- matching-document, SHA-256, shingle-ID and 128-value MinHash vectors under
  the pinned dependency;
- the 42 × 3 candidate route and both non-LSH candidate routes;
- exact Jaccard lanes, title/advertiser rules, rotating-slot exceptions and
  career-stage protection;
- sentinel, component, winner, current-assignment and determinism semantics;
- parity between the ordinary and Spark canonicalization paths;
- equality between an incremental construction and the full-history
  algorithm over the same complete history, on successive batches, compared
  as complete mapping rows rather than counts or component shapes; and the
  refusal — not an approximation — of each unsupported situation;
- incremental/rebuild publication, version binding, race checks and
  fail-closed freshness.

The production run is not required to execute the test suite or rebuild twice
to prove determinism.

### Monitoring

Standardization reports per-board/market counts for observations, payload and
identity-only rows, quarantine reasons, payload-bearing rows without
description text, and title-only matching documents. These counts do not block
publication except for the systematic quarantine rule.

Canonicalization emits candidate-route and pair-outcome counts together with
component count, near-component count, largest component node/row sizes,
accepted-edge count, weakest accepted edge, maximum node degree and median
component size. These are diagnostics, not expected corpus constants or
publication thresholds.

## 8. Generation binding and freshness

At canonicalization start, capture the current Delta version N of
`standardized_job_listings`. Every standardized read used by that
canonicalization—memberships, transitions, representative documents,
never-usable listings and joint validation—must be pinned to version N.

Immediately before publication, inspect every retained standardized commit
strictly above N:

| Classification | Rule |
| :--- | :--- |
| Proven maintenance-only | `OPTIMIZE`, `COMPUTE STATS`, `SET TBLPROPERTIES`, `VACUUM START` and `VACUUM END`. |
| Zero-row merge | `MERGE` is non-data-changing only when inserted, updated and deleted target-row metrics are all present, parseable, nonnegative and exactly zero. |
| Data-changing or unprovable | Every other operation, and any `MERGE` with a missing, malformed, negative or nonzero mutation metric. |

A numerically newer version is not automatically stale when every intervening
commit is provably maintenance-only. Any data-changing or unprovable commit
above N blocks publication.

Recognizing a standardized commit as append-only is a build-time
precondition for incremental construction, never a freshness rule: an
insert-only merge above the bound version still makes the mapping stale and
still requires canonicalization to run.

The atomic mapping publication records N as the
`bto.standardized_version` table property on `job_canonical_mapping`. It is
metadata, never a column. Immediately after publication, inspect commits above
N again. A data-changing commit in the race window fails the run and leaves
the mapping bound to N for the freshness guard to reject.

The mapping is current only when every fact is provable:

- both Silver tables exist;
- the property exists, parses as an integer and names a valid standardized
  version;
- the full sequence of versions above N remains available;
- no data-changing commit exists above N;
- the mapping has exactly one non-NULL `canonicalized_at` value;
- mapping identity, membership, sentinel, ID-owner and coverage invariants hold
  against standardized data.

Timestamp ordering is a build-time sanity check, not the freshness proof.
Anything missing, malformed, unknown or unverifiable fails closed with the
operator instruction to rerun the build. The build must prove freshness before
reporting success.

## 9. Consumer interface

A consumer reads `standardized_job_listings`, `job_canonical_mapping` and
observation chronology from `bto.bronze.scrape_runs`, and must apply the same
freshness guard before trusting the mapping.

### Deriving a listing’s current canonical assignment

For one source listing `(board, market, board_job_id)`:

1. Take its standardized observations whose `fingerprint` is non-NULL. An
   observation with a NULL fingerprint is not usable for assignment.
2. Order them by `scrape_runs.started_at DESC`, then bytewise `run_id DESC`
   as the deterministic tie-break for an identical instant. `run_id` order is
   never chronology; it only breaks an exact tie.
3. Take the first row's fingerprint — the listing's latest usable wording.
4. Join `(board, market, board_job_id, fingerprint)` to
   `job_canonical_mapping`.
5. That row's `canonical_job_id` is the listing's current canonical
   assignment.
6. Grouping the current assignments of many listings by `canonical_job_id`
   enumerates the source listings currently assigned to that component. This
   is the supported way to enumerate a component's members.
7. A later identity-only or otherwise unusable observation does not erase the
   latest usable assignment; step 1 simply skips it.
8. A listing that has never had a usable fingerprint has no usable membership
   and resolves through its sentinel row instead (§5).

### Current assignment is not vacancy liveness

A current canonical assignment means only that this is the listing's most
recent usable wording and the component it currently belongs to. It does not
mean the vacancy is open, live, or still accepting applications, and these
products do not claim otherwise. They preserve observations, assignment and
history; inferring whether an opportunity is still live is downstream work.

The evidence available for that inference is uneven by source, and the data
dictionary is authoritative for each field:

- `board_status` is board-reported and never normalized into a common
  lifecycle. MCF and SEEK supply it; it is NULL for Indeed and LinkedIn,
  which state nothing reliable.
- `expiry_date` is board-reported where present and is not necessarily an
  application deadline or the end of the underlying vacancy. LinkedIn supplies
  none.
- Sweep evidence from `bto.bronze.scrape_runs` may be usable downstream, but
  `full_sweep` means the run covered its configured search scope successfully.
  It is not proof that the external board was exhaustively crawled, so the
  absence of a listing from a sweep is weaker evidence than its presence.

This contract deliberately does not define a liveness or delisting algorithm.

Never use a historical `canonical_job_id` as durable suppression state:
rebuilds can change it. The durable identity this package provides is the
membership key in §5.

Suppression, filtering, triage and ranking are outside this contract and do not
require a third persistent table here. This package guarantees only that the
information needed to enumerate a component's members, and to inspect each
member's own observations and wordings, is present in the two products plus
`scrape_runs`.
