# research_job_listing

Job-listing research finds the employer's own job board and the page for one
vacancy. Results appear in **Links** after a job is saved to To Apply.

Saving a job requests its first search. Admins can also request a search or
retry from To Apply; viewers can read the results.

## How job-listing research works

```text
job saved to To Apply, or an admin's search request
        ↓
find the employer's job board, trying these sources in order
        1. stored apply links and employer website, including same-market copies
        2. Brave web searches
        3. known or guessed employer websites and common careers paths
        ↓
check that a candidate belongs to the employer and carries vacancies
        ↓ when a board is found
find this vacancy, trying these sources in order
        1. stored apply links on this listing and its same-market copies
        2. the hiring platform's API, using a requisition number when available
        3. board links, search pages and sitemaps
        4. a Brave search restricted to the board
        ↓
check against JD text, structured job details or the platform's record
        ↓
save the result in PostgreSQL → Links panel
```

Every candidate goes through the relevant check before the next source is
tried. Each part stops at the first accepted answer. Vacancy search runs only
after a board has been found; a verified board without this vacancy is a
normal result.

## Structure

```text
src/bto/research_job_listing/
├── README.md
├── __init__.py
├── run.py                             # search order, queue, results and command line
│
├── websearch_for_employer_board.py    # Brave queries for the employer's careers site
├── guess_employer_website.py          # known or guessed websites and careers paths
├── check_employer_board.py           # verify employer ownership and vacancy listings
│
├── search_the_board.py               # hiring-platform APIs, board searches and sitemaps
├── websearch_for_vacancy.py           # Brave queries restricted to the board
├── prove_same_job.py                 # verify that a candidate is this vacancy
│
├── brave_search.py                   # cached Brave requests and cost recording
├── fetch_page.py                     # read HTML/JSON and extract page text and job details
├── hiring_systems.py                 # hiring-platform adapters and reposting-site rules
├── private_adapters.py               # optional employer-specific hooks
├── private_adapters.example.py       # target-free hook interface
└── board_address.py                  # identify a hiring platform and its employer account
```

Employer-specific integrations stay in the ignored private module
`private/research_job_listing/adapters.py`, located through `BTO_PRIVATE_DIR`.
`private_adapters.example.py` shows its discovery, search and vacancy-reader
hooks. No example is loaded automatically; without the private module, the
generic hiring-platform checks remain available.

## Results and retries

| Result | Meaning |
| :--- | :--- |
| `resolved` | Both the employer's board and the vacancy page were found. |
| `partial` | The board was found, but no vacancy page was verified. |
| `unresolved` | No board was verified within the search limits. |
| `failed` | The attempt could not finish, for example after a restart or a Brave refusal with no links found. |

Searches run one at a time, oldest request first. A PostgreSQL lock prevents
the web worker and command line from processing the same queue together.
Workers start when a search is requested or To Apply is visited. Queued work
continues after a restart; interrupted running work is marked failed.

Saving the same job again keeps its existing result. **Search again** and
**Retry** request a new search. Previous links remain visible while it runs;
a failed search preserves them. The private `research_job_listing/limits.json`
sets the daily request allowance; `private_limits.example.json` shows its shape.
Elapsed-time guards live in `run.py`.
The time budget is checked between steps and candidate checks; a request
already in progress can finish after it.

## Configuration

PostgreSQL holds the saved jobs and search results. Configure its connection
and optional `BRAVE_FIND_DIRECT_LINK_API_KEY` in the environment or ignored
`.env`. Without a Brave key, the direct website and hiring-platform checks
still run.

`brave_search.py` caches matching searches for 30 days and is also used by
contact finding. `BTO_SEARCH_CACHE` selects the private cache directory;
otherwise it uses `~/.cache/bto/search`. Paid requests record their costs;
a failed cost write is logged without failing the search.

Failure logs include the step or provider, available search/run ids, the
exception type and stack locations. Exception text and provider responses
stay out of these logs.

The [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md) owns
stored links, search status and costs. The Review and research tables must
be set up before searches run; the website does not create them.

## Running

The web application normally starts searches. The command line can also queue
and process them:

```bash
python -m bto.research_job_listing.run --to-apply
python -m bto.research_job_listing.run --listing BOARD MARKET BOARD_JOB_ID
```

Both commands require jobs already saved to To Apply. The first selects saved
jobs without an earlier search; the second requests one saved job again.
