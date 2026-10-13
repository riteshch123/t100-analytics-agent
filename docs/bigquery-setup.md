# Setting up BigQuery and loading T-100

This is a one-time setup, then one command a month. Everything here stays inside BigQuery's free tier (10 GiB of storage and 1 TiB of query scanning a month), and the caps below make sure a mistake fails instead of costing money.

## 1. Google Cloud project (one time, about 15 minutes)

1. Go to <https://console.cloud.google.com>, create a project (for example `t100-agent`), and note its **project ID**.
2. Attach a billing account. This is what turns the sandbox (tables deleted after 60 days) into the normal free tier. Nothing is charged while usage stays inside the free tier.
3. Enable the **BigQuery API** for the project.
4. Set the safety nets:
   - **Budget alert:** Billing > Budgets & alerts > create a budget of ₹100 for this project, with email alerts at 50%, 90% and 100%.
   - **Daily query cap:** IAM & Admin > Quotas, search for "Query usage per day", and set it to 100 GiB. BigQuery then refuses queries past that for the rest of the day.
   - **Per-query cap:** already in the code. Every query the pipeline runs sets `maximum_bytes_billed` (2 GiB by default, `BQ_MAX_BYTES_BILLED` to change).

## 2. Your machine (one time)

```bash
# Python 3.11+, Google Chrome installed (the scraper drives it headless)
git clone https://github.com/riteshch123/t100-analytics-agent.git
cd t100-analytics-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[scrape,dev]"

# Log in to Google Cloud for local scripts (install the gcloud CLI first)
gcloud auth application-default login

export GCP_PROJECT=your-project-id
```

## 3. Try one month first

```bash
python -m t100_agent.ingest.run scrape --dataset both --from 2024-01 --to 2024-01
```

This saves `data/raw/t100_market/year=2024/month=01/data.parquet` and the segment equivalent, and prints the row count and file size. Multiply the size by 12 for a year: this tells us how much the full history needs.

## 4. Load history into BigQuery

```bash
python -m t100_agent.ingest.run sync --from 2015-01 --to 2025-06 --delete-local
```

For each month this downloads from BTS, loads into BigQuery, checks the row count matches the file, then deletes the local file, so your disk only ever holds one month. Months already in BigQuery are skipped, so you can stop and re-run at any time. Older years can be added later the same way.

## 5. Check the data

```bash
python -m t100_agent.ingest.checks
```

It reports missing months and leftover `'nan'` values, and shows how many segment rows share the old Snowflake merge key (rows the old pipeline could have overwritten).

## 6. Every month after that

```bash
python -m t100_agent.ingest.run sync --delete-local
```

It continues from the latest month already in BigQuery and stops at the first month BTS has not published yet.

## What lives where

| Thing | Where |
|---|---|
| Raw tables | `<project>.t100_raw.raw_market`, `<project>.t100_raw.raw_segment`, partitioned by `YEAR`, clustered by `MONTH` and `UNIQUE_CARRIER` |
| Enriched tables (lookups joined in) | Next step, once the lookup tables are rebuilt |
| Settings | Environment variables: `GCP_PROJECT`, `BQ_RAW_DATASET` (default `t100_raw`), `BQ_LOCATION` (default `US`), `T100_DATA_DIR` (default `data/raw`), `BQ_MAX_BYTES_BILLED` |
