import time

import pytest

from backend import data_fetcher


def _cached_record():
    return [{
        "time": "2026-11-16T10:00:00+00:00",
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.05,
        "volume": 1.0,
    }]


def _raise_download(*args, **kwargs):
    raise RuntimeError("provider unavailable")


def test_fetch_ohlcv_rejects_expired_cache_after_provider_error(monkeypatch):
    key = data_fetcher._cache_key("EURUSD=X", "15m")
    ttl = data_fetcher.CACHE_TTL.get("15m", 15)
    monkeypatch.setitem(data_fetcher._cache, key, {
        "data": _cached_record(),
        "ts": time.time() - (ttl * 60) - 1,
    })
    monkeypatch.setattr(data_fetcher.yf, "download", _raise_download)

    assert data_fetcher.fetch_ohlcv("EURUSD=X", "15m") == []


def test_fetch_ohlcv_uses_cache_only_within_ttl_after_provider_error(monkeypatch):
    key = data_fetcher._cache_key("EURUSD=X", "15m")
    monkeypatch.setitem(data_fetcher._cache, key, {
        "data": _cached_record(),
        "ts": time.time(),
    })
    monkeypatch.setattr(data_fetcher.yf, "download", _raise_download)

    assert data_fetcher.fetch_ohlcv("EURUSD=X", "15m") == _cached_record()


def test_fetch_ohlcv_does_not_use_cache_when_disabled(monkeypatch):
    key = data_fetcher._cache_key("EURUSD=X", "15m")
    monkeypatch.setitem(data_fetcher._cache, key, {
        "data": _cached_record(),
        "ts": time.time(),
    })
    monkeypatch.setattr(data_fetcher.yf, "download", _raise_download)

    assert data_fetcher.fetch_ohlcv("EURUSD=X", "15m", use_cache=False) == []


def test_cache_timestamp_in_future_is_invalid(monkeypatch):
    key = data_fetcher._cache_key("EURUSD=X", "15m")
    monkeypatch.setitem(data_fetcher._cache, key, {
        "data": _cached_record(),
        "ts": time.time() + 60,
    })

    assert not data_fetcher._is_cache_valid(key, 15)
