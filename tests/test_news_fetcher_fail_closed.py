import asyncio
import os

os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from backend import news_fetcher as nf


def test_unavailable_news_feed_is_not_treated_as_empty_calendar():
    safe, reason = nf.is_news_safe(None, "EURUSD=X", now="2024-01-01T10:00:00Z")
    assert safe is False
    assert "در دسترس" in reason


def test_fetch_news_returns_none_when_feed_fails_without_cache(monkeypatch):
    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("offline")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(nf.httpx, "AsyncClient", lambda *a, **k: FailingClient())
    monkeypatch.setattr(nf.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(nf, "MAX_RETRIES", 1)
    monkeypatch.setattr(nf, "_cache", {"data": [], "fetched_at": None, "ok": False})

    result = asyncio.run(nf.fetch_news(force=True))
    assert result is None


def test_fetch_news_uses_only_cache_within_stale_limit(monkeypatch):
    from datetime import datetime, timedelta, timezone

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("offline")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(nf.httpx, "AsyncClient", lambda *a, **k: FailingClient())
    monkeypatch.setattr(nf.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(nf, "MAX_RETRIES", 1)
    monkeypatch.setattr(nf, "_cache", {
        "data": [{"title": "cached", "country": "USD", "impact": "High",
                  "date": "2024-01-01T10:00:00Z"}],
        "fetched_at": datetime.now(timezone.utc) - timedelta(minutes=nf.MAX_STALE_MINUTES + 5),
        "ok": True,
    })

    result = asyncio.run(nf.fetch_news(force=True))
    assert result is None


def test_valid_empty_calendar_remains_distinct_from_feed_failure(monkeypatch):
    class Response:
        status_code = 200
        request = object()
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(nf.httpx, "AsyncClient", lambda *a, **k: Client())
    monkeypatch.setattr(nf, "_cache", {"data": [], "fetched_at": None, "ok": False})

    result = asyncio.run(nf.fetch_news(force=True))
    assert result == []
    safe, _ = nf.is_news_safe(result, "EURUSD=X", now="2024-01-01T10:00:00Z")
    assert safe is True
