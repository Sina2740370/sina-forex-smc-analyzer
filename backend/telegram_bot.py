"""
دستورهای تلگرام (webhook) برای ابزار معامله‌ی دستی: /size /log /stats /guard /last /undo /set

طراحی:
- webhook (نه polling): هم‌زمان‌شدن چند instance مشکل 409 نمی‌سازد و سرویس خوابیده با پیام بیدار می‌شود.
- امنیت: هدر X-Telegram-Bot-Api-Secret-Token (مشتق از ADMIN_KEY، بدون متغیر جدید) + فقط chat/user خود سینا.
- dedupe با update_id: تلگرام در تأخیر دوباره می‌فرستد؛ بدون این، یک /log دوبار ثبت می‌شد.
"""
import asyncio
import hashlib
import hmac
import html
import logging
import os
from typing import Optional

import httpx

from . import journal_store as js
from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .notifier import send_telegram
from engine.tools import journal as jr
from engine.tools import risk as rk
from engine.tools import tgparse as tp

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"

HELP = """🤖 <b>ابزار معامله‌ی دستی</b>

<code>/size EURUSD 1.1000 1.0980 [TP]</code>
حجم لات (گزینه‌ها: risk=1 bal=10000 lev=100)

<code>/log EURUSD buy 1.1000 1.0980 1.1040 برچسب</code>
ثبت معامله‌ی بسته‌شده (ورود، SL اولیه، خروج)

/stats آمار صادقانه · /guard نگهبان امروز
/last [n] آخرین معاملات · /undo حذف آخرین
/settings · <code>/set balance 10000</code> | risk | lev | maxloss | maxtrades | consec

ارقام فارسی هم قبول است. قیمت‌ها با ممیز یا ویرگول اعشاری."""


# ------------------------------------------------------------ امنیت
def webhook_secret(admin_key: str) -> str:
    return hmac.new(admin_key.encode("utf-8"), b"sina-telegram-webhook-v1", hashlib.sha256).hexdigest()[:48]


def verify_secret(header_value: Optional[str], admin_key: str) -> bool:
    if not admin_key:
        return False
    return hmac.compare_digest((header_value or "").encode("utf-8"),
                               webhook_secret(admin_key).encode("utf-8"))


# ------------------------------------------------------------ قالب‌بندی
def _fp(price: float, symbol: str) -> str:
    spec = rk.SPECS.get(symbol)
    dec = 5 if not spec else (2 if spec["pip"] >= 0.1 else 3 if spec["pip"] >= 0.01 else 5)
    return f"{price:.{dec}f}"


def _r(v) -> str:
    return "—" if v is None else f"{v:+.2f}R"


GUARD_ICON = {"ok": "🟢", "warn": "🟠", "stop": "🔴"}
GUARD_TITLE = {"ok": "امروز در محدوده‌ی امن هستی", "warn": "نزدیک سقف روزانه‌ای",
               "stop": "امروز دیگر معامله‌ی جدید باز نکن"}


def guard_text(g: dict) -> str:
    why = "\n".join("• " + html.escape(x) for x in g.get("reasons", []))
    lim = g.get("limits", {})
    return (f"{GUARD_ICON[g['state']]} <b>{GUARD_TITLE[g['state']]}</b>\n"
            f"امروز: {_r(g['today_r'])} · {g['trades_today']} معامله"
            f" (سقف {lim.get('maxloss')}R / {lim.get('maxtrades')} معامله)"
            + (f"\n{why}" if why else ""))


# ------------------------------------------------------------ دستورها (همگام؛ در thread اجرا می‌شود)
def _need_balance(s: dict, args: dict) -> float:
    bal = args.get("balance") or s.get("balance")
    if not bal:
        raise tp.CommandError("موجودی تنظیم نشده. اول بنویس:\n/set balance 10000")
    return float(bal)


