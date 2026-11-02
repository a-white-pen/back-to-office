# find_job_contacts

Contact finding identifies people connected to a job listing and looks up
their contact details. Results appear in **People** after a job is saved to
To Apply.

Saving a starred job, or starring a saved job, starts a people search.
Admins can also request a search for any saved job and look up individual
people. Viewers can read the results.

## How contact finding works

```text
saved and starred job, or an admin's search request
        ↓
1  identify the employer and its recruitment-firm/public-sector classification
        ↓
2  read contacts printed on the listing and its LinkedIn copy
        ├── recruitment firm → save those contacts and stop
        ↓
find more people through three parallel searches
        ├── 3  LinkedIn people searches through Apify; everyone in the market
        │      when no title matches
        ├── 4  Brave web searches, plus SGDI for public-sector employers
        └── 5  staff LinkedIn posts about the job, with reactions and comments
        ↓
6  merge duplicate people and group them; read key people's profiles
        named on the listing · recruiters · hiring manager and team · others
        ↓
7  find a trusted company email domain
        ↓
8  look up key people's contact details
        everyone named on the listing · up to 3 recruiters · up to 4 team members,
        or the company's leaders when no team member is found
        ↓
9  save people, posts and costs in PostgreSQL → People panel
```

Employer identification uses the listing's LinkedIn company details or its
same-market LinkedIn copy. Without a copy, it can use the latest LinkedIn
listing under the employer's name. A web search can supply a missing website.

Email lookup needs a trusted company domain: the employer website on LinkedIn,
an employer-owned page found by job-listing research, or a matching Hunter
result. Hunter must know addresses at the domain before it is used;
individual mailbox checks happen during lookup. Printed addresses are kept
even when those checks are unavailable.

Phone numbers come from the listing, SGDI or web results. Web results must
print the number near the person's name; a listing with just one named contact
assigns its printed numbers to that contact. Online accounts are confirmed
only when evidence connects them to the employer; same-name accounts can
appear as **unconfirmed**. A hidden surname is completed only when the profile
link supplies it.

## Structure

```text
src/bto/find_job_contacts/
├── README.md
├── __init__.py
├── run.py                             # queue searches, run the workflow and serve results
├── config.py                          # provider keys and private limit access
│
├── identify_company.py                # employer identity, website and classifications
├── find_relevant_personnel/
│   ├── in_listing.py                  # contacts printed on the listing or its LinkedIn copy
│   ├── on_linkedin.py                 # company people searches and full profiles through Apify
│   ├── from_web.py                    # LinkedIn profiles found through Brave
│   ├── in_sgdi.py                     # officers in the Singapore Government Directory
│   ├── in_job_posts.py                # job-related LinkedIn posts, reactions and comments
│   └── classify_found_persons.py      # merge people, assign groups and select key people
│
├── find_emails.py                     # trusted company domain, addresses and mailbox checks
├── find_phones.py                     # phone numbers printed near a person's name
├── find_online_accounts.py            # GitHub, X, Instagram and personal sites
│
└── providers/
    ├── apify.py                       # Actor runs, spending guards and reported costs
    ├── hunter.py                      # domain, address and mailbox requests; credit costs
    └── github.py                      # public account searches and profile reads
```

## Searches and limits

Searches run one at a time, oldest request first. A PostgreSQL lock prevents
the web worker and command line from processing the same queue together.
Workers start when a search is requested or To Apply is visited. After a
restart, queued searches continue; an interrupted running search is marked
failed and can be retried by the admin.

Automatic requests keep an existing search result. **Search again** and
**Retry** request a new search. Earlier people and posts remain visible while
it runs, and a failed search keeps them. LinkedIn people-search failure stops
the search; web, SGDI and post-search failures can be skipped.

Failure logs include the step or provider, available search/run ids, the
exception type and stack locations. Exception text and provider responses
stay out of these logs.

Daily search limits, provider budgets, credit reserves, lookup counts and post
search windows live in ignored `limits.py`. Copy the fictional
[limits example](limits.example.py), configure every field, and keep the result
private. `config.py` preserves the constant interface and validates this literal
data before use; it never loads the example or falls back to another budget.
The Hunter credit limit
is checked between people, so a lookup already in progress can finish above
it. Email checks can be held back without stopping phone and account lookups.
**Look up** lets the admin request an individual lookup, including an email
check when automatic checks were held back.

## Running

The web application normally starts searches. The command line can also queue
and process them:

```bash
python -m bto.find_job_contacts.run --starred-to-apply
python -m bto.find_job_contacts.run --listing BOARD MARKET BOARD_JOB_ID
```

Both commands require jobs already saved to To Apply. The first selects
starred jobs without an earlier search; the second requests one saved job
by hand.

Configure PostgreSQL and provider keys in the environment or ignored `.env`:
`APIFY_TOKEN`, `HUNTER_API_KEY`, `BRAVE_FIND_EMPLOYER_API_KEY`,
`BRAVE_FIND_PEOPLE_API_KEY` and `BRAVE_LOOK_UP_PERSON_API_KEY`. GitHub uses its
public API without a token. Employer classification also uses the private
[filtering preferences](../filter_and_bucket_job_listings/README.md#private-preferences)
when available. Brave's shared search cache is described in the
[job-listing research README](../research_job_listing/README.md#configuration).

The [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md) owns
stored people, posts, search status and cost contracts. The Review and research
tables must be set up before searches run; the website does not create them.
