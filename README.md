# VantaPulse

Minimal cybersecurity news aggregator built with FastAPI.

VantaPulse collects headlines from well-known cybersecurity RSS feeds, deduplicates them, stores them locally in SQLite, and serves them through a minimal dark web interface plus a simple JSON API.

## Overview

- Aggregates cybersecurity news from multiple RSS feeds
- Deduplicates articles by link and normalized title
- Stores articles locally in SQLite
- Exposes a minimal web interface and API

## Screenshot

![VantaPulse screenshot](assets/screenshot.png)

## Features

- Aggregates multiple RSS feeds
- Deduplicates articles
- Minimal dark UI
- Fast and lightweight
- Manual refresh endpoint
- SQLite local storage

## Sources

- The Hacker News
- BleepingComputer
- Krebs on Security

## Stack

- FastAPI
- feedparser
- SQLite via `sqlite3`
- Plain HTML, CSS, and JavaScript

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

## API

- `GET /`: Web interface
- `GET /api/news`: Returns the latest stored articles as JSON
- `GET /api/update`: Triggers a feed refresh manually

## Storage

- SQLite database path: `./data/news.db`
- Data directory is created automatically on startup
- Stored rows are capped to keep the database small

## Deployment

Run in production with:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

## Repository metadata

Recommended GitHub description:

`Minimal cybersecurity news aggregator built with FastAPI.`

Recommended topics:

`fastapi`, `python`, `cybersecurity`, `rss`, `sqlite`, `news-aggregator`