def _cmd_size(a: dict) -> str:
    s = js.get_settings()
    bal = _need_balance(s, a)
    risk = a.get("risk") or s["risk"]
    lev = a["lev"] if "lev" in a else s["lev"]
    try:
        res = rk.position_size(a["symbol"], bal, risk, a["entry"], a["sl"], a.get("tp"), leverage=lev)
    except ValueError as e:
        raise tp.CommandError(str(e))
    side = "BUY" if a["entry"] > a["sl"] else "SELL"
    head = f"📐 <b>{a['symbol']} {side}</b> · ورود {_fp(a['entry'], a['symbol'])} · SL {_fp(a['sl'], a['symbol'])}"
    if not res.ok:
        return f"{head}\n❌ {html.escape(res.note)}"
    lines = [head, f"حجم: <b>{res.lots:.2f} لات</b>",
             f"ریسک واقعی: ${res.actual_risk:.2f} از ${res.risk_amount:.2f} ({risk:g}٪ از ${bal:,.0f})",
             f"فاصله SL: {res.sl_pips:g} پیپ"]
    if res.rr:
        lines.append(f"R:R = {res.rr:g} (سربه‌سر در win rate {100 / (1 + res.rr):.1f}٪)")
    if res.margin is not None:
        lines.append(f"مارجین: ${res.margin:,.2f} ({res.margin_pct:g}٪ موجودی) · آزاد: ${res.free_margin:,.2f}")
    if res.note:
        lines.append("⚠️ " + html.escape(res.note))
    g = js.compute_guard()
    if g["state"] != "ok":
        lines.append("\n" + guard_text(g))
    return "\n".join(lines)


def _cmd_log(a: dict) -> str:
    try:
        r = rk.trade_r(a["side"], a["entry"], a["sl"], a["exit"])
    except ValueError as e:
        raise tp.CommandError(str(e))
    tid = js.add_trade(a["symbol"], a["side"], a["entry"], a["sl"], a["exit"], round(r, 4),
                       None, a.get("tag"), None, None, None)
    g = js.compute_guard()
    js.set_raw(f"guard_alert:{js.day_start_utc()[:10]}", g["state"])   # هشدار تکراری نفرستد
    out = f"{'✅' if r > 0 else '🔻' if r < 0 else '➖'} ثبت شد #{tid}: {a['symbol']} {a['side']} → <b>{_r(r)}</b>"
    if g["state"] != "ok" or r < 0:
        out += "\n\n" + guard_text(g)
    return out


def _cmd_stats() -> str:
    rows = js.to_records(js.list_trades(limit=2000))
    s = jr.journal_stats(rows)
    if not s["n"]:
        return "هنوز معامله‌ای ثبت نشده.\n" + html.escape(s["verdict"])
    lo, hi = s["expectancy_ci95"]
    ci = "نامشخص" if lo is None or hi is None or abs(lo) > 1e6 or abs(hi) > 1e6 else f"{lo:+.2f} تا {hi:+.2f}"
    dd = jr.drawdown_guard([t["r"] for t in jr.normalize(rows)])
    return (f"📊 <b>آمار ژورنال</b> ({s['n']} معامله)\n"
            f"Win Rate: {s['win_rate'] * 100:.1f}٪ (سربه‌سر {s['breakeven_win_rate'] * 100:.1f}٪)\n"
            f"امید ریاضی: {_r(s['expectancy_r'])} · بازه ۹۵٪: {ci}\n"
            f"Profit Factor: {s['profit_factor']:.2f} · مجموع: {_r(s['total_r'])}\n"
            f"افت فعلی: {dd['current_dd_r']:.2f}R (بیشترین {dd['max_dd_r']:.2f}R)\n\n"
            f"🧭 {html.escape(s['verdict'])}")


def _cmd_last(n: int) -> str:
    rows = js.list_trades(limit=n)
    if not rows:
        return "هنوز معامله‌ای ثبت نشده."
    return "🕘 <b>آخرین معاملات</b>\n" + "\n".join(
        f"#{t['id']} {t['symbol']} {t['side']} {_r(t['r'])}"
        + (f" · {html.escape(t['tag'])}" if t.get("tag") else "")
        + f" · {str(t['closed_at'])[:16].replace('T', ' ')}" for t in rows)


def _cmd_undo() -> str:
    rows = js.list_trades(limit=1)
    if not rows:
        return "چیزی برای حذف نیست."
    t = rows[0]
    js.delete_trade(int(t["id"]))
    return f"↩️ حذف شد: #{t['id']} {t['symbol']} {t['side']} {_r(t['r'])}"


def _cmd_settings() -> str:
    s = js.get_settings()
    return ("⚙️ <b>تنظیمات</b>\n"
            f"موجودی: {('$' + format(s['balance'], ',.0f')) if s['balance'] else 'تنظیم نشده'}\n"
            f"ریسک: {s['risk']:g}٪ · اهرم: {('1:' + format(s['lev'], 'g')) if s['lev'] else 'خاموش'}\n"
            f"نگهبان: سقف ضرر {s['maxloss']:g}R · حداکثر {s['maxtrades']} معامله · {s['consec']} ضرر پشت‌سرهم")


