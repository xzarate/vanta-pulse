# VantaPulse

Minimal cybersecurity news aggregator built with FastAPI.

## Features

- Aggregates multiple RSS feeds
- Deduplicates articles
- Minimal UI
- Fast and lightweight

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`

## Environment variables

- `PORT`: Optional server port. Defaults to `8000`.
- `UPDATE_TOKEN`: Optional token for `GET /api/update`. If set, use `/api/update?token=...`.

## Notes

- SQLite database path: `./data/news.db`
- News refresh runs on startup
- `/api/news` returns the latest articles
- `/api/update` triggers a manual refresh

## Deployment

Run in production with:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```
