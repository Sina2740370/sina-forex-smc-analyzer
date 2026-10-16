"""
احراز هویت مینی‌اپ تلگرام: فقط صاحب (TELEGRAM_CHAT_ID) اجازه دارد.
initData را تلگرام با امضای HMAC (کلید = توکن ربات) می‌سازد؛ جعل‌کردنش بدون توکن ممکن نیست.
"""
import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

MAX_AGE_SECONDS = 24 * 3600
MAX_FUTURE_SKEW_SECONDS = 60
MAX_INIT_DATA_LENGTH = 8192
MAX_INIT_DATA_FIELDS = 100


def verify_init_data(init_data: str) -> bool:
    """Verify Telegram Mini App initData, including duplicate and timestamp checks."""
    if (
        not isinstance(init_data, str)
        or not init_data
        or len(init_data) > MAX_INIT_DATA_LENGTH
        or not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return False
    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
        # Duplicate keys make canonicalization ambiguous and must not be accepted.
        if not pairs or len({key for key, _ in pairs}) != len(pairs):
            return False
        data = dict(pairs)
        received = data.pop("hash", None)
        if not received:
            return False
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(
            b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        calc = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return False

        # Reject missing, malformed, stale, or implausibly future auth timestamps.
        auth_date = int(data.get("auth_date", ""))
        now = int(time.time())
        age = now - auth_date
        if auth_date <= 0 or age > MAX_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
            return False

        user = json.loads(data.get("user", "{}"))
        if not isinstance(user, dict) or user.get("id") is None:
            return False
        return str(user["id"]) == str(TELEGRAM_CHAT_ID)
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
        return False


def init_data_from_header(authorization: Optional[str]) -> str:
    """هدر: Authorization: tma <initData>"""
    value = authorization or ""
    return value[4:] if value.startswith("tma ") else ""
  
