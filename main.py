from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import feedparser
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, JSONResponse, Response


APP_TITLE = "VantaPulse"
PORT = int(os.getenv("PORT", "8000"))
UPDATE_TOKEN = os.getenv("UPDATE_TOKEN")
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "news.db"
FEED_TIMEOUT_SECONDS = 10
NEWS_CAP = 1000
RSS_FEEDS = [
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.bleepingcomputer.com/feed/",
    "https://krebsonsecurity.com/feed/",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("vantapulse")

app = FastAPI(title=APP_TITLE)

FAVICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#0b0f14"/>
  <path d="M12 36h11l6-12 8 23 6-11h9" fill="none" stroke="#86efac" stroke-linecap="round" stroke-linejoin="round" stroke-width="5"/>
</svg>
""".strip()


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=True))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_data_dir()
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def build_title_hash(title: str) -> str:
    return sha256(normalize_title(title).encode("utf-8")).hexdigest()


def init_db() -> None:
    ensure_data_dir()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT UNIQUE NOT NULL,
                normalized_title_hash TEXT,
                source TEXT NOT NULL,
                published_at TEXT
            )
            """
        )

        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(news)").fetchall()
        }
        if "normalized_title_hash" not in columns:
            connection.execute("ALTER TABLE news ADD COLUMN normalized_title_hash TEXT")

        if "title_hash" in columns:
            connection.execute(
                """
                UPDATE news
                SET normalized_title_hash = COALESCE(normalized_title_hash, title_hash)
                WHERE title_hash IS NOT NULL
                """
            )

        rows = connection.execute(
            """
            SELECT id, title
            FROM news
            WHERE normalized_title_hash IS NULL OR normalized_title_hash = ''
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE news SET normalized_title_hash = ? WHERE id = ?",
                (build_title_hash(row["title"]), row["id"]),
            )

        connection.execute(
            """
            DELETE FROM news
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM news
                GROUP BY normalized_title_hash
            )
            AND normalized_title_hash IS NOT NULL
            """
        )

        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_link ON news(link)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_news_normalized_title_hash
            ON news(normalized_title_hash)
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at)"
        )
        connection.commit()


def extract_domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def normalize_datetime(value: Any) -> str | None:
    try:
        if isinstance(value, tuple) and len(value) >= 6:
            dt = datetime(*value[:6], tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")

        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        if isinstance(value, str) and value.strip():
            dt = parsedate_to_datetime(value.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    return None


def normalize_date(entry: Any) -> str | None:
    for candidate in (
        getattr(entry, "published_parsed", None),
        getattr(entry, "updated_parsed", None),
        getattr(entry, "published", None),
        getattr(entry, "updated", None),
    ):
        normalized = normalize_datetime(candidate)
        if normalized:
            return normalized
    return None


def normalize_entry(entry: Any) -> dict[str, str | None] | None:
    title = (getattr(entry, "title", "") or "").strip()
    link = (getattr(entry, "link", "") or "").strip()
    if not title or not link:
        return None

    return {
        "title": title,
        "link": link,
        "normalized_title_hash": build_title_hash(title),
        "source": extract_domain(link),
        "published_at": normalize_date(entry),
    }


def fetch_feed(feed_url: str) -> Any:
    request = Request(feed_url, headers={"User-Agent": "VantaPulse/1.0"})
    with urlopen(request, timeout=FEED_TIMEOUT_SECONDS) as response:
        return feedparser.parse(response.read())


def trim_old_news(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        DELETE FROM news
        WHERE id NOT IN (
            SELECT id
            FROM news
            ORDER BY
                CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,
                julianday(published_at) DESC,
                id DESC
            LIMIT {NEWS_CAP}
        )
        """
    )


