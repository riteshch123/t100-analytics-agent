# t100-analytics-agent

A natural-language analytics agent over the US DOT T-100 aviation data: ask "annual flights out of Springfield, IL 2022-24 by destination state" and get correct SQL, a table and a short answer.

## Status

- **Data pipeline:** downloads T-100 monthly from BTS, saves it as Parquet and loads it into BigQuery with row-count checks and cost caps, then decodes it through BTS lookup tables and an aircraft classification into enriched views. See [docs/bigquery-setup.md](docs/bigquery-setup.md).
- **Agent:** designed, not built yet.

## Layout

```
src/t100_agent/ingest/   download, normalize, load and check the T-100 data
reference/               hand-built reference data (aircraft classification)
tests/                   unit tests (pytest)
docs/                    setup guides
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```
