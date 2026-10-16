"""Security regression tests for Telegram Mini App initData validation."""
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from backend import tg_auth


def signed_init_data(auth_date, user_id=12345, token="test-bot-token"):
    data = [
        ("auth_date", str(auth_date)),
        ("user", json.dumps({"id": user_id}, separators=(",", ":"))),
        ("query_id", "AAEAA"),
    ]
    check = "\n".join(f"{key}={value}" for key, value in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data + [("hash", digest)])


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setattr(tg_auth, "TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(tg_auth, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(tg_auth.time, "time", lambda: 2_000_000_000)
    return 2_000_000_000


def test_accepts_valid_recent_signed_init_data(auth_env):
    init_data = signed_init_data(auth_env - 120)
    assert tg_auth.verify_init_data(init_data)


def test_allows_small_clock_skew_but_rejects_future_timestamp(auth_env):
    assert tg_auth.verify_init_data(signed_init_data(auth_env + 30))
    assert not tg_auth.verify_init_data(signed_init_data(auth_env + 61))


def test_rejects_stale_or_missing_auth_date(auth_env):
    assert not tg_auth.verify_init_data(
        signed_init_data(auth_env - tg_auth.MAX_AGE_SECONDS - 1)
    )
    data = signed_init_data(auth_env).replace("auth_date=", "auth_date_missing=")
    assert not tg_auth.verify_init_data(data)


def test_rejects_duplicate_keys_even_if_original_signature_was_valid(auth_env):
    data = signed_init_data(auth_env)
    assert not tg_auth.verify_init_data(data + "&auth_date=" + str(auth_env))


@pytest.mark.parametrize(
    "init_data",
    ["", "not-a-query", "hash=bad", "auth_date=abc&hash=bad", "x=" * 5000],
)
def test_rejects_malformed_or_oversized_init_data(init_data, auth_env):
    assert not tg_auth.verify_init_data(init_data)


def test_rejects_wrong_user_and_bad_signature(auth_env):
    assert not tg_auth.verify_init_data(signed_init_data(auth_env, user_id=999))
    data = signed_init_data(auth_env).replace("hash=", "hash=0")
    assert not tg_auth.verify_init_data(data)
