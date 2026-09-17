# Data Dictionary: PostgreSQL

PostgreSQL is the operational database for Back to Office. This file documents
only decided contracts; it does not imply that a table has been created. The
[deployment README](../../../../deploy/README.md) owns live server state.

Add schemas and tables only as the project needs them. Do not create placeholder
structures for future features.

## Contents

- **[Naming conventions](#naming-conventions)**
- **[Schemas](#schemas)**
  - [`costs`](#schema-costs)
    - [`apify_indeed`](#table-apify_indeed)
    - [`apify_linkedin`](#table-apify_linkedin)
    - [`aws_monthly`](#table-aws_monthly)

---

## Naming conventions

Keep names plain and understandable.

| Object | Convention | Example |
| :--- | :--- | :--- |
| Schemas | lowercase `snake_case`, named for what the data represents | `costs` |
| Tables | lowercase `snake_case`; for provider-specific cost tables use `{provider}_{use_case}` | `apify_indeed` |
| Columns | lowercase `snake_case` | `apify_run_id` |
| Provider IDs | `{provider}_..._id` | `apify_run_id` |
| Back to Office run IDs | `bto_run_id` | `indeed_sg_20260828_033000_a7f3` |
| Timestamps | end in `_at` | `started_at` |
| Money | include the currency in the name | `cost_usd` |

Do not repeat the schema name in the table name: use `costs.apify_indeed`, not
`costs.apify_indeed_costs`.

---

## Schemas

### Schema: `costs`

**Purpose:** Records the cost of external services used by Back to Office.

Providers and use cases may have different table shapes; they do not need one
universal cost table. A reporting view can combine them later.

Currently defined:

- `costs.apify_indeed`
- `costs.apify_linkedin`
- `costs.aws_monthly`

---

#### Table: `apify_indeed`

**Full name:** `costs.apify_indeed`

**Actor:** [`curious_coder/indeed-scraper`](https://apify.com/curious_coder/indeed-scraper)  
**Actor ID:** `qA8rz8tR61HdkfTBL`

**Grain:** One row per Apify Actor run.

Indeed starts one Apify Actor run per search term, so several rows may belong to the same `bto_run_id`.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `apify_run_id` | `TEXT` | No | Apify's unique ID for the Actor run. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office board × market run that started this Actor run. |
| `market` | `TEXT` | No | Market being searched, such as `sg`, `hk` or `th`. |
| `search_term` | `TEXT` | No | Search term used for this Actor run. |
| `started_at` | `TIMESTAMPTZ` | No | Time the Apify Actor run started. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the Apify Actor run finished. NULL while unfinished. |
| `status` | `TEXT` | No | Apify run status. |
| `result_count` | `INTEGER` | Yes | Number of dataset rows returned by the Actor run. |
| `cost_usd` | `NUMERIC(12,6)` | Yes | Run cost in USD reported by Apify as `usageTotalUsd`. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this cost record was written or last refreshed. |

---

#### Table: `apify_linkedin`

**Full name:** `costs.apify_linkedin`

**Actor:** [`cheap_scraper/linkedin-job-scraper`](https://apify.com/cheap_scraper/linkedin-job-scraper)  
**Actor ID:** `2rJKkhh7vjpX7pvjg`

**Grain:** One row per Apify Actor run.

LinkedIn starts one Actor run for all configured search terms in that board × market run, so there is no `search_term` column here.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `apify_run_id` | `TEXT` | No | Apify's unique ID for the Actor run. Primary key. |
| `bto_run_id` | `TEXT` | No | Back to Office board × market run that started this Actor run. |
| `market` | `TEXT` | No | Market being searched, such as `sg`, `hk` or `th`. |
| `started_at` | `TIMESTAMPTZ` | No | Time the Apify Actor run started. |
| `finished_at` | `TIMESTAMPTZ` | Yes | Time the Apify Actor run finished. NULL while unfinished. |
| `status` | `TEXT` | No | Apify run status. |
| `result_count` | `INTEGER` | Yes | Number of dataset rows returned by the Actor run. |
| `cost_usd` | `NUMERIC(12,6)` | Yes | Run cost in USD reported by Apify as `usageTotalUsd`. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this cost record was written or last refreshed. |

##### Apify cost source

In both Apify tables, `cost_usd` is the completed run's `usageTotalUsd`.
Run-cost fields can take a few seconds to settle, so an in-progress value is not
final. These tables record run-level Actor cost only; fixed subscriptions and
other account-level charges are excluded.

---

#### Table: `aws_monthly`

**Full name:** `costs.aws_monthly`

**Source:** AWS Cost Explorer, `GetCostAndUsage`

**Grain:** One row per billing month × AWS service × AWS billing record type.

Unlike the Apify tables, this one has no `bto_run_id`. AWS bills by month and by
service, not by collection run, and no run can be attributed a share of it.

| Column | PostgreSQL Type | Nullable | Description |
| :--- | :--- | :---: | :--- |
| `billing_month` | `DATE` | No | First day of the AWS billing month, such as `2026-08-01`. Part of the primary key. |
| `service` | `TEXT` | No | AWS service name exactly as Cost Explorer returns it for the `SERVICE` dimension, such as `Amazon Simple Storage Service`. Part of the primary key. |
| `record_type` | `TEXT` | No | AWS billing record type exactly as Cost Explorer returns it for the `RECORD_TYPE` dimension, such as `Usage`, `Credit`, `Refund` or `Tax`. Part of the primary key. |
| `amount_usd` | `NUMERIC(12,6)` | No | `UnblendedCost` in USD as reported by AWS. Negative for credits and refunds. |
| `recorded_at` | `TIMESTAMPTZ` | No | Time this row was written or last refreshed. |

**Primary key:** `(billing_month, service, record_type)`

The money column is `amount_usd`, not `cost_usd` as in the Apify tables. A row
here can be a credit or a refund, which is not a cost. The name still carries the
currency, as the naming conventions require.

##### Cost source

Cost Explorer `GetCostAndUsage` with `Granularity=MONTHLY`,
`Metrics=["UnblendedCost"]`, grouped by the `SERVICE` and `RECORD_TYPE`
dimensions. One request returns every row for one month.

The metric is `UnblendedCost`, not `NetUnblendedCost`. `NetUnblendedCost`
already has credits deducted; storing it alongside the `Credit` rows would
subtract the same credits twice. `UnblendedCost` keeps gross usage and credits
as separate rows, which is what lets this table answer both questions asked of
it.

No price is ever written by hand. If the Lightsail plan changes price, the next
refresh records whatever AWS reports.

##### Sign convention

`amount_usd` keeps the sign AWS reports. `Usage` and `Tax` rows are positive.
`Credit` and `Refund` rows are negative.

Every query against this table depends on that:

- **What is costing money** — filter `record_type = 'Usage'`.
- **What was actually payable** — sum `amount_usd` across all record types.

Never `ABS()` the column, and never sum only the `Usage` rows and call it the
bill.

##### Deriving what was payable

```sql
SELECT billing_month, SUM(amount_usd) AS payable_usd
FROM costs.aws_monthly
GROUP BY billing_month
ORDER BY billing_month;
```

Nothing stores this figure. It is always derived, so it cannot drift from the
components it is derived from.

##### Collection behaviour

- Cost Explorer is the source of truth. Nothing in this table is entered by hand.
- Writes are UPSERT on `(billing_month, service, record_type)`. AWS billing data
  is delayed and is revised after a month closes, so a refresh must overwrite
  rather than insert a second row.
- Each refresh covers the current month and the previous month, so late AWS
  adjustments are picked up. A month closed longer than that is treated as
  settled.
- Cost Explorer charges per request. Those charges arrive on a later AWS bill as
  ordinary usage and are recorded by this table like any other service. They are
  never calculated or inserted separately.
- Cost Explorer can return a group with an empty service key. The collector must
  map that to a non-empty placeholder before writing, because all three primary
  key columns are `NOT NULL`.
- `recorded_at` records when a row was last refreshed, not whether its value
  changed. This table keeps no revision history.

##### Account scope

This table records the whole AWS account, not only Back to Office.
`SERVICE` × `RECORD_TYPE` has no project-allocation dimension, so read a figure
from this table as *the account's* cost, not the project's.

##### Not in scope

Per-bucket S3 attribution, usage-type breakdowns, reporting views, dashboards,
and any further AWS cost table.
