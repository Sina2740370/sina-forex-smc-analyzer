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

MAX_AGE_SECONDS = 24 * 3600   # اعتبار initData (مینی‌اپ باز می‌ماند؛ یک روز کافیه)


def verify_init_data(init_data: str) -> bool:
    if not init_data or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received = data.pop("hash", None)
        if not received:
            return False
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return False
        if time.time() - int(data.get("auth_date", 0)) > MAX_AGE_SECONDS:
            return False
        user = json.loads(data.get("user", "{}"))
        return str(user.get("id", "")) == str(TELEGRAM_CHAT_ID)
    except Exception:
        return False


def init_data_from_header(authorization: Optional[str]) -> str:
    """هدر: Authorization: tma <initData>"""
    value = authorization or ""
    return value[4:] if value.startswith("tma ") else ""
  