def dispatch(text: str) -> Optional[str]:
    p = tp.parse(text)
    c, a = p.cmd, p.args
    if c == "none":
        return None
    if c == "help":
        return HELP
    if c == "ping":
        return "✅ آنلاینم"
    if c == "unknown":
        return f"دستور ناشناخته: /{html.escape(a['cmd'])}\n/help"
    if c == "size":
        return _cmd_size(a)
    if c == "log":
        return _cmd_log(a)
    if c == "stats":
        return _cmd_stats()
    if c == "guard":
        return guard_text(js.compute_guard())
    if c == "last":
        return _cmd_last(a["n"])
    if c == "undo":
        return _cmd_undo()
    if c == "settings":
        return _cmd_settings()
    if c == "set":
        js.set_setting(a["key"], a["value"])
        return "✅ ثبت شد\n" + _cmd_settings()
    return None


# ------------------------------------------------------------ webhook
async def handle_update(update: dict) -> None:
    msg = update.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    sender = str((msg.get("from") or {}).get("id", ""))
    allowed = str(TELEGRAM_CHAT_ID)
    if not msg.get("text") or chat != allowed or sender != allowed:
        return                                  # بی‌صدا نادیده بگیر؛ وجود ربات را فاش نکن
    uid = update.get("update_id")
    if uid is not None and not await asyncio.to_thread(js.mark_update, int(uid)):
        return                                  # تکراری
    try:
        reply = await asyncio.to_thread(dispatch, msg["text"])
    except tp.CommandError as e:
        reply = "⚠️ " + html.escape(str(e))
    except Exception:
        logger.exception("خطا در پردازش دستور تلگرام")
        reply = "⚠️ خطای داخلی؛ دوباره امتحان کن."
    if reply:
        await send_telegram(reply)


async def check_guard_alert() -> None:
    """بعد از ثبت معامله از اپ: فقط وقتی وضعیت نگهبان امروز تغییر کرد (به warn/stop) هشدار بفرست."""
    try:
        g = await asyncio.to_thread(js.compute_guard)
        key = f"guard_alert:{js.day_start_utc()[:10]}"
        last = await asyncio.to_thread(js.get_raw, key)
        if g["state"] == last:
            return
        await asyncio.to_thread(js.set_raw, key, g["state"])
        if g["state"] in ("warn", "stop"):
            await send_telegram(guard_text(g))
    except Exception:
        logger.exception("بررسی هشدار نگهبان ناموفق بود")


async def _tg_call(method: str, payload: Optional[dict] = None) -> dict:
    url = API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload or {})
        r.raise_for_status()
        return r.json()


async def register_webhook(admin_key: str) -> bool:
    base = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base or not admin_key:
        logger.info("webhook تلگرام ثبت نشد (PUBLIC_URL/RENDER_EXTERNAL_URL یا ADMIN_KEY موجود نیست)")
        return False
    try:
        res = await _tg_call("setWebhook", {
            "url": f"{base}/api/telegram/webhook", "secret_token": webhook_secret(admin_key),
            "allowed_updates": ["message"], "max_connections": 1, "drop_pending_updates": False})
        logger.info("webhook تلگرام ثبت شد: %s", res.get("description"))
        await register_menu_button(base)
        return bool(res.get("ok"))
    except Exception as e:
        logger.error("ثبت webhook ناموفق: %s", e)
        return False


async def register_menu_button(base: str) -> bool:
    """دکمه‌ی مینی‌اپ فقط تو چت خود سینا (TELEGRAM_CHAT_ID) نمایش داده می‌شود."""
    try:
        res = await _tg_call("setChatMenuButton", {
            "chat_id": int(TELEGRAM_CHAT_ID),
            "menu_button": {"type": "web_app", "text": "Sina Quant",
                            "web_app": {"url": f"{base}/"}}})
        logger.info("دکمه‌ی مینی‌اپ ثبت شد: %s", res.get("ok"))
        return bool(res.get("ok"))
    except Exception as e:
        logger.error("ثبت دکمه‌ی مینی‌اپ ناموفق: %s", e)
        return False


async def webhook_status() -> dict:
    info = (await _tg_call("getWebhookInfo")).get("result", {})
    return {"url_set": bool(info.get("url")), "pending": info.get("pending_update_count"),
            "last_error": info.get("last_error_message"), "last_error_date": info.get("last_error_date")}
        