def update_news() -> dict[str, Any]:
    inserted = 0
    skipped = 0
    failed = 0
    sources: list[dict[str, int | str | bool]] = []

    log_event("refresh_start", feeds=len(RSS_FEEDS))

    with closing(get_connection()) as connection:
        for feed_url in RSS_FEEDS:
            source_name = extract_domain(feed_url)
            source_fetched = 0
            source_inserted = 0
            source_skipped = 0

            try:
                feed = fetch_feed(feed_url)
                source_fetched = len(feed.entries)
            except Exception as exc:
                failed += 1
                sources.append(
                    {
                        "source": source_name,
                        "fetched": 0,
                        "inserted": 0,
                        "skipped": 0,
                        "failed": True,
                    }
                )
                log_event(
                    "feed_failed",
                    source=source_name,
                    feed=feed_url,
                    error=str(exc),
                )
                continue

            for entry in feed.entries:
                item = normalize_entry(entry)
                if item is None:
                    skipped += 1
                    source_skipped += 1
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO news (
                        title,
                        link,
                        normalized_title_hash,
                        source,
                        published_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["title"],
                        item["link"],
                        item["normalized_title_hash"],
                        item["source"],
                        item["published_at"],
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    source_inserted += 1
                else:
                    skipped += 1
                    source_skipped += 1

            sources.append(
                {
                    "source": source_name,
                    "fetched": source_fetched,
                    "inserted": source_inserted,
                    "skipped": source_skipped,
                    "failed": False,
                }
            )

        trim_old_news(connection)
        connection.commit()

    log_event(
        "refresh_end",
        inserted=inserted,
        skipped=skipped,
        failed=failed,
    )
    return {
        "status": "ok",
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "sources": sources,
    }


def verify_update_token(token: str | None) -> None:
    if UPDATE_TOKEN and token != UPDATE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: FastAPIRequest, exc: HTTPException
) -> JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"status": "error", "detail": exc.detail}, status_code=exc.status_code)
    return JSONResponse({"status": "error", "detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: FastAPIRequest, exc: Exception
) -> JSONResponse:
    log_event("request_failed", path=request.url.path, error=str(exc))
    if request.url.path.startswith("/api/"):
        return JSONResponse({"status": "error", "detail": "Internal server error"}, status_code=500)
    return JSONResponse({"status": "error", "detail": "Internal server error"}, status_code=500)


@app.on_event("startup")
def startup() -> None:
    log_event("startup", port=PORT, db_path=str(DB_PATH))
    init_db()
    try:
        update_news()
    except Exception as exc:
        log_event("startup_refresh_failed", error=str(exc))


@app.get("/api/news")
def get_news() -> list[dict[str, str | int | None]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id, title, link, source, published_at
            FROM news
            ORDER BY
                CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,
                datetime(published_at) DESC,
                id DESC
            LIMIT 50
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/update")
def refresh_news(token: str | None = Query(default=None)) -> dict[str, Any]:
    verify_update_token(token)
    try:
        return update_news()
    except Exception as exc:
        log_event("refresh_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Refresh failed") from exc


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VantaPulse</title>
  <meta name="description" content="Minimal cybersecurity news aggregator" />
  <meta property="og:title" content="VantaPulse" />
  <meta property="og:description" content="Minimal cybersecurity news aggregator" />
  <link rel="icon" href="/favicon.ico" />
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0f14;
      --panel: #121821;
      --text: #edf2f7;
      --muted: #8a98ab;
      --border: #243041;
      --accent: #86efac;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top, rgba(134, 239, 172, 0.08), transparent 28%),
        var(--bg);
      color: var(--text);
      font-family: "Segoe UI", sans-serif;
    }

    main {
      width: min(860px, calc(100% - 32px));
      margin: 0 auto;
      padding: 48px 0 64px;
    }

    h1 {
      margin: 0;
      font-size: 2.1rem;
      letter-spacing: 0.03em;
    }

    p {
      color: var(--muted);
      margin: 10px 0 0;
    }

    .toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 28px 0 10px;
    }

    .top-meta {
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    button {
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 10px;
      cursor: pointer;
    }

    button:hover {
      border-color: var(--accent);
    }

    button:disabled {
      opacity: 0.65;
      cursor: wait;
    }

    #status {
      font-size: 0.95rem;
      color: var(--muted);
    }

    .list {
      display: grid;
      gap: 12px;
    }

    .item {
      border: 1px solid var(--border);
      border-radius: 14px;
      background: rgba(18, 24, 33, 0.88);
      transition: border-color 0.18s ease, background 0.18s ease, transform 0.18s ease;
    }

    .item:hover {
      border-color: rgba(134, 239, 172, 0.3);
      background: rgba(21, 28, 39, 0.96);
      transform: scale(1.01);
    }

    .item a {
      display: block;
      padding: 16px;
      color: var(--text);
      text-decoration: none;
      font-size: 1.02rem;
      font-weight: 600;
      border-radius: 14px;
      cursor: pointer;
    }

    .title-text {
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      line-clamp: 2;
    }

    .item a:hover {
      color: var(--accent);
    }

    .item a:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }

    .meta {
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 400;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .source-badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(134, 239, 172, 0.06);
      color: var(--text);
      font-size: 0.78rem;
      letter-spacing: 0.02em;
    }

    .date-text {
      color: var(--muted);
    }

    footer {
      margin-top: 32px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 0.9rem;
      display: flex;
      justify-content: flex-start;
      align-items: center;
      gap: 10px 16px;
      flex-wrap: wrap;
    }

    footer span {
      white-space: nowrap;
    }

    footer a {
      color: var(--accent);
      text-decoration: none;
    }

    footer a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <main>
    <h1>VantaPulse</h1>
    <p>Latest cybersecurity headlines from trusted RSS feeds.</p>

    <div class="toolbar">
      <div id="status">Loading news...</div>
      <button id="refreshButton" type="button">Refresh feeds</button>
    </div>

    <div id="lastUpdated" class="top-meta">Last updated: not yet</div>

    <section id="newsList" class="list">
      <div class="item"><div class="title-text" style="padding: 16px;">Loading...</div></div>
    </section>

    <footer>
      <span>VantaPulse</span>
      <span>Minimal cybersecurity news aggregator</span>
      <a href="https://github.com/xzarate" target="_blank" rel="noreferrer">GitHub Profile</a>
      <a href="https://github.com/xzarate/vanta-pulse" target="_blank" rel="noreferrer">Repository</a>
    </footer>
  </main>

  <script>
    const newsList = document.getElementById("newsList");
    const status = document.getElementById("status");
    const refreshButton = document.getElementById("refreshButton");
    const lastUpdated = document.getElementById("lastUpdated");

    function formatDate(value) {
      if (!value) {
        return "Unknown date";
      }

      const date = new Date(value);
      if (Number.isNaN(date.getTime())) {
        return value;
      }
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      const hours = String(date.getHours()).padStart(2, "0");
      const minutes = String(date.getMinutes()).padStart(2, "0");
      return `${year}-${month}-${day} ${hours}:${minutes}`;
    }

    function updateLastUpdated(value = new Date()) {
      lastUpdated.textContent = `Last updated: ${formatDate(value)}`;
    }

    function clearNews() {
      while (newsList.firstChild) {
        newsList.removeChild(newsList.firstChild);
      }
    }

    function renderMessage(message) {
      clearNews();
      const item = document.createElement("div");
      item.className = "item";
      item.textContent = message;
      newsList.appendChild(item);
    }

    function renderNews(items) {
      clearNews();

      if (!items.length) {
        renderMessage("No articles available");
        return;
      }

      for (const item of items) {
        const article = document.createElement("article");
        article.className = "item";

        const link = document.createElement("a");
        link.href = item.link;
        link.target = "_blank";
        link.rel = "noreferrer";

        const title = document.createElement("div");
        title.className = "title-text";
        title.textContent = item.title;

        const meta = document.createElement("div");
        meta.className = "meta";

        const source = document.createElement("span");
        source.className = "source-badge";
        source.textContent = item.source;

        const date = document.createElement("span");
        date.className = "date-text";
        date.textContent = formatDate(item.published_at);

        link.appendChild(title);
        meta.appendChild(source);
        meta.appendChild(date);
        link.appendChild(meta);
        article.appendChild(link);
        newsList.appendChild(article);
      }
    }

    async function parseJson(response) {
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "Request failed");
      }
      return data;
    }

    async function loadNews() {
      status.textContent = "Loading...";
      renderMessage("Loading...");
      const items = await parseJson(await fetch("/api/news"));
      renderNews(items);
      status.textContent = `${items.length} articles loaded`;
      updateLastUpdated();
    }

    async function refreshNews() {
      status.textContent = "Refreshing feeds...";
      refreshButton.disabled = true;
      try {
        const result = await parseJson(await fetch("/api/update"));
        await loadNews();
        status.textContent = `Refresh complete: ${result.inserted} new, ${result.skipped} skipped`;
        updateLastUpdated();
      } catch (error) {
        status.textContent = error.message || "Refresh failed";
      } finally {
        refreshButton.disabled = false;
      }
    }

    refreshButton.addEventListener("click", refreshNews);
    loadNews().catch(() => {
      renderMessage("Failed to load news");
      status.textContent = "Failed to load news";
    });
  </script>
</body>
</html>
    """


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
